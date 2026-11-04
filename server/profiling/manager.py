"""Profiles: one xctrace recording per device, summarised and kept on disk (#245).

A profile's life: started (xctrace launches the app and records), then either
its time limit ends it or `stop` sends SIGINT, then xctrace saves the trace,
then quern exports the tables it summarises. The trace stays for download
until the disk budget needs its room.

**xctrace outlives the server on purpose.** It saves for 13 s or more, and
`quern stop` kills the server about 5 s after asking, so a shutdown that
waited for it would lose the trace instead. xctrace runs in its own session
with its own time limit; the server only stops watching it, and the next
server re-adopts it by pid -- confirmed by its command line, never the pid
alone -- or, if it has finished meanwhile, summarises what it left.

**What xctrace says decides the state, not what is on disk.** Its bundle
exists from the moment recording starts, so a trace directory proves
nothing; "Output file saved as" in its output is what says the trace is
whole. And "Saving output file" means it is no longer recording, so it is
not signalled again: a second SIGINT into a save is a risk nobody measured.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import time
import uuid
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from server import config as config_mod
from server.profiling import android as ad
from server.profiling import xctrace as xt
from server.profiling.trace_processor import TraceProcessor

logger = logging.getLogger(__name__)

TIME_LIMIT_DEFAULT_S = 30
TIME_LIMIT_MAX_S = 300
#: Until xctrace says it is recording. Launching on a device measured about
#: 4 s, and a device xctrace thinks is offline takes 30 s to say so.
START_TIMEOUT_S = 90.0
#: For the CoreDevice warm-up before a physical device. It is one devicectl
#: call, measured 0.35-0.8 s from cold; one that does not answer must not hold
#: the device's profile slot with it.
WARM_UP_TIMEOUT_S = 20.0
#: From the end of recording to a saved trace. Measured 13 s for 30 s of Time
#: Profiler on an iPhone 11; Animation Hitches traces are ten times larger.
FINALIZE_TIMEOUT_S = 180.0
#: One table's export. Measured 26 s for a table of a 192 MB trace.
EXPORT_TIMEOUT_S = 120.0
#: Exports a summary can run: the table of contents and four tables.
EXPORTS_MAX = 5
#: How long stop() waits for the summary before answering 504 ("ask again
#: with get_profile"). Below the ~300 s a client's request waits for headers,
#: so the caller gets that answer instead of a dropped connection.
STOP_WAIT_S = 240.0
#: Hangs and hitches listed in a summary; the count says how many there were.
LIST_LIMIT = 50
#: Hangs triage reads in detail (longest first), and how many of those get
#: their main thread's stack -- each a further `xctrace show`, ~1 s.
TRIAGE_LIMIT = 10
TRIAGE_STACKS = 3
#: triage and the potential-hangs table place a hang at the same start
#: (measured, to the millisecond); this much apart is the same hang.
TRIAGE_MATCH_S = 0.05
#: How long an Android profile's summary waits for trace_processor still
#: downloading before it says what the device reported instead.
TP_WAIT_S = 30.0

ACTIVE = ("recording", "finalizing")
STATE_FILE = "profiles.json"
ID = re.compile(r"^prof_[0-9a-f]{12}$")


class ProfileError(Exception):
    """A request about a profile that cannot be carried out; `status` is the
    HTTP status it should answer with."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _now() -> str:
    return datetime.now(UTC).isoformat()



def _recorded_s(profile: Profile, recording_at: str | None = None) -> float | None:
    """How long the profile ran: from when it was recording (else its start)
    to its end, but never past its time limit -- one finished by a later
    server ended long before it was read. One nobody stopped was to run its
    whole limit: perfetto ending sooner by itself (a `perfetto --stop` from
    another shell, a crash) ends it near the trace's end, which would hide
    the cut (CodeRabbit)."""
    if profile.stop_reason == "time_limit":
        return float(profile.time_limit_s)
    try:
        span = (datetime.fromisoformat(profile.ended_at or "")
                - datetime.fromisoformat(recording_at or profile.started_at)).total_seconds()
    except ValueError:
        return None
    return min(span, profile.time_limit_s) if span > 0 else None

def _root() -> Path:
    # Read at call time, so QUERN_STATE_DIR set by a test applies.
    return config_mod.CONFIG_DIR / "profiles"


@dataclass
class Profile:
    id: str
    udid: str
    #: The identifier xctrace's --device takes: the simulator's UDID, or a
    #: physical device's hardware UDID; an Android device's serial.
    device: str
    platform: str                     # "simulator", "device" or "android"
    bundle_id: str
    preset: str
    time_limit_s: int
    started_at: str
    principal: str = "owner"
    state: str = "recording"
    pid: int | None = None
    ended_at: str | None = None
    #: Why recording ended: "time_limit", "stopped", "session_ended",
    #: "server_restart" (finished while no server watched), or "killed".
    stop_reason: str | None = None
    error: str | None = None
    proc: asyncio.subprocess.Process | None = field(default=None, repr=False)
    done: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    task: asyncio.Task | None = field(default=None, repr=False)
    #: Downloads in progress: eviction leaves the profile alone meanwhile.
    readers: int = field(default=0, repr=False)

    @property
    def dir(self) -> Path:
        return _root() / self.id

    @property
    def android(self) -> bool:
        return self.platform == "android"

    @property
    def trace(self) -> Path:
        # perfetto writes one file; xctrace a bundle (a directory).
        return self.dir / ("profile.pftrace" if self.android else "profile.trace")

    @property
    def log(self) -> Path:
        return self.dir / ("perfetto.log" if self.android else "xctrace.log")

    @property
    def start_file(self) -> Path:
        """An Android profile's reading of the app when it began: what the
        summary measures CPU and frames from without trace_processor."""
        return self.dir / "start.json"

    @property
    def summary_file(self) -> Path:
        return self.dir / "summary.json"

    def record(self) -> dict:
        return {k: getattr(self, k) for k in (
            "id", "udid", "device", "platform", "bundle_id", "preset", "time_limit_s",
            "started_at", "principal", "state", "pid", "ended_at", "stop_reason", "error")}

    def log_text(self) -> str:
        try:
            return self.log.read_text(errors="replace")
        except OSError:
            return ""

    def saving(self) -> bool:
        """xctrace has stopped recording and is writing the trace. perfetto
        writes as it records, so an Android profile never is."""
        return not self.android and any(m in self.log_text() for m in xt.SAVING_MARKERS)

    def summary(self) -> dict | None:
        try:
            return json.loads(self.summary_file.read_text())
        except (OSError, ValueError):
            return None

    def view(self) -> dict:
        state = self.state
        if state == "recording" and self.saving():
            state = "finalizing"          # xctrace said so; the watch has not seen it exit yet
        if self.android:
            template = "perfetto"
            files = (("summary.json", self.summary_file), ("perfetto.log", self.log),
                     ("profile.pftrace", self.trace))
        else:
            preset = xt.PRESETS.get(self.preset)
            template = preset.template if preset else None
            files = (("summary.json", self.summary_file), ("xctrace.log", self.log),
                     ("profile.trace.zip", self.trace))
        return {
            **{k: v for k, v in self.record().items() if k not in ("principal", "pid", "device")},
            "state": state,
            "template": template,
            "bytes": _size(self.dir),
            "summary": self.summary() if self.state not in ACTIVE else None,
            # The trace is offered only once it is known whole.
            "files": [name for name, path in files
                      if path.exists() and (name not in ("profile.trace.zip", "profile.pftrace")
                                            or self.state == "finished")],
        }


def _size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(root, name)).st_size
    return total


def _tail(path: Path, lines: int = 6) -> str:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return ""
    return " / ".join(ln.strip() for ln in text.splitlines()[-lines:] if ln.strip())


def _command_of(pid: int) -> str | None:
    """`ps`'s command line for `pid`: "" when there is no such process, and
    None when ps could not be asked -- not the same answer, so not the same
    value.

    `-ww`, for the whole line: procps cuts it to 80 columns when its output is
    not a terminal, which drops the --output path the match depends on (the
    Linux CI job caught it; macOS ps does not cut)."""
    try:
        r = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "command="],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode == 0:
        return r.stdout.strip()
    # ps exits 1, printing nothing, for a pid that is not running.
    return "" if r.returncode == 1 and not r.stdout.strip() and not r.stderr.strip() else None


def _records_into(command: str | None, trace: Path) -> bool:
    """Whether `command` is an xctrace recording into exactly `trace`, so a
    reused pid never passes for the profile's process."""
    return bool(command) and "xctrace" in command and f" --output {trace} " in f" {command} "



def _why(e: BaseException) -> str:
    """An exception in words; a timeout's own str() is empty."""
    if isinstance(e, TimeoutError):
        return f"xctrace did not answer within {EXPORT_TIMEOUT_S:.0f} s"
    return str(e) or type(e).__name__

class ProfileManager:
    def __init__(self, xctrace: tuple[str, ...] = ("xcrun", "xctrace"),
                 warm_up: Callable[[str], Awaitable[None]] | None = None,
                 settings: Callable[[], dict] = config_mod.profile_settings,
                 adb: ad.Runner | None = None,
                 trace_processor: TraceProcessor | None = None,
                 logged_hangs: Callable[[str, str, str, str, float],
                                        Awaitable[tuple[list[dict] | None, str | None]]]
                 | None = None) -> None:
        self.xctrace = xctrace
        #: Runs adb for Android profiles (`android.adb_runner`); None where
        #: this server cannot.
        self.adb = adb
        self.trace_processor = trace_processor or TraceProcessor()
        #: InputDispatcher's hang reports from the crash buffer, for an
        #: Android summary without trace_processor: (serial, package, from,
        #: to) -> (hangs, or None and why there are none to read).
        self.logged_hangs = logged_hangs
        #: Called before recording a physical device: CoreDevice's tunnel is
        #: up only while something holds it, and xctrace waits for it instead
        #: of asking ("Waiting for device to boot", then a timeout).
        self.warm_up = warm_up
        self.settings = settings
        self._profiles: dict[str, Profile] = {}
        #: An Android profile's perfetto session, one per profile.
        self._devices: dict[str, ad.Device] = {}
        #: udid -> profile id, taken before any await so two starts cannot both pass.
        self._active: dict[str, str] = {}
        self._save_lock = asyncio.Lock()

    # -- reading ---------------------------------------------------------------

    def list(self) -> list[Profile]:
        return sorted(self._profiles.values(), key=lambda p: p.started_at)

    def get(self, profile_id: str) -> Profile:
        try:
            return self._profiles[profile_id]
        except KeyError:
            raise ProfileError(f"no profile {profile_id!r}", 404) from None

    # -- starting --------------------------------------------------------------

    async def start(self, *, udid: str, platform: str, device: str, bundle_id: str,
                    preset: str, time_limit_s: int, principal: str = "owner",
                    warm_up: Callable[[str], Awaitable[None]] | None = None,
                    ) -> tuple[Profile, dict]:
        """Start recording; returns the profile and what was done to make room."""
        presets = ad.PRESETS if platform == "android" else xt.PRESETS
        chosen = presets.get(preset)
        if chosen is None:
            raise ProfileError(f"the {preset!r} preset is not available on "
                               f"{'Android' if platform == 'android' else 'iOS'}; one of "
                               f"{', '.join(presets)}")
        if platform == "android" and self.adb is None:
            raise ProfileError("Android profiles cannot be recorded on this server: adb is "
                               "not available", 503)
        if platform == "simulator" and not chosen.on_simulator:
            raise ProfileError(
                f"the {preset} preset ({chosen.template}) does not run on simulators: "
                "xctrace refuses it there. Use a physical device, or hangs or cpu.")
        if not 1 <= time_limit_s <= TIME_LIMIT_MAX_S:
            raise ProfileError(f"time_limit_s must be 1 to {TIME_LIMIT_MAX_S}")
        if udid in self._active:
            raise ProfileError(
                f"{udid} is already being profiled ({self._active[udid]}); stop that first", 409)
        profile = Profile(id=f"prof_{uuid.uuid4().hex[:12]}", udid=udid, device=device,
                          platform=platform, bundle_id=bundle_id, preset=preset,
                          time_limit_s=time_limit_s, started_at=_now(), principal=principal)
        self._active[udid] = profile.id
        try:
            room = await self._make_room()
            profile.dir.mkdir(parents=True, exist_ok=False)
            if platform == "android":
                await self._start_android(profile, chosen)
            else:
                warm_up = warm_up or self.warm_up
                if platform == "device" and warm_up is not None:
                    try:
                        await asyncio.wait_for(warm_up(udid), WARM_UP_TIMEOUT_S)
                    except Exception as e:  # noqa: BLE001 -- xctrace's own answer says more
                        logger.info("Warming up %s before profiling failed: %r", udid[:8], e)
                await self._spawn(profile, chosen)
            profile.task = asyncio.create_task(self._watch(profile))
        except BaseException:
            # Cancelled or failed before it was watched: end what was spawned,
            # or it records on, into a directory removed under it.
            if profile.proc is not None:
                await asyncio.shield(_end(profile.proc))
            if profile.android and self.adb is not None:
                # Whether or not it was confirmed: a session may exist anyway,
                # and stopping one that does not is a harmless "gone". Then its
                # file, which nothing would ever remove (review). Their own
                # failures must not replace the reason the start failed (live).
                device = self._devices.pop(profile.id, None) or self._device(profile)
                self._devices.pop(profile.id, None)
                with contextlib.suppress(Exception):
                    await asyncio.shield(device.stop(profile.id))
                with contextlib.suppress(Exception):
                    await asyncio.shield(device.remove(profile.id))
            self._active.pop(udid, None)
            self._profiles.pop(profile.id, None)
            if profile.dir.exists():
                await asyncio.shield(asyncio.to_thread(shutil.rmtree, profile.dir, True))
            await asyncio.shield(self._save())
            raise
        return profile, room

    def _device(self, profile: Profile) -> ad.Device:
        """One per profile, so its calls to perfetto take turns (see Device)."""
        device = self._devices.get(profile.id)
        if device is None:
            device = self._devices[profile.id] = ad.Device(self.adb, profile.device)
        return device

    async def _start_android(self, profile: Profile, preset: ad.Preset) -> None:
        """perfetto on the device, detached; the app read first, so a summary
        without trace_processor has something to measure from. Fetching
        trace_processor starts here, while it records, not at the stop."""
        device = self._device(profile)
        first = await device.snapshot(profile.bundle_id)
        # Recorded with the start, so the summary judges the trace by the
        # limit it was written under, not by today's config.
        settings = self.settings()
        trace_bytes = settings.get("android_max_trace_bytes", ad.MAX_TRACE_BYTES)
        # Never past the whole store's budget: one trace over it is kept, and
        # the store stays over until the next start (CodeRabbit). Which one
        # binds is kept too, so the summary names the setting that raises it.
        if settings["max_bytes"] < trace_bytes:
            first["max_trace_bytes"], first["max_trace_setting"] = (
                settings["max_bytes"], "profiles.max_mb")
        elif settings["max_bytes"] == trace_bytes:
            # Both bind: raising either alone leaves the limit where it is.
            first["max_trace_bytes"], first["max_trace_setting"] = (
                trace_bytes, "profiles.max_mb and profiles.android_max_trace_mb")
        else:
            first["max_trace_bytes"], first["max_trace_setting"] = (
                trace_bytes, "profiles.android_max_trace_mb")
        await asyncio.to_thread(_write_json_atomic, profile.start_file, first)
        self.trace_processor.ensure()
        # Recorded before perfetto is asked, as an xctrace is at spawn: a
        # server killed between the two would otherwise leave a session
        # nothing knows about (review). A failed start removes the record.
        self._profiles[profile.id] = profile
        await self._save()
        try:
            await device.start(profile.id, profile.bundle_id, preset, profile.time_limit_s,
                               profile.log, first["max_trace_bytes"])
        except ad.StartFailed as e:
            raise ProfileError(str(e), 502) from e
        except (OSError, TimeoutError) as e:
            raise ProfileError(f"perfetto could not be started on {profile.device}: {e!r}",
                               502) from e
        # When perfetto said it was recording: what a trace's span is held to,
        # not started_at, which is before the snapshot and the device's start
        # (CodeRabbit). Without it only a stop at the time limit is judged.
        first["recording_at"] = _now()
        with contextlib.suppress(OSError):
            await asyncio.to_thread(_write_json_atomic, profile.start_file, first)

    async def _spawn(self, profile: Profile, preset: xt.Preset) -> None:
        cmd = xt.record_command(self.xctrace, profile.device, preset, profile.time_limit_s,
                                profile.trace, profile.bundle_id)
        # Output to a file, never a pipe: a pipe would tie xctrace's life to
        # this process's, and it must outlive a server restart.
        try:
            log = open(profile.log, "wb")
        except OSError as e:
            raise ProfileError(f"could not write {profile.id}'s log: {e}", 500) from e
        with log:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=log,
                    stderr=asyncio.subprocess.STDOUT, start_new_session=True)
            except OSError as e:
                raise ProfileError(f"xctrace could not be run: {e}", 500) from e
        profile.proc, profile.pid = proc, proc.pid
        # Recorded at once: a server killed while xctrace starts would leave a
        # recording nothing knows about, with a directory the budget never counts.
        self._profiles[profile.id] = profile
        await self._save()
        deadline = time.monotonic() + START_TIMEOUT_S
        while True:
            text = profile.log_text()
            if xt.STARTED_MARKER in text:
                return
            failed = next((m for m in xt.FAILURE_MARKERS if m in text), None)
            if failed or proc.returncode is not None or time.monotonic() > deadline:
                if proc.returncode is None:
                    await _end(proc)
                # The line that said it failed comes first: what follows it
                # (xctrace saving, or exiting) can push it out of the tail.
                said = next((ln.strip() for ln in text.splitlines() if failed and failed in ln),
                            None)
                tail = _tail(profile.log)
                parts = [said] if said else []
                if tail and not (said and said in tail):
                    parts.append(tail)
                why = " / ".join(parts) or f"no answer within {START_TIMEOUT_S:g}s"
                hint = ""
                if profile.platform == "device" and "Waiting for device to boot" in text:
                    hint = (" (xctrace saw the device as offline: its CoreDevice tunnel was "
                            "down. Check the device is connected, unlocked and trusted.)")
                raise ProfileError(f"xctrace did not start recording: {why}{hint}", 502)
            await asyncio.sleep(0.25)

    # -- stopping --------------------------------------------------------------

    async def request_stop(self, profile_id: str, reason: str = "stopped") -> Profile:
        """Ask xctrace to stop, without waiting for it: for callers that must
        not be held while a trace saves (a session ending)."""
        profile = self.get(profile_id)
        if profile.state == "recording" and profile.stop_reason in (None, "resumed") \
                and not profile.saving():
            # Once, and never into a save: the reason is the first one given.
            sent = await self._signal(profile, signal.SIGINT)
            if sent == "sent":
                profile.stop_reason = reason
            elif sent == "unknown":
                raise ProfileError(
                    f"could not tell whether perfetto on {profile.device} is still recording "
                    f"{profile_id} (the device did not answer); ask again" if profile.android
                    else f"could not tell whether xctrace for {profile_id} is still running "
                         "(ps did not answer); ask again", 503)
            # "gone": it has ended by itself, and the watch is finishing it.
        return profile

    async def stop(self, profile_id: str, reason: str = "stopped") -> Profile:
        """Stop recording early and wait for the summary."""
        profile = await self.request_stop(profile_id, reason)
        if profile.state in ACTIVE:
            try:
                await asyncio.wait_for(asyncio.shield(profile.done.wait()), STOP_WAIT_S)
            except TimeoutError:
                raise ProfileError(
                    f"{profile_id} has not finished; it is {profile.state}. Ask again later.",
                    504) from None
        return profile

    async def _signal(self, profile: Profile, sig: int) -> str:
        """Signal this profile's xctrace. "sent"; "gone" when it is no longer
        running (or the pid is now another process's); "unknown" when ps
        could not say, so nothing was sent. An Android profile is stopped by
        perfetto itself: the shell may not signal it (measured)."""
        if profile.android:
            return await self._device(profile).stop(profile.id)
        try:
            if profile.proc is not None:
                if profile.proc.returncode is not None:
                    return "gone"
                profile.proc.send_signal(sig)
                return "sent"
            if not profile.pid:
                return "gone"
            command = await asyncio.to_thread(_command_of, profile.pid)
            if command is None:
                return "unknown"
            if not _records_into(command, profile.trace):
                return "gone"
            os.kill(profile.pid, sig)
            return "sent"
        except ProcessLookupError:
            return "gone"

    async def _watch(self, profile: Profile) -> None:
        """Wait for xctrace to finish, however that happens, then summarise."""
        try:
            await self._wait_out(profile)
            await self._finalize(profile)
        except asyncio.CancelledError:
            raise                        # shutdown: the next server picks it up
        except Exception as e:  # noqa: BLE001 -- a profile must never take the server down
            logger.exception("Profile %s failed while finishing", profile.id)
            profile.state, profile.error = "failed", f"quern could not finish it: {e}"
            await self._close(profile)

    async def _wait_out(self, profile: Profile) -> None:
        # Past its time limit plus saving, it is stuck: ask, then insist.
        budget = profile.time_limit_s + FINALIZE_TIMEOUT_S
        started = time.monotonic()
        resumed = profile.stop_reason == "resumed"
        if resumed:
            profile.stop_reason = None
        while (running := await self._alive_state(profile)) is not False:
            if running:
                resumed = False             # seen recording: it did not end unwatched
            if time.monotonic() - started > budget:
                logger.warning("xctrace for %s overran %gs; stopping it", profile.id, budget)
                profile.stop_reason = "killed"
                sent = await self._signal(profile, signal.SIGINT)
                if profile.android:
                    # perfetto's stop is its only lever; and "killed" only if
                    # it landed (review).
                    if sent != "sent":
                        profile.stop_reason = "overran"
                    break
                await asyncio.sleep(10)
                await self._signal(profile, signal.SIGKILL)
                if profile.proc is not None:              # reaped, not left a zombie
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(profile.proc.wait(), 5.0)
                break
            if profile.proc is not None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(profile.proc.wait(), 1.0)
            else:
                await asyncio.sleep(1.0)
        profile.state = "finalizing"
        # Over at the first answer after a restart: it ended while no server
        # watched -- a device that could not be asked at resume, or a dead pid.
        profile.stop_reason = profile.stop_reason or ("server_restart" if resumed
                                                      else "time_limit")

    async def _alive_state(self, profile: Profile) -> bool | None:
        """True running, False over, None could not ask (Android only)."""
        if profile.android:
            return await self._device(profile).alive(profile.id)
        return await self._alive(profile)

    async def _alive(self, profile: Profile) -> bool:
        if profile.android:
            # Could not ask is not over: keep waiting, inside the overrun budget.
            return await self._device(profile).alive(profile.id) is not False
        if profile.proc is not None:
            return profile.proc.returncode is None
        if not profile.pid:
            return False
        command = await asyncio.to_thread(_command_of, profile.pid)
        # Could not ask is not gone: keep waiting, inside the overrun budget.
        return command is None or _records_into(command, profile.trace)

    async def _finalize(self, profile: Profile) -> None:
        if profile.android:
            await self._finalize_android(profile)
            await self._make_room(keep=profile.id)
            await self._close(profile)
            return
        text = profile.log_text()
        if xt.SAVED_MARKER not in text:
            # Killed, crashed, or stopped before saving: a bundle may exist,
            # but nothing says it is whole, so it is not offered as a result.
            profile.state = "failed"
            profile.error = ("xctrace did not finish saving the trace"
                             + (" (quern stopped it after it overran)"
                                if profile.stop_reason == "killed" else "")
                             + f": {_tail(profile.log) or 'no output'}")
        else:
            summary = await self.summarize(profile)
            _write_json_atomic(profile.summary_file, summary)
            if "trace" in summary["unavailable"]:
                profile.state = "failed"
                profile.error = summary["unavailable"]["trace"]
            else:
                profile.state = "finished"
        # Trimmed before it is announced done, so whoever was waiting on it
        # sees the store within its budget.
        await self._make_room(keep=profile.id)
        await self._close(profile)

    async def _finalize_android(self, profile: Profile) -> None:
        """Read the app again, copy the trace off the device, and summarise.
        A trace that could not be copied is a failed profile: the device's
        readings alone are not what was asked for."""
        device = self._device(profile)
        profile.ended_at = profile.ended_at or _now()
        last = await device.snapshot(profile.bundle_id)
        # Already here means a finish cut off after the pull (a restart): the
        # device's copy is gone, and pulling again would fail a whole trace.
        if not (profile.trace.is_file() and profile.trace.stat().st_size > 0):
            try:
                await device.pull(profile.id, profile.trace)
            except (ad.PullFailed, OSError, TimeoutError) as e:
                profile.state, profile.error = "failed", str(e)
                return
        try:
            first = json.loads(profile.start_file.read_text())
        except (OSError, ValueError):
            first = {"unavailable": {"cpu": "the reading at the start was lost",
                                     "frames": "the reading at the start was lost"}}
        # Without when perfetto began -- the record unreadable, or its second
        # write lost -- a stop's time runs from the request, setup and all,
        # and could read as a trace ended early: only a profile that ran its
        # limit is still held to it (CodeRabbit).
        recording_at = first.get("recording_at")
        expected_s = (_recorded_s(profile, recording_at)
                      if recording_at or profile.stop_reason == "time_limit" else None)
        status = await self.trace_processor.wait(TP_WAIT_S)
        logged: tuple[list[dict] | None, str | None] = (
            None, "InputDispatcher's reports are read from logcat, which this server cannot")
        if self.logged_hangs is not None:
            try:
                logged = await self.logged_hangs(profile.device, profile.bundle_id,
                                                 profile.started_at, profile.ended_at,
                                                 first.get("clock_offset_s") or 0.0)
            except Exception as e:  # noqa: BLE001 -- said in the summary
                logged = (None, f"the logged hangs could not be read: {e}")
        summary = await ad.summarize(
            preset=profile.preset, package=profile.bundle_id, trace=profile.trace,
            first=first, last=last, tp=self.trace_processor, tp_status=status.as_dict(),
            logged_hangs=logged, expected_s=expected_s)
        await asyncio.to_thread(_write_json_atomic, profile.summary_file, summary)
        profile.state = "finished"

    async def _close(self, profile: Profile) -> None:
        profile.ended_at = profile.ended_at or _now()
        profile.proc = None
        self._devices.pop(profile.id, None)
        self._active.pop(profile.udid, None)
        profile.done.set()
        await self._save()

    # -- summarising -----------------------------------------------------------

    async def _export(self, trace: Path, schema: str | None) -> str:
        return await self._run_xctrace(xt.export_command(self.xctrace, trace, schema))

    async def _run_xctrace(self, cmd: list[str]) -> str:
        """One xctrace reading command: its output, or ProfileError with its words."""
        verb = f"xctrace {cmd[len(self.xctrace)]}"
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except OSError as e:
            raise ProfileError(f"{verb} could not be run: {e}", 500) from e
        try:
            out, err = await asyncio.wait_for(proc.communicate(), EXPORT_TIMEOUT_S)
        except BaseException:
            await asyncio.shield(_end(proc))
            raise
        if proc.returncode != 0:
            raise ProfileError(f"{verb} failed: {err.decode(errors='replace').strip()}"
                               f" (exit {proc.returncode})", 500)
        return out.decode(errors="replace")

    async def summarize(self, profile: Profile) -> dict:
        """What the trace says, section by section. A section that could not
        be read is null with its reason in `unavailable`, never empty: no
        hangs found and hangs not read are different answers."""
        preset = xt.PRESETS.get(profile.preset)
        summary: dict = {"preset": profile.preset, "hangs": None, "hitches": None,
                         "time_profile": None, "unavailable": {}}
        if profile.preset == "launch":
            summary["launch"] = None
        try:
            toc = xt.parse_toc(await self._export(profile.trace, None))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 -- reported in the summary
            summary["unavailable"]["trace"] = f"the trace's contents could not be read: {e}"
            return summary
        summary.update({k: toc[k] for k in ("template", "end_reason", "duration_s",
                                             "device", "process")})
        process = (toc.get("process") or {}).get("name")
        readers = {
            "potential-hangs": ("hangs", xt.parse_hangs),
            "hitches": ("hitches", xt.parse_hitches),
            "time-profile": ("time_profile", lambda text: xt.parse_time_profile(text, process)),
        }
        if profile.preset == "launch":
            # Every Time Profiler trace carries this table, empty: only a
            # launch profile is about it.
            readers["life-cycle-period"] = ("launch", xt.parse_launch_phases)
        for schema, (section, parse) in readers.items():
            if schema not in toc["tables"]:
                summary["unavailable"][section] = (
                    f"not recorded by the {toc.get('template') or preset.template} template")
                continue
            try:
                value = parse(await self._export(profile.trace, schema))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 -- one table never sinks the others
                summary["unavailable"][section] = f"the {schema} table could not be read: {e}"
                continue
            if isinstance(value, list):
                summary[f"{section}_count"] = len(value)
                if section != "hangs":              # cut after triage has read them
                    value = value[:LIST_LIMIT]
            if section == "launch" and not value["phases"]:
                summary["unavailable"]["launch"] = (
                    "the trace recorded no launch phases" + (
                        " (simulators record none, measured)"
                        if profile.platform == "simulator" else ""))
                continue
            if section == "time_profile" and not value["samples"]:
                value["note"] = (f"no CPU samples from {process or 'the app'}: Time Profiler "
                                 "samples running threads only, so an idle or blocked app has none")
            summary[section] = value
        # triage needs both tables, and has something to say only about hangs.
        if summary.get("hangs") and {"potential-hangs", "time-profile"} <= set(toc["tables"]):
            await self._triage(profile, summary, process)
        if summary.get("hangs") and len(summary["hangs"]) > LIST_LIMIT:
            # triage reads the longest, which can be anywhere in the trace:
            # keep those, then the earliest of the rest, in the trace's order.
            hangs = summary["hangs"]
            kept = {i for i, h in enumerate(hangs) if "tier" in h}
            for i in range(len(hangs)):
                if len(kept) >= LIST_LIMIT:
                    break
                kept.add(i)
            summary["hangs"] = [h for i, h in enumerate(hangs) if i in kept]
        return summary

    async def _triage(self, profile: Profile, summary: dict, process: str | None) -> None:
        """Apple's reading of each hang, onto the hangs already listed:
        `xctrace triage --style hangs` (Xcode 27.2) for its tier, the main
        thread's CPU and the system calls it made; then, for the longest
        few, the main thread's heaviest stack over the hang -- of its system
        calls when it was blocked, of its CPU samples when it was busy.

        Blocked or busy is the first thing to know about a hang (Apple's
        xctrace skill says to establish it before anything else): a busy
        hang is in the CPU profile, a blocked one is not -- the thread is
        not running, so Time Profiler has nothing -- and is in the system
        call it waited in. Measured on the probe: CPU 0.0%, and
        `DiagViewController.hang()` -> `sleepForTimeInterval:` ->
        `__semwait_signal`, 12.00 s."""
        try:
            triage = xt.parse_triage(await self._run_xctrace(
                xt.triage_command(self.xctrace, profile.trace, TRIAGE_LIMIT)))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 -- the hangs above stand without it
            summary["unavailable"]["triage"] = (
                f"xctrace triage could not read the hangs: {_why(e)}. triage is in Xcode 27.2 "
                "and later; the hangs above are Instruments' own list")
            return
        summary["hang_tiers"] = triage["tiers"]
        stacks = matched = 0
        for read in triage["hangs"]:
            hang = next((h for h in summary["hangs"]
                         if h.get("start_s") is not None and read["start_s"] is not None
                         and abs(h["start_s"] - read["start_s"]) < TRIAGE_MATCH_S), None)
            if hang is None:
                continue
            matched += 1
            hang["tier"] = read["tier"]
            hang["main_thread_cpu"] = read["main_thread_cpu"]
            hang["nature"] = xt.nature(read["main_thread_cpu"])
            # Whether there are system calls to read is triage's to say, per
            # hang, not the TOC's: an iPhone hitches trace has a syscall table
            # and triage reads "N/A" for every one of its hangs (measured).
            syscalls_recorded = read["system_calls"] is not None
            if syscalls_recorded:
                hang["system_calls"] = read["system_calls"][:5]
            else:
                hang["system_calls"] = None
                hang["system_calls_note"] = (
                    "triage had no system calls to read for it: the profile did not record "
                    "System Call Trace (the hangs preset records it now)")
            if stacks >= TRIAGE_STACKS or not read["window"]:
                continue
            if hang["nature"] == "blocked" and not syscalls_recorded:
                # Its CPU samples are from the moments around the hang when
                # the thread did run: a stack from them reads as an answer and
                # is noise (measured -- the run loop, not the sleep).
                hang["stack_note"] = ("blocked, so its CPU samples cannot show where it waited; "
                                      "System Call Trace would (the hangs preset records it now)")
                continue
            stacks += 1
            blocked = hang["nature"] in ("blocked", "mixed") and syscalls_recorded
            calltree = "Profile: System Calls" if blocked else "Profile"
            window = read["window"]
            try:
                stack = xt.parse_heaviest(await self._run_xctrace(xt.calltree_command(
                    self.xctrace, profile.trace, window["track"], calltree,
                    window["start_ns"], window["duration_ns"])), process)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 -- one hang's stack never sinks the rest
                hang["stack_note"] = f"its {calltree} call tree could not be read: {_why(e)}"
                continue
            if stack is None:
                hang["stack_note"] = (
                    "no CPU samples on the main thread over the hang: it was not running"
                    if calltree == "Profile" else "no system calls on the main thread over it")
            else:
                hang["stack"] = stack
                hang["stack_from"] = "system calls" if blocked else "CPU samples"
        summary["hangs_triaged"] = matched
        if not matched:
            # Output it could not parse reads as no hangs at all: say so,
            # rather than leave it looking like triage had nothing to add.
            summary["unavailable"]["triage"] = (
                f"xctrace triage read {len(triage['hangs'])} hangs, and none of them is one "
                "listed above")

    # -- disk ------------------------------------------------------------------

    def _orphans(self) -> list[Path]:
        """Profile directories no record knows: a server killed before it
        could save one, or a profiles.json that was lost. Counted against the
        budget, and the first to go. A start still warming up has a directory
        and no record yet, and is not one."""
        root = _root()
        try:
            entries = list(root.iterdir())
        except OSError:
            return []
        starting = set(self._active.values())
        return [p for p in entries if ID.fullmatch(p.name) and p.is_dir()
                and not p.is_symlink() and p.name not in self._profiles
                and p.name not in starting]

    async def usage(self) -> int:
        dirs = [p.dir for p in self._profiles.values()] + self._orphans()
        return await asyncio.to_thread(lambda: sum(_size(d) for d in dirs))

    async def _make_room(self, keep: str | None = None) -> dict:
        """Remove the oldest finished profiles until the kept ones fit the
        budget. Nothing is removed unless that is enough: a start refused
        because profiles still recording fill the budget removes nothing.
        Sizes and removals run off the event loop: a trace bundle can be
        hundreds of files."""
        limit = self.settings()["max_bytes"]
        orphans = self._orphans()
        removable = [p for p in self.list()
                     if p.state not in ACTIVE and p.id != keep and p.readers == 0]
        dirs = {p.id: p.dir for p in self._profiles.values()} | {o.name: o for o in orphans}
        sizes = await asyncio.to_thread(lambda: {i: _size(d) for i, d in dirs.items()})
        used = sum(sizes.values())
        if keep is None and used >= limit:
            freeable = sum(sizes[o.name] for o in orphans) + sum(sizes[p.id] for p in removable)
            if used - freeable >= limit:
                raise ProfileError(
                    f"the kept profiles take {used // 2**20} MB of the {limit // 2**20} MB "
                    "allowed (config.json profiles.max_mb), and those still recording or being "
                    "downloaded cannot be removed", 507)
        evicted: list[dict] = []
        candidates = [(o.name, o, None) for o in sorted(orphans, key=_mtime)] + \
                     [(p.id, p.dir, p.principal) for p in removable]
        for name, directory, owner in candidates:
            if used < limit:
                break
            # Asked again here, not from the list above: a download can begin
            # while the sizes are read. With no await between this check and
            # the pop, zip_trace either sees the record gone or is seen here.
            current = self._profiles.get(name)
            if current is not None and current.readers:
                continue
            self._profiles.pop(name, None)
            await asyncio.to_thread(shutil.rmtree, directory, True)
            used -= sizes[name]
            evicted.append({"id": name, "principal": owner})
        if evicted:
            await self._save()
        return {"evicted": evicted, "used_bytes": used, "budget_bytes": limit}

    # -- files -----------------------------------------------------------------

    async def zip_trace(self, profile: Profile) -> Path:
        """A temporary zip of the trace bundle; the caller removes it."""
        if self._profiles.get(profile.id) is not profile:
            raise ProfileError(f"{profile.id} was removed to make room", 404)
        if profile.state != "finished":
            raise ProfileError(
                f"{profile.id} is {profile.state}: its trace is offered once finished", 409)
        if profile.android or not profile.trace.is_dir() or profile.trace.is_symlink():
            raise ProfileError(f"{profile.id} has no trace bundle to zip", 404)
        tmp = _root() / f".{profile.id}.{uuid.uuid4().hex[:8]}.zip"
        profile.readers += 1
        try:
            await asyncio.to_thread(_zip_dir, profile.trace, tmp)
        except FileNotFoundError as e:
            raise ProfileError(f"{profile.id}'s trace was removed while zipping it", 409) from e
        except OSError as e:
            full = e.errno in (errno.ENOSPC, errno.EDQUOT)
            raise ProfileError(f"could not zip {profile.id}'s trace: {e}",
                               507 if full else 500) from e
        finally:
            profile.readers -= 1
        return tmp

    # -- persistence -----------------------------------------------------------

    async def _save(self) -> bool:
        async with self._save_lock:
            try:
                _root().mkdir(parents=True, exist_ok=True)
                _write_json_atomic(_root() / STATE_FILE,
                                   {"profiles": [p.record() for p in self.list()]})
                return True
            except OSError as e:
                logger.warning("Could not save the profiles: %s", e)
                return False

    async def resume(self) -> list[str]:
        """Pick up the profiles a previous server left: re-adopt an xctrace
        still recording, and summarise any that finished unwatched."""
        root = _root()
        for stale in root.glob(".prof_*.zip") if root.is_dir() else []:
            with contextlib.suppress(OSError):
                stale.unlink()           # a download's temporary zip, cut off by the restart
        state = root / STATE_FILE
        try:
            raw = json.loads(state.read_text())
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            # Kept aside rather than overwritten by the next save; the
            # directories it named are still counted and evicted as orphans.
            aside = state.with_name(f"{STATE_FILE}.unreadable-{int(time.time())}")
            with contextlib.suppress(OSError):
                state.rename(aside)
            logger.warning("Could not read %s (%s); kept it as %s", state, e, aside.name)
            return []
        adopted = []
        for entry in raw.get("profiles", []) if isinstance(raw, dict) else []:
            if not isinstance(entry, dict) or not ID.fullmatch(str(entry.get("id", ""))):
                continue
            try:
                profile = Profile(**{k: entry[k] for k in Profile.__dataclass_fields__
                                     if k in entry and k not in ("proc", "done", "task",
                                                                 "readers")})
            except TypeError:
                continue
            if not profile.dir.is_dir() or profile.dir.is_symlink():
                continue
            self._profiles[profile.id] = profile
            if profile.state in ACTIVE:
                self._active[profile.udid] = profile.id
                if profile.android:
                    if not ad.PACKAGE_OK.fullmatch(profile.bundle_id):
                        # A hand-edited record must not reach a device shell
                        # or the summary's SQL (review).
                        self._active.pop(profile.udid, None)
                        profile.state, profile.error = "failed", "its record is not valid"
                        continue
                    self.trace_processor.ensure()
                    if profile.state == "recording" and profile.stop_reason is None:
                        # Asked by the watch, which can wait for a device the
                        # server cannot reach yet (its controller comes later).
                        profile.stop_reason = "resumed"
                elif profile.state == "recording" and not await self._alive(profile):
                    profile.stop_reason = profile.stop_reason or "server_restart"
                profile.task = asyncio.create_task(self._watch(profile))
                adopted.append(profile.id)
        await self._save()
        return adopted

    async def shutdown(self) -> None:
        """Stop watching; xctrace itself keeps going (see the module docstring)."""
        tasks = [p.task for p in self._profiles.values() if p.task and not p.task.done()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(BaseException):
                await task
        await self._save()


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


async def _end(proc: asyncio.subprocess.Process) -> None:
    """Ask a process to stop, then make it."""
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        proc.send_signal(signal.SIGINT)
    try:
        await asyncio.wait_for(asyncio.shield(proc.wait()), 5.0)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await asyncio.shield(proc.wait())


def _raise(e: OSError) -> None:
    raise e


def _zip_dir(source: Path, dest: Path) -> None:
    try:
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
            # A directory that vanishes mid-walk is an error, not a smaller
            # zip: os.walk skips what it cannot list unless told otherwise.
            for root, _dirs, files in os.walk(source, onerror=_raise):
                for name in files:
                    path = Path(root) / name
                    if not path.is_symlink():
                        z.write(path, Path(source.name) / path.relative_to(source))
    except BaseException:
        with contextlib.suppress(OSError):
            dest.unlink()
        raise


def _write_json_atomic(path: Path, body: dict) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(json.dumps(body, indent=2, default=str))
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()
