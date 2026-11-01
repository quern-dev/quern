"""get_performance (#245): the parsers, the per-platform samplers, the
controller's dispatch and the route.

The fixtures under tests/fixtures/performance/ are real output: a Pixel 3 XL
on Android 10, an API 34 emulator (Android 14), and QuernProbe on an iOS 18.6
simulator.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from server.config import ServerConfig
from server.device import performance
from server.device.controller import DeviceController
from server.main import create_app
from server.models import DeviceError, DeviceType, PerformanceSnapshot

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

    def test_footprint_garbage_is_none(self):
        assert performance.parse_footprint_json("not json", 1) is None

    def test_proc_stat_counts_fields_after_the_command_name(self):
        # A command name with spaces and a parenthesis would shift a naive split.
        line = "42 (a b) c) S 1 1 0 0 -1 0 0 0 0 0 700 300 0 0 20 0 9 0 1 2 3"
        assert performance.parse_proc_stat_ticks(line) == 1000

    def test_android_cpu_from_measured_output(self):
        # The probe idled through the measured sample: same ticks twice.
        cpu, threads = performance.parse_android_cpu_sample(fixture("android10-stat.txt"), 6403)
        assert cpu == 0.0
        assert threads == 49

    def test_android_cpu_busy(self):
        text = ("100.00 1.00\n7 (app) S " + "0 " * 10 + "100 50 0\nThreads:\t12\n"
                "102.00 1.00\n7 (app) S " + "0 " * 10 + "250 100 0\n")
        cpu, threads = performance.parse_android_cpu_sample(text, 7)
        assert cpu == pytest.approx(100.0)  # 200 ticks = 2 s of CPU in 2 s
        assert threads == 12

    def test_android_cpu_with_one_reading_is_none(self):
        # The app died between the readings: the second cat printed nothing.
        text = "100.00 1.00\n7 (app) S " + "0 " * 10 + "100 50 0\n102.00 1.00\n"
        assert performance.parse_android_cpu_sample(text, 7) is None

    def test_meminfo_android_10(self):
        assert performance.parse_meminfo_total_pss_kb(fixture("android10-meminfo.txt")) == 90665

    def test_meminfo_android_14_spelling(self):
        assert performance.parse_meminfo_total_pss_kb(fixture("android14-meminfo.txt")) == 78690

    def test_meminfo_reads_the_summary_not_the_table(self):
        text = ("        TOTAL    11111    0\n App Summary\n               TOTAL:    22222\n")
        assert performance.parse_meminfo_total_pss_kb(text) == 22222

    def test_meminfo_without_a_summary_is_none(self):
        assert performance.parse_meminfo_total_pss_kb("No process found for: 123\n") is None

    def test_gfxinfo_android_10_has_only_the_legacy_count(self):
        stats = performance.parse_gfxinfo(fixture("android10-gfxinfo.txt"))
        assert stats.total_frames == 2637
        assert (stats.janky_frames, stats.janky_percent, stats.janky_method) == (15, 0.57, "legacy")
        assert (stats.p50_ms, stats.p90_ms, stats.p95_ms, stats.p99_ms) == (5, 5, 6, 8)

    def test_gfxinfo_android_14_reports_the_deadline_count(self):
        stats = performance.parse_gfxinfo(fixture("android14-gfxinfo.txt"))
        assert (stats.janky_frames, stats.janky_percent) == (12, 7.64)  # not legacy's 33
        assert stats.janky_method == "deadline"
        # the frame percentiles, not the "gpu percentile" lines below them
        assert (stats.p50_ms, stats.p95_ms, stats.p99_ms) == (16, 40, 1700)

    def test_gfxinfo_with_no_frames_is_none(self):
        assert performance.parse_gfxinfo("No process found for: com.x\n") is None


# -- iOS simulator -----------------------------------------------------------

class FakeHost:
    """Answers the host commands simulator_snapshot runs."""

    def __init__(self, cputimes=("0:10.00", "0:10.50"), footprint=37423696, threads=PS_M):
        self.cputimes = list(cputimes)
        self.footprint = footprint
        self.threads = threads

    async def run(self, *args, timeout):
        if args[:3] == ("ps", "-o", "time="):
            value = self.cputimes.pop(0)
            return (0, value) if value is not None else (1, "")
        if args[:2] == ("ps", "-M"):
            return (0, self.threads) if self.threads is not None else (1, "")
        if args[0] == "footprint":
            if self.footprint is None:
                return 1, ""
            if isinstance(self.footprint, Exception):
                raise self.footprint
            path = Path(args[args.index("-j") + 1])
            path.write_text(fixture("sim-footprint.json").replace("37423696", str(self.footprint)))
            return 0, ""
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


class TestSimulator:
    async def test_running_app(self, monkeypatch, no_sleep):
        host = FakeHost()
        monkeypatch.setattr(performance, "_run", host.run)
        clock = iter([100.0, 101.0])
        monkeypatch.setattr(performance, "_clock", lambda: next(clock))
        snap = await performance.simulator_snapshot(_sim(13093), "SIM", "com.x", 1.0)
        assert snap.running and snap.pid == 13093
        assert snap.cpu_percent == pytest.approx(50.0)
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

    async def test_one_failed_reading_does_not_sink_the_others(self, monkeypatch, no_sleep):
        host = FakeHost(footprint=DeviceError("footprint did not answer", tool="footprint"))
        monkeypatch.setattr(performance, "_run", host.run)
        snap = await performance.simulator_snapshot(_sim(13093), "SIM", "com.x", 1.0)
        assert snap.memory_bytes is None and snap.memory_kind is None
        assert "footprint did not answer" in snap.unavailable["memory_bytes"]
        assert snap.threads == 4 and snap.cpu_percent is not None
        every_null_has_a_reason(snap)

    async def test_unreadable_cpu_is_null_not_zero(self, monkeypatch, no_sleep):
        host = FakeHost(cputimes=("0:10.00", None))
        monkeypatch.setattr(performance, "_run", host.run)
        snap = await performance.simulator_snapshot(_sim(13093), "SIM", "com.x", 1.0)
        assert snap.cpu_percent is None
        assert snap.unavailable["cpu_percent"]


# -- Android -----------------------------------------------------------------

class FakeAdb:
    def __init__(self, pidof=(0, "6403\n", ""), stat=None, meminfo=None, gfxinfo=None):
        self.pidof = pidof
        self.stat = fixture("android10-stat.txt") if stat is None else stat
        self.meminfo = fixture("android10-meminfo.txt") if meminfo is None else meminfo
        self.gfxinfo = fixture("android10-gfxinfo.txt") if gfxinfo is None else gfxinfo
        self.calls: list[tuple] = []

    async def _run_unjudged(self, serial, *args, timeout):
        self.calls.append(args)
        if args[:2] == ("shell", "pidof"):
            return self.pidof
        if isinstance(self.stat, Exception):
            raise self.stat
        return 0, self.stat, ""

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

    async def test_meminfo_failing_leaves_the_rest(self):
        adb = FakeAdb(meminfo=DeviceError("adb shell failed: device offline", tool="adb"))
        snap = await performance.android_snapshot(adb, "S", "com.quern.probe", 1.0)
        assert snap.memory_bytes is None and snap.memory_kind is None
        assert "device offline" in snap.unavailable["memory_bytes"]
        assert snap.threads == 49 and snap.frames is not None
        every_null_has_a_reason(snap)

    async def test_app_exiting_mid_sample(self):
        adb = FakeAdb(stat="3161679.51 1.0\n3161680.62 1.0\n")
        snap = await performance.android_snapshot(adb, "S", "com.quern.probe", 1.0)
        assert snap.cpu_percent is None and snap.threads is None
        assert "has the app exited" in snap.unavailable["cpu_percent"]
        every_null_has_a_reason(snap)

    async def test_no_window_means_no_frames_with_a_reason(self):
        snap = await performance.android_snapshot(
            FakeAdb(gfxinfo="No process found\n"), "S", "com.quern.probe", 1.0)
        assert snap.frames is None and "no frames" in snap.unavailable["frames"]


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


def _proc(pid, cpu, mem=12125344, threads=3):
    return {"pid": pid, "cpuUsage": cpu, "physFootprint": mem, "threadCount": threads}


class TestDeviceSampling:
    async def test_the_first_null_cpu_sample_is_not_the_answer(self, monkeypatch):
        clock = iter([0.0, 0.5, 1.2, 1.7])
        monkeypatch.setattr(performance, "_clock", lambda: next(clock))
        sysmon = FakeSysmontap.with_samples([[_proc(9, None)], [_proc(9, 12.5)], [_proc(9, 99.0)]])
        snap = PerformanceSnapshot(udid="D", bundle_id="b", platform="ios_device",
                                   running=True, sample_seconds=1.0)
        await performance._sysmon(sysmon, None, 9, 1.0, snap)
        assert snap.cpu_percent == 12.5  # first non-null once the sample has elapsed
        assert (snap.memory_bytes, snap.memory_kind) == (12125344, "phys_footprint")
        assert snap.threads == 3

    async def test_an_app_gone_from_the_sample(self, monkeypatch):
        monkeypatch.setattr(performance, "_clock", lambda: 0.0)
        sysmon = FakeSysmontap.with_samples([[_proc(1, 3.0)]])
        snap = PerformanceSnapshot(udid="D", bundle_id="b", platform="ios_device",
                                   running=True, sample_seconds=1.0)
        await performance._sysmon(sysmon, None, 9, 1.0, snap)
        assert snap.cpu_percent is None and "has the app exited" in snap.unavailable["cpu_percent"]

    async def test_no_tunnel_is_an_error_naming_the_fix(self, monkeypatch):
        from server.device.ios import pmd3, tunneld

        monkeypatch.setattr(tunneld, "is_tunneld_running", AsyncMock(return_value=False))
        monkeypatch.setattr(pmd3, "_recover_tunneld_if_wedged", AsyncMock(return_value=False))
        with pytest.raises(DeviceError, match="tunneld is not serving"):
            await performance.device_snapshot("B34C4EE9-0000", "com.x", 1.0)


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

    async def test_a_device_error_is_mapped(self, client):
        http, ctrl = client
        ctrl.get_performance.side_effect = DeviceError("no tunnel to B34C", tool="pymobiledevice3")
        async with http:
            r = await http.get("/api/v1/device/app/performance", params={"bundle_id": "com.x"})
        assert r.status_code == 500
        assert "no tunnel" in r.json()["detail"]
