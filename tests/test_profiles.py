"""Profiles (#245 phase 3): the xctrace readers, the manager's life cycle, and
the routes.

The readers run on real exports (tests/fixtures/profiles/: an iOS 27.0
simulator and an iPhone 11 on iOS 26.7, Xcode 27.2). The manager runs a fake
xctrace as a real subprocess -- fake_xctrace.py -- so signals, time limits,
exits and adoption after a restart are the real thing, not mocks of them.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from server.config import ServerConfig
from server.device.controller import DeviceController
from server.main import create_app
from server.models import DeviceType
from server.profiling import manager as manager_mod
from server.profiling import xctrace as xt
from server.profiling.manager import Profile, ProfileError, ProfileManager

FIXTURES = Path(__file__).parent / "fixtures" / "profiles"
FAKE = (sys.executable, str(FIXTURES / "fake_xctrace.py"))
SIM = "C5D36699-A47F-4A03-8D1C-01503E10DD2F"
PHONE = "B34C4EE9-AF48-53C6-BD13-2BFA66E7EE91"


def fixture(name: str) -> str:
    return (FIXTURES / f"{name}.xml").read_text()


# -- readers -----------------------------------------------------------------

class TestReaders:
    def test_toc(self):
        toc = xt.parse_toc(fixture("sim-hangs-toc"))
        assert toc["template"] == "Time Profiler"
        assert toc["end_reason"] == "Time limit reached"
        assert toc["duration_s"] == pytest.approx(41.3, abs=0.01)
        assert toc["process"] == {"name": "QuernProbeScene", "pid": 50515, "exit": "exit(0)"}
        assert toc["device"]["platform"] == "iOS Simulator"
        assert "potential-hangs" in toc["tables"] and "hitches" not in toc["tables"]

    def test_a_toc_without_a_run_is_refused(self):
        with pytest.raises(ValueError):
            xt.parse_toc("<trace-toc/>")

    def test_hangs(self):
        hangs = xt.parse_hangs(fixture("sim-hangs-potential-hangs"))
        assert [(h["start_s"], h["duration_ms"], h["kind"]) for h in hangs] == [
            (6.979, 2646.2, "Severe Hang"), (24.726, 12045.0, "Severe Hang")]
        assert hangs[1]["thread"].startswith("Main Thread")

    def test_hitches_are_read_by_column_not_by_tag(self):
        # The explanation is a <string> cell, like two other columns: only its
        # position says it is the narrative.
        [hitch] = xt.parse_hitches(fixture("device-hitches"))
        assert hitch == {"start_s": 16.906, "duration_ms": 16.7, "process": "QuernProbe (20772)",
                         "explanation": "Potentially expensive render, 24 offscreen passes)"}

    def test_time_profile(self):
        tp = xt.parse_time_profile(fixture("device-time-profile"), "QuernProbe", top=3)
        assert tp["samples"] == 150
        assert tp["top_functions"][0] == {"function": "objc_msgSend", "binary": "libobjc.A.dylib",
                                          "samples": 19, "percent": 12.7}
        # The device container is lowercase (/private/var/containers/...).
        assert tp["top_app_functions"][0]["binary"] == "QuernProbe"

    def test_time_profile_counts_only_the_process_asked_about(self):
        assert xt.parse_time_profile(fixture("device-time-profile"), "SpringBoard")["samples"] == 0

    def test_app_functions_are_the_innermost_app_frame(self):
        """Counting every app frame put `main` at 99%; each sample counts once,
        at the app frame nearest the CPU."""
        tp = xt.parse_time_profile(fixture("device-time-profile"), "QuernProbe")
        assert sum(f["samples"] for f in tp["top_app_functions"]) <= tp["samples"]

    def test_launch_phases(self):
        launch = xt.parse_launch_phases(fixture("device-launch-life-cycle-period"))
        assert len(launch["phases"]) == 9
        assert launch["phases"][0] == {
            "start_s": 0.364, "duration_ms": 52.1,
            "phase": "Initializing - System Interface Initialization",
            "explanation": "The system frameworks took 52.07 ms to initialize."}
        # "Initial Frame Rendering" ends 814 ms after the first phase starts, plus its 67 ms.
        assert launch["first_frame_ms"] == pytest.approx(881.4)

    def test_record_command(self):
        cmd = xt.record_command(("xcrun", "xctrace"), "00008030-X", xt.PRESETS["hangs"], 30,
                                Path("/p/profile.trace"), "com.x")
        assert cmd == ["xcrun", "xctrace", "record", "--device", "00008030-X",
                       "--template", "Time Profiler", "--instrument", "Hangs",
                       "--time-limit", "30s", "--no-prompt", "--output", "/p/profile.trace",
                       "--launch", "--", "com.x"]

    def test_only_an_xctrace_recording_into_this_trace_is_ours(self):
        trace = Path("/q/profiles/prof_1/profile.trace")
        mine = f"/usr/bin/xctrace record --device X --output {trace} --launch -- com.x"
        assert manager_mod._records_into(mine, trace)
        assert not manager_mod._records_into(mine.replace("prof_1", "prof_10"), trace)
        assert not manager_mod._records_into(f"/bin/sleep --output {trace} ", trace)
        assert not manager_mod._records_into(None, trace)


# -- the manager -------------------------------------------------------------

@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(manager_mod.config_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setenv("FAKE_XCTRACE_MODE", "ok")
    monkeypatch.setenv("FAKE_XCTRACE_SCALE", "0.02")
    return tmp_path


def _manager(max_mb=2048, **kw):
    return ProfileManager(xctrace=FAKE, settings=lambda: {"max_bytes": max_mb * 2**20}, **kw)


async def _start(m, udid=SIM, platform="simulator", preset="hangs", limit=30, **kw):
    return await m.start(udid=udid, platform=platform, device=udid, bundle_id="com.quern.probe",
                         preset=preset, time_limit_s=limit, **kw)


async def _finished(profile, timeout=20):
    async with asyncio.timeout(timeout):
        await profile.done.wait()
    return profile


class TestLifeCycle:
    async def test_stop_signals_xctrace_and_summarises(self, state):
        m = _manager()
        profile, room = await _start(m, limit=300)          # 6 s at this scale
        assert profile.state == "recording" and room["evicted"] == []
        async with asyncio.timeout(20):
            profile = await m.stop(profile.id)
        assert (profile.state, profile.stop_reason) == ("finished", "stopped")
        summary = profile.summary()
        assert summary["hangs_count"] == 2 and summary["hangs"][1]["duration_ms"] == 12045.0
        # The simulator TOC names QuernProbeScene; the samples are QuernProbe's.
        assert summary["time_profile"]["samples"] == 0
        assert summary["unavailable"]["hitches"] == "not recorded by the Time Profiler template"
        assert summary["launch"] is None and "launch" not in summary["unavailable"]
        assert SIM not in m._active

    async def test_the_time_limit_ends_it_by_itself(self, state):
        m = _manager()
        profile, _ = await _start(m, limit=10)              # 0.2 s
        await _finished(profile)
        assert (profile.state, profile.stop_reason) == ("finished", "time_limit")

    async def test_one_profile_per_device(self, state):
        m = _manager()
        first, _ = await _start(m, limit=300)
        with pytest.raises(ProfileError) as e:
            await _start(m)
        assert e.value.status == 409 and first.id in str(e.value)
        await m.stop(first.id)
        again, _ = await _start(m, limit=10)                # free once finished
        await _finished(again)

    async def test_hitches_are_refused_on_a_simulator_before_launching(self, state):
        m = _manager()
        with pytest.raises(ProfileError, match="does not run on simulators"):
            await _start(m, preset="hitches")
        assert not (state / "profiles").exists() or not any((state / "profiles").iterdir())
        assert SIM not in m._active

    async def test_a_device_xctrace_calls_offline_says_why(self, state, monkeypatch):
        monkeypatch.setenv("FAKE_XCTRACE_MODE", "offline")
        m = _manager()
        with pytest.raises(ProfileError) as e:
            await _start(m, udid=PHONE, platform="device")
        assert e.value.status == 502
        assert "Timed out waiting for device to boot" in str(e.value)
        assert "CoreDevice tunnel" in str(e.value)
        assert m.list() == [] and PHONE not in m._active
        assert not any((state / "profiles").iterdir())   # nothing left behind

    async def test_xctrace_exiting_before_recording_is_a_failed_start(self, state, monkeypatch):
        monkeypatch.setenv("FAKE_XCTRACE_MODE", "exit-early")
        with pytest.raises(ProfileError, match="did not start recording"):
            await _start(_manager())

    async def test_a_missing_xctrace_is_an_error_not_a_crash(self, state):
        m = ProfileManager(xctrace=("/nonexistent/xctrace",),
                           settings=lambda: {"max_bytes": 2**30})
        with pytest.raises(ProfileError, match="could not be run"):
            await _start(m)
        assert SIM not in m._active

    async def test_no_trace_left_is_a_failure_with_the_log(self, state, monkeypatch):
        monkeypatch.setenv("FAKE_XCTRACE_MODE", "no-trace")
        profile, _ = await _start(_manager(), limit=10)
        await _finished(profile)
        assert profile.state == "failed"
        assert "Recording completed" in profile.error   # the log's own last words

    async def test_one_unreadable_table_leaves_the_others(self, state, monkeypatch):
        monkeypatch.setenv("FAKE_XCTRACE_MODE", "fail=potential-hangs")
        profile, _ = await _start(_manager(), limit=10)
        await _finished(profile)
        summary = profile.summary()
        assert summary["hangs"] is None
        assert "potential-hangs is damaged" in summary["unavailable"]["hangs"]
        assert summary["time_profile"] is not None

    async def test_a_launch_profile_reports_its_phases(self, state):
        profile, _ = await _start(_manager(), udid=PHONE, platform="device", preset="launch",
                                  limit=10)
        await _finished(profile)
        launch = profile.summary()["launch"]
        assert len(launch["phases"]) == 9 and launch["first_frame_ms"] == pytest.approx(881.4)

    async def test_a_simulator_launch_with_no_phases_says_so(self, state, monkeypatch):
        monkeypatch.setenv("FAKE_XCTRACE_MODE", "sim-launch")
        profile, _ = await _start(_manager(), preset="launch", limit=10)
        await _finished(profile)
        summary = profile.summary()
        assert summary["launch"] is None                    # not an empty success
        assert "simulators record none" in summary["unavailable"]["launch"]

    async def test_xctrace_that_ignores_sigint_is_not_waited_on_forever(self, state, monkeypatch):
        monkeypatch.setenv("FAKE_XCTRACE_MODE", "stubborn")
        monkeypatch.setattr(manager_mod, "FINALIZE_TIMEOUT_S", 0.5)
        monkeypatch.setattr(manager_mod, "EXPORT_TIMEOUT_S", 0.5)
        m = _manager()
        profile, _ = await _start(m, limit=300)
        with pytest.raises(ProfileError) as e:
            await m.stop(profile.id)
        assert e.value.status == 504                        # said, not hung
        profile.proc.kill()
        await _finished(profile)

    async def test_the_physical_device_tunnel_is_warmed_first(self, state):
        warm = AsyncMock()
        profile, _ = await _start(_manager(), udid=PHONE, platform="device", limit=10,
                                  warm_up=warm)
        warm.assert_awaited_once_with(PHONE)
        await _finished(profile)

    async def test_a_simulator_is_not_warmed(self, state):
        warm = AsyncMock()
        profile, _ = await _start(_manager(), limit=10, warm_up=warm)
        warm.assert_not_awaited()
        await _finished(profile)

    async def test_a_failing_warm_up_still_tries(self, state):
        warm = AsyncMock(side_effect=RuntimeError("devicectl: no such device"))
        profile, _ = await _start(_manager(), udid=PHONE, platform="device", limit=10,
                                  warm_up=warm)
        await _finished(profile)
        assert profile.state == "finished"


class TestDisk:
    def _old(self, state, name, size, when):
        d = state / "profiles" / name
        d.mkdir(parents=True)
        (d / "data").write_bytes(b"x" * size)
        return Profile(id=name, udid="OLD", device="OLD", platform="simulator",
                       bundle_id="com.x", preset="hangs", time_limit_s=30, started_at=when,
                       state="finished")

    async def test_the_oldest_finished_make_room(self, state):
        m = _manager(max_mb=2)
        for name, when in (("prof_a", "2026-01-01"), ("prof_b", "2026-01-02")):
            m._profiles[name] = self._old(state, name, 1_500_000, when)
        profile, room = await _start(m, limit=10)
        assert room["evicted"] == ["prof_a"]                # oldest first, and only as needed
        assert not (state / "profiles" / "prof_a").exists()
        assert (state / "profiles" / "prof_b").exists()
        await _finished(profile)

    async def test_profiles_still_recording_are_never_removed(self, state, monkeypatch):
        monkeypatch.setenv("FAKE_XCTRACE_BYTES", str(3 * 2**20))
        m = _manager(max_mb=2)
        busy, _ = await _start(m, limit=300)
        with pytest.raises(ProfileError) as e:
            await _start(m, udid="OTHER-SIM")
        assert e.value.status == 507 and busy.dir.exists()
        await m.stop(busy.id)

    async def test_a_finished_profile_trims_the_others_but_not_itself(self, state, monkeypatch):
        monkeypatch.setenv("FAKE_XCTRACE_BYTES", str(3 * 2**20))
        m = _manager(max_mb=2)
        profile, _ = await _start(m, limit=10)
        await _finished(profile)
        assert profile.dir.exists() and m.get(profile.id)


class TestRestart:
    async def test_shutdown_leaves_xctrace_recording(self, state):
        m = _manager()
        profile, _ = await _start(m, limit=300)
        await m.shutdown()
        assert profile.proc.returncode is None              # still recording
        profile.proc.send_signal(signal.SIGINT)
        await profile.proc.wait()

    async def test_the_next_server_adopts_a_recording_xctrace(self, state):
        first = _manager()
        profile, _ = await _start(first, limit=300)
        pid = profile.pid
        await first.shutdown()
        second = _manager()
        assert await second.resume() == [profile.id]
        adopted = second.get(profile.id)
        assert adopted.state == "recording" and adopted.proc is None and adopted.pid == pid
        assert profile.udid in second._active
        async with asyncio.timeout(20):
            done = await second.stop(profile.id)          # signals the adopted pid
        assert (done.state, done.stop_reason) == ("finished", "stopped")
        assert done.summary()["hangs_count"] == 2

    async def test_one_that_finished_unwatched_is_summarised(self, state):
        first = _manager()
        profile, _ = await _start(first, limit=10)
        await first.shutdown()                              # stop watching at once
        await profile.proc.wait()                           # xctrace finishes alone
        second = _manager()
        await second.resume()
        done = await _finished(second.get(profile.id))
        assert (done.state, done.stop_reason) == ("finished", "server_restart")

    async def test_a_reused_pid_is_not_adopted(self, state, tmp_path):
        first = _manager()
        profile, _ = await _start(first, limit=10)
        await first.shutdown()
        await profile.proc.wait()
        stranger = await asyncio.create_subprocess_exec(sys.executable, "-c",
                                                        "import time; time.sleep(30)")
        record = json.loads((state / "profiles" / "profiles.json").read_text())
        record["profiles"][0]["pid"] = stranger.pid
        (state / "profiles" / "profiles.json").write_text(json.dumps(record))
        try:
            second = _manager()
            await second.resume()
            done = await _finished(second.get(profile.id))
            assert done.state == "finished"                 # not waiting on the stranger
            assert stranger.returncode is None              # and never signalled
        finally:
            stranger.kill()
            await stranger.wait()

    async def test_a_profile_whose_directory_is_gone_is_dropped(self, state):
        first = _manager()
        profile, _ = await _start(first, limit=10)
        await _finished(profile)
        import shutil
        shutil.rmtree(profile.dir)
        second = _manager()
        await second.resume()
        assert second.list() == []


class TestFiles:
    async def test_the_trace_zips_as_its_bundle(self, state):
        m = _manager()
        profile, _ = await _start(m, limit=10)
        with pytest.raises(ProfileError, match="still recording"):
            await m.zip_trace(profile)
        await _finished(profile)
        assert "profile.trace.zip" in profile.view()["files"]
        path = await m.zip_trace(profile)
        try:
            assert zipfile.ZipFile(path).namelist() == ["profile.trace/data"]
        finally:
            os.unlink(path)

    async def test_the_zip_is_not_offered_while_recording(self, state):
        m = _manager()
        profile, _ = await _start(m, limit=300)
        assert "profile.trace.zip" not in profile.view()["files"]
        await m.stop(profile.id)


# -- routes ------------------------------------------------------------------

@pytest.fixture
def app(state):
    app = create_app(config=ServerConfig(api_key="test-key-12345"),
                     enable_oslog=False, enable_crash=False, enable_proxy=False)
    ctrl = DeviceController()
    ctrl.resolve_udid = AsyncMock(side_effect=lambda udid, **kw: udid or SIM)
    ctrl._ensure_device_type_cached = AsyncMock()
    ctrl._device_type_cache.update({SIM: DeviceType.SIMULATOR, PHONE: DeviceType.DEVICE,
                                    "emulator-5554": DeviceType.ANDROID_EMULATOR})
    ctrl.devicectl._run_devicectl = AsyncMock(return_value=("", ""))
    app.state.device_controller = ctrl
    app.state.profiles = _manager()
    return app


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                       headers={"Authorization": "Bearer test-key-12345"})


class TestRoutes:
    async def test_start_says_the_app_is_relaunched_and_still_launching(self, app):
        async with _client(app) as http:
            r = await http.post("/api/v1/profiles", json={"bundle_id": "com.quern.probe",
                                                          "udid": SIM, "time_limit_s": 10})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["state"] == "recording" and body["template"] == "Time Profiler"
            assert any("terminates it" in n for n in body["notes"])
            assert any("still launching" in n for n in body["notes"])
            await _finished(app.state.profiles.get(body["id"]))
            got = (await http.get(f"/api/v1/profiles/{body['id']}")).json()
            assert got["state"] == "finished" and got["summary"]["hangs_count"] == 2

    async def test_a_physical_device_is_recorded_by_its_hardware_udid(self, app, monkeypatch):
        from server.api import profiles as route
        monkeypatch.setattr(route, "spellings_of", lambda u: [u, "00008030-000C59623A69802E"])
        async with _client(app) as http:
            r = await http.post("/api/v1/profiles", json={"bundle_id": "com.x", "udid": PHONE,
                                                          "time_limit_s": 10})
        profile = app.state.profiles.get(r.json()["id"])
        assert profile.device == "00008030-000C59623A69802E" and profile.platform == "device"
        app.state.device_controller.devicectl._run_devicectl.assert_awaited()   # warmed
        await _finished(profile)

    @pytest.mark.parametrize("body,status", [
        ({"bundle_id": "com.x", "udid": "emulator-5554"}, 400),    # Android: not yet
        ({"bundle_id": "com.x;reboot", "udid": SIM}, 422),         # never reaches a shell
        ({"bundle_id": "com.x", "udid": SIM, "preset": "nope"}, 422),
        ({"bundle_id": "com.x", "udid": SIM, "time_limit_s": 301}, 422),
        ({"bundle_id": "com.x", "udid": SIM, "preset": "hitches"}, 400),
    ])
    async def test_refusals(self, app, body, status):
        async with _client(app) as http:
            r = await http.post("/api/v1/profiles", json=body)
        assert r.status_code == status, r.text
        assert app.state.profiles.list() == []

    async def test_files_are_named_ones_only(self, app):
        async with _client(app) as http:
            r = await http.post("/api/v1/profiles", json={"bundle_id": "com.x", "udid": SIM,
                                                          "time_limit_s": 10})
            pid = r.json()["id"]
            await _finished(app.state.profiles.get(pid))
            summary = await http.get(f"/api/v1/profiles/{pid}/files/summary.json")
            assert summary.status_code == 200
            other = await http.get(f"/api/v1/profiles/{pid}/files/profiles.json")
            assert other.status_code == 404
            z = await http.get(f"/api/v1/profiles/{pid}/files/profile.trace.zip")
            assert z.status_code == 200 and z.content[:2] == b"PK"
        leftovers = [p for p in (manager_mod._root()).iterdir() if p.name.endswith(".zip")]
        assert leftovers == []                               # the temporary zip is removed

    async def test_an_unknown_or_malformed_id_is_404(self, app):
        async with _client(app) as http:
            for path in ("/api/v1/profiles/prof_000000000000", "/api/v1/profiles/..%2Fx",
                         "/api/v1/profiles/prof_000000000000/files/summary.json"):
                assert (await http.get(path)).status_code == 404

    async def test_another_principals_profile_reads_as_missing(self, app, monkeypatch):
        async with _client(app) as http:
            r = await http.post("/api/v1/profiles", json={"bundle_id": "com.x", "udid": SIM,
                                                          "time_limit_s": 10})
            profile = app.state.profiles.get(r.json()["id"])
            await _finished(profile)
            profile.principal = "client:someone-else"
            from server import visibility
            from server.api import profiles as route
            monkeypatch.setattr(route.visibility, "current_caller",
                                lambda: visibility.Caller(session="s", principal="client:me"))
            assert (await http.get(f"/api/v1/profiles/{profile.id}")).status_code == 404
            listed = (await http.get("/api/v1/profiles")).json()["profiles"]
            assert listed == []
