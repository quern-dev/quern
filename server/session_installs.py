"""What a session installed on a device it holds, cleaned up at release (#581).

A shared machine grew without bound: apps remote agents installed, and their
data, stayed on the simulators after their sessions ended (17 GB across 44
simulators on romulus), and on a machine several people use, one client's
apps, data and keychain reached the next client to reserve the simulator.

Each install by a session that holds the device records what it added: the
device's apps after it, less those before. When the hold ends, the release
policy for the caller's kind of key runs (``sessions.client_release``,
``sessions.owner_release``, read at start):

- ``uninstall``: remove what the session's installs added, and nothing else --
  not an app the owner installed meanwhile, not quern's own drivers. An
  install that added nothing (a reinstall) leaves what it replaced, and says so.
- ``erase``: erase each iOS simulator the session held, whether or not anything
  was installed, and boot it again if it was booted. Android devices and
  physical iPhones fall back to ``uninstall``: an emulator is wiped only by
  relaunching it, and a phone cannot be erased safely from here.
- ``none``: leave the device as the session left it.

What an install added is trusted only when it is one app, and, where the
artifact's bundle id or package can be read (an ``.app``, an ``.ipa``, an APK
through the Android SDK's ``aapt2``, a Gradle build's metadata), only when it
is that app: a listing that came back short before the install would make
apps already there look new, and an app another caller installed meanwhile
would look like this install's. Anything else, or a result that could not be
listed, is counted as unclear; its apps are left, and the release says so.
An APK on a machine with no build-tools is held to the one-app rule alone.

The record belongs to the session and its opening, not to its key: a device
another session of the same key holds by then -- the same name opened again
included -- takes the record over, as a device proxy does, and is never erased
under it. A clean-up that fails keeps the record and is tried again at the next
sweep, up to three times. An erase that finished, or was given up on, is not
run again for that opening, whatever else on the device is retried.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import plistlib
import re
import shutil
import subprocess
import zipfile
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from server import principal
from server.device.ios.wda import WDA_BUNDLE_ID, WDA_RUNNER_BUNDLE_ID

#: Never uninstalled: quern's own drivers, which it installs itself -- its
#: WebDriverAgent, and the uiautomator2 APK that carries Android's keyboard
#: and set_locale's receiver.
KEPT = frozenset({WDA_BUNDLE_ID, WDA_RUNNER_BUNDLE_ID, "com.github.uiautomator"})

#: Attempts at a clean-up before giving up on it.
ATTEMPTS = 3

#: How long one clean-up may take: it runs in the sweep every session's
#: expiry waits on. An erase and reboot is the slow case.
TIMEOUT_S = 120.0

#: The largest Info.plist read from an .ipa: an upload's cap bounds its
#: compressed size, not what one member inflates to.
_MAX_PLIST_BYTES = 8 * 1024 * 1024

#: How long `aapt2 dump packagename` may take. Measured: 0.3 s on a 10 MB APK.
_AAPT2_TIMEOUT_S = 30.0

#: A Java package name, as aapt2 prints it.
_PACKAGE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+")

#: How many finished erases are remembered. A retry comes within a few sweeps
#: of its release, so only the recent ones matter.
_FINISHED_MAX = 512


class BaselineUnavailable(Exception):
    """The device's apps could not be listed before an install, so what the
    install adds could not be told apart from what was there."""


@dataclass
class InstallRecord:
    session: str
    opened: Any
    #: Apps the session's installs added, one per install.
    added: set[str] = field(default_factory=set)
    #: Installs that added no app: one already there was reinstalled.
    reinstalls: int = 0
    #: Installs whose effect could not be told; their apps are left.
    unclear: int = 0
    #: Installs that failed and left nothing new: not reinstalls.
    failed: int = 0
    attempts: int = 0
    #: Set while its release cleans up: an install finishing then is past the
    #: clean-up, and removes its own app as if the hold had ended.
    ending: bool = False
    #: Kept for the report: what this record has uninstalled so far.
    removed: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Install:
    """An install under way: whose, where, and what was there before it."""
    session: str
    opened: Any
    udid: str
    before: frozenset[str]
    principal: str = ""


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


def _finished(app: Any) -> OrderedDict:
    finished = getattr(app.state, "session_installs_finished", None)
    if finished is None:
        finished = app.state.session_installs_finished = OrderedDict()
    return finished


def _drop(records: dict, udid: str, record: InstallRecord | None) -> None:
    """Drop `record`, not whatever is under `udid` by now: a session of the
    same key reserving during the clean-up has a record of its own there."""
    if record is not None and records.get(udid) is record:
        records.pop(udid, None)


def _erase_attempts(app: Any) -> dict:
    tries = getattr(app.state, "session_installs_erase_attempts", None)
    if tries is None:
        tries = app.state.session_installs_erase_attempts = {}
    return tries


def _was_booted(app: Any) -> dict:
    booted = getattr(app.state, "session_installs_was_booted", None)
    if booted is None:
        booted = app.state.session_installs_was_booted = {}
    return booted


def _finish(app: Any, key: tuple) -> None:
    _erase_attempts(app).pop(key, None)
    _was_booted(app).pop(key, None)
    finished = _finished(app)
    finished[key] = True
    while len(finished) > _FINISHED_MAX:
        finished.popitem(last=False)


def bundle_id_of(app_path: str | Path) -> str | None:
    """The bundle id an ``.app`` or ``.ipa`` installs as, or None when it
    cannot be read from it (an APK, a damaged bundle)."""
    path = Path(app_path)
    try:
        if path.is_dir():
            with open(path / "Info.plist", "rb") as f:
                info = plistlib.load(f)
        elif path.suffix.lower() == ".ipa":
            with zipfile.ZipFile(path) as z:
                name = next(n for n in z.namelist() if n.count("/") == 2
                            and n.startswith("Payload/") and n.endswith(".app/Info.plist"))
                if z.getinfo(name).file_size > _MAX_PLIST_BYTES:
                    return None
                info = plistlib.loads(z.read(name))
        elif path.suffix.lower() == ".apk":
            return _apk_package(path)
        else:
            return None
    except Exception:  # noqa: BLE001 -- unreadable is unknown, not an error
        return None
    bundle_id = info.get("CFBundleIdentifier") if isinstance(info, dict) else None
    return bundle_id if isinstance(bundle_id, str) and bundle_id else None


def _aapt2() -> str | None:
    """The SDK's aapt2: on the PATH, else the newest build-tools' under
    ANDROID_HOME, ANDROID_SDK_ROOT or Android Studio's default SDK."""
    from server.device.android.adb import _SDK_SEARCH_PATHS

    found = shutil.which("aapt2")
    if found:
        return found
    roots = [Path(os.environ[v]) for v in ("ANDROID_HOME", "ANDROID_SDK_ROOT")
             if os.environ.get(v)] + list(_SDK_SEARCH_PATHS)
    for root in roots:
        tools = [t for t in (root / "build-tools").glob("*/aapt2") if t.is_file()]
        if tools:
            newest = max(tools, key=lambda t: tuple(
                int(n) for n in re.findall(r"\d+", t.parent.name)))
            return str(newest)
    return None


def _apk_package(path: Path) -> str | None:
    """The package an APK installs as, read by aapt2; None without one."""
    tool = _aapt2()
    if tool is None:
        return None
    done = subprocess.run([tool, "dump", "packagename", str(path)], capture_output=True,
                          text=True, timeout=_AAPT2_TIMEOUT_S)
    name = done.stdout.strip() if done.returncode == 0 else ""
    return name if _PACKAGE.fullmatch(name) else None


def hold_ended_note(ended: dict) -> str | None:
    """`after_install`'s answer as a build's per-device note, or None."""
    if not ended.get("hold_ended"):
        return None
    if ended.get("removed"):
        return (f"the session's hold ended during this install, so {ended['removed'][0]} "
                "was removed again")
    if ended.get("handed_over"):
        return (f"the session's hold ended during this install; {ended['handed_over'][0]} "
                f"is now {ended['to']}'s, whose release removes it")
    left = ", ".join(ended.get("left") or []) or "nothing new"
    why = f" (removing it failed: {ended['error']})" if ended.get("error") else ""
    return (f"the session's hold ended during this install, and what it added was left on "
            f"the device: {left}{why}")


def _opening(session: Any) -> tuple:
    return getattr(session, "id", None), getattr(session, "opened_at", None)


async def _apps(controller: Any, udid: str) -> frozenset[str]:
    # set_active=False: a release or an install's bookkeeping must not move the
    # owner's active device.
    apps, _ = await controller.list_apps(udid, set_active=False)
    return frozenset(a.bundle_id for a in apps if getattr(a, "app_type", "") != "System")


async def before_install(app: Any, udid: str, caller: Any) -> Install | None:
    """Note the device's apps before an install by `caller`, when its session
    holds the device and its policy would clean up. Returns them for
    `after_install`, or None when nothing is tracked. Raises
    `BaselineUnavailable` when they could not be listed: an install whose
    release could not be cleaned up is refused rather than made."""
    registry = getattr(app.state, "sessions", None)
    session_id = getattr(caller, "session", None)
    held = registry.holder_of(udid) if registry is not None and session_id else None
    if held is None or getattr(held.session, "id", None) != session_id \
            or policy_for(app, caller.principal) == "none":
        return None
    try:
        before = await _apps(app.state.device_controller, udid)
    except Exception as e:
        raise BaselineUnavailable(str(e) or type(e).__name__) from e
    # The listing can take seconds: a hold that ended meanwhile has been
    # released without this record, and nothing would ever clean it up.
    now = registry.holder_of(udid)
    if now is None or _opening(now.session) != _opening(held.session):
        raise BaselineUnavailable("the session's hold ended while the apps were listed")
    session, opened = _opening(held.session)
    records = _records(app)
    record = records.get(udid)
    if record is None or (record.session, record.opened) != (session, opened):
        # A record left by another session of the same key -- its clean-up
        # still to retry -- carries over rather than being dropped.
        carried = record.added if record is not None else set()
        records[udid] = InstallRecord(session=session, opened=opened, added=set(carried))
    return Install(session, opened, udid, before, caller.principal)


async def after_install(app: Any, install: Install | None,
                        expected: str | None = None, *, failed: bool = False) -> dict:
    """Record what an install added. `expected` is the bundle id or package
    the artifact installs as, when it could be read: then only that app is
    the install's. `failed`: the install reported an error -- read all the
    same, since a backend can fail after changing the device. Returns fields
    for the install's answer: none normally;
    when the hold ended while the install ran, the release has cleaned up
    without this app, so it is removed here instead, and the answer says so.
    Never raises."""
    if install is None:
        return {}
    controller = app.state.device_controller
    try:
        added = await _apps(controller, install.udid) - install.before
    except Exception:
        added = None
    known = (added is not None and len(added) == 1 and not added & KEPT
             and (expected is None or added == {expected}))
    record = _records(app).get(install.udid)
    if record is None or record.ending \
            or (record.session, record.opened) != (install.session, install.opened):
        if not known:
            return {"hold_ended": True, "left": sorted(added or ())}
        (bundle_id,) = added
        registry = getattr(app.state, "sessions", None)
        held = registry.holder_of(install.udid) if registry is not None else None
        if held is not None:
            # Someone holds it already: not this install's to change. Its
            # key's next session takes the app, for its release to remove.
            successor = record if record is not None and not record.ending and (
                record.session, record.opened) == _opening(held.session) else None
            if successor is None and record is None:
                # The new hold has installed nothing yet, so has no record:
                # one is made for it, or the app would outlive its release.
                session, opened = _opening(held.session)
                successor = InstallRecord(session=session, opened=opened)
            if successor is not None and held.session.principal == install.principal:
                _records(app).setdefault(install.udid, successor)
                successor.added.add(bundle_id)
                return {"hold_ended": True, "handed_over": [bundle_id], "to": held.session.id}
            return {"hold_ended": True, "left": [bundle_id]}
        # Marked, as a release's clean-up is: a reservation during the
        # uninstall would have it finish under the new hold (#598).
        cleaning = getattr(registry, "cleaning", None)
        try:
            with cleaning([install.udid]) if cleaning else contextlib.nullcontext():
                # Bounded: nobody can reserve the device until it returns.
                await asyncio.wait_for(controller.uninstall_app(
                    bundle_id, install.udid, set_active=False), TIMEOUT_S)
        except Exception as e:  # noqa: BLE001 -- reported on the answer
            error = (f"did not finish within {TIMEOUT_S:g}s" if isinstance(e, TimeoutError)
                     else str(e) or type(e).__name__)
            return {"hold_ended": True, "left": [bundle_id], "error": error}
        return {"hold_ended": True, "removed": [bundle_id]}
    if added is not None and not added:
        if failed:
            record.failed += 1
        else:
            record.reinstalls += 1
    elif known:
        record.added |= added
    else:
        record.unclear += 1
    return {}


async def end_for_release(app: Any, registry: Any, session: Any, udid: str,
                          actions: list[dict]) -> None:
    """Run the release policy for `session`'s hold on `udid` ending. Never
    raises; what it did, or why not, is appended to `actions`."""
    from server.models import DeviceType

    records = _records(app)
    record = records.get(udid)
    opening = _opening(session)
    mine = record is not None and (record.session, record.opened) == opening
    held = registry.holder_of(udid)
    if held is not None and _opening(held.session) == opening:
        # Released and reserved again by the same opening before the sweep
        # ran: the hold goes on, and so does the record.
        return
    if held is not None and _opening(held.session) != opening:
        if mine and held.session.principal == session.principal:
            # The key holds it still, through another session or the same
            # name opened again: that one's release cleans up.
            record.session, record.opened = _opening(held.session)
            actions.append({"udid": udid, "action": "installs_handed_over", "ok": True,
                            "to": held.session.id})
        elif mine:
            records.pop(udid, None)
            actions.append({"udid": udid, "action": "installs_left", "ok": True,
                            "note": "another session holds it now, so what this session "
                                    "installed was left"})
        return
    policy = policy_for(app, session.principal)
    if policy == "none":
        return
    controller = getattr(app.state, "device_controller", None)
    if controller is None:
        return
    key = (*opening, udid)
    erase = (policy == "erase" and key not in _finished(app)
             and controller._device_type(udid) == DeviceType.SIMULATOR)
    if not mine:
        if not erase:
            return
        record = None                   # another opening's, not this release's
    else:
        record.ending = True
    try:
        if erase:
            done = await asyncio.wait_for(
                _erase(app, controller, session, udid, key, registry), TIMEOUT_S)
            _finish(app, key)
        else:
            done = await asyncio.wait_for(
                _uninstall(controller, record, udid, registry), TIMEOUT_S)
        _drop(records, udid, record)
        actions.append({"udid": udid, "ok": True, **done})
    except _Reserved:
        # Reserved again mid-clean-up -- only its own key can reserve a device
        # still draining: what is left is the new hold's, handed over below.
        if record is not None:
            record.ending = False
        if registry.holder_of(udid) is not None:
            await end_for_release(app, registry, session, udid, actions)
    except Exception as e:  # noqa: BLE001 -- reported, never raised
        error = (f"did not finish within {TIMEOUT_S:g}s" if isinstance(e, TimeoutError)
                 else str(e) or type(e).__name__)
        if erase:
            # Counted apart from the records, which hold installs only: an
            # erase runs with or without one, and must not replace another
            # opening's.
            tries = _erase_attempts(app)
            attempts = tries[key] = tries.get(key, 0) + 1
        else:
            attempts = record.attempts = record.attempts + 1
        if record is not None:
            record.ending = False
        gave_up = attempts >= ATTEMPTS
        if gave_up:
            _drop(records, udid, record)
            if erase:
                _finish(app, key)
        actions.append({"udid": udid, "action": "erased" if erase else "installs_removed",
                        "ok": False, "error": error, **({"gave_up": True, "note": (
                            f"Tried {attempts} times: what this session installed "
                            "may still be on it.")} if gave_up else {})})


class _Reserved(Exception):
    """The device was reserved again while its release cleaned up."""


def _still_free(registry: Any, udid: str) -> None:
    # Before each step: a reservation during the clean-up's awaits is a new
    # hold, and nothing more is done under it.
    if registry is not None and registry.holder_of(udid) is not None:
        raise _Reserved()


async def _uninstall(controller: Any, record: InstallRecord, udid: str,
                     registry: Any = None) -> dict:
    present = await _apps(controller, udid)
    for bundle_id in sorted((record.added & present) - KEPT - set(record.removed)):
        _still_free(registry, udid)
        await controller.uninstall_app(bundle_id, udid, set_active=False)
        record.removed.append(bundle_id)
    return {"action": "installs_removed", "removed": list(record.removed),
            **({"installs_adding_nothing": record.reinstalls} if record.reinstalls else {}),
            **({"failed_installs": record.failed} if record.failed else {}),
            **({"unclear_installs": record.unclear,
                "note": "some installs could not be told apart from what was already "
                        "there, so their apps were left"} if record.unclear else {})}


async def _erase(app: Any, controller: Any, session: Any, udid: str, key: tuple,
                 registry: Any = None) -> dict:
    reservation = (getattr(session, "reservations", None) or {}).get(udid)
    shutting_down = (getattr(reservation, "on_release", "") == "shutdown_booted"
                     and getattr(reservation, "booted_here", False))
    # Read before the first attempt only: a retry after an erase whose reboot
    # failed finds it shut down, and would leave it so.
    booted = _was_booted(app)
    if key not in booted:
        booted[key] = await _is_booted(controller, udid)
    was_booted = booted[key]
    _still_free(registry, udid)
    await controller.erase(udid)
    rebooted = False
    if was_booted and not shutting_down:
        await controller.boot(udid, set_active=False)
        rebooted = True
    return {"action": "erased", "rebooted": rebooted}


async def _is_booted(controller: Any, udid: str) -> bool:
    from server.models import DeviceState

    devices = await controller.list_devices()
    match = next((d for d in devices if d.udid == udid), None)
    return match is not None and match.state == DeviceState.BOOTED
