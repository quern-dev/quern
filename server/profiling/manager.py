"""Profiles: one xctrace recording per device, summarised and kept on disk (#245).

A profile's life: started (xctrace launches the app and records), then either
its time limit ends it or `stop` sends SIGINT, then xctrace finalises the
trace, then quern exports the tables it summarises. The trace stays for
download until the disk budget needs its room.

**xctrace outlives the server on purpose.** It finalises for 13 s or more,
and `quern stop` kills the server about 5 s after asking, so a shutdown that
waited for it would lose the trace instead. xctrace runs in its own session
with its own time limit; the server only stops watching it, and the next
server re-adopts it by pid -- confirmed by its command line, never the pid
alone -- or, if it has finished meanwhile, summarises what it left.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
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
from server.profiling import xctrace as xt

logger = logging.getLogger(__name__)

TIME_LIMIT_DEFAULT_S = 30
TIME_LIMIT_MAX_S = 300
#: Until xctrace says it is recording. Launching on a device measured about
#: 4 s, and a device xctrace thinks is offline takes 30 s to say so.
START_TIMEOUT_S = 90.0
#: From the end of recording to a finished trace. Measured 13 s for 30 s of
#: Time Profiler on an iPhone 11; Animation Hitches traces are ten times larger.
FINALIZE_TIMEOUT_S = 180.0
#: One table's export. Measured 26 s for a table of a 192 MB trace.
EXPORT_TIMEOUT_S = 120.0
#: Hangs and hitches listed in a summary; the count says how many there were.
LIST_LIMIT = 50

ACTIVE = ("recording", "finalizing")
STATE_FILE = "profiles.json"


class ProfileError(Exception):
    """A request about a profile that cannot be carried out; `status` is the
    HTTP status it should answer with."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _root() -> Path:
    # Read at call time, so QUERN_STATE_DIR set by a test applies.
    return config_mod.CONFIG_DIR / "profiles"


@dataclass
class Profile:
    id: str
    udid: str
    #: The identifier xctrace's --device takes: the simulator's UDID, or a
    #: physical device's hardware UDID.
    device: str
    platform: str                     # "simulator" or "device"
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

    @property
    def dir(self) -> Path:
        return _root() / self.id

    @property
    def trace(self) -> Path:
        return self.dir / "profile.trace"

    @property
    def log(self) -> Path:
        return self.dir / "xctrace.log"

    @property
    def summary_file(self) -> Path:
        return self.dir / "summary.json"

    def record(self) -> dict:
        return {k: getattr(self, k) for k in (
            "id", "udid", "device", "platform", "bundle_id", "preset", "time_limit_s",
            "started_at", "principal", "state", "pid", "ended_at", "stop_reason", "error")}

    def summary(self) -> dict | None:
        try:
            return json.loads(self.summary_file.read_text())
        except (OSError, ValueError):
            return None

    def view(self) -> dict:
        preset = xt.PRESETS.get(self.preset)
        return {
            **{k: v for k, v in self.record().items() if k not in ("principal", "pid", "device")},
            "template": preset.template if preset else None,
            "bytes": _size(self.dir),
            "summary": self.summary() if self.state not in ACTIVE else None,
            # The trace is served only once xctrace has finished writing it.
            "files": [name for name, path in (("summary.json", self.summary_file),
                                              ("xctrace.log", self.log),
                                              ("profile.trace.zip", self.trace))
                      if path.exists() and not (name.endswith(".zip")
                                                and self.state in ACTIVE)],
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
    """`ps`'s command line for `pid`, or None if it has none (gone, or ps failed)."""
    try:
        r = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True,
                           text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() or None if r.returncode == 0 else None


def _records_into(command: str | None, trace: Path) -> bool:
    """Whether `command` is an xctrace recording into exactly `trace`, so a
    reused pid never passes for the profile's process."""
    return bool(command) and "xctrace" in command and f" --output {trace} " in f" {command} "


class ProfileManager:
    def __init__(self, xctrace: tuple[str, ...] = ("xcrun", "xctrace"),
                 warm_up: Callable[[str], Awaitable[None]] | None = None,
                 settings: Callable[[], dict] = config_mod.profile_settings) -> None:
        self.xctrace = xctrace
        #: Called before recording a physical device: CoreDevice's tunnel is
        #: up only while something holds it, and xctrace waits for it instead
        #: of asking ("Waiting for device to boot", then a timeout).
        self.warm_up = warm_up
        self.settings = settings
        self._profiles: dict[str, Profile] = {}
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
        chosen = xt.PRESETS.get(preset)
        if chosen is None:
            raise ProfileError(f"unknown preset {preset!r}; one of {', '.join(xt.PRESETS)}")
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
            warm_up = warm_up or self.warm_up
            if platform == "device" and warm_up is not None:
                try:
                    await warm_up(udid)
                except Exception as e:  # noqa: BLE001 -- xctrace's own answer says more
                    logger.info("Warming up %s before profiling failed: %s", udid[:8], e)
            await self._spawn(profile, chosen)
        except BaseException:
            self._active.pop(udid, None)
            if profile.dir.exists():
                shutil.rmtree(profile.dir, ignore_errors=True)
            raise
        self._profiles[profile.id] = profile
        await self._save()
        profile.task = asyncio.create_task(self._watch(profile))
        return profile, room

    async def _spawn(self, profile: Profile, preset: xt.Preset) -> None:
        cmd = xt.record_command(self.xctrace, profile.device, preset, profile.time_limit_s,
                                profile.trace, profile.bundle_id)
        # Output to a file, never a pipe: a pipe would tie xctrace's life to
        # this process's, and it must outlive a server restart.
        with open(profile.log, "wb") as log:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=log,
                    stderr=asyncio.subprocess.STDOUT, start_new_session=True)
            except OSError as e:
                raise ProfileError(f"xctrace could not be run: {e}", 500) from e
        profile.proc, profile.pid = proc, proc.pid
        deadline = time.monotonic() + START_TIMEOUT_S
        while True:
            text = profile.log.read_text(errors="replace") if profile.log.exists() else ""
            if xt.STARTED_MARKER in text:
                return
            failed = next((m for m in xt.FAILURE_MARKERS if m in text), None)
            if failed or proc.returncode is not None or time.monotonic() > deadline:
                if proc.returncode is None:
                    await _end(proc)
                why = _tail(profile.log) or (
                    f"no answer within {START_TIMEOUT_S:g}s" if not failed else failed)
                hint = ""
                if profile.platform == "device" and "Waiting for device to boot" in text:
                    hint = (" (xctrace saw the device as offline: its CoreDevice tunnel was "
                            "down. Check the device is connected, unlocked and trusted.)")
                raise ProfileError(f"xctrace did not start recording: {why}{hint}", 502)
            await asyncio.sleep(0.25)

    # -- stopping --------------------------------------------------------------

    async def stop(self, profile_id: str, reason: str = "stopped") -> Profile:
        """Stop recording early and wait for the summary."""
        profile = self.get(profile_id)
        if profile.state == "recording":
            profile.stop_reason = reason
            await self._signal(profile, signal.SIGINT)
        if profile.state in ACTIVE:
            try:
                await asyncio.wait_for(profile.done.wait(),
                                       FINALIZE_TIMEOUT_S + 3 * EXPORT_TIMEOUT_S)
            except TimeoutError:
                raise ProfileError(
                    f"{profile_id} has not finished; it is {profile.state}. Ask again later.",
                    504) from None
        return profile

    async def _signal(self, profile: Profile, sig: int) -> None:
        if profile.proc is not None and profile.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                profile.proc.send_signal(sig)
        elif profile.pid and _records_into(
                await asyncio.to_thread(_command_of, profile.pid), profile.trace):
            with contextlib.suppress(ProcessLookupError):
                os.kill(profile.pid, sig)

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
        # Past its time limit plus finalising, it is stuck: ask, then insist.
        budget = profile.time_limit_s + FINALIZE_TIMEOUT_S
        started = time.monotonic()
        while await self._alive(profile):
            if time.monotonic() - started > budget:
                logger.warning("xctrace for %s overran %gs; stopping it", profile.id, budget)
                profile.stop_reason = profile.stop_reason or "killed"
                await self._signal(profile, signal.SIGINT)
                await asyncio.sleep(10)
                await self._signal(profile, signal.SIGKILL)
                profile.stop_reason = "killed"
                break
            if profile.proc is not None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(profile.proc.wait(), 1.0)
            else:
                await asyncio.sleep(1.0)
        profile.state = "finalizing"
        profile.stop_reason = profile.stop_reason or "time_limit"

    async def _alive(self, profile: Profile) -> bool:
        if profile.proc is not None:
            return profile.proc.returncode is None
        return bool(profile.pid) and _records_into(
            await asyncio.to_thread(_command_of, profile.pid), profile.trace)

    async def _finalize(self, profile: Profile) -> None:
        if not profile.trace.exists():
            profile.state = "failed"
            profile.error = f"xctrace left no trace: {_tail(profile.log) or 'no output'}"
        else:
            summary = await self.summarize(profile)
            _write_json_atomic(profile.summary_file, summary)
            profile.state = "finished"
        await self._close(profile)
        await self._make_room(keep=profile.id)

    async def _close(self, profile: Profile) -> None:
        profile.ended_at = profile.ended_at or _now()
        profile.proc = None
        self._active.pop(profile.udid, None)
        profile.done.set()
        await self._save()

    # -- summarising -----------------------------------------------------------

    async def _export(self, trace: Path, schema: str | None) -> str:
        cmd = xt.export_command(self.xctrace, trace, schema)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except OSError as e:
            raise ProfileError(f"xctrace export could not be run: {e}", 500) from e
        try:
            out, err = await asyncio.wait_for(proc.communicate(), EXPORT_TIMEOUT_S)
        except BaseException:
            await _end(proc)
            raise
        if proc.returncode != 0:
            raise ProfileError(f"xctrace export failed: {err.decode(errors='replace').strip()}"
                               f" (exit {proc.returncode})", 500)
        return out.decode(errors="replace")

    async def summarize(self, profile: Profile) -> dict:
        """What the trace says, section by section. A section that could not
        be read is null with its reason in `unavailable`, never empty: no
        hangs found and hangs not read are different answers."""
        preset = xt.PRESETS.get(profile.preset)
        summary: dict = {"preset": profile.preset, "hangs": None, "hitches": None,
                         "time_profile": None, "launch": None, "unavailable": {}}
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
                value = value[:LIST_LIMIT]
            if section == "launch" and not value["phases"]:
                summary["unavailable"]["launch"] = (
                    "the trace recorded no launch phases" + (
                        " (simulators record none, measured)"
                        if profile.platform == "simulator" else ""))
                continue
            summary[section] = value
        return summary

    # -- disk ------------------------------------------------------------------

    async def usage(self) -> int:
        dirs = [p.dir for p in self._profiles.values()]
        return await asyncio.to_thread(lambda: sum(_size(d) for d in dirs))

    async def _make_room(self, keep: str | None = None) -> dict:
        """Remove the oldest finished profiles until the kept ones fit the
        budget. Profiles still recording are never removed; when they alone
        are over it, a new one is refused. Sizes and removals run off the
        event loop: a trace bundle can be hundreds of files."""
        limit = self.settings()["max_bytes"]
        profiles = self.list()
        dirs = {p.id: p.dir for p in profiles}
        sizes = await asyncio.to_thread(lambda: {i: _size(d) for i, d in dirs.items()})
        used = sum(sizes.values())
        evicted = []
        for profile in profiles:
            if used < limit:
                break
            if profile.state in ACTIVE or profile.id == keep:
                continue
            self._profiles.pop(profile.id, None)
            await asyncio.to_thread(shutil.rmtree, profile.dir, True)
            used -= sizes[profile.id]
            evicted.append(profile.id)
        if keep is None and used >= limit:
            raise ProfileError(
                f"the kept profiles take {used // 2**20} MB of the {limit // 2**20} MB allowed "
                "(config.json profiles.max_mb), and none can be removed while recording", 507)
        return {"evicted": evicted, "used_bytes": used, "budget_bytes": limit}

    # -- files -----------------------------------------------------------------

    async def zip_trace(self, profile: Profile) -> Path:
        """A temporary zip of the trace bundle; the caller removes it."""
        if profile.state in ACTIVE:
            raise ProfileError(f"{profile.id} is still {profile.state}", 409)
        if not profile.trace.is_dir():
            raise ProfileError(f"{profile.id} has no trace", 404)
        tmp = _root() / f".{profile.id}.{uuid.uuid4().hex[:8]}.zip"
        await asyncio.to_thread(_zip_dir, profile.trace, tmp)
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
        try:
            raw = json.loads((_root() / STATE_FILE).read_text())
        except (OSError, ValueError):
            return []
        adopted = []
        for entry in raw.get("profiles", []) if isinstance(raw, dict) else []:
            try:
                profile = Profile(**{k: entry[k] for k in Profile.__dataclass_fields__
                                     if k in entry and k not in ("proc", "done", "task")})
            except TypeError:
                continue
            if not profile.dir.is_dir() or profile.dir.is_symlink():
                continue
            self._profiles[profile.id] = profile
            if profile.state in ACTIVE:
                self._active[profile.udid] = profile.id
                if profile.state == "recording" and not await self._alive(profile):
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


def _zip_dir(source: Path, dest: Path) -> None:
    try:
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
            for root, _dirs, files in os.walk(source):
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
