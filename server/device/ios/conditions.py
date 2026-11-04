"""Induced conditions on a physical iPhone: a slow network, a thermal state
(#245, phase 6).

The device's own Instruments channel offers them -- the Network Link
conditioner's profiles, three thermal states, three GPU performance states --
and each changes the whole phone, not one app. So:

- **The device is asked, never assumed.** It reports each group's `isActive`
  and `activeProfile`, so a set is confirmed by reading it back, and so is a
  clear.
- **The connection that set a condition is held open for as long as it
  lasts.** Measured on an iPhone 11 (iOS 26.7): a network profile ends the
  moment the connection that set it closes -- a set-and-close read back as
  active and was gone a second later -- while a thermal state outlives its
  connection but not the process that set it. So each phone with a condition
  has one held connection, and a task that runs set, read and clear on it in
  turn and reads the device every `WATCH_S`: a condition that ends -- the
  tunnel dropping, a clear in Xcode -- is said, never shown as active. Other
  connections from the same process leave it alone (measured).
- **A clear is waited out before its connection closes.** It is sent with no
  reply (`disableActiveCondition`), and one closed at once was dropped: the
  phone stayed throttled. Held open, it cleared in 1.0 s.
- **What quern set, quern undoes**: when asked, when the hold that set it
  ends, and when the server stops -- and a server that dies takes its
  conditions with it (measured, `kill -9` included). The record on disk,
  written *before* the device is asked, is the backstop for an OS that keeps
  one: a server that starts with a record it did not write clears it. A
  condition someone set in Xcode is reported, and replaced only when asked.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from server.models import DeviceCondition, DeviceConditions, DeviceError

logger = logging.getLogger(__name__)

_FILE = "device-conditions.json"
#: How long a set or clear may take to show on the device before it is
#: reported as not having landed. Measured: a clear 1.0 s, a set at once.
CONFIRM_S = 5.0
_POLL_S = 0.25
#: Connecting, or one command on a held connection, plus its confirmation.
CALL_TIMEOUT_S = 20.0
#: How often a held condition's device is read, to see it is still in force.
WATCH_S = 10.0

#: The groups, by the identifier the device gives them, and what kind of
#: condition each is. Thermal and GPU states change how the whole phone
#: performs, so they are set only when asked for by name (`allow_performance`).
KINDS = {"SlowNetworkCondition": "network", "ThermalCondition": "thermal",
         "GPUPerformanceState": "gpu"}
NETWORK = "network"


def _path() -> Path:
    from server import config

    return config.CONFIG_DIR / _FILE


class _Unreadable(Exception):
    """The record file exists and cannot be read: not the same as empty."""


class ConditionRecords:
    """udid -> what quern set there: {profile, group, principal, session,
    set_at}. Kept on disk, so a crashed server's are found by the next."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def _load(self) -> dict[str, dict]:
        try:
            data = json.loads(_path().read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            raise _Unreadable(str(e)) from e
        if not isinstance(data, dict):
            raise _Unreadable(f"expected an object, found {type(data).__name__}")
        return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, dict)}

    def _save(self, records: dict[str, dict]) -> None:
        path = _path()
        if not records:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{_FILE}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(records, indent=2, sort_keys=True) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _update(self, change: Callable[[dict[str, dict]], object]) -> None:
        """Read, change, write. Raises _Unreadable rather than overwrite a
        file it could not read: other devices' records are in it."""
        with self._lock:
            records = self._load()
            change(records)
            self._save(records)

    def all(self) -> dict[str, dict]:
        """Every record; an unreadable file reads as none, and says so."""
        with self._lock:
            try:
                return self._load()
            except _Unreadable as e:
                logger.warning("Could not read %s (%s); conditions to clear are unknown",
                               _path(), e)
                return {}

    def get(self, udid: str) -> dict | None:
        return self.all().get(udid)

    def put(self, udid: str, record: dict) -> None:
        self._update(lambda r: r.__setitem__(udid, record))

    def drop(self, udid: str) -> None:
        self._update(lambda r: r.pop(udid, None))


# -- the device ----------------------------------------------------------------

def _pmd3() -> Any:
    from types import SimpleNamespace

    try:
        from pymobiledevice3.remote.remote_service_discovery import (
            RemoteServiceDiscoveryService,
        )
        from pymobiledevice3.services.dvt.instruments.condition_inducer import (
            ConditionInducer,
        )
        from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
    except ImportError as e:
        raise DeviceError(f"device conditions need pymobiledevice3's Instruments channel "
                          f"({e})", tool="pymobiledevice3") from e
    return SimpleNamespace(RemoteServiceDiscoveryService=RemoteServiceDiscoveryService,
                           DvtProvider=DvtProvider, ConditionInducer=ConditionInducer)


def _failure(uuid: str, what: str, e: BaseException) -> DeviceError:
    if isinstance(e, DeviceError):
        return e
    return DeviceError(f"{what} on {uuid[:8]} failed: {type(e).__name__}: {e}",
                       tool="pymobiledevice3")


async def _with_inducer(uuid: str, what: str, fn: Callable[[Any], Any]) -> Any:
    """Run `fn(inducer)` on a connection of its own, closed after: a read, or
    a clear of a condition quern does not hold."""
    from server.device.performance import _device_address

    lib = _pmd3()
    address = await _device_address(uuid)
    rsd = lib.RemoteServiceDiscoveryService(address)
    deadline = asyncio.timeout(CALL_TIMEOUT_S)
    try:
        async with deadline:
            try:
                await rsd.connect()
                async with lib.DvtProvider(rsd) as dvt:
                    async with lib.ConditionInducer(dvt) as inducer:
                        return await fn(inducer)
            finally:
                with contextlib.suppress(Exception):
                    await rsd.close()
    except TimeoutError:
        if deadline.expired():
            raise DeviceError(f"{uuid[:8]} did not answer within {CALL_TIMEOUT_S:g}s "
                              f"while {what}", tool="pymobiledevice3") from None
        raise
    except Exception as e:  # pymobiledevice3 raises its own hierarchy
        raise _failure(uuid, what, e) from e


def parse_groups(groups: list[dict]) -> tuple[list[DeviceCondition], DeviceCondition | None]:
    """(every profile the device offers, the one active), from the device's list."""
    offered, active = [], None
    for group in groups or []:
        gid = str(group.get("identifier") or "")
        kind = KINDS.get(gid, "other")
        for p in group.get("profiles") or []:
            c = DeviceCondition(profile=str(p.get("identifier") or ""), group=gid, kind=kind,
                                name=str(p.get("name") or ""),
                                description=str(p.get("description") or ""))
            offered.append(c)
            if group.get("isActive") and group.get("activeProfile") == c.profile:
                active = c
        if group.get("isActive") and active is None:
            # Active with a profile it does not list: still active, and said.
            active = DeviceCondition(profile=str(group.get("activeProfile") or ""), group=gid,
                                     kind=kind)
    return offered, active


async def _read(inducer: Any) -> tuple[list[DeviceCondition], DeviceCondition | None]:
    return parse_groups(await inducer.list())


async def _until(inducer: Any, done: Callable[[DeviceCondition | None], bool]
                 ) -> tuple[list[DeviceCondition], DeviceCondition | None, bool]:
    """Read the device until `done(active)` or CONFIRM_S, on the same
    connection: closing it early drops a clear."""
    end = time.monotonic() + CONFIRM_S
    while True:
        offered, active = await _read(inducer)
        if done(active) or time.monotonic() >= end:
            return offered, active, done(active)
        await asyncio.sleep(_POLL_S)


async def _clear_on(uuid: str, inducer: Any) -> DeviceCondition | None:
    """Clear whatever is active on this connection, waited out on it.
    Returns what was active, None when nothing was."""
    _, before = await _read(inducer)
    if before is None:
        return None
    await inducer.clear()
    _, active, ok = await _until(inducer, lambda a: a is None)
    if not ok:
        raise DeviceError(f"{uuid[:8]} still reports {active.profile if active else '?'} "
                          f"active {CONFIRM_S:g}s after it was cleared", tool="pymobiledevice3")
    return before


async def read_conditions(uuid: str) -> tuple[list[DeviceCondition], DeviceCondition | None]:
    return await _with_inducer(uuid, "reading conditions", _read)


async def clear_condition(uuid: str) -> DeviceCondition | None:
    """Clear one quern does not hold -- set in Xcode, say -- on a connection of
    its own, held until the device says it is gone."""
    return await _with_inducer(uuid, "clearing the condition",
                               lambda inducer: _clear_on(uuid, inducer))


# -- holding a condition --------------------------------------------------------

class _Hold:
    """One phone's held connection: commands in, one at a time."""

    def __init__(self, udid: str) -> None:
        self.udid = udid
        self.queue: asyncio.Queue[tuple[str, str | None, asyncio.Future]] = asyncio.Queue()
        self.task: asyncio.Task | None = None
        #: Why it ended without a clear, when it did.
        self.ended: str | None = None
        self.cleared = False

    @property
    def live(self) -> bool:
        return self.task is not None and not self.task.done()

    async def ask(self, command: str, arg: str | None = None) -> Any:
        if not self.live:
            raise DeviceError(f"the connection holding {self.udid[:8]}'s condition has "
                              f"closed{f': {self.ended}' if self.ended else ''}",
                              tool="pymobiledevice3")
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        await self.queue.put((command, arg, future))
        try:
            return await asyncio.wait_for(asyncio.shield(future), CALL_TIMEOUT_S)
        except TimeoutError:
            raise DeviceError(f"{self.udid[:8]} did not answer within {CALL_TIMEOUT_S:g}s",
                              tool="pymobiledevice3") from None


async def _run_hold(hold: _Hold, ready: asyncio.Future,
                    on_end: Callable[[_Hold], None]) -> None:
    """Open the phone's connection and serve `hold`'s commands until a clear
    lands, the device says the condition ended, or the connection fails."""
    from server.device.performance import _device_address

    rsd = None
    try:
        lib = _pmd3()
        address = await asyncio.wait_for(_device_address(hold.udid), CALL_TIMEOUT_S)
        rsd = lib.RemoteServiceDiscoveryService(address)
        await asyncio.wait_for(rsd.connect(), CALL_TIMEOUT_S)
        async with lib.DvtProvider(rsd) as dvt:
            async with lib.ConditionInducer(dvt) as inducer:
                ready.set_result(None)
                await _serve(hold, inducer)
    except asyncio.CancelledError:
        hold.ended = hold.ended or "the server stopped"
        raise
    except Exception as e:  # noqa: BLE001 -- said on the hold and to whoever waits
        error = _failure(hold.udid, "holding the condition", e)
        if isinstance(e, TimeoutError):
            error = DeviceError(f"{hold.udid[:8]} did not answer within {CALL_TIMEOUT_S:g}s",
                                tool="pymobiledevice3")
        hold.ended = hold.ended or str(error)
        if not ready.done():
            ready.set_exception(error)
    finally:
        if rsd is not None:
            with contextlib.suppress(Exception):
                await rsd.close()
        while not hold.queue.empty():          # nobody is left to answer them
            _, _, future = hold.queue.get_nowait()
            if not future.done():
                future.set_exception(DeviceError(
                    f"the connection holding {hold.udid[:8]}'s condition closed: "
                    f"{hold.ended or 'cleared'}", tool="pymobiledevice3"))
        on_end(hold)


async def _serve(hold: _Hold, inducer: Any) -> None:
    while True:
        try:
            command, arg, future = await asyncio.wait_for(hold.queue.get(), WATCH_S)
        except TimeoutError:
            _, active = await _read(inducer)   # raises when the connection is gone
            if active is None:
                hold.ended = "the device reports none active: it was cleared elsewhere"
                return
            continue
        try:
            if command == "set":
                await inducer.set(arg)
                _, active, ok = await _until(
                    inducer, lambda a: a is not None and a.profile == arg)
                if not ok:
                    raise DeviceError(
                        f"{hold.udid[:8]} did not report {arg} active within {CONFIRM_S:g}s"
                        + (f" (it reports {active.profile})" if active else
                           " (it reports none)"), tool="pymobiledevice3")
                future.set_result(active)
            elif command == "read":
                future.set_result(await _read(inducer))
            elif command == "clear":
                future.set_result(await _clear_on(hold.udid, inducer))
                hold.cleared = True
                return                         # cleared: the connection can go
        except Exception as e:  # noqa: BLE001 -- the caller gets it
            if not future.done():
                future.set_exception(_failure(hold.udid, f"{command} of the condition", e))
            # A clear that failed keeps the connection: closing it could
            # leave a thermal state on.


# -- what callers use ----------------------------------------------------------

class ConditionKeeper:
    """Sets and clears conditions, holds the connections they need, and keeps
    the records that make sure what quern set is undone."""

    def __init__(self, records: ConditionRecords | None = None) -> None:
        self.records = records or ConditionRecords()
        self._locks: dict[str, asyncio.Lock] = {}
        self._holds: dict[str, _Hold] = {}
        #: Why a condition quern held ended without being cleared, per phone.
        self._ended: dict[str, str] = {}
        # What a previous server left: the records there before this one
        # wrote any. Only these are cleared at start-up -- in the background,
        # after requests are served, so one this server wrote is its own.
        self._leftover = self.records.all()

    def _lock(self, udid: str) -> asyncio.Lock:
        return self._locks.setdefault(udid, asyncio.Lock())

    def _live(self, udid: str) -> _Hold | None:
        hold = self._holds.get(udid)
        return hold if hold is not None and hold.live else None

    def _hold_ended(self, hold: _Hold) -> None:
        if self._holds.get(hold.udid) is hold:
            del self._holds[hold.udid]
        if hold.cleared or not hold.ended or hold.ended == "the server stopped":
            return
        record = self.records.get(hold.udid)
        profile = record.get("profile") if record else "the condition"
        logger.warning("%s's %s, which quern set, ended: %s", hold.udid, profile, hold.ended)
        self._ended[hold.udid] = f"{profile}, which quern set, ended: {hold.ended}"
        with contextlib.suppress(_Unreadable):
            self.records.drop(hold.udid)

    async def _held(self, udid: str) -> _Hold:
        if (hold := self._live(udid)) is not None:
            return hold
        hold = _Hold(udid)
        ready: asyncio.Future = asyncio.get_running_loop().create_future()
        hold.task = asyncio.create_task(_run_hold(hold, ready, self._hold_ended))
        self._holds[udid] = hold
        await ready
        return hold

    async def _read(self, udid: str) -> tuple[list[DeviceCondition], DeviceCondition | None]:
        hold = self._live(udid)
        return await (hold.ask("read") if hold is not None else read_conditions(udid))

    async def describe(self, udid: str) -> DeviceConditions:
        offered, active = await self._read(udid)
        out = self._describe(udid, offered, active)
        if active is None:
            out.note = self._ended.get(udid)
        return out

    def _describe(self, udid: str, offered: list[DeviceCondition],
                  active: DeviceCondition | None) -> DeviceConditions:
        record = self.records.get(udid)
        if active is not None:
            ours = record is not None and record.get("profile") == active.profile
            active = active.model_copy(update={"set_by": "quern" if ours else "other",
                                               "set_at": record.get("set_at") if ours else None})
        return DeviceConditions(udid=udid, active=active, available=offered)

    async def set(self, udid: str, profile: str, *, allow_performance: bool,
                  principal: str | None, session: str | None) -> DeviceConditions:
        async with self._lock(udid):
            offered, active = await self._read(udid)
            wanted = next((c for c in offered if c.profile == profile), None)
            if wanted is None:
                names = ", ".join(c.profile for c in offered if c.kind == NETWORK)
                raise ValueError(f"{profile!r} is not a condition {udid[:8]} offers; its "
                                 f"network ones are: {names}")
            if wanted.kind != NETWORK and not allow_performance:
                raise ValueError(
                    f"{profile} is a {wanted.kind} condition: it changes how the whole phone "
                    f"performs, not only its network, so it is set only with "
                    f"allow_performance: true")
            replaced = None
            if active is not None and active.profile != profile:
                replaced = self._describe(udid, offered, active).active
            fresh = self._live(udid) is None
            # Recorded before the device is asked: a server killed between
            # the two still leaves a record for the next one to clear.
            self.records.put(udid, {"profile": profile, "group": wanted.group,
                                    "principal": principal, "session": session,
                                    "set_at": _now()})
            self._ended.pop(udid, None)
            try:
                hold = await self._held(udid)
                await hold.ask("set", profile)
            except BaseException:
                await self._after_failed_set(udid, fresh)
                raise
            out = self._describe(udid, offered, wanted)
            out.replaced = replaced
            return out

    async def _after_failed_set(self, udid: str, fresh: bool) -> None:
        """A set that failed may or may not have landed: one on a connection
        this call opened is undone on it, and the record is kept unless the
        device then says nothing is active."""
        with contextlib.suppress(Exception):
            hold = self._live(udid)
            if fresh and hold is not None:
                await hold.ask("clear")
            _, active = await self._read(udid)
            if active is None:
                self.records.drop(udid)

    async def clear(self, udid: str) -> DeviceCondition | None:
        """Clear whatever is active, quern's or not, and the record."""
        async with self._lock(udid):
            record = self.records.get(udid)
            hold = self._live(udid)
            cleared = await (hold.ask("clear") if hold is not None else clear_condition(udid))
            self.records.drop(udid)
            self._ended.pop(udid, None)
            if cleared is not None:
                ours = record is not None and record.get("profile") == cleared.profile
                cleared = cleared.model_copy(update={
                    "set_by": "quern" if ours else "other",
                    "set_at": record.get("set_at") if ours else None})
            return cleared

    async def clear_recorded(self, udid: str, *, session: str | None = None,
                             principal: str | None = None) -> dict | None:
        """Undo what quern set here -- only that, and only if `session` or
        `principal` (when given) set it. An action for the caller's report,
        None when there was nothing of quern's to clear. Never raises."""
        record = self.records.get(udid)
        if record is None:
            return None
        if session is not None or principal is not None:
            # A hold ending: only what that session, or its key through another
            # session, set. Someone else's condition is theirs to end.
            mine = ((session is not None and record.get("session") == session)
                    or (principal is not None and record.get("principal") == principal))
            if not mine:
                return None
        action = {"udid": udid, "action": "condition_cleared", "profile": record.get("profile")}
        try:
            async with self._lock(udid):
                hold = self._live(udid)
                _, active = await self._read(udid)
                if active is not None and active.profile == record.get("profile"):
                    await (hold.ask("clear") if hold is not None else clear_condition(udid))
                    action["ok"] = True
                else:
                    # Not ours any more (cleared by hand, or replaced in
                    # Xcode): nothing of quern's to undo, and nothing to hold.
                    action.update(ok=True, already="cleared" if active is None else "replaced")
                    if hold is not None:
                        hold.cleared = True
                        hold.task.cancel()
                self.records.drop(udid)
        except Exception as e:  # noqa: BLE001 -- reported, never raised
            action.update(ok=False, error=str(e))
        return action

    async def clear_all(self) -> list[dict]:
        """Every condition quern holds or recorded, at once: shutdown."""
        udids = set(self.records.all()) | set(self._holds)
        results = await asyncio.gather(*(self.clear_recorded(u) for u in udids))
        return [r for r in results if r is not None]

    async def clear_leftovers(self) -> list[dict]:
        """What a previous server recorded and this one has not touched since:
        start-up. A record this server wrote is its own, not a leftover."""
        current = self.records.all()
        udids = [u for u, r in self._leftover.items() if current.get(u) == r]
        self._leftover = {}
        results = await asyncio.gather(*(self.clear_recorded(u) for u in udids))
        return [r for r in results if r is not None]


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()
