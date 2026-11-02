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
from types import SimpleNamespace
from typing import TYPE_CHECKING

from server.models import (
    DeviceError,
    FrameStats,
    InvalidDeviceRequestError,
    PerformanceSnapshot,
)

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


def parse_android_cpu_sample(text: str, pid: int) -> tuple[float, int | None, float] | None:
    """(cpu percent, threads, seconds measured over) from the two-reading
    shell script below.

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
    return (ticks[1] - ticks[0]) / USER_HZ / elapsed * 100, threads, elapsed


def parse_proc_status_rss_kb(text: str) -> int | None:
    """Resident set size in kB from `/proc/<pid>/status`'s `VmRSS:` line,
    the last one when the text holds several."""
    found = None
    for line in text.splitlines():
        if line.strip().startswith("VmRSS:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                found = int(parts[1])
    return found


def parse_meminfo_total_pss_kb(text: str) -> int | None:
    """Total PSS in KB from `dumpsys meminfo <pid>`'s App Summary.

    Android 10 ends the summary with `TOTAL:  90665  TOTAL SWAP PSS: ...`;
    Android 12+ spells it `TOTAL PSS:  ...  TOTAL RSS: ...`. The colon is what
    tells it from the table's own `TOTAL` row above, which has none.
    """
    m = re.search(r"^\s*TOTAL(?: PSS)?:\s+(\d+)", text, re.MULTILINE)
    return int(m.group(1)) if m else None


def parse_gfxinfo(text: str, pid: int | None = None) -> FrameStats | None:
    """Frame stats from `dumpsys gfxinfo <package>`, or None if it has none.

    A package with several processes gets one `** Graphics info for pid N
    [...] **` section each; with `pid`, only that process's is read, so the
    frames belong to the process the CPU and memory came from.
    """
    if pid is not None:
        headers = list(re.finditer(r"^\*\* Graphics info for pid (\d+)", text, re.MULTILINE))
        if headers:
            mine = [i for i, h in enumerate(headers) if int(h.group(1)) == pid]
            if not mine:
                return None
            i = mine[0]
            end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
            text = text[headers[i].start():end]
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

async def _run(*args: str, timeout: float) -> tuple[int, str, str]:
    """Run a host command; (returncode, stdout, stderr).

    Raises DeviceError if it cannot be run at all or does not finish. The
    process is killed on timeout *and* on cancellation, so a caller that gives
    up never leaves a `footprint` behind.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except OSError as e:
        raise DeviceError(f"{args[0]} could not be run: {e}", tool=args[0]) from e
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except BaseException as e:
        if proc.returncode is None:
            proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.shield(proc.wait())
        if isinstance(e, TimeoutError):
            raise DeviceError(
                f"{args[0]} did not answer within {timeout:g}s", tool=args[0]) from None
        raise
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


def _failed(tool: str, rc: int, err: str) -> DeviceError:
    return DeviceError(f"{tool} failed: {err.strip() or f'exit {rc}'}", tool=tool)


async def _ps_cputime(pid: int) -> float | None:
    """CPU seconds so far, or None when the process is gone."""
    rc, out, err = await _run("ps", "-o", "time=", "-p", str(pid), timeout=5)
    if rc != 0:
        # ps exits 1 and prints nothing for a pid that no longer exists.
        if err.strip():
            raise _failed("ps", rc, err)
        return None
    value = parse_ps_cputime(out)
    if value is None:
        raise DeviceError(f"ps printed a CPU time quern cannot read: {out.strip()!r}", tool="ps")
    return value


async def _footprint(pid: int) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "footprint.json"
        rc, _, err = await _run("footprint", "-p", str(pid), "-j", str(path), timeout=10)
        if rc != 0:
            raise _failed("footprint", rc, err)
        text = path.read_text(errors="replace") if path.exists() else ""
        value = parse_footprint_json(text, pid)
        if value is None:
            raise DeviceError(f"footprint reported nothing quern can read for pid {pid}",
                              tool="footprint")
        return value


async def _ps_threads(pid: int) -> int | None:
    rc, out, err = await _run("ps", "-M", "-p", str(pid), timeout=5)
    if rc != 0:
        if err.strip():
            raise _failed("ps", rc, err)
        return None
    value = parse_ps_thread_count(out)
    if value is None:
        raise DeviceError("ps -M printed no thread list quern can read", tool="ps")
    return value


async def simulator_snapshot(
    simctl: SimctlBackend, udid: str, bundle_id: str, seconds: float,
) -> PerformanceSnapshot:
    budget = seconds + _ANSWER_SLACK_S
    try:
        async with asyncio.timeout(budget):
            return await _simulator_snapshot(simctl, udid, bundle_id, seconds)
    except TimeoutError:
        raise DeviceError(
            f"simulator {udid[:8]} did not answer within {budget:g}s", tool="simctl") from None


async def _simulator_snapshot(
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
        snap.sample_seconds = t1 - t0
        return (end - start) / (t1 - t0) * 100

    results = await asyncio.gather(cpu(), _footprint(pid), _ps_threads(pid),
                                   return_exceptions=True)
    for name, value in zip(("cpu_percent", "memory_bytes", "threads"), results, strict=True):
        _store(snap, name, value, f"pid {pid} could not be read; has the app exited?")
    if snap.memory_bytes is not None:
        snap.memory_kind = "phys_footprint"
    return _finish(snap)


# -- Android -----------------------------------------------------------------

#: What Android and iOS accept as a package or bundle id. It reaches a device
#: shell on Android (adb joins its arguments into one command line), so
#: anything outside it is refused before it gets there.
PACKAGE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


async def android_snapshot(
    adb: AdbBackend, serial: str, package: str, seconds: float, *, memory: str = "pss",
) -> PerformanceSnapshot:
    """`memory="rss"` reads the kernel's resident size from `/proc` instead of
    `dumpsys meminfo`'s PSS. For sampling every second: meminfo is answered
    inside the app's own process, and measured on a Pixel 3 XL it cost the
    app 4.7% of a core at one call a second -- an idle app at 0.2% read as
    eight to sixteen. `/proc` cost it nothing; gfxinfo, which frames need,
    0.4%."""
    if not PACKAGE_NAME.fullmatch(package):
        raise InvalidDeviceRequestError(f"{package!r} is not a package name")
    snap = PerformanceSnapshot(
        udid=serial, bundle_id=package, platform="android", running=False,
        sample_seconds=seconds)
    rc, out, err = await adb._run_unjudged(serial, "shell", "pidof", package, timeout=10)
    pids = [int(p) for p in out.split() if p.isdigit()]
    if not pids:
        # pidof exits 1 with nothing printed when nothing matches. Anything
        # else means it could not answer, which is not "not running" -- and
        # before Android 7 adb exits 0 with the shell's stderr on stdout.
        if rc in (0, 1) and not err.strip() and not out.strip():
            return _not_running(snap)
        raise DeviceError(
            f"pidof {package} on {serial} failed: {(err or out).strip() or f'exit {rc}'}",
            tool="adb")
    pid = pids[0]
    snap.running, snap.pid = True, pid

    script = (f"cat /proc/uptime /proc/{pid}/stat; grep '^Threads:' /proc/{pid}/status; "
              f"sleep {seconds:g}; cat /proc/uptime /proc/{pid}/stat")
    if memory == "rss":
        script += f"; grep '^VmRSS:' /proc/{pid}/status"
    sample_text: list[str] = []

    async def cpu_and_threads() -> tuple[float, int | None, float] | None:
        rc, sample, err = await adb._run_unjudged(
            serial, "shell", script, timeout=seconds + _ANSWER_SLACK_S)
        sample_text.append(sample)
        parsed = parse_android_cpu_sample(sample, pid)
        if parsed is None and (rc != 0 or err.strip()):
            # adb itself failed (device offline, unauthorized): not the app's doing.
            raise DeviceError(f"adb could not read /proc/{pid}: "
                              f"{(err or sample).strip() or f'exit {rc}'}", tool="adb")
        return parsed

    async def pss() -> int | None:
        if memory == "rss":
            return None                 # read from the CPU sample's text below
        stdout, _ = await adb._run_adb_for_device(
            serial, "shell", "dumpsys", "meminfo", str(pid), timeout=_ANSWER_SLACK_S)
        kb = parse_meminfo_total_pss_kb(stdout)
        return kb * 1024 if kb is not None else None

    async def frames() -> FrameStats | None:
        stdout, _ = await adb._run_adb_for_device(
            serial, "shell", "dumpsys", "gfxinfo", package, timeout=_ANSWER_SLACK_S)
        return parse_gfxinfo(stdout, pid)

    sampled, mem, fr = await asyncio.gather(
        cpu_and_threads(), pss(), frames(), return_exceptions=True)
    if memory == "rss" and not isinstance(mem, BaseException):
        kb = parse_proc_status_rss_kb(sample_text[0]) if sample_text else None
        mem = kb * 1024 if kb is not None else (
            sampled if isinstance(sampled, BaseException) else None)
    if isinstance(sampled, tuple):
        cpu, threads, elapsed = sampled
        if elapsed < seconds / 2:
            # Before Android 6 toolbox `sleep` takes whole seconds and reads
            # 0.5 as 0, so the two readings land a tick apart.
            snap.unavailable["cpu_percent"] = (
                f"the device's sleep returned after {elapsed:.2f}s of the {seconds:g}s "
                "asked for; try a whole number of seconds")
        else:
            snap.cpu_percent, snap.sample_seconds = cpu, elapsed
        snap.threads = threads
        if threads is None:
            snap.unavailable["threads"] = f"/proc/{pid}/status had no Threads: line"
    else:
        reason = _reason(sampled, f"/proc/{pid} could not be read twice; has the app exited?")
        snap.unavailable["cpu_percent"] = snap.unavailable["threads"] = reason
    _store(snap, "memory_bytes", mem,
           f"/proc/{pid}/status had no VmRSS: line; has the app exited?" if memory == "rss"
           else f"dumpsys meminfo {pid} had no total PSS")
    if snap.memory_bytes is not None:
        snap.memory_kind = "rss" if memory == "rss" else "pss"
    _store(snap, "frames", fr, f"gfxinfo reported no frames for pid {pid}; "
                               "the app may have no window")
    if len(pids) > 1:
        logger.debug("pidof %s returned %s; measured %s", package, pids, pid)
    return _finish(snap)


# -- iOS device --------------------------------------------------------------

def _pmd3() -> SimpleNamespace:
    """The pymobiledevice3 pieces the device path uses, in one place so the
    path can be tested without a phone."""
    try:
        from pymobiledevice3.remote.remote_service_discovery import (
            RemoteServiceDiscoveryService,
        )
        from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
        from pymobiledevice3.services.dvt.instruments.graphics import Graphics
        from pymobiledevice3.services.dvt.instruments.process_control import ProcessControl
        from pymobiledevice3.services.dvt.instruments.sysmontap import Sysmontap
    except ImportError as e:
        raise DeviceError(
            f"get_performance on a physical device needs pymobiledevice3 9.4.4 or "
            f"later in quern's environment ({e})", tool="pymobiledevice3") from e
    return SimpleNamespace(
        RemoteServiceDiscoveryService=RemoteServiceDiscoveryService, DvtProvider=DvtProvider,
        Graphics=Graphics, ProcessControl=ProcessControl, Sysmontap=Sysmontap)


async def _tunnel_address(tunnel_udid: str) -> tuple[str, int] | None:
    """The device's tunnel endpoint, read through quern's own tunneld client.

    pymobiledevice3's `get_tunneld_device_by_udid` asks tunneld with a
    blocking `requests.get` and no timeout, which would stall the whole server
    on a wedged tunneld; it also connects *every* tunnel a device has and
    returns one, leaking the rest.
    """
    from server.device.ios.tunneld import get_tunneld_devices

    for entry in (await get_tunneld_devices()).get(tunnel_udid) or []:
        address, port = entry.get("tunnel-address"), entry.get("tunnel-port")
        if address and isinstance(port, int):
            return address, port
    return None


async def _device_address(uuid: str) -> tuple[str, int]:
    """The tunnel endpoint for a physical iPhone, or a DeviceError naming why
    there is none."""
    from server.device.ios.pmd3 import _no_tunnel_hint, _recover_tunneld_if_wedged
    from server.device.ios.tunneld import is_tunneld_running, resolve_tunnel_udid

    serving = await is_tunneld_running() or await _recover_tunneld_if_wedged()
    tunnel_udid = await resolve_tunnel_udid(uuid) if serving else None
    address = await _tunnel_address(tunnel_udid) if tunnel_udid else None
    if address is None:
        raise DeviceError(
            f"no tunnel to {uuid[:8]}{_no_tunnel_hint(None, serving)}",
            tool="pymobiledevice3")
    return address


async def device_snapshot(uuid: str, bundle_id: str, seconds: float) -> PerformanceSnapshot:
    """Read a physical iPhone through pymobiledevice3's Instruments channel."""
    lib = _pmd3()
    address = await _device_address(uuid)

    snap = PerformanceSnapshot(
        udid=uuid, bundle_id=bundle_id, platform="ios_device", running=False,
        sample_seconds=seconds)
    budget = seconds + _ANSWER_SLACK_S
    rsd = lib.RemoteServiceDiscoveryService(address)
    deadline = asyncio.timeout(budget)
    try:
        async with deadline:
            try:
                await rsd.connect()
                async with lib.DvtProvider(rsd) as dvt:
                    async with lib.ProcessControl(dvt) as pc:
                        pid = await pc.process_identifier_for_bundle_identifier(bundle_id)
                    if not pid:
                        return _not_running(snap)
                    snap.running, snap.pid = True, pid
                    await _sysmon(lib.Sysmontap, dvt, pid, seconds, snap)
                    await _display_fps(lib.Graphics, dvt, snap)
            finally:
                with contextlib.suppress(Exception):
                    await rsd.close()
    except TimeoutError as e:
        if deadline.expired():
            raise DeviceError(f"{uuid[:8]} did not answer within {budget:g}s",
                              tool="pymobiledevice3") from None
        raise DeviceError(f"Instruments channel to {uuid[:8]} timed out: {e!r}",
                          tool="pymobiledevice3") from e
    except DeviceError:
        raise
    except Exception as e:  # pymobiledevice3 raises its own hierarchy
        raise DeviceError(f"Instruments channel to {uuid[:8]} failed: {e!r}",
                          tool="pymobiledevice3") from e
    return _finish(snap)


#: How long a gauge stream waits between looking for an app that is not
#: running, so a recording of an app not yet launched costs one lookup a
#: second, not a busy loop.
_NOT_RUNNING_POLL_S = 1.0


async def device_stream(uuid: str, bundle_id: str, interval: float):
    """Samples of one app on a physical iPhone, one per `interval`, for as
    long as the caller iterates (#245, phase 2).

    One Instruments connection, held: a snapshot per sample would connect the
    tunnel, open the DVT channel and wait out a Graphics reading every
    second. sysmontap streams on its own channel, and Core Animation's FPS on
    another beside it -- the connection reads every message and hands each
    channel its own, so the two do not take each other's.

    An app that is not running yields a not-running sample each poll and is
    looked for again, so one launched after the recording began is picked up;
    one that exits mid-stream is looked for again the same way. Whatever
    breaks the connection is raised, and the caller decides whether to retry.
    """
    lib = _pmd3()
    address = await _device_address(uuid)
    rsd = lib.RemoteServiceDiscoveryService(address)
    try:
        try:
            async with asyncio.timeout(_ANSWER_SLACK_S):
                await rsd.connect()
        except TimeoutError:
            raise DeviceError(f"{uuid[:8]} did not answer within {_ANSWER_SLACK_S:g}s",
                              tool="pymobiledevice3") from None
        async with lib.DvtProvider(rsd) as dvt:
            fps: dict = {"value": None, "why": "no FPS reading yet", "at": None}
            feed = asyncio.create_task(_fps_feed(lib.Graphics, dvt, fps))
            try:
                while True:
                    async with asyncio.timeout(_ANSWER_SLACK_S), lib.ProcessControl(dvt) as pc:
                        pid = await pc.process_identifier_for_bundle_identifier(bundle_id)
                    if not pid:
                        # Said after the wait, so its window is the time it
                        # speaks for (see `DeviceController.gauge_stream`).
                        await asyncio.sleep(_NOT_RUNNING_POLL_S)
                        yield _not_running(PerformanceSnapshot(
                            udid=uuid, bundle_id=bundle_id, platform="ios_device",
                            running=False, sample_seconds=_NOT_RUNNING_POLL_S))
                        continue
                    # Closed here, while the connection it reads is still open:
                    # left to the finalizer, its sysmontap closed after `rsd`.
                    async with contextlib.aclosing(_sysmon_stream(
                            lib.Sysmontap, dvt, uuid, bundle_id, pid, interval, fps)) as sysmon:
                        async for snap in sysmon:
                            yield snap
                    # It exited, or sysmontap never listed it: paced, so an
                    # app sysmontap cannot see is not asked about in a loop.
                    await asyncio.sleep(_NOT_RUNNING_POLL_S)
            finally:
                feed.cancel()
                with contextlib.suppress(BaseException):
                    await feed
    except (DeviceError, asyncio.CancelledError, GeneratorExit):
        raise
    except Exception as e:  # pymobiledevice3 raises its own hierarchy
        raise DeviceError(f"Instruments channel to {uuid[:8]} failed: {e!r}",
                          tool="pymobiledevice3") from e
    finally:
        with contextlib.suppress(Exception):
            await rsd.close()


async def _sysmon_stream(Sysmontap, dvt, uuid: str, bundle_id: str, pid: int,
                         interval: float, fps: dict):
    """One sample per sysmontap tick for `pid`, until it leaves the list.

    A pid Instruments names for the app can be missing from sysmontap's list
    from the start: measured on an iPhone 11, an app left in the background
    was absent from all 545 processes listed, and the same app brought to the
    foreground was there. That is said as a sample -- running, unmeasured --
    rather than read as an exit, which looked the pid up again and again and
    wrote nothing at all (live).
    """
    previous: dict | None = None
    async with await Sysmontap.create(dvt, interval=max(100, int(interval * 1000))) as s:
        async for procs in s.iter_processes():
            mine = next((p for p in procs if p.get("pid") == pid), None)
            if mine is None:
                if previous is None:
                    snap = PerformanceSnapshot(udid=uuid, bundle_id=bundle_id,
                                               platform="ios_device", running=True, pid=pid,
                                               sample_seconds=interval)
                    why = (f"pid {pid} is running but not in sysmontap's process list; iOS "
                           f"leaves out an app in the background -- bring it to the foreground")
                    for name in ("cpu_percent", "memory_bytes", "threads"):
                        snap.unavailable[name] = why
                    _take_fps(snap, fps, interval)
                    yield _finish(snap)
                else:
                    # Said, not skipped: until the next pid's first full tick
                    # -- measured at about 3 s after a relaunch -- there is
                    # otherwise no sample at all.
                    gone = PerformanceSnapshot(udid=uuid, bundle_id=bundle_id,
                                               platform="ios_device", running=False, pid=pid,
                                               sample_seconds=interval)
                    for name in MEASURED["ios_device"]:
                        gone.unavailable[name] = f"pid {pid} left sysmontap's list: the app exited"
                    yield _finish(gone)
                return                      # exited, or unseen; the caller looks again
            if previous is None:
                previous = mine
                continue
            snap = PerformanceSnapshot(udid=uuid, bundle_id=bundle_id, platform="ios_device",
                                       running=True, pid=pid, sample_seconds=interval)
            measured = cpu_between(previous, mine)
            if measured is None:
                snap.unavailable["cpu_percent"] = "sysmontap's samples had no cumulative CPU time"
            else:
                snap.cpu_percent, snap.sample_seconds = measured
            mem, threads = mine.get("physFootprint"), mine.get("threadCount")
            _store(snap, "memory_bytes", mem if isinstance(mem, int) and mem > 0 else None,
                   "sysmontap gave no physFootprint")
            if snap.memory_bytes is not None:
                snap.memory_kind = "phys_footprint"
            _store(snap, "threads", threads if isinstance(threads, int) and threads > 0
                   else None, "sysmontap gave no threadCount")
            _take_fps(snap, fps, interval)
            previous = mine
            yield _finish(snap)


#: How long a Core Animation reading stands for, at the least: Graphics sends
#: about one a second, so an older one means the feed has gone quiet.
_FPS_STALE_S = 3.0


def _take_fps(snap: PerformanceSnapshot, fps: dict, interval: float) -> None:
    """The latest FPS onto `snap`, unless there is none or it is stale -- a
    feed gone quiet without ending would otherwise repeat its last reading as
    current for the rest of the recording (review)."""
    at = fps.get("at")
    if fps["value"] is None:
        snap.unavailable["display_fps"] = fps["why"]
    elif at is not None and _clock() - at > max(_FPS_STALE_S, interval * 3):
        snap.unavailable["display_fps"] = (
            f"no FPS reading for {_clock() - at:.0f}s; the graphics service went quiet")
    else:
        snap.display_fps = fps["value"]


async def _fps_feed(Graphics, dvt, fps: dict) -> None:
    """Keep `fps` holding the latest Core Animation reading, skipping the
    first (taken before any interval has elapsed). FPS is a bonus: a feed
    that fails says why in `fps`, and the samples carry on without it."""
    try:
        async with Graphics(dvt) as g:
            n = 0
            async for ev in g:
                n += 1
                if n >= 2 and isinstance(ev, dict) and "CoreAnimationFramesPerSecond" in ev:
                    fps["value"] = float(ev["CoreAnimationFramesPerSecond"])
                    fps["at"] = _clock()
        fps["value"], fps["why"] = None, "the graphics service stopped sending FPS"
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 -- said in every sample, never fatal
        fps["value"], fps["why"] = None, f"graphics service did not answer: {e!r}"


def cpu_between(first: dict, last: dict) -> tuple[float, float] | None:
    """(percent of one core, window in seconds) between two sysmontap samples.

    `cpuTotalUser`, `cpuTotalSystem` and `procAge` are cumulative nanoseconds,
    so the difference is an average over the whole window, timed on the
    device. `cpuUsage` would cover only the last interval, and is null in the
    first sample.
    """
    keys = ("cpuTotalUser", "cpuTotalSystem", "procAge")
    if not all(isinstance(s.get(k), int) for s in (first, last) for k in keys):
        return None
    age = last["procAge"] - first["procAge"]
    if age <= 0:
        return None
    busy = (last["cpuTotalUser"] + last["cpuTotalSystem"]
            - first["cpuTotalUser"] - first["cpuTotalSystem"])
    return busy / age * 100, age / 1e9


async def _sysmon(Sysmontap, dvt, pid: int, seconds: float, snap: PerformanceSnapshot) -> None:
    """Sample until `seconds` have passed since the app's first sample."""
    first: dict | None = None
    last: dict | None = None
    t0 = 0.0
    async with await Sysmontap.create(dvt, interval=min(500, int(seconds * 1000))) as s:
        async for procs in s.iter_processes():
            mine = next((p for p in procs if p.get("pid") == pid), None)
            if mine is None:
                break
            if first is None:
                first = last = mine
                t0 = _clock()
                continue
            last = mine
            if _clock() - t0 >= seconds:
                break
    if first is None or last is None:
        for name in ("cpu_percent", "memory_bytes", "threads"):
            snap.unavailable[name] = f"pid {pid} was not in sysmontap's sample; has the app exited?"
        return
    measured = cpu_between(first, last) if last is not first else None
    if measured is None:
        snap.unavailable["cpu_percent"] = (
            "sysmontap gave too few samples to measure CPU over; has the app exited?"
            if last is first else "sysmontap's samples had no cumulative CPU time")
    else:
        snap.cpu_percent, snap.sample_seconds = measured
    mem, threads = last.get("physFootprint"), last.get("threadCount")
    _store(snap, "memory_bytes", mem if isinstance(mem, int) and mem > 0 else None,
           "sysmontap gave no physFootprint")
    if snap.memory_bytes is not None:
        snap.memory_kind = "phys_footprint"
    _store(snap, "threads", threads if isinstance(threads, int) and threads > 0 else None,
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
    """Why a reading is missing. A DeviceError already names its cause, and
    putting the default ("has the app exited?") in front of it would blame
    the app for adb or footprint failing."""
    if exc is None:
        return default
    if isinstance(exc, DeviceError):
        return str(exc)
    return f"{default} ({exc!r})"


def _store(snap: PerformanceSnapshot, name: str, value: object, missing: str) -> None:
    """Set `name`, or record why it has no value. Exceptions from a gather
    land here as values, so one failed reading never sinks the others."""
    if isinstance(value, BaseException):
        snap.unavailable[name] = _reason(value, missing)
    elif value is None:
        snap.unavailable[name] = missing
    else:
        setattr(snap, name, value)
