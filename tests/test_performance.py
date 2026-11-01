"""get_performance (#245): the parsers, the per-platform samplers, the
controller's dispatch and the route.

The fixtures under tests/fixtures/performance/ are real output: a Pixel 3 XL
on Android 10, an API 34 emulator (Android 14), and QuernProbe on an iOS 18.6
simulator.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from server.config import ServerConfig
from server.device import performance
from server.device.controller import DeviceController
from server.main import create_app
from server.models import (
    DeviceError,
    DeviceType,
    InvalidDeviceRequestError,
    PerformanceSnapshot,
)

FIXTURES = Path(__file__).parent / "fixtures" / "performance"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


PS_M = """\
USER   PID   TT   %CPU STAT PRI     STIME     UTIME COMMAND
jham 13093   ??    0.0 S    47T   0:06.54   0:50.43 /Users/x/QuernProbe.app/QuernProbe
     13093         0.0 S    47T   0:00.17   0:00.81
     13093         0.0 S    47T   0:00.00   0:00.00
     13093         0.0 S    31T   0:00.00   0:00.00
"""


def every_null_has_a_reason(snap: PerformanceSnapshot) -> None:
    for name in ("cpu_percent", "memory_bytes", "threads", "frames", "display_fps"):
        if getattr(snap, name) is None:
            assert snap.unavailable.get(name), f"{name} is null with no reason: {snap.unavailable}"


def android_stat(t0, ticks0, t1, ticks1, pid=7, threads="Threads:\t12\n"):
    def stat(ticks):
        return f"{pid} (app) S " + "0 " * 10 + f"{ticks} 0 0\n"
    return f"{t0:.2f} 1.00\n{stat(ticks0)}{threads}{t1:.2f} 1.00\n{stat(ticks1)}"


# -- parsers -----------------------------------------------------------------

class TestParsers:
    @pytest.mark.parametrize("text,seconds", [
        ("  0:59.01\n", 59.01),
        ("12:03.50", 723.5),
        ("1:02:03.45", 3723.45),
        ("2-01:00:00.00", 2 * 86400 + 3600),
    ])
    def test_ps_cputime(self, text, seconds):
        assert performance.parse_ps_cputime(text) == pytest.approx(seconds)

    @pytest.mark.parametrize("text", ["", "  ", "garbage", "1:2:3:4", "x-1:00.00"])
    def test_ps_cputime_unreadable_is_none_not_zero(self, text):
        assert performance.parse_ps_cputime(text) is None

    def test_thread_count_is_lines_after_the_header(self):
        assert performance.parse_ps_thread_count(PS_M) == 4

    def test_thread_count_without_a_header_is_none(self):
        assert performance.parse_ps_thread_count("") is None
        assert performance.parse_ps_thread_count("ps: no such process\n") is None

    def test_footprint_reads_the_pid_asked_about(self):
        text = fixture("sim-footprint.json")
        assert performance.parse_footprint_json(text, 13093) == 37423696
        assert performance.parse_footprint_json(text, 1) is None

    def test_footprint_in_another_unit_is_not_guessed(self):
        text = fixture("sim-footprint.json").replace('"unit":"byte"', '"unit":"page"')
        assert '"unit":"page"' in text
        assert performance.parse_footprint_json(text, 13093) is None

    def test_a_zero_footprint_is_not_a_reading(self):
        text = fixture("sim-footprint.json").replace("37423696", "0")
        assert performance.parse_footprint_json(text, 13093) is None

    def test_footprint_garbage_is_none(self):
        assert performance.parse_footprint_json("not json", 1) is None

    def test_proc_stat_counts_fields_after_the_command_name(self):
        # A command name with spaces and a parenthesis would shift a naive split.
        line = "42 (a b) c) S 1 1 0 0 -1 0 0 0 0 0 700 300 0 0 20 0 9 0 1 2 3"
        assert performance.parse_proc_stat_ticks(line) == 1000

    def test_android_cpu_from_measured_output(self):
        # The probe idled through the measured sample: same ticks twice.
        cpu, threads, elapsed = performance.parse_android_cpu_sample(
            fixture("android10-stat.out"), 6403)
        assert cpu == 0.0
        assert threads == 49
        assert elapsed == pytest.approx(1.11)  # the device's own clock, not the request's

    def test_android_cpu_busy(self):
        cpu, threads, elapsed = performance.parse_android_cpu_sample(
            android_stat(100.0, 150, 102.0, 350), 7)
        assert cpu == pytest.approx(100.0)  # 200 ticks = 2 s of CPU in 2 s
        assert (threads, elapsed) == (12, pytest.approx(2.0))

    def test_android_cpu_with_one_reading_is_none(self):
        # The app died between the readings: the second cat printed nothing.
        text = "100.00 1.00\n7 (app) S " + "0 " * 10 + "100 50 0\n102.00 1.00\n"
        assert performance.parse_android_cpu_sample(text, 7) is None

    def test_android_cpu_with_a_clock_that_did_not_advance_is_none(self):
        assert performance.parse_android_cpu_sample(android_stat(100.0, 1, 100.0, 9), 7) is None

    def test_meminfo_android_10(self):
        assert performance.parse_meminfo_total_pss_kb(fixture("android10-meminfo.out")) == 90665

    def test_meminfo_android_14_spelling(self):
        assert performance.parse_meminfo_total_pss_kb(fixture("android14-meminfo.out")) == 78690

    def test_meminfo_ignores_the_tables_own_total_row(self):
        # The table's TOTAL row has no colon; only the summary's does.
        text = "        TOTAL    11111    0\n App Summary\n               TOTAL:    22222\n"
        assert performance.parse_meminfo_total_pss_kb(text) == 22222

    def test_meminfo_without_a_summary_is_none(self):
        assert performance.parse_meminfo_total_pss_kb("No process found for: 123\n") is None

    def test_gfxinfo_android_10_has_only_the_legacy_count(self):
        stats = performance.parse_gfxinfo(fixture("android10-gfxinfo.out"))
        assert stats.total_frames == 2637
        assert (stats.janky_frames, stats.janky_percent, stats.janky_method) == (15, 0.57, "legacy")
        assert (stats.p50_ms, stats.p90_ms, stats.p95_ms, stats.p99_ms) == (5, 5, 6, 8)

    def test_gfxinfo_android_14_reports_the_deadline_count(self):
        stats = performance.parse_gfxinfo(fixture("android14-gfxinfo.out"))
        assert (stats.janky_frames, stats.janky_percent) == (12, 7.64)  # not legacy's 33
        assert stats.janky_method == "deadline"
        assert (stats.p50_ms, stats.p95_ms, stats.p99_ms) == (16, 40, 1700)

    def test_gfxinfo_reads_only_the_measured_processs_section(self):
        one = fixture("android14-gfxinfo.out")
        other = one.replace("pid 1751", "pid 2222").replace(
            "Total frames rendered: 157", "Total frames rendered: 9")
        assert performance.parse_gfxinfo(other + one, 1751).total_frames == 157
        assert performance.parse_gfxinfo(other + one, 2222).total_frames == 9
        assert performance.parse_gfxinfo(one, 4444) is None

    def test_gfxinfo_with_no_frames_is_none(self):
        assert performance.parse_gfxinfo("No process found for: com.x\n") is None

    @pytest.mark.parametrize("first,last,expected", [
        # 0.5 s of CPU across 2 s of the process's life: 25% of a core.
        ({"cpuTotalUser": 1_000, "cpuTotalSystem": 0, "procAge": 10_000_000_000},
         {"cpuTotalUser": 400_001_000, "cpuTotalSystem": 100_000_000, "procAge": 12_000_000_000},
         (25.0, 2.0)),
        ({"cpuTotalUser": 1, "cpuTotalSystem": 1, "procAge": 5},
         {"cpuTotalUser": 9, "cpuTotalSystem": 9, "procAge": 5}, None),  # no time passed
        ({"cpuTotalUser": 1, "procAge": 5}, {"cpuTotalUser": 9, "procAge": 9}, None),  # missing
    ])
    def test_cpu_between_sysmontap_samples(self, first, last, expected):
        got = performance.cpu_between(first, last)
        assert got == (pytest.approx(expected) if expected else None)


class TestEveryNullHasAReason:
    def test_a_measured_metric_left_empty_is_still_explained(self):
        # A backstop for paths that forget to say why: none do today.
        snap = PerformanceSnapshot(udid="U", bundle_id="b", platform="android",
                                   running=True, sample_seconds=1.0, cpu_percent=1.0)
        performance._finish(snap)
        every_null_has_a_reason(snap)
        assert snap.unavailable["threads"] == "not read"
        assert snap.unavailable["display_fps"] == "iOS devices only"
        assert "cpu_percent" not in snap.unavailable


# -- running host commands ---------------------------------------------------

def _sleeper(seconds: float) -> tuple[str, ...]:
    return (sys.executable, "-c", f"import time; time.sleep({seconds})")


def _alive(pid: int) -> bool:
    import os
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class TestRun:
    async def test_returns_stdout_stderr_and_code(self):
        rc, out, err = await performance._run(
            sys.executable, "-c", "import sys; print('o'); sys.stderr.write('e'); sys.exit(3)",
            timeout=10)
        assert (rc, out.strip(), err) == (3, "o", "e")

    async def test_a_missing_tool_is_a_device_error(self):
        with pytest.raises(DeviceError, match="could not be run"):
            await performance._run("/nonexistent/footprint", timeout=1)

    async def test_a_timeout_kills_the_process(self, monkeypatch):
        spawned = []
        real = asyncio.create_subprocess_exec

        async def spy(*a, **k):
            proc = await real(*a, **k)
            spawned.append(proc)
            return proc

        monkeypatch.setattr(performance.asyncio, "create_subprocess_exec", spy)
        with pytest.raises(DeviceError, match="did not answer"):
            await performance._run(*_sleeper(30), timeout=0.3)
        assert spawned[0].returncode is not None and not _alive(spawned[0].pid)

    async def test_cancelling_kills_the_process(self, monkeypatch):
        spawned = []
        real = asyncio.create_subprocess_exec

        async def spy(*a, **k):
            proc = await real(*a, **k)
            spawned.append(proc)
            return proc

        monkeypatch.setattr(performance.asyncio, "create_subprocess_exec", spy)
        task = asyncio.create_task(performance._run(*_sleeper(30), timeout=60))
        while not spawned:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert spawned[0].returncode is not None and not _alive(spawned[0].pid)


# -- iOS simulator -----------------------------------------------------------

class FakeHost:
    """Answers the host commands simulator_snapshot runs: (rc, stdout, stderr)."""

    def __init__(self, cputimes=((0, "0:10.00", ""), (0, "0:10.50", "")), footprint=37423696,
                 threads=(0, PS_M, "")):
        self.cputimes = list(cputimes)
        self.footprint = footprint
        self.threads = threads

    async def run(self, *args, timeout):
        if args[:3] == ("ps", "-o", "time="):
            return self.cputimes.pop(0)
        if args[:2] == ("ps", "-M"):
            return self.threads
        if args[0] == "footprint":
            if isinstance(self.footprint, tuple):
                return self.footprint
            path = Path(args[args.index("-j") + 1])
            path.write_text(fixture("sim-footprint.json").replace("37423696", str(self.footprint)))
            return 0, "", ""
        raise AssertionError(f"unexpected command {args}")


@pytest.fixture
def no_sleep(monkeypatch):
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(performance.asyncio, "sleep", fake_sleep)
    return sleeps


def _sim(pid):
    simctl = AsyncMock()
    simctl.running_pid = AsyncMock(return_value=pid)
    return simctl


@pytest.fixture
def clock(monkeypatch):
    ticks = iter([100.0, 101.25])
    monkeypatch.setattr(performance, "_clock", lambda: next(ticks))


class TestSimulator:
    async def test_running_app(self, monkeypatch, no_sleep, clock):
        monkeypatch.setattr(performance, "_run", FakeHost().run)
        snap = await performance.simulator_snapshot(_sim(13093), "SIM", "com.x", 1.0)
        assert snap.running and snap.pid == 13093
        assert snap.cpu_percent == pytest.approx(40.0)  # 0.5 s of CPU in 1.25 s
        assert snap.sample_seconds == pytest.approx(1.25)  # what was measured, not asked for
        assert (snap.memory_bytes, snap.memory_kind) == (37423696, "phys_footprint")
        assert snap.threads == 4
        assert no_sleep == [1.0]
        assert set(snap.unavailable) == {"frames", "display_fps"}

    async def test_not_running(self, monkeypatch):
        monkeypatch.setattr(performance, "_run", AsyncMock(side_effect=AssertionError("ran")))
        snap = await performance.simulator_snapshot(_sim(None), "SIM", "com.x", 1.0)
        assert not snap.running and snap.pid is None
        assert snap.unavailable["cpu_percent"] == performance.NOT_RUNNING
        every_null_has_a_reason(snap)

    async def test_an_unreadable_simulator_raises_rather_than_reading_as_not_running(self):
        simctl = AsyncMock()
        simctl.running_pid = AsyncMock(
            side_effect=DeviceError("launchctl list failed", tool="simctl"))
        with pytest.raises(DeviceError):
            await performance.simulator_snapshot(simctl, "SIM", "com.x", 1.0)

    async def test_a_hung_simulator_is_bounded(self, monkeypatch):
        monkeypatch.setattr(performance, "_ANSWER_SLACK_S", 0.05)
        simctl = AsyncMock()

        async def hang(*a):
            await asyncio.Event().wait()

        simctl.running_pid = hang
        with pytest.raises(DeviceError, match="did not answer"):
            await performance.simulator_snapshot(simctl, "SIM", "com.x", 0.2)

    async def test_a_failed_footprint_says_why(self, monkeypatch, no_sleep, clock):
        host = FakeHost(footprint=(1, "", "footprint: task_for_pid failed (os/kern) failure"))
        monkeypatch.setattr(performance, "_run", host.run)
        snap = await performance.simulator_snapshot(_sim(13093), "SIM", "com.x", 1.0)
        assert snap.memory_bytes is None and snap.memory_kind is None
        assert "task_for_pid failed" in snap.unavailable["memory_bytes"]
        assert snap.threads == 4 and snap.cpu_percent is not None  # the others survive
        every_null_has_a_reason(snap)

    async def test_a_ps_that_errors_says_why(self, monkeypatch, no_sleep, clock):
        host = FakeHost(cputimes=((0, "0:10.00", ""), (1, "", "ps: illegal option")))
        monkeypatch.setattr(performance, "_run", host.run)
        snap = await performance.simulator_snapshot(_sim(13093), "SIM", "com.x", 1.0)
        assert snap.cpu_percent is None
        assert "illegal option" in snap.unavailable["cpu_percent"]

    async def test_a_process_gone_mid_sample_is_null_not_zero(self, monkeypatch, no_sleep, clock):
        host = FakeHost(cputimes=((0, "0:10.00", ""), (1, "", "")))
        monkeypatch.setattr(performance, "_run", host.run)
        snap = await performance.simulator_snapshot(_sim(13093), "SIM", "com.x", 1.0)
        assert snap.cpu_percent is None
        assert "has the app exited" in snap.unavailable["cpu_percent"]

    async def test_a_clock_that_did_not_advance_is_not_a_reading(self, monkeypatch, no_sleep):
        monkeypatch.setattr(performance, "_clock", lambda: 5.0)
        monkeypatch.setattr(performance, "_run", FakeHost().run)
        snap = await performance.simulator_snapshot(_sim(13093), "SIM", "com.x", 1.0)
        assert snap.cpu_percent is None and snap.unavailable["cpu_percent"]

    async def test_unreadable_ps_output_is_an_error_not_a_zero(self, monkeypatch, no_sleep, clock):
        host = FakeHost(cputimes=((0, "0:10.00", ""), (0, "??", "")))
        monkeypatch.setattr(performance, "_run", host.run)
        snap = await performance.simulator_snapshot(_sim(13093), "SIM", "com.x", 1.0)
        assert snap.cpu_percent is None
        assert "cannot read" in snap.unavailable["cpu_percent"]


# -- Android -----------------------------------------------------------------

class FakeAdb:
    def __init__(self, pidof=(0, "6403\n", ""), stat=None, meminfo=None, gfxinfo=None):
        self.pidof = pidof
        self.stat = (0, fixture("android10-stat.out"), "") if stat is None else stat
        self.meminfo = fixture("android10-meminfo.out") if meminfo is None else meminfo
        self.gfxinfo = fixture("android10-gfxinfo.out") if gfxinfo is None else gfxinfo
        self.calls: list[tuple] = []

    async def _run_unjudged(self, serial, *args, timeout):
        self.calls.append(args)
        if args[:2] == ("shell", "pidof"):
            return self.pidof
        if isinstance(self.stat, Exception):
            raise self.stat
        return self.stat

    async def _run_adb_for_device(self, serial, *args, timeout=None):
        self.calls.append(args)
        value = self.meminfo if args[2] == "meminfo" else self.gfxinfo
        if isinstance(value, Exception):
            raise value
        return value, ""


class TestAndroid:
    async def test_running_app_from_measured_output(self):
        adb = FakeAdb()
        snap = await performance.android_snapshot(adb, "8BAY0WCL7", "com.quern.probe", 1.0)
        assert snap.running and snap.pid == 6403
        assert snap.cpu_percent == 0.0  # measured idle: a zero that was read
        assert snap.sample_seconds == pytest.approx(1.11)
        assert snap.threads == 49
        assert (snap.memory_bytes, snap.memory_kind) == (90665 * 1024, "pss")
        assert snap.frames.total_frames == 2637
        assert set(snap.unavailable) == {"display_fps"}
        # meminfo is asked about the pid, gfxinfo about the package
        assert ("shell", "dumpsys", "meminfo", "6403") in adb.calls
        assert ("shell", "dumpsys", "gfxinfo", "com.quern.probe") in adb.calls

    async def test_the_cpu_window_is_timed_on_the_device(self):
        adb = FakeAdb()
        await performance.android_snapshot(adb, "S", "com.quern.probe", 2.5)
        script = next(a[1] for a in adb.calls if a[0] == "shell" and "sleep" in a[1])
        assert "sleep 2.5" in script and script.count("/proc/uptime") == 2

    async def test_not_running(self):
        snap = await performance.android_snapshot(FakeAdb(pidof=(1, "", "")), "S", "com.x", 1.0)
        assert not snap.running
        assert snap.unavailable["frames"] == performance.NOT_RUNNING
        every_null_has_a_reason(snap)

    async def test_a_pidof_that_could_not_answer_is_not_not_running(self):
        adb = FakeAdb(pidof=(127, "", "/system/bin/sh: pidof: not found"))
        with pytest.raises(DeviceError, match="pidof: not found"):
            await performance.android_snapshot(adb, "S", "com.x", 1.0)

    async def test_an_old_adb_folding_the_error_into_stdout_is_not_not_running(self):
        # Before Android 7, adb exits 0 and the shell's stderr arrives on stdout.
        adb = FakeAdb(pidof=(0, "/system/bin/sh: pidof: not found\n", ""))
        with pytest.raises(DeviceError, match="pidof: not found"):
            await performance.android_snapshot(adb, "S", "com.x", 1.0)

    async def test_meminfo_failing_leaves_the_rest(self):
        adb = FakeAdb(meminfo=DeviceError("adb shell failed: device offline", tool="adb"))
        snap = await performance.android_snapshot(adb, "S", "com.quern.probe", 1.0)
        assert snap.memory_bytes is None and snap.memory_kind is None
        assert "device offline" in snap.unavailable["memory_bytes"]
        assert snap.threads == 49 and snap.frames is not None
        every_null_has_a_reason(snap)

    async def test_adb_failing_during_the_sample_is_blamed_on_adb_not_the_app(self):
        adb = FakeAdb(stat=(1, "", "error: device offline"))
        snap = await performance.android_snapshot(adb, "S", "com.quern.probe", 1.0)
        assert snap.running and snap.cpu_percent is None
        assert "device offline" in snap.unavailable["cpu_percent"]
        assert "has the app exited" not in snap.unavailable["cpu_percent"]

    async def test_app_exiting_mid_sample(self):
        adb = FakeAdb(stat=(0, "3161679.51 1.0\n3161680.62 1.0\n", ""))
        snap = await performance.android_snapshot(adb, "S", "com.quern.probe", 1.0)
        assert snap.cpu_percent is None and snap.threads is None
        assert "has the app exited" in snap.unavailable["cpu_percent"]
        every_null_has_a_reason(snap)

    async def test_a_sleep_that_did_not_wait_is_not_a_reading(self):
        # Android 5's toolbox reads `sleep 0.5` as 0: the readings land 0.01 s apart.
        adb = FakeAdb(stat=(0, android_stat(100.00, 1, 100.01, 2, pid=6403), ""))
        snap = await performance.android_snapshot(adb, "S", "com.quern.probe", 0.5)
        assert snap.cpu_percent is None
        assert "whole number of seconds" in snap.unavailable["cpu_percent"]
        assert snap.sample_seconds == 0.5

    async def test_a_missing_threads_line_says_so(self):
        adb = FakeAdb(stat=(0, android_stat(100.0, 1, 101.0, 2, pid=6403, threads=""), ""))
        snap = await performance.android_snapshot(adb, "S", "com.quern.probe", 1.0)
        assert snap.threads is None and snap.cpu_percent is not None
        assert "no Threads: line" in snap.unavailable["threads"]

    async def test_no_window_means_no_frames_with_a_reason(self):
        snap = await performance.android_snapshot(
            FakeAdb(gfxinfo="No process found\n"), "S", "com.quern.probe", 1.0)
        assert snap.frames is None and "no frames" in snap.unavailable["frames"]

    @pytest.mark.parametrize("package", ["com.x;pm uninstall com.y", "com.x $(id)", "a b", "x|y"])
    async def test_a_package_that_would_reach_the_shell_is_refused(self, package):
        adb = FakeAdb()
        with pytest.raises(InvalidDeviceRequestError):
            await performance.android_snapshot(adb, "S", package, 1.0)
        assert adb.calls == []


# -- iOS device --------------------------------------------------------------

class FakeSysmontap:
    def __init__(self, samples):
        self.samples = samples

    @classmethod
    def with_samples(cls, samples):
        holder = cls(samples)

        class _Factory:
            @staticmethod
            async def create(dvt, interval):
                return holder

        return _Factory

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def iter_processes(self):
        for s in self.samples:
            yield s


def _proc(pid, user_ns, age_ns, mem=12125344, threads=3, cpu=None):
    return {"pid": pid, "cpuUsage": cpu, "cpuTotalUser": user_ns, "cpuTotalSystem": 0,
            "procAge": age_ns, "physFootprint": mem, "threadCount": threads}


def _snap(platform="ios_device"):
    return PerformanceSnapshot(udid="D", bundle_id="b", platform=platform,
                               running=True, sample_seconds=1.0)


class TestSysmon:
    async def test_cpu_is_averaged_over_the_whole_window(self, monkeypatch):
        ticks = iter([0.0, 0.5, 1.2])
        monkeypatch.setattr(performance, "_clock", lambda: next(ticks))
        sysmon = FakeSysmontap.with_samples([
            [_proc(9, 0, 10_000_000_000, cpu=None)],
            [_proc(9, 100_000_000, 10_500_000_000, cpu=99.0)],   # a spike
            [_proc(9, 120_000_000, 11_200_000_000, cpu=2.0)],
        ])
        snap = _snap()
        await performance._sysmon(sysmon, None, 9, 1.0, snap)
        # 0.12 s of CPU over 1.2 s, not the last interval's 2% or the spike's 99%
        assert snap.cpu_percent == pytest.approx(10.0)
        assert snap.sample_seconds == pytest.approx(1.2)
        assert (snap.memory_bytes, snap.memory_kind, snap.threads) == (
            12125344, "phys_footprint", 3)

    async def test_one_sample_is_not_enough(self, monkeypatch):
        monkeypatch.setattr(performance, "_clock", lambda: 0.0)
        sysmon = FakeSysmontap.with_samples([[_proc(9, 0, 10)], [_proc(1, 0, 10)]])
        snap = _snap()
        await performance._sysmon(sysmon, None, 9, 1.0, snap)
        assert snap.cpu_percent is None and "too few samples" in snap.unavailable["cpu_percent"]
        assert snap.memory_bytes == 12125344  # the one sample still gives memory

    async def test_an_app_gone_from_the_sample(self, monkeypatch):
        monkeypatch.setattr(performance, "_clock", lambda: 0.0)
        sysmon = FakeSysmontap.with_samples([[_proc(1, 0, 10)]])
        snap = _snap()
        await performance._sysmon(sysmon, None, 9, 1.0, snap)
        assert snap.cpu_percent is None and "has the app exited" in snap.unavailable["cpu_percent"]

    @pytest.mark.parametrize("mem,threads", [(0, 3), (None, 3), (12, None), (12, 0), (12, "3")])
    async def test_zero_or_missing_fields_are_not_readings(self, monkeypatch, mem, threads):
        ticks = iter([0.0, 2.0])
        monkeypatch.setattr(performance, "_clock", lambda: next(ticks))
        sysmon = FakeSysmontap.with_samples([
            [_proc(9, 0, 10, mem=mem, threads=threads)],
            [_proc(9, 5, 2_000_000_010, mem=mem, threads=threads)],
        ])
        snap = _snap()
        await performance._sysmon(sysmon, None, 9, 1.0, snap)
        if mem in (0, None):
            assert snap.memory_bytes is None and snap.unavailable["memory_bytes"]
        if threads in (None, 0, "3"):
            assert snap.threads is None and snap.unavailable["threads"]


class FakeGraphics:
    def __init__(self, events=None, error=None):
        self.events, self.error = events or [], error

    def __call__(self, dvt):
        return self

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self

    async def __aexit__(self, *exc):
        return False

    async def __aiter__(self):
        for e in self.events:
            yield e


class TestDisplayFps:
    async def test_the_first_sample_is_skipped(self):
        snap = _snap()
        g = FakeGraphics([{"CoreAnimationFramesPerSecond": 0},
                          {"CoreAnimationFramesPerSecond": 58}])
        await performance._display_fps(g, None, snap)
        assert snap.display_fps == 58.0

    async def test_no_reading_says_so(self):
        snap = _snap()
        await performance._display_fps(FakeGraphics([{"CoreAnimationFramesPerSecond": 60}]),
                                       None, snap)
        assert snap.display_fps is None and "no FPS" in snap.unavailable["display_fps"]

    async def test_a_failing_service_does_not_fail_the_snapshot(self):
        snap = _snap()
        await performance._display_fps(FakeGraphics(error=OSError("boom")), None, snap)
        assert snap.display_fps is None and "boom" in snap.unavailable["display_fps"]


class FakeRsd:
    instances: list[FakeRsd] = []

    def __init__(self, address, connect_error=None):
        self.address, self.connect_error = address, connect_error
        self.connected = self.closed = False
        FakeRsd.instances.append(self)

    async def connect(self):
        if self.connect_error:
            raise self.connect_error
        self.connected = True

    async def close(self):
        self.closed = True


class FakeService:
    """Stands in for DvtProvider and ProcessControl: an async context manager."""

    def __init__(self, *_, pid=583, error=None):
        self.pid, self.error = pid, error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def process_identifier_for_bundle_identifier(self, bundle_id):
        if self.error:
            raise self.error
        return self.pid


@pytest.fixture
def phone(monkeypatch):
    """A fake phone behind a fake tunnel; returns knobs for the test."""
    from server.device.ios import tunneld

    FakeRsd.instances = []
    knobs = SimpleNamespace(pid=583, pc_error=None, connect_error=None,
                            samples=[[_proc(583, 0, 10_000_000_000)],
                                     [_proc(583, 50_000_000, 11_000_000_000)]])
    monkeypatch.setattr(tunneld, "is_tunneld_running", AsyncMock(return_value=True))
    monkeypatch.setattr(tunneld, "resolve_tunnel_udid", AsyncMock(return_value="00008030-X"))
    monkeypatch.setattr(tunneld, "get_tunneld_devices", AsyncMock(return_value={
        "00008030-X": [{"tunnel-address": "fd2b::1", "tunnel-port": 63372}]}))
    ticks = iter([0.0, 2.0])
    monkeypatch.setattr(performance, "_clock", lambda: next(ticks))
    monkeypatch.setattr(performance, "_pmd3", lambda: SimpleNamespace(
        RemoteServiceDiscoveryService=lambda a: FakeRsd(a, knobs.connect_error),
        DvtProvider=FakeService,
        ProcessControl=lambda dvt: FakeService(pid=knobs.pid, error=knobs.pc_error),
        Sysmontap=FakeSysmontap.with_samples(knobs.samples),
        Graphics=FakeGraphics([{"CoreAnimationFramesPerSecond": 0},
                               {"CoreAnimationFramesPerSecond": 30}])))
    return knobs


class TestDevice:
    async def test_a_running_app(self, phone):
        snap = await performance.device_snapshot("B34C4EE9-0000", "com.x", 1.0)
        assert snap.running and snap.pid == 583
        assert snap.cpu_percent == pytest.approx(5.0)  # 0.05 s over 1 s
        assert snap.display_fps == 30.0
        assert set(snap.unavailable) == {"frames"}
        [rsd] = FakeRsd.instances
        assert rsd.address == ("fd2b::1", 63372) and rsd.closed

    async def test_pid_zero_is_not_running(self, phone):
        phone.pid = 0
        snap = await performance.device_snapshot("B34C4EE9-0000", "com.x", 1.0)
        assert not snap.running
        assert snap.unavailable["display_fps"] == performance.NOT_RUNNING
        every_null_has_a_reason(snap)
        assert FakeRsd.instances[0].closed

    async def test_a_failure_still_closes_the_tunnel_connection(self, phone):
        phone.pc_error = ConnectionResetError("peer reset")
        with pytest.raises(DeviceError, match="peer reset"):
            await performance.device_snapshot("B34C4EE9-0000", "com.x", 1.0)
        assert FakeRsd.instances[0].closed

    async def test_a_connect_failure_names_itself(self, phone):
        phone.connect_error = ConnectionRefusedError("refused")
        with pytest.raises(DeviceError, match="refused"):
            await performance.device_snapshot("B34C4EE9-0000", "com.x", 1.0)

    async def test_a_library_timeout_is_not_reported_as_ours(self, phone):
        phone.connect_error = TimeoutError("handshake")
        with pytest.raises(DeviceError, match="timed out") as e:
            await performance.device_snapshot("B34C4EE9-0000", "com.x", 1.0)
        assert "did not answer within" not in str(e.value)

    async def test_our_deadline_says_so(self, phone, monkeypatch):
        monkeypatch.setattr(performance, "_ANSWER_SLACK_S", 0.0)

        async def hang():
            await asyncio.Event().wait()

        phone.connect_error = None
        monkeypatch.setattr(FakeRsd, "connect", lambda self: hang())
        with pytest.raises(DeviceError, match="did not answer within 0.2s"):
            await performance.device_snapshot("B34C4EE9-0000", "com.x", 0.2)
        assert FakeRsd.instances[0].closed

    async def test_no_tunnel_is_an_error_naming_the_fix(self, monkeypatch):
        from server.device.ios import pmd3, tunneld

        monkeypatch.setattr(tunneld, "is_tunneld_running", AsyncMock(return_value=False))
        monkeypatch.setattr(pmd3, "_recover_tunneld_if_wedged", AsyncMock(return_value=False))
        with pytest.raises(DeviceError, match="tunneld is not serving"):
            await performance.device_snapshot("B34C4EE9-0000", "com.x", 1.0)

    async def test_a_tunnel_entry_without_an_address_is_no_tunnel(self, phone, monkeypatch):
        from server.device.ios import tunneld

        monkeypatch.setattr(tunneld, "get_tunneld_devices", AsyncMock(return_value={
            "00008030-X": [{"tunnel-address": "fd2b::1"}]}))
        with pytest.raises(DeviceError, match="no tunnel"):
            await performance.device_snapshot("B34C4EE9-0000", "com.x", 1.0)
        assert FakeRsd.instances == []


# -- controller --------------------------------------------------------------

class TestDispatch:
    @pytest.mark.parametrize("device_type,target", [
        (DeviceType.SIMULATOR, "simulator_snapshot"),
        (DeviceType.DEVICE, "device_snapshot"),
        (DeviceType.ANDROID_EMULATOR, "android_snapshot"),
        (DeviceType.ANDROID_DEVICE, "android_snapshot"),
    ])
    async def test_each_kind_of_device(self, monkeypatch, device_type, target):
        ctrl = DeviceController()
        ctrl._device_type_cache["U-1"] = device_type
        called = AsyncMock(return_value="snap")
        monkeypatch.setattr(performance, target, called)
        assert await ctrl.get_performance("com.x", udid="U-1", sample_seconds=2.0) == "snap"
        assert called.await_args.args[-3:] == ("U-1", "com.x", 2.0)

    async def test_asking_does_not_change_the_active_device(self, monkeypatch):
        ctrl = DeviceController()
        ctrl._device_type_cache["U-1"] = DeviceType.SIMULATOR
        ctrl._active_udid = "OTHER"
        monkeypatch.setattr(performance, "simulator_snapshot", AsyncMock(return_value="snap"))
        await ctrl.get_performance("com.x", udid="U-1")
        assert ctrl._active_udid == "OTHER"

    async def test_an_unknown_kind_of_device_is_refused_not_guessed(self, monkeypatch):
        ctrl = DeviceController()
        monkeypatch.setattr(ctrl, "resolve_udid", AsyncMock(return_value="U-9"))
        sim = AsyncMock()
        monkeypatch.setattr(performance, "simulator_snapshot", sim)
        with pytest.raises(DeviceError, match="could not tell"):
            await ctrl.get_performance("com.x", udid="U-9")
        sim.assert_not_awaited()

    async def test_a_shell_shaped_bundle_id_is_refused_before_any_device(self, monkeypatch):
        ctrl = DeviceController()
        resolve = AsyncMock()
        monkeypatch.setattr(ctrl, "resolve_udid", resolve)
        with pytest.raises(InvalidDeviceRequestError):
            await ctrl.get_performance("com.x;reboot", udid="U-1")
        resolve.assert_not_awaited()


# -- route -------------------------------------------------------------------

@pytest.fixture
def app():
    return create_app(config=ServerConfig(api_key="test-key-12345"),
                      enable_oslog=False, enable_crash=False, enable_proxy=False)


@pytest.fixture
def client(app):
    ctrl = DeviceController()
    ctrl.get_performance = AsyncMock(return_value=PerformanceSnapshot(
        udid="U-1", bundle_id="com.x", platform="android", running=True, pid=7,
        sample_seconds=1.0, cpu_percent=3.5, unavailable={"display_fps": "iOS devices only"}))
    app.state.device_controller = ctrl
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test",
                       headers={"Authorization": "Bearer test-key-12345"}), ctrl


class TestRoute:
    async def test_passes_the_request_through(self, client):
        http, ctrl = client
        async with http:
            r = await http.get("/api/v1/device/app/performance",
                               params={"bundle_id": "com.x", "udid": "U-1", "sample_seconds": 2})
        assert r.status_code == 200
        assert r.json()["cpu_percent"] == 3.5
        ctrl.get_performance.assert_awaited_once_with("com.x", udid="U-1", sample_seconds=2.0)

    @pytest.mark.parametrize("params", [
        {},
        {"bundle_id": ""},
        {"bundle_id": "com.x", "sample_seconds": 0.1},
        {"bundle_id": "com.x", "sample_seconds": 6},
    ])
    async def test_refuses_what_it_cannot_measure(self, client, params):
        http, ctrl = client
        async with http:
            r = await http.get("/api/v1/device/app/performance", params=params)
        assert r.status_code == 422
        ctrl.get_performance.assert_not_awaited()

    async def test_a_bad_bundle_id_is_a_400(self, client):
        http, ctrl = client
        ctrl.get_performance.side_effect = InvalidDeviceRequestError("not a bundle id")
        async with http:
            r = await http.get("/api/v1/device/app/performance", params={"bundle_id": "a;b"})
        assert r.status_code == 400

    async def test_a_device_error_is_mapped(self, client):
        http, ctrl = client
        ctrl.get_performance.side_effect = DeviceError(
            "no tunnel to B34C", tool="pymobiledevice3")
        async with http:
            r = await http.get("/api/v1/device/app/performance", params={"bundle_id": "com.x"})
        assert r.status_code == 500
        assert "no tunnel" in r.json()["detail"]
