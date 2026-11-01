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
import subprocess
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
        assert summary["time_profile"]["samples"] == 150
        assert summary["unavailable"]["hitches"] == "not recorded by the Time Profiler template"
        assert "launch" not in summary          # only a launch profile is about launching
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
        async with asyncio.timeout(20):
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
        assert not list((state / "profiles").glob("prof_*"))   # nothing left behind

    async def test_xctrace_exiting_before_recording_is_a_failed_start(self, state, monkeypatch):
        monkeypatch.setenv("FAKE_XCTRACE_MODE", "exit-early")
        with pytest.raises(ProfileError, match="did not start recording"):
            await _start(_manager())

    async def test_a_failure_xctrace_reports_ends_the_start_without_waiting(self, state,
                                                                          monkeypatch):
        """xctrace can print a failure and keep running: the start must not
        wait out its whole start timeout for it to exit, and must end it."""
        monkeypatch.setenv("FAKE_XCTRACE_MODE", "error-stays")
        spawned = []
        real = asyncio.create_subprocess_exec

        async def spy(*a, **k):
            proc = await real(*a, **k)
            spawned.append(proc)
            return proc

        monkeypatch.setattr(manager_mod.asyncio, "create_subprocess_exec", spy)
        with pytest.raises(ProfileError, match="Recording failed"):
            async with asyncio.timeout(15):                 # START_TIMEOUT_S is 90
                await _start(_manager())
        assert spawned[0].returncode is not None            # ended, not left running

    async def test_a_missing_xctrace_is_an_error_not_a_crash(self, state):
        m = ProfileManager(xctrace=("/nonexistent/xctrace",),
                           settings=lambda: {"max_bytes": 2**30})
        with pytest.raises(ProfileError, match="could not be run"):
            await _start(m)
        assert SIM not in m._active

    async def test_a_crash_mid_recording_is_failed_though_the_bundle_exists(self, state,
                                                                            monkeypatch):
        """xctrace's bundle exists from the start, so a directory proves
        nothing: only "Output file saved as" says the trace is whole."""
        monkeypatch.setenv("FAKE_XCTRACE_MODE", "crash")
        profile, _ = await _start(_manager(), limit=300)
        await _finished(profile)
        assert profile.trace.is_dir()                       # there, but not a result
        assert profile.state == "failed"
        assert "did not finish saving" in profile.error
        assert "profile.trace.zip" not in profile.view()["files"]
        assert profile.summary() is None

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
        monkeypatch.setattr(manager_mod, "STOP_WAIT_S", 0.5)
        m = _manager()
        profile, _ = await _start(m, limit=300)
        with pytest.raises(ProfileError) as e:
            async with asyncio.timeout(20):
                await m.stop(profile.id)
        assert e.value.status == 504                        # said, not hung
        assert "get_profile" in str(e.value) or "Ask again" in str(e.value)
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


class TestTheSaveIsLeftAlone:
    """xctrace is saving once its time limit passes or it has had one ^C:
    it is not signalled again, and the reason it stopped is kept (review)."""

    async def test_a_stop_during_the_save_does_not_signal_it(self, state, monkeypatch):
        monkeypatch.setenv("FAKE_XCTRACE_SAVE_S", "1.5")
        m = _manager()
        profile, _ = await _start(m, limit=10)              # 0.2 s, then a 1.5 s save
        for _ in range(100):
            if profile.saving():
                break
            await asyncio.sleep(0.02)
        assert profile.saving() and profile.view()["state"] == "finalizing"
        async with asyncio.timeout(20):
            await m.stop(profile.id)
        async with asyncio.timeout(20):
            await m.stop(profile.id)
        assert not (profile.dir / "signals").exists()        # no SIGINT into the save
        assert (profile.state, profile.stop_reason) == ("finished", "time_limit")

    async def test_a_second_stop_signals_nothing_more(self, state, monkeypatch):
        monkeypatch.setenv("FAKE_XCTRACE_SAVE_S", "1.0")
        m = _manager()
        profile, _ = await _start(m, limit=300)
        first = asyncio.create_task(m.stop(profile.id))
        for _ in range(100):
            if profile.saving():
                break
            await asyncio.sleep(0.02)
        async with asyncio.timeout(20):
            await m.stop(profile.id, reason="session_ended")
        async with asyncio.timeout(20):
            await first
        assert not (profile.dir / "signals").exists()
        assert profile.stop_reason == "stopped"              # the first reason given

    async def test_a_stopped_profile_reads_as_finalizing_at_once(self, state, monkeypatch):
        """On an iPhone "Stopping recording..." came seconds before "Saving
        output file"; in between it read as recording (measured)."""
        monkeypatch.setenv("FAKE_XCTRACE_SAVE_S", "1.0")
        m = _manager()
        profile, _ = await _start(m, limit=300)
        await m.request_stop(profile.id)
        for _ in range(100):
            if "Stopping recording" in profile.log_text():
                break
            await asyncio.sleep(0.02)
        assert profile.view()["state"] == "finalizing"
        await _finished(profile)

    async def test_request_stop_does_not_wait_for_the_summary(self, state, monkeypatch):
        monkeypatch.setenv("FAKE_XCTRACE_SAVE_S", "2.0")
        m = _manager()
        profile, _ = await _start(m, limit=300)
        async with asyncio.timeout(0.5):                    # the save alone takes 2 s
            await m.request_stop(profile.id, reason="session_ended")
        assert profile.stop_reason == "session_ended"
        await _finished(profile)


class TestStartsThatDoNotHappen:
    async def test_a_cancelled_start_ends_xctrace_and_frees_the_device(self, state, monkeypatch):
        spawned = []
        real = asyncio.create_subprocess_exec

        async def spy(*a, **k):
            proc = await real(*a, **k)
            spawned.append(proc)
            return proc

        m = _manager()
        monkeypatch.setattr(manager_mod.asyncio, "create_subprocess_exec", spy)
        monkeypatch.setattr(manager_mod.xt, "STARTED_MARKER", "a marker xctrace never prints")
        task = asyncio.create_task(_start(m, limit=300))
        for _ in range(200):
            if spawned:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert spawned[0].returncode is not None             # not left recording
        assert m.list() == [] and SIM not in m._active
        assert not list((state / "profiles").glob("prof_*"))

    async def test_a_warm_up_that_never_answers_does_not_hold_the_device(self, state,
                                                                       monkeypatch):
        monkeypatch.setattr(manager_mod, "WARM_UP_TIMEOUT_S", 0.2)

        async def hang(udid):
            await asyncio.Event().wait()

        m = _manager()
        async with asyncio.timeout(10):
            profile, _ = await _start(m, udid=PHONE, platform="device", limit=10, warm_up=hang)
        await _finished(profile)

    async def test_a_refused_start_removes_nothing(self, state, monkeypatch):
        """The 507 used to come after evicting: every finished profile went,
        and the start still did not happen (review)."""
        monkeypatch.setenv("FAKE_XCTRACE_BYTES", str(3 * 2**20))
        m = _manager(max_mb=2)
        busy, _ = await _start(m, limit=300)
        old = TestDisk._old(None, state, "prof_000000000aaa", 10_000, "2026-01-01")
        m._profiles[old.id] = old
        with pytest.raises(ProfileError) as e:
            await _start(m, udid="OTHER-SIM")
        assert e.value.status == 507
        assert old.dir.exists() and old.id in m._profiles
        async with asyncio.timeout(20):
            await m.stop(busy.id)


class TestUnreadableIsNotEmpty:
    async def test_an_export_with_no_table_is_not_no_hangs(self, state, monkeypatch):
        monkeypatch.setenv("FAKE_XCTRACE_MODE", "empty=potential-hangs")
        profile, _ = await _start(_manager(), limit=10)
        await _finished(profile)
        summary = profile.summary()
        assert summary["hangs"] is None and "hangs_count" not in summary
        assert "no table" in summary["unavailable"]["hangs"]

    def test_the_reader_refuses_an_empty_export(self):
        with pytest.raises(ValueError, match="no table"):
            xt.parse_hangs("<trace-query-result></trace-query-result>")

    async def test_no_samples_says_why(self, state, monkeypatch):
        monkeypatch.setenv("FAKE_XCTRACE_MODE", "toc=sim-hangs-toc")   # names another process
        profile, _ = await _start(_manager(), limit=10)
        await _finished(profile)
        tp = profile.summary()["time_profile"]
        assert tp["samples"] == 0 and "running threads only" in tp["note"]

    async def test_an_unreadable_trace_is_failed_not_finished(self, state, monkeypatch):
        monkeypatch.setenv("FAKE_XCTRACE_MODE", "toc=does-not-exist")
        profile, _ = await _start(_manager(), limit=10)
        await _finished(profile)
        assert profile.state == "failed" and "could not be read" in profile.error


class TestOverrun:
    async def test_xctrace_that_never_finishes_is_killed_and_failed(self, state, monkeypatch):
        monkeypatch.setenv("FAKE_XCTRACE_MODE", "stubborn")
        monkeypatch.setattr(manager_mod, "FINALIZE_TIMEOUT_S", 0.0)
        real_sleep = asyncio.sleep

        async def quick(seconds):                            # the 10 s grace, made short
            await real_sleep(min(seconds, 0.05))

        monkeypatch.setattr(manager_mod.asyncio, "sleep", quick)
        profile, _ = await _start(_manager(), limit=1)
        proc = profile.proc
        await _finished(profile, timeout=30)
        assert proc.returncode == -signal.SIGKILL            # the watch killed it
        assert (profile.state, profile.stop_reason) == ("failed", "killed")
        assert "overran" in profile.error


class TestOnlyOurs:
    async def test_a_stranger_with_the_pid_is_never_signalled(self, state):
        stranger = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import signal, sys, time\n"
            "signal.signal(signal.SIGINT, lambda *_: sys.exit(42))\ntime.sleep(30)")
        try:
            profile = Profile(id="prof_00000000beef", udid="U", device="U", platform="simulator",
                              bundle_id="com.x", preset="hangs", time_limit_s=30,
                              started_at="2026-01-01", pid=stranger.pid)
            await _manager()._signal(profile, signal.SIGINT)
            await asyncio.sleep(0.3)
            assert stranger.returncode is None               # never signalled
        finally:
            stranger.kill()
            await stranger.wait()

    async def test_finishing_trims_the_other_profiles(self, state, monkeypatch):
        monkeypatch.setenv("FAKE_XCTRACE_BYTES", str(1 * 2**20))
        m = _manager(max_mb=2)
        old = TestDisk._old(None, state, "prof_000000000aaa", 2 * 2**20, "2026-01-01")
        m._profiles[old.id] = old
        monkeypatch.setattr(manager_mod.ProfileManager, "_make_room", _skip_at_start(m))
        profile, _ = await _start(m, limit=10)
        await _finished(profile)
        assert not old.dir.exists() and profile.dir.exists()


def _skip_at_start(m):
    """_make_room that does nothing when a start calls it, so only the trim
    after finishing can remove anything."""
    real = ProfileManager._make_room

    async def make_room(self, keep=None):
        if keep is None:
            return {"evicted": [], "used_bytes": 0, "budget_bytes": 0}
        return await real(self, keep=keep)
    return make_room


def _exited_pid() -> int:
    """A pid that was valid a moment ago and is not running now. (999999999
    is no use: ps calls it "too large", an error, not "no such process".)"""
    proc = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                          capture_output=True, text=True, check=True)
    return int(proc.stdout)


def _adopted(pid=None):
    """A profile as the next server sees it: a pid, no process object."""
    pid = _exited_pid() if pid is None else pid
    return Profile(id="prof_00000000c0de", udid="U", device="U", platform="simulator",
                   bundle_id="com.x", preset="hangs", time_limit_s=30,
                   started_at="2026-01-01", pid=pid)


class TestGoneIsNotUnknown:
    """CodeRabbit: a stop that sent nothing must not read as sent. Whether
    xctrace is gone and whether ps could say are different answers."""

    def test_ps_says_no_such_process_with_an_empty_line(self):
        assert manager_mod._command_of(_exited_pid()) == ""

    def test_ps_that_complains_is_could_not_ask(self, monkeypatch):
        # macOS ps says "process id too large: 999999999" and exits 1: an
        # error, not "no such process". Faked, as procps words it otherwise.
        monkeypatch.setattr(manager_mod.subprocess, "run", lambda *a, **k:
                            subprocess.CompletedProcess(a[0], 1, "", "ps: process id too large"))
        assert manager_mod._command_of(1) is None

    def test_ps_that_cannot_be_asked_is_none(self, monkeypatch):
        def broken(*a, **k):
            raise OSError("ps: not found")
        monkeypatch.setattr(manager_mod.subprocess, "run", broken)
        assert manager_mod._command_of(1) is None

    async def test_a_stop_ps_cannot_answer_is_refused_and_records_nothing(self, state,
                                                                        monkeypatch):
        m = _manager()
        profile = _adopted()
        m._profiles[profile.id] = profile
        monkeypatch.setattr(manager_mod, "_command_of", lambda pid: None)
        with pytest.raises(ProfileError) as e:
            await m.request_stop(profile.id)
        assert e.value.status == 503 and profile.stop_reason is None

    async def test_a_stop_of_an_xctrace_already_gone_records_nothing(self, state):
        m = _manager()
        profile = _adopted()                                 # no such pid
        m._profiles[profile.id] = profile
        await m.request_stop(profile.id)
        assert profile.stop_reason is None                   # the watch says how it ended

    async def test_could_not_ask_is_not_gone_to_the_watch(self, state, monkeypatch):
        monkeypatch.setattr(manager_mod, "_command_of", lambda pid: None)
        assert await _manager()._alive(_adopted()) is True

    def test_the_stop_wait_ends_before_a_client_gives_up(self):
        # Node's fetch waits about 300 s for headers; a 504 must come first.
        assert manager_mod.STOP_WAIT_S < 300


class TestTheRecordsSurvive:
    async def test_an_unreadable_record_is_kept_aside_and_its_dirs_still_count(self, state):
        root = state / "profiles"
        root.mkdir()
        (root / "profiles.json").write_text("{not json")
        orphan = root / "prof_00000000dead"
        orphan.mkdir()
        (orphan / "data").write_bytes(b"x" * (3 * 2**20))
        m = _manager(max_mb=2)
        assert await m.resume() == []
        assert list(root.glob("profiles.json.unreadable-*"))     # not overwritten
        assert await m.usage() >= 3 * 2**20                       # the orphan counts
        profile, room = await _start(m, limit=10)
        assert {"id": "prof_00000000dead", "principal": None} in room["evicted"]
        await _finished(profile)

    async def test_resume_ignores_a_record_with_no_profile_id(self, state):
        root = state / "profiles"
        root.mkdir()
        (root / "profiles.json").write_text(json.dumps({"profiles": [
            {"id": "", "udid": "U", "device": "U", "platform": "simulator", "bundle_id": "x",
             "preset": "hangs", "time_limit_s": 30, "started_at": "2026", "state": "finished"}]}))
        m = _manager()
        await m.resume()
        assert m.list() == []                     # "" would have made the root its directory

    async def test_resume_ignores_a_symlinked_directory(self, state, tmp_path_factory):
        first = _manager()
        profile, _ = await _start(first, limit=10)
        await _finished(profile)
        elsewhere = tmp_path_factory.mktemp("elsewhere")
        import shutil
        shutil.move(str(profile.dir), str(elsewhere / "moved"))
        os.symlink(elsewhere / "moved", profile.dir)
        second = _manager()
        await second.resume()
        assert second.list() == []

    async def test_a_stale_download_zip_is_swept(self, state):
        root = state / "profiles"
        root.mkdir()
        (root / ".prof_00000000aaaa.1234abcd.zip").write_bytes(b"PK")
        await _manager().resume()
        assert not list(root.glob(".prof_*.zip"))

    async def test_the_zip_leaves_out_symlinks(self, state, tmp_path_factory):
        m = _manager()
        profile, _ = await _start(m, limit=10)
        await _finished(profile)
        secret = tmp_path_factory.mktemp("secret") / "key"
        secret.write_text("not for download")
        os.symlink(secret, profile.trace / "link")
        path = await m.zip_trace(profile)
        try:
            assert zipfile.ZipFile(path).namelist() == ["profile.trace/data"]
        finally:
            os.unlink(path)


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
        # Oldest first, and only as many as needed.
        assert room["evicted"] == [{"id": "prof_a", "principal": "owner"}]
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
        async with asyncio.timeout(20):
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
        async with asyncio.timeout(20):
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
        async with asyncio.timeout(20):
            await profile.proc.wait()                           # xctrace finishes alone
        second = _manager()
        await second.resume()
        done = await _finished(second.get(profile.id))
        assert (done.state, done.stop_reason) == ("finished", "server_restart")

    async def test_a_reused_pid_is_not_adopted(self, state, tmp_path):
        first = _manager()
        profile, _ = await _start(first, limit=10)
        await first.shutdown()
        async with asyncio.timeout(20):
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
        with pytest.raises(ProfileError, match="offered once finished"):
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
        async with asyncio.timeout(20):
            await m.stop(profile.id)


class TestRacesWithEviction:
    """Review on #593: a start warming up, and a download beginning, while
    _make_room reads sizes."""

    @staticmethod
    def _shrinkable():
        """A manager whose budget can drop to nothing once a start is past it."""
        budget = {"max_bytes": 2048 * 2**20}
        return ProfileManager(xctrace=FAKE, settings=lambda: budget), budget

    async def test_a_start_still_warming_up_is_not_an_orphan(self, state):
        m, budget = self._shrinkable()
        warming, release = asyncio.Event(), asyncio.Event()

        async def warm(_udid):
            warming.set()
            await release.wait()

        task = asyncio.create_task(_start(m, udid=PHONE, platform="device", limit=10,
                                          warm_up=warm))
        async with asyncio.timeout(20):
            await warming.wait()
        budget["max_bytes"] = 0                 # every byte is over budget now
        starting = next(d for d in (state / "profiles").iterdir() if d.is_dir())
        (starting / "data").write_bytes(b"x" * 1000)
        assert starting not in m._orphans()
        await m._make_room(keep="another")      # as a finishing profile would
        assert starting.is_dir()
        release.set()
        async with asyncio.timeout(20):
            profile, _ = await task
        await _finished(profile)
        assert profile.state == "finished"

    async def test_a_download_that_begins_while_sizes_are_read_keeps_its_profile(
            self, state, monkeypatch):
        m, budget = self._shrinkable()
        profile, _ = await _start(m, limit=10)
        await _finished(profile)
        budget["max_bytes"] = 0
        real = manager_mod._size

        def size_then_a_download_begins(path):
            profile.readers = 1                 # zip_trace registers mid-scan
            return real(path)

        monkeypatch.setattr(manager_mod, "_size", size_then_a_download_begins)
        room = await m._make_room(keep="another")
        assert room["evicted"] == [] and profile.dir.is_dir() and m.get(profile.id)

    async def test_a_download_of_a_profile_just_removed_is_refused(self, state):
        m, budget = self._shrinkable()
        profile, _ = await _start(m, limit=10)
        await _finished(profile)
        budget["max_bytes"] = 0
        assert (await m._make_room(keep="another"))["evicted"] == [
            {"id": profile.id, "principal": "owner"}]
        with pytest.raises(ProfileError) as e:
            await m.zip_trace(profile)          # the record the route already held
        assert e.value.status == 404 and "removed" in str(e.value)
        assert list((state / "profiles").glob(".*.zip")) == []

    def test_a_directory_that_cannot_be_read_is_an_error_not_a_smaller_zip(self, tmp_path):
        source = tmp_path / "profile.trace"
        (source / "sub").mkdir(parents=True)
        (source / "data").write_text("x")
        (source / "sub" / "more").write_text("y")
        dest = tmp_path / "out.zip"
        (source / "sub").chmod(0)
        if os.access(source / "sub", os.R_OK):          # root reads it anyway
            (source / "sub").chmod(0o755)
            pytest.skip("this user can read a mode-0 directory")
        try:
            with pytest.raises(OSError):
                manager_mod._zip_dir(source, dest)
        finally:
            (source / "sub").chmod(0o755)
        assert not dest.exists()

    async def test_a_log_that_cannot_be_written_is_a_profile_error(self, state):
        async def block_the_log(_udid):
            for d in (state / "profiles").iterdir():
                (d / "xctrace.log").mkdir()

        with pytest.raises(ProfileError) as e:
            await _start(_manager(), udid=PHONE, platform="device", warm_up=block_the_log)
        assert e.value.status == 500 and "log" in str(e.value)
        assert [d for d in (state / "profiles").iterdir() if d.is_dir()] == []


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
                                                          "udid": SIM, "time_limit_s": 300})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["state"] == "recording" and body["template"] == "Time Profiler"
            assert any("terminates it" in n for n in body["notes"])
            assert any("still launching" in n for n in body["notes"])
            assert body["disk"]["budget_bytes"] == 2048 * 2**20     # the owner is told
            async with asyncio.timeout(20):
                stopped = await http.post(f"/api/v1/profiles/{body['id']}/stop")
            assert stopped.status_code == 200 and stopped.json()["stop_reason"] == "stopped"
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

    async def test_a_client_is_told_only_of_its_own_evictions(self, app, state, monkeypatch):
        from server import visibility
        from server.api import profiles as route
        m = _manager(max_mb=1)
        app.state.profiles = m
        for name, who in (("prof_00000000000a", "client:me"), ("prof_00000000000b", "owner")):
            old = TestDisk._old(None, state, name, 2**20, "2026-01-0" + name[-1].replace("a", "1")
                                .replace("b", "2"))
            old.principal = who
            m._profiles[name] = old
        monkeypatch.setattr(route.visibility, "current_caller",
                            lambda: visibility.Caller(session="s", principal="client:me"))
        async with _client(app) as http:
            r = await http.post("/api/v1/profiles", json={"bundle_id": "com.x", "udid": SIM,
                                                          "time_limit_s": 10})
        body = r.json()
        removal = next(n for n in body["notes"] if n.startswith("removed"))
        assert "prof_00000000000a" in removal and "prof_00000000000b" not in removal
        assert "1 of other keys'" in removal
        assert "disk" not in body                           # the server's totals are the owner's
        await _finished(m.get(body["id"]))

    async def test_a_file_that_is_a_symlink_is_not_served(self, app, tmp_path_factory):
        async with _client(app) as http:
            r = await http.post("/api/v1/profiles", json={"bundle_id": "com.x", "udid": SIM,
                                                          "time_limit_s": 10})
            profile = app.state.profiles.get(r.json()["id"])
            await _finished(profile)
            secret = tmp_path_factory.mktemp("secret") / "key"
            secret.write_text("not for download")
            profile.log.unlink()
            os.symlink(secret, profile.log)
            got = await http.get(f"/api/v1/profiles/{profile.id}/files/xctrace.log")
        assert got.status_code == 404

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
