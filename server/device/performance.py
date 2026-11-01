"""One app's CPU, memory and threads at one moment, on any device (#245).

Each platform measures with what it already has, with nothing to install:

| platform      | process            | cpu                       | memory              |
|---------------|--------------------|---------------------------|---------------------|
| iOS simulator | simulator launchd  | host `ps` CPU time delta  | `footprint` (phys)  |
| iOS device    | DVT process control| DVT sysmontap             | sysmontap (phys)    |
| Android       | `pidof`            | `/proc/<pid>/stat` delta  | `dumpsys meminfo`   |

A simulator app is a Mac process, so the host can read it directly. A device
app is read through the Instruments channel pymobiledevice3 speaks, over the
tunnel tunneld already keeps for WDA.

**A metric that could not be read is null, never zero, and says why** in
`unavailable`. "This app uses no CPU" and "quern could not ask" must not look
alike, because only one of them is a finding.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

from server.models import DeviceError, FrameStats, PerformanceSnapshot

if TYPE_CHECKING:
    from server.device.android.adb import AdbBackend
    from server.device.ios.simctl import SimctlBackend

logger = logging.getLogger(__name__)

SAMPLE_SECONDS_DEFAULT = 1.0
SAMPLE_SECONDS_MIN = 0.2
SAMPLE_SECONDS_MAX = 5.0

#: Clock ticks per second in /proc/<pid>/stat. This is Linux's USER_HZ, which
#: is part of the kernel ABI and 100 on every architecture Android ships on,
#: independent of the kernel's internal HZ.
USER_HZ = 100

#: Extra time allowed on top of the sample for a device to answer.
_ANSWER_SLACK_S = 15.0

NOT_RUNNING = "the app is not running"

#: The sampling clock, apart from `time.monotonic` so tests can drive it
#: without stopping asyncio's own.
_clock = time.monotonic

#: What each platform measures. Anything else is null, and `_finish` says why.
MEASURED = {
    "ios_simulator": ("cpu_percent", "memory_bytes", "threads"),
    "ios_device": ("cpu_percent", "memory_bytes", "threads", "display_fps"),
    "android": ("cpu_percent", "memory_bytes", "threads", "frames"),
}
_NOT_MEASURED = {
    "frames": "Android only",
    "display_fps": "iOS devices only",
}


# -- parsers -----------------------------------------------------------------

def parse_ps_cputime(text: str) -> float | None:
    """Seconds of CPU time from `ps -o time=`, e.g. `0:59.01` or `1:02:03.45`.

    macOS prints `[[dd-]hh:]mm:ss.cc`. None for anything else.
    """
    s = text.strip()
    days = 0
    if "-" in s:
        d, s = s.split("-", 1)
        if not d.isdigit():
            return None
        days = int(d)
    parts = s.split(":")
    if not 2 <= len(parts) <= 3:
        return None
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    seconds = 0.0
    for n in nums:
        seconds = seconds * 60 + n
    return days * 86400 + seconds


def parse_ps_thread_count(text: str) -> int | None:
    """Threads from `ps -M -p <pid>`: a header, then one line per thread."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2 or not lines[0].lstrip().startswith("USER"):
        return None
    return len(lines) - 1


def parse_footprint_json(text: str, pid: int) -> int | None:
    """phys_footprint bytes for `pid` from `footprint -j`."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("unit") != "byte":
        return None
    for proc in data.get("processes") or []:
        if isinstance(proc, dict) and proc.get("pid") == pid:
            value = proc.get("footprint")
            return value if isinstance(value, int) and value > 0 else None
    return None


def parse_proc_stat_ticks(line: str) -> int | None:
    """utime + stime, in clock ticks, from one /proc/<pid>/stat line.

    Fields are counted after the last `)`, because the command name in
    parentheses can itself contain spaces and parentheses.
    """
    close = line.rfind(")")
    if close < 0:
        return None
    fields = line[close + 1:].split()
    # After ")": state is field 3, so utime (14) and stime (15) are at 11, 12.
    if len(fields) < 13:
        return None
    try:
        return int(fields[11]) + int(fields[12])
    except ValueError:
        return None


def parse_android_cpu_sample(text: str, pid: int) -> tuple[float, int | None] | None:
    """(cpu percent, threads) from the two-reading shell script below.

    The script prints `/proc/uptime`, `/proc/<pid>/stat` and the `Threads:`
    line, sleeps, then prints uptime and stat again. Elapsed time is read on
    the device, so adb's round trip does not count against the app.
    """
    uptimes: list[float] = []
    ticks: list[int] = []
    threads: int | None = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(f"{pid} ("):
            t = parse_proc_stat_ticks(line)
            if t is not None:
                ticks.append(t)
        elif line.startswith("Threads:"):
            with contextlib.suppress(ValueError):
                threads = int(line.split(":", 1)[1])
        else:
            first = line.split(" ", 1)[0]
            with contextlib.suppress(ValueError):
                if re.fullmatch(r"\d+\.\d+", first):
                    uptimes.append(float(first))
    if len(uptimes) != 2 or len(ticks) != 2:
        return None
    elapsed = uptimes[1] - uptimes[0]
    if elapsed <= 0:
        return None
    return (ticks[1] - ticks[0]) / USER_HZ / elapsed * 100, threads


def parse_meminfo_total_pss_kb(text: str) -> int | None:
    """Total PSS in KB from `dumpsys meminfo <pid>`'s App Summary.

    Android 10 ends the summary with `TOTAL:  90665  TOTAL SWAP PSS: ...`;
    Android 12+ spells it `TOTAL PSS:  ...  TOTAL RSS: ...`. The colon is what
    tells it from the table's own `TOTAL` row above, which has none.
    """
    m = re.search(r"^\s*TOTAL(?: PSS)?:\s+(\d+)", text, re.MULTILINE)
    return int(m.group(1)) if m else None


def parse_gfxinfo(text: str) -> FrameStats | None:
    """Frame stats from `dumpsys gfxinfo <package>`, or None if it has none."""
    m = re.search(r"^Total frames rendered:\s*(\d+)", text, re.MULTILINE)
    if not m:
        return None
    stats = FrameStats(total_frames=int(m.group(1)))
    janky = re.search(r"^Janky frames:\s*(\d+)\s*\(([\d.]+)%\)", text, re.MULTILINE)
    if janky:
        stats.janky_frames = int(janky.group(1))
        stats.janky_percent = float(janky.group(2))
        # Android 12+ adds a legacy line beside it, which makes the plain one
        # the deadline-based count. Without it, the plain one is the legacy count.
        legacy = re.search(r"^Janky frames \(legacy\):", text, re.MULTILINE)
        stats.janky_method = "deadline" if legacy else "legacy"
    for pct in (50, 90, 95, 99):
        p = re.search(rf"^{pct}th percentile:\s*(\d+)ms", text, re.MULTILINE)
        if p:
            setattr(stats, f"p{pct}_ms", int(p.group(1)))
    return stats


# -- iOS simulator -----------------------------------------------------------

async def _run(*args: str, timeout: float) -> tuple[int, str]:
    """Run a host command; (returncode, stdout). Raises DeviceError if it
    cannot be run at all or does not finish."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except OSError as e:
        raise DeviceError(f"{args[0]} could not be run: {e}", tool=args[0]) from e
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), 1.0)
        raise DeviceError(f"{args[0]} did not answer within {timeout:g}s", tool=args[0]) from None
    return proc.returncode or 0, out.decode(errors="replace")


async def _ps_cputime(pid: int) -> float | None:
    rc, out = await _run("ps", "-o", "time=", "-p", str(pid), timeout=5)
    return parse_ps_cputime(out) if rc == 0 else None


async def _footprint(pid: int) -> int | None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "footprint.json"
        rc, _ = await _run("footprint", "-p", str(pid), "-j", str(path), timeout=10)
        if rc != 0 or not path.exists():
            return None
        return parse_footprint_json(path.read_text(errors="replace"), pid)


async def simulator_snapshot(
    simctl: SimctlBackend, udid: str, bundle_id: str, seconds: float,
) -> PerformanceSnapshot:
    snap = PerformanceSnapshot(
        udid=udid, bundle_id=bundle_id, platform="ios_simulator", running=False,
        sample_seconds=seconds)
    pid = await simctl.running_pid(udid, bundle_id)
    if pid is None:
        return _not_running(snap)
    snap.running, snap.pid = True, pid

    async def cpu() -> float | None:
        start, t0 = await _ps_cputime(pid), _clock()
        await asyncio.sleep(seconds)
        end, t1 = await _ps_cputime(pid), _clock()
        if start is None or end is None or t1 <= t0:
            return None
        return (end - start) / (t1 - t0) * 100

    async def threads() -> int | None:
        rc, out = await _run("ps", "-M", "-p", str(pid), timeout=5)
        return parse_ps_thread_count(out) if rc == 0 else None

    results = await asyncio.gather(cpu(), _footprint(pid), threads(), return_exceptions=True)
    for name, value in zip(("cpu_percent", "memory_bytes", "threads"), results, strict=True):
        _store(snap, name, value, f"could not be read for pid {pid}; has the app exited?")
    if snap.memory_bytes is not None:
        snap.memory_kind = "phys_footprint"
    return _finish(snap)


# -- Android -----------------------------------------------------------------

async def android_snapshot(
    adb: AdbBackend, serial: str, package: str, seconds: float,
) -> PerformanceSnapshot:
    snap = PerformanceSnapshot(
        udid=serial, bundle_id=package, platform="android", running=False,
        sample_seconds=seconds)
    rc, out, err = await adb._run_unjudged(serial, "shell", "pidof", package, timeout=10)
    pids = [int(p) for p in out.split() if p.isdigit()]
    if not pids:
        # pidof exits 1 with nothing printed when nothing matches. Anything
        # else means it could not answer, which is not "not running".
        if rc in (0, 1) and not err.strip():
            return _not_running(snap)
        raise DeviceError(
            f"pidof {package} on {serial} failed: {(err or out).strip() or f'exit {rc}'}",
            tool="adb")
    pid = pids[0]
    snap.running, snap.pid = True, pid

    script = (f"cat /proc/uptime /proc/{pid}/stat; grep '^Threads:' /proc/{pid}/status; "
              f"sleep {seconds:g}; cat /proc/uptime /proc/{pid}/stat")

    async def cpu_and_threads() -> tuple[float, int | None] | None:
        _, sample, _ = await adb._run_unjudged(
            serial, "shell", script, timeout=seconds + _ANSWER_SLACK_S)
        return parse_android_cpu_sample(sample, pid)

    async def memory() -> int | None:
        stdout, _ = await adb._run_adb_for_device(
            serial, "shell", "dumpsys", "meminfo", str(pid), timeout=_ANSWER_SLACK_S)
        kb = parse_meminfo_total_pss_kb(stdout)
        return kb * 1024 if kb is not None else None

    async def frames() -> FrameStats | None:
        stdout, _ = await adb._run_adb_for_device(
            serial, "shell", "dumpsys", "gfxinfo", package, timeout=_ANSWER_SLACK_S)
        return parse_gfxinfo(stdout)

    sampled, mem, fr = await asyncio.gather(
        cpu_and_threads(), memory(), frames(), return_exceptions=True)
    if isinstance(sampled, tuple):
        snap.cpu_percent, snap.threads = sampled
        if snap.threads is None:
            snap.unavailable["threads"] = f"/proc/{pid}/status had no Threads: line"
    else:
        reason = _reason(sampled, f"/proc/{pid} could not be read twice; has the app exited?")
        snap.unavailable["cpu_percent"] = snap.unavailable["threads"] = reason
    _store(snap, "memory_bytes", mem, f"dumpsys meminfo {pid} had no total PSS")
    if snap.memory_bytes is not None:
        snap.memory_kind = "pss"
    _store(snap, "frames", fr, "gfxinfo reported no frames; the app may have no window")
    if len(pids) > 1:
        logger.debug("pidof %s returned %s; measured %s", package, pids, pid)
    return _finish(snap)


# -- iOS device --------------------------------------------------------------

async def device_snapshot(uuid: str, bundle_id: str, seconds: float) -> PerformanceSnapshot:
    """Read a physical iPhone through pymobiledevice3's Instruments channel."""
    try:
        from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
        from pymobiledevice3.services.dvt.instruments.graphics import Graphics
        from pymobiledevice3.services.dvt.instruments.process_control import ProcessControl
        from pymobiledevice3.services.dvt.instruments.sysmontap import Sysmontap
        from pymobiledevice3.tunneld.api import get_tunneld_device_by_udid
    except ImportError as e:
        raise DeviceError(
            f"get_performance on a physical device needs pymobiledevice3 11 or "
            f"later in quern's environment ({e})", tool="pymobiledevice3") from e

    from server.device.ios.pmd3 import _no_tunnel_hint, _recover_tunneld_if_wedged
    from server.device.ios.tunneld import is_tunneld_running, resolve_tunnel_udid

    serving = await is_tunneld_running() or await _recover_tunneld_if_wedged()
    tunnel_udid = await resolve_tunnel_udid(uuid) if serving else None
    if tunnel_udid is None:
        raise DeviceError(
            f"no tunnel to {uuid[:8]}{_no_tunnel_hint(None, serving)}",
            tool="pymobiledevice3")

    snap = PerformanceSnapshot(
        udid=uuid, bundle_id=bundle_id, platform="ios_device", running=False,
        sample_seconds=seconds)
    try:
        async with asyncio.timeout(seconds + _ANSWER_SLACK_S):
            rsd = await get_tunneld_device_by_udid(tunnel_udid)
            if rsd is None:
                raise DeviceError(
                    f"tunneld has no tunnel for {uuid[:8]}{_no_tunnel_hint(None, True)}",
                    tool="pymobiledevice3")
            # Already connected: `async with rsd` would connect it a second
            # time, so close it by hand.
            try:
                async with DvtProvider(rsd) as dvt:
                    async with ProcessControl(dvt) as pc:
                        pid = await pc.process_identifier_for_bundle_identifier(bundle_id)
                    if not pid:
                        return _not_running(snap)
                    snap.running, snap.pid = True, pid
                    await _sysmon(Sysmontap, dvt, pid, seconds, snap)
                    await _display_fps(Graphics, dvt, snap)
            finally:
                with contextlib.suppress(Exception):
                    await rsd.close()
    except TimeoutError:
        raise DeviceError(
            f"{uuid[:8]} did not answer within {seconds + _ANSWER_SLACK_S:g}s",
            tool="pymobiledevice3") from None
    except DeviceError:
        raise
    except Exception as e:  # pymobiledevice3 raises its own hierarchy
        raise DeviceError(f"Instruments channel to {uuid[:8]} failed: {e!r}",
                          tool="pymobiledevice3") from e
    return _finish(snap)


async def _sysmon(Sysmontap, dvt, pid: int, seconds: float, snap: PerformanceSnapshot) -> None:
    """Sample until `seconds` have passed and CPU has a value.

    sysmontap's first sample carries a null cpuUsage (it has nothing to
    difference against yet), so one sample is never enough.
    """
    latest: dict | None = None
    t0 = _clock()
    async with await Sysmontap.create(dvt, interval=min(500, int(seconds * 1000))) as s:
        async for procs in s.iter_processes():
            mine = next((p for p in procs if p.get("pid") == pid), None)
            if mine is None:
                break
            latest = mine
            if _clock() - t0 >= seconds and mine.get("cpuUsage") is not None:
                break
    if latest is None:
        for name in ("cpu_percent", "memory_bytes", "threads"):
            snap.unavailable[name] = f"pid {pid} was not in sysmontap's sample; has the app exited?"
        return
    cpu = latest.get("cpuUsage")
    mem = latest.get("physFootprint")
    threads = latest.get("threadCount")
    _store(snap, "cpu_percent", float(cpu) if cpu is not None else None,
           "sysmontap gave no CPU reading within the sample")
    _store(snap, "memory_bytes", mem if isinstance(mem, int) and mem > 0 else None,
           "sysmontap gave no physFootprint")
    if snap.memory_bytes is not None:
        snap.memory_kind = "phys_footprint"
    _store(snap, "threads", threads if isinstance(threads, int) else None,
           "sysmontap gave no threadCount")


async def _display_fps(Graphics, dvt, snap: PerformanceSnapshot) -> None:
    """One Core Animation FPS reading, skipping the first sample (taken before
    any interval has elapsed)."""
    try:
        async with asyncio.timeout(4), Graphics(dvt) as g:
            n = 0
            async for ev in g:
                n += 1
                if n >= 2 and isinstance(ev, dict) and "CoreAnimationFramesPerSecond" in ev:
                    snap.display_fps = float(ev["CoreAnimationFramesPerSecond"])
                    return
    except Exception as e:  # FPS is a bonus; never fail the snapshot for it
        snap.unavailable["display_fps"] = f"graphics service did not answer: {e!r}"
        return
    snap.unavailable["display_fps"] = "graphics service gave no FPS reading"


# -- shared ------------------------------------------------------------------

def _not_running(snap: PerformanceSnapshot) -> PerformanceSnapshot:
    for name in MEASURED[snap.platform]:
        snap.unavailable[name] = NOT_RUNNING
    return _finish(snap)


def _finish(snap: PerformanceSnapshot) -> PerformanceSnapshot:
    """Give every null metric a reason: the ones this platform does not
    measure, and any measured one that was left empty without saying why."""
    for name, why in _NOT_MEASURED.items():
        if name not in MEASURED[snap.platform]:
            snap.unavailable[name] = why
    for name in MEASURED[snap.platform]:
        if getattr(snap, name) is None:
            snap.unavailable.setdefault(name, "not read")
    return snap


def _reason(exc: BaseException | None, default: str) -> str:
    if exc is None:
        return default
    return f"{default} ({exc})"


def _store(snap: PerformanceSnapshot, name: str, value: object, missing: str) -> None:
    """Set `name`, or record why it has no value. Exceptions from a gather
    land here as values, so one failed reading never sinks the others."""
    if isinstance(value, BaseException):
        snap.unavailable[name] = _reason(value, missing)
    elif value is None:
        snap.unavailable[name] = missing
    else:
        setattr(snap, name, value)
