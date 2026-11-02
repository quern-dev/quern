"""What a session installed on a device it holds, cleaned up at release (#581).

A shared machine grew without bound: apps remote agents installed, and their
data, stayed on the simulators after their sessions ended (17 GB across 44
simulators on romulus), and on a machine several people use, one client's
apps, data and keychain reached the next client to reserve the simulator.

At a session's first install on a device it holds, the device's apps are
noted -- the baseline. When the hold ends, the release policy for the
caller's kind of key runs (``sessions.client_release``,
``sessions.owner_release``, read at start):

- ``uninstall``: remove the apps not in the baseline, except quern's own
  driver. An app the session reinstalled over one already there is left, and
  said.
- ``erase``: erase the simulator, whether or not anything was installed, and
  boot it again if it was booted. Android devices and physical iPhones fall
  back to ``uninstall``: an emulator is wiped only by relaunching it, and a
  phone cannot be erased safely from here.
- ``none``: leave the device as the session left it.

The record belongs to the session, not to its key, so a device another
session of the same key holds by then takes it over, as a device proxy's
does. A clean-up that fails keeps the record and is tried again at the next
sweep, up to three times; then it is given up on and said, so a device that
went away cannot stay unreservable for good.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from server import principal

#: Never uninstalled: quern's WebDriverAgent, which drives physical iPhones
#: and which quern installs itself.
KEPT = frozenset({"com.facebook.WebDriverAgentRunner",
                  "com.facebook.WebDriverAgentRunner.xctrunner"})

#: Attempts at a clean-up before giving up on it.
ATTEMPTS = 3

#: How long one clean-up may take: it runs in the sweep every session's
#: expiry waits on. An erase and reboot is the slow case.
TIMEOUT_S = 120.0


class BaselineUnavailable(Exception):
    """The device's apps could not be listed before an install, so what the
    install adds could not be told apart from what was there."""


@dataclass
class InstallRecord:
    session: str
    opened: Any
    #: Bundle ids on the device before the session's first install. None for a
    #: record kept only to count a failed erase's attempts: no baseline, so
    #: an uninstall from it removes nothing -- never "everything was new".
    baseline: frozenset[str] | None
    #: Installs that added no app: an app already there was reinstalled.
    reinstalled: int = 0
    attempts: int = 0
    #: Kept for the report: what this record has uninstalled so far.
    removed: list[str] = field(default_factory=list)


def policy_for(app: Any, principal_id: str) -> str:
    policy = getattr(app.state, "release_policy", None) or {}
    if principal.label_of(principal_id) is not None:
        return policy.get("client", "uninstall")
    return policy.get("owner", "none")


def _records(app: Any) -> dict[str, InstallRecord]:
    records = getattr(app.state, "session_installs", None)
    if records is None:
        records = app.state.session_installs = {}
    return records


def _holding_session(app: Any, udid: str, session_id: str | None) -> Any:
    registry = getattr(app.state, "sessions", None)
    if registry is None or not session_id:
        return None
    held = registry.holder_of(udid)
    return held.session if held is not None and held.session.id == session_id else None


async def before_install(app: Any, udid: str, caller: Any) -> frozenset[str] | None:
    """Note the device's apps before an install by `caller`, when its session
    holds the device and its policy would clean up. Returns the apps there now,
    for `after_install`, or None when nothing is tracked. Raises
    `BaselineUnavailable` when they could not be listed: an install whose
    release could not be cleaned up is refused rather than made."""
    session = _holding_session(app, udid, getattr(caller, "session", None))
    if session is None or policy_for(app, caller.principal) == "none":
        return None
    try:
        apps, _ = await app.state.device_controller.list_apps(udid)
    except Exception as e:
        raise BaselineUnavailable(str(e) or type(e).__name__) from e
    present = frozenset(a.bundle_id for a in apps)
    records = _records(app)
    record = records.get(udid)
    opened = getattr(session, "opened_at", None)
    if record is None or (record.session, record.opened) != (session.id, opened):
        records[udid] = InstallRecord(session=session.id, opened=opened, baseline=present)
    return present


async def after_install(app: Any, udid: str, before: frozenset[str] | None) -> None:
    """Count an install that added no app: one already there was reinstalled,
    and is left at release. Never raises."""
    record = _records(app).get(udid)
    if before is None or record is None:
        return
    try:
        apps, _ = await app.state.device_controller.list_apps(udid)
    except Exception:
        return
    if not {a.bundle_id for a in apps} - before:
        record.reinstalled += 1


async def end_for_release(app: Any, registry: Any, session: Any, udid: str,
                          actions: list[dict]) -> None:
    """Run the release policy for `session`'s hold on `udid` ending. Never
    raises; what it did, or why not, is appended to `actions`."""
    from server.models import DeviceType

    records = _records(app)
    record = records.get(udid)
    opened = getattr(session, "opened_at", None)
    mine = record is not None and (record.session, record.opened) == (session.id, opened)
    held = registry.holder_of(udid)
    if held is not None and getattr(held.session, "id", None) != session.id:
        if mine and held.session.principal == session.principal:
            # The key holds it still, through another session: that one's
            # release cleans up.
            record.session = held.session.id
            record.opened = getattr(held.session, "opened_at", None)
            actions.append({"udid": udid, "action": "installs_handed_over", "ok": True,
                            "to": held.session.id})
        elif mine:
            records.pop(udid, None)
            actions.append({"udid": udid, "action": "installs_left", "ok": True,
                            "note": "another session holds it now, so what this session "
                                    "installed was left"})
        return
    policy = policy_for(app, session.principal)
    if policy == "none" or (policy == "uninstall" and not mine):
        return
    controller = getattr(app.state, "device_controller", None)
    if controller is None:
        return
    erase = policy == "erase" and controller._device_type(udid) == DeviceType.SIMULATOR
    if not erase and not mine:
        return
    try:
        if erase:
            done = await asyncio.wait_for(
                _erase(app, controller, session, udid), TIMEOUT_S)
        else:
            done = await asyncio.wait_for(_uninstall(controller, record, udid), TIMEOUT_S)
        records.pop(udid, None)
        actions.append({"udid": udid, "ok": True, **done})
    except Exception as e:  # noqa: BLE001 -- reported, never raised
        error = (f"did not finish within {TIMEOUT_S:g}s" if isinstance(e, TimeoutError)
                 else str(e) or type(e).__name__)
        if record is None:
            record = records[udid] = InstallRecord(session=session.id, opened=opened,
                                                   baseline=None)
        record.attempts += 1
        gave_up = record.attempts >= ATTEMPTS
        if gave_up:
            records.pop(udid, None)
        actions.append({"udid": udid, "action": "erased" if erase else "installs_removed",
                        "ok": False, "error": error, **({"gave_up": True, "note": (
                            f"Tried {record.attempts} times: what this session installed "
                            "may still be on it.")} if gave_up else {})})


async def _uninstall(controller: Any, record: InstallRecord, udid: str) -> dict:
    if record.baseline is None:
        return {"action": "installs_removed", "removed": [], "note": "no baseline was noted"}
    apps, _ = await controller.list_apps(udid)
    added = [a.bundle_id for a in apps
             if a.bundle_id not in record.baseline and a.bundle_id not in KEPT
             and getattr(a, "app_type", "") != "System"]
    for bundle_id in added:
        await controller.uninstall_app(bundle_id, udid)
        record.removed.append(bundle_id)
    return {"action": "installs_removed", "removed": list(record.removed),
            **({"reinstalled_kept": record.reinstalled} if record.reinstalled else {})}


async def _erase(app: Any, controller: Any, session: Any, udid: str) -> dict:
    reservation = (getattr(session, "reservations", None) or {}).get(udid)
    shutting_down = (getattr(reservation, "on_release", "") == "shutdown_booted"
                     and getattr(reservation, "booted_here", False))
    was_booted = await _is_booted(controller, udid)
    await controller.erase(udid)
    rebooted = False
    if was_booted and not shutting_down:
        await controller.boot(udid)
        rebooted = True
    return {"action": "erased", "rebooted": rebooted}


async def _is_booted(controller: Any, udid: str) -> bool:
    from server.models import DeviceState

    devices = await controller.list_devices()
    match = next((d for d in devices if d.udid == udid), None)
    return match is not None and match.state == DeviceState.BOOTED
