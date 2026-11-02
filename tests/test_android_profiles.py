"""Android profiles (#245, phase 4): perfetto on the device, trace_processor
fetched on first use, and the summary with and without it.

adb is a scripted fake that answers as perfetto was measured to on a Pixel 3
XL: `--detach` exits 0, `--is_detached` 0 while running and 2 once over,
`--attach --stop` 0, or 2 for a session over -- or busy with another attach.
trace_processor's answers are its real CSV output for each summary query,
captured from a live hang profile (tests/fixtures/profiles/android-*.csv).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.models import LogEntry, LogLevel, LogSource
from server.profiling import android as ad
from server.profiling import manager as manager_mod
from server.profiling import trace_processor as tpm
from server.profiling.manager import ProfileError, ProfileManager
from server.profiling.trace_processor import Status, TraceProcessor, parse_csv

FIXTURES = Path(__file__).parent / "fixtures" / "profiles"
SERIAL = "8BAY0WCL7"
PKG = "com.quern.probe"


def csv_fixture(name: str) -> str:
    return (FIXTURES / f"android-{name}.csv").read_text()


# ── the fake adb ─────────────────────────────────────────────────────────────

class FakeAdb:
    """perfetto on a device, as measured: one detached session per key."""

    def __init__(self):
        self.sessions: dict[str, bool] = {}       # key -> running
        self.calls: list[str] = []
        self.busy_stops = 0                        # stops refused as busy before one lands
        self.start_rc = 0
        self.start_said = "perfetto_cmd.cc:658 Connected to the Perfetto traced service, TTL: 60s"
        self.confirm = True                        # does --is_detached see it after start
        self.pull_rc = 0
        self.pull_bytes = b"trace" * 100
        self.unreachable = False
        self.in_flight = 0
        self.overlapped = False
        self.pid = "4242"
        self.ticks = [100, 160]
        self.frames = [100, 190]
        #: The trace files on the device: made by --detach, removed by rm, and
        #: a pull of one not there fails -- as adb does. Not modelling it hid
        #: two findings (review).
        self.remote: set[str] = set()
        self.clock_offset = 0.0

    async def __call__(self, serial, *args, stdin=None, timeout=30.0):
        if self.unreachable:
            raise OSError("adb: device offline")
        cmd = " ".join(args)
        self.calls.append(cmd)
        self.in_flight += 1
        if self.in_flight > 1:
            self.overlapped = True
        try:
            await asyncio.sleep(0.01)
            return self._answer(args, cmd, stdin)
        finally:
            self.in_flight -= 1

    def _answer(self, args, cmd, stdin):
        if "--detach=" in cmd:
            key = cmd.split("--detach=")[1].split()[0]
            assert stdin and b"duration_ms" in stdin
            if self.start_rc == 0:
                self.sessions[key] = self.confirm
                self.remote.add(cmd.split(" -o ")[1].strip())
            return self.start_rc, self.start_said, ""
        if "--is_detached=" in cmd:
            key = cmd.split("--is_detached=")[1].split()[0]
            return (0 if self.sessions.get(key) else 2), "", ""
        if "--attach=" in cmd:
            key = cmd.split("--attach=")[1].split()[0]
            if self.sessions.get(key) and self.busy_stops:
                self.busy_stops -= 1
                return 2, "", "Session re-attach failed"
            if self.sessions.get(key):
                self.sessions[key] = False
                return 0, "Trace written into the output file", ""
            return 2, "", "Session re-attach failed"
        if args[0] == "pull":
            if self.pull_rc != 0 or args[1] not in self.remote:
                return 1, "", f"adb: error: failed to stat remote object '{args[1]}'"
            Path(args[2]).write_bytes(self.pull_bytes)
            return 0, "", ""
        if cmd == "shell date +%s.%N":
            import time
            return 0, f"{time.time() + self.clock_offset:.9f}\n", ""
        if "pidof" in cmd:
            return (0, self.pid + "\n", "") if self.pid else (1, "", "")
        if "/proc/uptime" in cmd:
            t = self.ticks.pop(0) if self.ticks else 0
            stat = f"{self.pid} (com.quern.probe) S " + " ".join(["0"] * 10) + f" {t} 0"
            return 0, f"{1000 + len(self.ticks) * -10 + 10.0} 0\n{stat}\n", ""
        if "gfxinfo" in cmd:
            total = self.frames.pop(0) if self.frames else 0
            return 0, (f"** Graphics info for pid {self.pid} [com.quern.probe] **\n"
                       f"Total frames rendered: {total}\nJanky frames: 3 (3.00%)\n"), ""
        if cmd.startswith("shell rm"):
            self.remote.discard(cmd.split("rm -f ")[1].strip())
            return 0, "", ""
        raise AssertionError(f"unexpected adb call: {cmd}")


class FakeTP:
    """trace_processor with its real answers, by query."""

    def __init__(self, state="installed", fail: set[str] = frozenset()):
        self._state = state
        self.fail = fail
        self.ensured = 0

    def status(self):
        return Status(self._state)

    def ensure(self):
        self.ensured += 1
        return self.status()

    async def wait(self, timeout):
        return self.status()

    async def query(self, trace, sql):
        assert "json_group_array" in sql, sql          # one query, one load of the trace
        if self.fail:
            raise RuntimeError("trace_processor failed (exit -11): no output")
        return parse_csv(csv_fixture("summary"))


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(manager_mod.config_mod, "CONFIG_DIR", tmp_path)
    return tmp_path


def _manager(adb, tp=None, logged=None):
    return ProfileManager(settings=lambda: {"max_bytes": 2048 * 2**20}, adb=adb,
                          trace_processor=tp or FakeTP(), logged_hangs=logged)


async def _start(m, preset="hangs", limit=30):
    return await m.start(udid=SERIAL, platform="android", device=SERIAL, bundle_id=PKG,
                         preset=preset, time_limit_s=limit)


async def _done(profile, timeout=10):
    async with asyncio.timeout(timeout):
        await profile.done.wait()
    return profile


# ── perfetto's config and the device ─────────────────────────────────────────

class TestConfig:
    @pytest.mark.parametrize("preset", sorted(ad.PRESETS))
    def test_every_preset_is_a_config_perfetto_can_parse(self, preset):
        """Measured: one brace short, perfetto said "The trace config is
        invalid, bailing out" and nothing recorded."""
        text = ad.config_text(ad.PRESETS[preset], PKG, 45)
        assert text.count("{") == text.count("}")
        assert "duration_ms: 45000" in text and f'atrace_apps: "{PKG}"' in text
        assert "write_into_file: true" in text            # streamed, not a ring buffer

    def test_the_hangs_preset_records_input(self):
        assert "input" in ad.PRESETS["hangs"].atrace and "input" not in ad.PRESETS["cpu"].atrace


class TestDevice:
    async def test_a_start_is_confirmed_running(self, tmp_path):
        adb = FakeAdb()
        await ad.Device(adb, SERIAL).start("prof_x", PKG, ad.PRESETS["hangs"], 30,
                                           tmp_path / "perfetto.log")
        assert adb.calls[-1] == "shell perfetto --is_detached=quern_prof_x"
        assert "exit 0" in (tmp_path / "perfetto.log").read_text()

    async def test_a_refused_config_says_perfettos_words(self, tmp_path):
        adb = FakeAdb()
        adb.start_rc = 1
        adb.start_said = "perfetto_cmd.cc:485 The trace config is invalid, bailing out."
        with pytest.raises(ad.StartFailed, match="config is invalid"):
            await ad.Device(adb, SERIAL).start("prof_x", PKG, ad.PRESETS["hangs"], 30,
                                               tmp_path / "log")

    async def test_said_started_and_not_running_is_a_failure(self, tmp_path):
        adb = FakeAdb()
        adb.confirm = False
        with pytest.raises(ad.StartFailed, match="no session is running"):
            await ad.Device(adb, SERIAL).start("prof_x", PKG, ad.PRESETS["hangs"], 30,
                                               tmp_path / "log")

    async def test_alive(self):
        adb = FakeAdb()
        d = ad.Device(adb, SERIAL)
        adb.sessions["quern_a"] = True
        assert await d.alive("a") is True and await d.alive("b") is False
        adb.unreachable = True
        assert await d.alive("a") is None             # could not ask is not over

    async def test_a_stop_refused_as_busy_is_tried_again_not_read_as_over(self):
        """Measured: --attach --stop exits 2 while another client is attached,
        the same as for a session that is over."""
        adb = FakeAdb()
        adb.sessions["quern_a"] = True
        adb.busy_stops = 2
        assert await ad.Device(adb, SERIAL).stop("a") == "sent"
        assert adb.sessions["quern_a"] is False

    async def test_a_stop_of_a_session_over_is_gone(self):
        assert await ad.Device(FakeAdb(), SERIAL).stop("a") == "gone"

    async def test_a_stop_that_cannot_land_is_unknown(self):
        adb = FakeAdb()
        adb.sessions["quern_a"] = True
        adb.busy_stops = 99
        assert await ad.Device(adb, SERIAL).stop("a") == "unknown"
        adb.unreachable = True
        assert await ad.Device(adb, SERIAL).stop("a") == "unknown"

    async def test_a_sessions_calls_take_turns(self):
        """The race measured live: the watch's --is_detached attached while
        the stop's --attach ran, and the stop read as over."""
        adb = FakeAdb()
        adb.sessions["quern_a"] = True
        d = ad.Device(adb, SERIAL)
        await asyncio.gather(*(d.alive("a") for _ in range(5)), d.stop("a"))
        assert not adb.overlapped

    async def test_pull(self, tmp_path):
        adb = FakeAdb()
        adb.remote.add(ad.remote_trace("a"))
        await ad.Device(adb, SERIAL).pull("a", tmp_path / "t.pftrace")
        assert not (tmp_path / "t.pftrace.part").exists() and not adb.remote
        assert (tmp_path / "t.pftrace").stat().st_size > 0
        assert adb.calls[-1].startswith("shell rm -f /data/misc/perfetto-traces/quern-a")

    @pytest.mark.parametrize("rc,data", [(1, b"x"), (0, b"")])
    async def test_a_pull_that_brought_nothing_fails(self, tmp_path, rc, data):
        adb = FakeAdb()
        adb.remote.add(ad.remote_trace("a"))
        adb.pull_rc, adb.pull_bytes = rc, data
        with pytest.raises(ad.PullFailed, match="still on the device"):
            await ad.Device(adb, SERIAL).pull("a", tmp_path / "t.pftrace")
        assert not any(c.startswith("shell rm") for c in adb.calls)   # kept on the device
        assert not (tmp_path / "t.pftrace").exists() and not (tmp_path / "t.pftrace.part").exists()

    async def test_could_not_tell_after_an_exit_2_is_unknown_not_gone(self):
        """A stop is "gone" only when the session is: one the device could not
        say about is not (mutation survived, review)."""
        adb = FakeAdb()
        adb.sessions["quern_a"] = True
        adb.busy_stops = 1
        real = adb._answer

        def answer(args, cmd, stdin):
            if "--is_detached=" in cmd:
                return 255, "", "error: device offline"
            return real(args, cmd, stdin)
        adb._answer = answer
        assert await ad.Device(adb, SERIAL).stop("a") == "unknown"

    async def test_a_start_that_could_not_be_confirmed_is_a_failure(self, tmp_path):
        adb = FakeAdb()
        real = adb._answer

        def answer(args, cmd, stdin):
            if "--is_detached=" in cmd:
                return 255, "", "error: closed"
            return real(args, cmd, stdin)
        adb._answer = answer
        with pytest.raises(ad.StartFailed, match="could not be told"):
            await ad.Device(adb, SERIAL).start("p", PKG, ad.PRESETS["hangs"], 30,
                                               tmp_path / "log")

    async def test_pidof_failing_is_not_not_running(self):
        adb = FakeAdb()
        real = adb._answer

        def answer(args, cmd, stdin):
            if "pidof" in cmd:
                return 127, "", "/system/bin/sh: pidof: not found"
            return real(args, cmd, stdin)
        adb._answer = answer
        snap = await ad.Device(adb, SERIAL).snapshot(PKG)
        assert "could not say whether the app was running" in snap["unavailable"]["cpu"]

    async def test_the_device_clock_is_measured(self):
        adb = FakeAdb()
        adb.clock_offset = -20.0
        snap = await ad.Device(adb, SERIAL).snapshot(PKG)
        assert snap["clock_offset_s"] == pytest.approx(-20.0, abs=0.5)

    async def test_a_snapshot_of_an_app_not_running(self):
        adb = FakeAdb()
        adb.pid = ""
        snap = await ad.Device(adb, SERIAL).snapshot(PKG)
        assert snap["pid"] is None and snap["unavailable"]["cpu"] == "the app was not running"


# ── the summary ──────────────────────────────────────────────────────────────

def _decoded():
    [row] = parse_csv(csv_fixture("summary"))
    return ad.decode_summary_row(row)


class TestReducers:
    def test_the_hang_from_a_live_trace(self):
        d = _decoded()
        [hang] = ad.hangs_from_counters(d["wq"], PKG, d["start_ts"], d["end_ts"])
        assert hang["duration_ms"] == pytest.approx(10635.4, abs=0.1)
        assert hang["window"].startswith("com.quern.probe/") and "note" not in hang

    def test_a_hang_with_more_input_queued_is_still_one_hang(self):
        """A busy window is sent more events, so its queue climbs during one
        hang; judged a value at a time, a 600 ms hang vanished (review)."""
        w = "wq:2 com.quern.probe/x.Main"
        wq = [[w, 0, 1], [w, 200_000_000, 2], [w, 400_000_000, 3], [w, 600_000_000, 0]]
        [hang] = ad.hangs_from_counters(wq, PKG, 0, 10**10)
        assert hang["duration_ms"] == 600.0 and hang["start_s"] == 0.0

    def test_other_windows_and_short_waits_are_not_the_apps_hangs(self):
        wq = [["wq:1 com.other/x.Main", 0, 1], ["wq:1 com.other/x.Main", 9e9, 0],
              ["wq:2 com.quern.probe/x.Main", 0, 1], ["wq:2 com.quern.probe/x.Main", 2e8, 0]]
        assert ad.hangs_from_counters(wq, PKG, 0, 10**10) == []

    def test_a_cut_window_name_matches_and_says_it_may_be_another(self):
        """Cut, the name also fits a debug build beside the release one."""
        long_pkg = "com.example.averyverylongapplication"
        track = "wq:670dd51 com.example.averyverylongapp"     # cut, as the measured one: 39
        assert len(track) == 39
        assert ad.window_matches(track, long_pkg) == (True, True)
        [hang] = ad.hangs_from_counters([[track, 0, 1], [track, 10**9, 0]], long_pkg, 0, 10**10)
        assert "another app whose package begins" in hang["note"]

    def test_a_short_cut_name_is_not_trusted(self):
        # 39 characters and more, but too little of the package left to tell by.
        track = "wq:670dd51f0a1b2c3d4e5f6a7b8c9d0e1 com.e"
        assert len(track) >= 39
        assert ad.window_matches(track, "com.example.app") == (False, False)

    def test_a_hang_still_open_at_the_end_says_so(self):
        w = "wq:2 com.quern.probe/x"
        [hang] = ad.hangs_from_counters([[w, 1e9, 1]], PKG, 0, 3e9)
        assert hang["start_s"] == 1.0 and hang["duration_ms"] == 2000.0
        assert "still unanswered" in hang["note"]

    def test_frames(self):
        f = ad.frames_from_durations(_decoded()["frames"])
        assert f["count"] == 12 and f["source"] == "trace" and f["over_budget"] == 0
        assert ad.frames_from_durations([]) is None

    def test_cpu(self):
        d = _decoded()
        c = ad.cpu_from_threads(d["cpu"], d["end_ts"] - d["start_ts"])
        assert c["threads"][0]["thread"] == "RenderThread" and c["cpu_ms"] > 0

    def test_snapshots(self):
        first = {"pid": 1, "ticks": 100, "uptime": 10.0,
                 "frames": {"total_frames": 50, "janky_frames": 2, "janky_method": "legacy"}}
        last = {"pid": 1, "ticks": 150, "uptime": 20.0,
                "frames": {"total_frames": 80, "janky_frames": 5, "janky_method": "legacy"}}
        assert ad.cpu_from_snapshots(first, last)[0]["cpu_percent"] == 5.0
        assert ad.frames_from_snapshots(first, last)[0]["janky"] == 3
        assert "restarted" in ad.cpu_from_snapshots(first, {**last, "pid": 2})[1]
        restarted = {**last, "pid": 2}
        assert ad.frames_from_snapshots(first, restarted)[0] is None
        assert "restarted" in ad.frames_from_snapshots(first, restarted)[1]
        reset = {**last, "frames": {**last["frames"], "total_frames": 10}}
        assert "reset" in ad.frames_from_snapshots(first, reset)[1]
        two_ways = {**last, "frames": {**last["frames"], "janky_method": "deadline"}}
        assert ad.frames_from_snapshots(first, two_ways)[0]["janky"] is None


async def _summary(tp, logged=(None, "logcat was not captured"), preset="hangs"):
    first = {"pid": 1, "ticks": 100, "uptime": 10.0,
             "frames": {"total_frames": 50, "janky_frames": 2, "janky_method": "legacy"}}
    last = {"pid": 1, "ticks": 150, "uptime": 20.0,
            "frames": {"total_frames": 80, "janky_frames": 5, "janky_method": "legacy"}}
    return await ad.summarize(preset=preset, package=PKG, trace=Path("/t"), first=first,
                              last=last, tp=tp, tp_status=tp.status().as_dict(),
                              logged_hangs=logged)


class TestSummarize:
    async def test_with_trace_processor_it_reads_the_trace(self):
        s = await _summary(FakeTP())
        assert s["hangs_count"] == 1 and s["hangs_source"].startswith("trace")
        assert s["frames"]["source"] == "trace" and s["cpu"]["source"] == "trace"
        assert len(s["slowest_slices"]) == 10 and s["unavailable"] == {}

    @pytest.mark.parametrize("tp_state", ["missing", "downloading", "failed", "disabled",
                                          "unsupported"])
    async def test_without_it_the_device_answers_and_says_why(self, tp_state):
        logged = ([{"start_s": 1.0, "duration_ms": 10649.0}], None)
        s = await _summary(FakeTP(tp_state), logged)
        assert s["hangs_source"].startswith("logcat") and s["hangs_count"] == 1
        assert s["frames"]["source"] == "gfxinfo" and s["cpu"]["source"] == "/proc"
        assert s["slowest_slices"] is None and "trace_processor" in s["unavailable"]
        assert s["trace_processor"]["state"] == tp_state

    async def test_no_source_for_hangs_is_said_not_empty(self):
        s = await _summary(FakeTP("disabled"))
        assert s["hangs"] is None and s["unavailable"]["hangs"] == "logcat was not captured"

    async def test_trace_processor_failing_falls_back_and_says_so(self):
        """Its failing is not the trace being bad: the device's readings stand
        in, and the profile is still a result (review)."""
        logged = ([{"start_s": 1.0, "duration_ms": 10649.0}], None)
        s = await _summary(FakeTP(fail={"summary"}), logged)
        assert "could not read the trace" in s["unavailable"]["trace_processor"]
        assert "trace" not in s["unavailable"]
        assert s["frames"]["source"] == "gfxinfo" and s["hangs_source"].startswith("logcat")

    async def test_a_trace_with_no_input_queues_is_not_no_hangs(self, monkeypatch):
        """No wq: counter for any window means the trace did not record them
        -- a later Android may name them otherwise -- not that there were no
        hangs (CodeRabbit). The logcat reports stand in when there are some."""
        real = ad.decode_summary_row
        monkeypatch.setattr(ad, "decode_summary_row", lambda row: {**real(row), "wq": []})
        s = await _summary(FakeTP())
        assert s["hangs"] is None and "wait-queue counters" in s["unavailable"]["hangs"]
        logged = ([{"start_s": 1.0, "duration_ms": 3000.0}], None)
        s = await _summary(FakeTP(), logged)
        assert s["hangs_source"].startswith("logcat") and "hangs" not in s["unavailable"]

    async def test_input_queues_with_none_from_the_app_are_no_hangs(self, monkeypatch):
        real = ad.decode_summary_row
        other = "wq:1 StatusBar (server)"
        monkeypatch.setattr(ad, "decode_summary_row", lambda row: {
            **real(row), "wq": [[other, 0, 1], [other, 5, 0]]})
        s = await _summary(FakeTP())
        assert s["hangs"] == [] and s["hangs_count"] == 0       # recorded, and none

    async def test_a_limit_on_the_logged_hangs_is_said_beside_them(self):
        logged = ([{"start_s": 9.0, "duration_ms": 3000.0}],
                  "logcat capture began 5s into the profile; hangs before then are not known")
        s = await _summary(FakeTP("disabled"), logged)
        assert s["hangs_count"] == 1 and "5s into the profile" in s["hangs_note"]

    async def test_the_cpu_preset_has_no_input_so_hangs_come_from_logcat(self):
        logged = ([{"start_s": 1.0, "duration_ms": 3000.0}], None)
        s = await _summary(FakeTP(), logged, preset="cpu")
        assert s["hangs_source"].startswith("logcat")


# ── trace_processor's download ───────────────────────────────────────────────

BINARY = b"#!/bin/sh\necho fake\n"


@pytest.fixture
def pinned(state, monkeypatch):
    """A manifest pinning the fake binary, on a platform it has an entry for."""
    sha = hashlib.sha256(BINARY).hexdigest()
    monkeypatch.setattr(tpm, "MANIFEST", {("testos", "arm"): ("https://example.invalid/tp", sha,
                                                              len(BINARY))})
    monkeypatch.setattr(tpm.config_mod, "read_user_config", lambda: {})
    return sha


def _tp(fetch):
    return TraceProcessor(fetch=fetch, platform_key=lambda: ("testos", "arm"))


class TestDownload:
    async def test_first_use_downloads_verifies_and_installs(self, pinned):
        fetched = []

        def fetch(url, dest, timeout):
            fetched.append(url)
            dest.write_bytes(BINARY)
        tp = _tp(fetch)
        assert tp.status().state == "missing"
        assert tp.ensure().state == "downloading"
        assert (await tp.wait(5)).state == "installed"
        assert fetched == ["https://example.invalid/tp"]
        assert os.access(tpm.binary_path(), os.X_OK)
        assert tp.ensure().state == "installed" and len(fetched) == 1     # once only

    async def test_a_checksum_that_differs_is_deleted_and_said(self, pinned):
        def fetch(url, dest, timeout):
            dest.write_bytes(b"something else")
        tp = _tp(fetch)
        tp.ensure()
        status = await tp.wait(5)
        assert status.state == "failed" and "SHA-256" in status.detail
        assert not tpm.binary_path().exists()
        assert list(tpm.binary_path().parent.iterdir()) == []         # no temporary left

    async def test_a_failed_download_is_said_and_tried_again_next_time(self, pinned):
        calls = []

        def fetch(url, dest, timeout):
            calls.append(1)
            if len(calls) == 1:
                raise OSError("connection reset")
            dest.write_bytes(BINARY)
        tp = _tp(fetch)
        tp.ensure()
        assert "connection reset" in (await tp.wait(5)).detail
        tp.ensure()
        assert (await tp.wait(5)).state == "installed"

    async def test_off_is_off_even_when_installed(self, pinned, monkeypatch):
        """Measured live: with it turned off, an installed copy was still used."""
        tpm.binary_path().parent.mkdir(parents=True)
        tpm.binary_path().write_bytes(BINARY)
        tpm.binary_path().chmod(0o755)
        monkeypatch.setattr(tpm.config_mod, "read_user_config",
                            lambda: {"profiles": {"trace_processor": False}})
        tp = _tp(lambda *a: pytest.fail("downloaded while off"))
        assert tp.ensure().state == "disabled"
        with pytest.raises(RuntimeError, match="disabled"):
            await tp.query(Path("/t"), "select 1")

    @pytest.mark.parametrize("value", ["false", 0, None, "off"])
    def test_only_a_literal_false_turns_it_off(self, pinned, monkeypatch, value):
        monkeypatch.setattr(tpm.config_mod, "read_user_config",
                            lambda: {"profiles": {"trace_processor": value}})
        assert tpm.enabled()

    def test_a_platform_without_a_build_is_said(self, pinned):
        tp = TraceProcessor(fetch=lambda *a: None, platform_key=lambda: ("plan9", "mips"))
        assert tp.ensure().state == "unsupported"

    async def test_a_query_runs_the_binary_and_reads_its_csv(self, pinned, tmp_path):
        tpm.binary_path().parent.mkdir(parents=True)
        tpm.binary_path().write_text(
            "#!/bin/sh\necho 'Trace loaded: 1 MB' >&2\nprintf '\"n\",\"name\"\\n3,\"a,b\"\\n'\n")
        tpm.binary_path().chmod(0o755)
        rows = await _tp(lambda *a: None).query(tmp_path / "t", "select 1")
        assert rows == [{"n": 3, "name": "a,b"}]

    async def test_a_query_that_fails_says_trace_processors_words(self, pinned, tmp_path):
        tpm.binary_path().parent.mkdir(parents=True)
        tpm.binary_path().write_text("#!/bin/sh\necho 'no such table: wq' >&2\nexit 1\n")
        tpm.binary_path().chmod(0o755)
        with pytest.raises(RuntimeError, match="no such table"):
            await _tp(lambda *a: None).query(tmp_path / "t", "select 1")

    async def test_a_binary_that_crashes_is_removed_and_fetched_again(self, pinned, tmp_path):
        """A broken copy read as installed for good, failing every profile
        (review): one that crashes and will not even say its version goes."""
        tpm.binary_path().parent.mkdir(parents=True)
        tpm.binary_path().write_text("#!/bin/sh\nkill -SEGV $$\n")
        tpm.binary_path().chmod(0o755)
        fetched = []

        def fetch(url, dest, timeout):
            fetched.append(url)
            dest.write_bytes(BINARY)
        tp = _tp(fetch)
        with pytest.raises(RuntimeError, match="exit -11"):
            await tp.query(tmp_path / "t", "select 1")
        status = tp.status()
        assert status.state == "failed" and "would not run" in status.detail
        assert not tpm.binary_path().exists()
        tp.ensure()
        assert (await tp.wait(5)).state == "installed" and len(fetched) == 1

    async def test_a_query_error_is_not_a_broken_binary(self, pinned, tmp_path):
        tpm.binary_path().parent.mkdir(parents=True)
        tpm.binary_path().write_text(
            '#!/bin/sh\n[ "$1" = --version ] && exit 0\necho "no such table: wq" >&2\nexit 1\n')
        tpm.binary_path().chmod(0o755)
        tp = _tp(lambda *a: None)
        with pytest.raises(RuntimeError, match="no such table"):
            await tp.query(tmp_path / "t", "select 1")
        assert tp.status().state == "installed"

    async def test_a_query_that_does_not_answer_says_how_long_it_waited(self, pinned, tmp_path,
                                                                        monkeypatch):
        monkeypatch.setattr(tpm, "QUERY_TIMEOUT_S", 0.3)
        tpm.binary_path().parent.mkdir(parents=True)
        # exec: a sleep left as the shell's child would hold the pipes open.
        tpm.binary_path().write_text("#!/bin/sh\nexec sleep 30\n")
        tpm.binary_path().chmod(0o755)
        with pytest.raises(RuntimeError, match="did not answer within 0s"):
            await _tp(lambda *a: None).query(tmp_path / "t", "select 1")

    async def test_a_query_leaves_no_sql_file(self, pinned, tmp_path, monkeypatch):
        import tempfile
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
        (tmp_path / "tmp").mkdir()
        tpm.binary_path().parent.mkdir(parents=True)
        tpm.binary_path().write_text("#!/bin/sh\nprintf '\"n\"\\n1\\n'\n")
        tpm.binary_path().chmod(0o755)
        await _tp(lambda *a: None).query(tmp_path / "t", "select 1")
        assert list((tmp_path / "tmp").iterdir()) == []

    async def test_a_download_cut_off_by_a_restart_is_swept(self, pinned):
        tpm.binary_path().parent.mkdir(parents=True)
        stale = tpm.binary_path().parent / ".trace_processor-abc123"
        stale.write_bytes(b"half")

        def fetch(url, dest, timeout):
            dest.write_bytes(BINARY)
        tp = _tp(fetch)
        tp.ensure()
        await tp.wait(5)
        assert not stale.exists()

    def test_the_real_manifest_pins_every_platform(self):
        for (_system, _machine), (url, sha, size) in tpm.MANIFEST.items():
            assert url.startswith("https://commondatastorage.googleapis.com/perfetto-luci")
            assert f"/{tpm.VERSION}/" in url and len(sha) == 64 and size > 10_000_000


class TestParseCsv:
    def test_nulls_numbers_and_quoted_text(self):
        rows = parse_csv('"a","b","c"\n1,[NULL],"x"\n2.5,3,"y,z"\n')
        assert rows == [{"a": 1, "b": None, "c": "x"}, {"a": 2.5, "b": 3, "c": "y,z"}]

    def test_nothing(self):
        assert parse_csv("") == [] and parse_csv("Query executed in 0 ms\n") == []


# ── the manager ──────────────────────────────────────────────────────────────

class TestManager:
    async def test_start_stop_summary(self, state):
        adb, tp = FakeAdb(), FakeTP()
        m = _manager(adb, tp)
        profile, _ = await _start(m, limit=60)
        assert profile.state == "recording" and tp.ensured == 1
        assert json.loads(profile.start_file.read_text())["pid"] == 4242
        async with asyncio.timeout(10):
            profile = await m.stop(profile.id)
        assert (profile.state, profile.stop_reason) == ("finished", "stopped")
        s = profile.summary()
        assert s["platform"] == "android" and s["hangs_count"] == 1
        assert profile.view()["files"] == ["summary.json", "perfetto.log", "profile.pftrace"]
        assert profile.trace.read_bytes() == adb.pull_bytes
        assert SERIAL not in m._active and profile.id not in m._devices

    async def test_the_time_limit_ends_it(self, state):
        adb = FakeAdb()
        m = _manager(adb)
        profile, _ = await _start(m)
        adb.sessions[ad.session_key(profile.id)] = False          # perfetto's own limit
        await _done(profile)
        assert (profile.state, profile.stop_reason) == ("finished", "time_limit")

    async def test_a_stop_while_the_watch_polls_lands(self, state):
        """Live: the stop got exit 2 from the race and the profile recorded on
        to its 60 s limit."""
        adb = FakeAdb()
        m = _manager(adb)
        profile, _ = await _start(m, limit=60)
        adb.busy_stops = 1                    # one refusal, as the race gave
        async with asyncio.timeout(10):
            profile = await m.stop(profile.id)
        assert profile.stop_reason == "stopped"

    async def test_a_restart_during_the_summary_keeps_the_pulled_trace(self, state):
        """The trace was pulled and removed from the device, then quern
        stopped mid-summary; the next one pulled again, failed, and withheld a
        whole trace (review)."""
        adb = FakeAdb()
        m = _manager(adb)
        profile, _ = await _start(m)
        await m.shutdown()
        device = m._device(profile) if profile.id in m._devices else ad.Device(adb, SERIAL)
        adb.sessions[ad.session_key(profile.id)] = False
        await device.pull(profile.id, profile.trace)          # as the first finish did
        profile.state = "finalizing"
        await m._save()
        m2 = _manager(adb)
        await m2.resume()
        again = await _done(m2.get(profile.id))
        assert again.state == "finished", again.error
        assert "profile.pftrace" in again.view()["files"]

    async def test_trace_processor_failing_still_finishes_with_the_trace(self, state):
        m = _manager(FakeAdb(), FakeTP(fail={"summary"}))
        profile, _ = await _start(m)
        async with asyncio.timeout(10):
            profile = await m.stop(profile.id)
        assert profile.state == "finished" and "profile.pftrace" in profile.view()["files"]
        assert "trace_processor" in profile.summary()["unavailable"]

    async def test_a_trace_that_could_not_be_copied_fails_the_profile(self, state):
        adb = FakeAdb()
        adb.pull_rc = 1
        m = _manager(adb)
        profile, _ = await _start(m)
        async with asyncio.timeout(10):
            profile = await m.stop(profile.id)
        assert profile.state == "failed" and "could not be copied" in profile.error
        assert "profile.pftrace" not in profile.view()["files"]

    async def test_a_failed_start_leaves_nothing_and_stops_any_session(self, state):
        adb = FakeAdb()
        adb.confirm = False
        m = _manager(adb)
        with pytest.raises(ProfileError) as e:
            await _start(m)
        assert e.value.status == 502 and "no session is running" in str(e.value)
        assert m.list() == [] and SERIAL not in m._active
        assert any("--attach=" in c for c in adb.calls)            # stopped, in case
        assert [p for p in (state / "profiles").iterdir() if p.is_dir()] == []
        assert adb.remote == set() and m._devices == {}            # nothing left behind

    async def test_a_start_is_recorded_before_perfetto_is_asked(self, state):
        """As an xctrace is at spawn: killed in between, nothing would know
        the session was there (review)."""
        adb = FakeAdb()
        m = _manager(adb)
        seen = []
        real = adb._answer

        def answer(args, cmd, stdin):
            if "--detach=" in cmd:
                seen.append(json.loads((state / "profiles" / "profiles.json").read_text()))
            return real(args, cmd, stdin)
        adb._answer = answer
        profile, _ = await _start(m)
        assert [p["id"] for p in seen[0]["profiles"]] == [profile.id]
        await m.stop(profile.id)

    async def test_a_stop_that_cannot_reach_the_device_names_the_device(self, state):
        adb = FakeAdb()
        m = _manager(adb)
        profile, _ = await _start(m, limit=60)
        adb.unreachable = True
        with pytest.raises(ProfileError) as e:
            await m.request_stop(profile.id)
        said = str(e.value)
        assert e.value.status == 503 and "perfetto" in said and "xctrace" not in said
        adb.unreachable = False
        await m.stop(profile.id)

    async def test_an_overrun_whose_stop_did_not_land_is_not_killed(self, state, monkeypatch):
        monkeypatch.setattr(manager_mod, "FINALIZE_TIMEOUT_S", 0)
        adb = FakeAdb()
        adb.busy_stops = 99                            # a stop never lands
        m = _manager(adb)
        profile, _ = await _start(m, limit=1)
        await _done(profile)
        assert profile.stop_reason == "overran"

    async def test_a_resumed_profile_fetches_trace_processor_too(self, state):
        adb, tp = FakeAdb(), FakeTP()
        m = _manager(adb, tp)
        profile, _ = await _start(m, limit=60)
        await m.shutdown()
        tp2 = FakeTP()
        m2 = _manager(adb, tp2)
        await m2.resume()
        assert tp2.ensured == 1
        await m2.stop(profile.id)

    async def test_a_hand_edited_record_does_not_reach_the_device(self, state):
        adb = FakeAdb()
        m = _manager(adb)
        profile, _ = await _start(m, limit=60)
        await m.shutdown()
        raw = json.loads((state / "profiles" / "profiles.json").read_text())
        raw["profiles"][0]["bundle_id"] = "x'; drop table slice; --"
        (state / "profiles" / "profiles.json").write_text(json.dumps(raw))
        calls_before = len(adb.calls)
        m2 = _manager(adb)
        await m2.resume()
        assert m2.get(profile.id).state == "failed" and len(adb.calls) == calls_before

    async def test_a_server_without_adb_refuses(self, state):
        m = ProfileManager(settings=lambda: {"max_bytes": 2**31})
        with pytest.raises(ProfileError) as e:
            await _start(m)
        assert e.value.status == 503

    @pytest.mark.parametrize("preset", ["hitches", "launch"])
    async def test_ios_presets_are_refused_on_android(self, state, preset):
        with pytest.raises(ProfileError, match="not available on Android"):
            await _start(_manager(FakeAdb()), preset=preset)

    async def test_logged_hangs_reach_the_summary_without_trace_processor(self, state):
        seen = []

        async def logged(serial, package, started, ended, clock_offset_s):
            seen.append((serial, package))
            return [{"start_s": 2.0, "duration_ms": 4000.0}], None
        m = _manager(FakeAdb(), FakeTP("disabled"), logged)
        profile, _ = await _start(m)
        async with asyncio.timeout(10):
            profile = await m.stop(profile.id)
        assert seen == [(SERIAL, PKG)] and profile.summary()["hangs_source"].startswith("logcat")

    async def test_a_restart_adopts_a_session_still_recording(self, state):
        adb = FakeAdb()
        m = _manager(adb)
        profile, _ = await _start(m, limit=60)
        await m.shutdown()
        m2 = _manager(adb)
        assert await m2.resume() == [profile.id]
        again = m2.get(profile.id)
        assert again.state == "recording"
        async with asyncio.timeout(10):
            again = await m2.stop(profile.id)
        assert again.state == "finished" and again.stop_reason == "stopped"

    async def test_a_restart_finishes_a_session_that_ended_meanwhile(self, state):
        adb = FakeAdb()
        m = _manager(adb)
        profile, _ = await _start(m, limit=60)
        await m.shutdown()
        adb.sessions[ad.session_key(profile.id)] = False
        m2 = _manager(adb)
        await m2.resume()
        again = await _done(m2.get(profile.id))
        assert (again.state, again.stop_reason) == ("finished", "server_restart")

    async def test_ended_while_the_device_could_not_be_asked_is_still_a_restart(self, state):
        """At startup the controller -- and so adb -- comes after resume: the
        first answer, later, is what says it ended unwatched (review)."""
        adb = FakeAdb()
        m = _manager(adb)
        profile, _ = await _start(m, limit=60)
        await m.shutdown()
        adb.sessions[ad.session_key(profile.id)] = False
        adb.unreachable = True
        m2 = _manager(adb)
        await m2.resume()
        await asyncio.sleep(1.5)
        adb.unreachable = False
        again = await _done(m2.get(profile.id))
        assert again.stop_reason == "server_restart"

    async def test_a_device_that_cannot_be_asked_is_not_read_as_over(self, state, monkeypatch):
        monkeypatch.setattr(manager_mod, "FINALIZE_TIMEOUT_S", 600)
        adb = FakeAdb()
        m = _manager(adb)
        profile, _ = await _start(m, limit=60)
        adb.unreachable = True
        await asyncio.sleep(2.5)                 # the watch polls, and cannot ask
        assert profile.state == "recording"
        adb.unreachable = False
        async with asyncio.timeout(10):
            await m.stop(profile.id)


# ── InputDispatcher's reports, from the crash buffer ─────────────────────────

class TestLoggedHangs:
    T0 = datetime(2026, 11, 2, 9, tzinfo=UTC)

    def _app(self, running=True, process_filter=None, started_at=None):
        from server.storage.ring_buffer import RingBuffer
        buf = RingBuffer(max_size=100)
        adapter = SimpleNamespace(is_running=running, process_filter=process_filter,
                                  started_at=started_at or self.T0 - timedelta(minutes=5))
        state = SimpleNamespace(device_log_adapters={SERIAL: adapter}, crash_buffer=buf)
        return SimpleNamespace(state=state), buf

    def _hang(self, at, device=SERIAL, process=PKG, ms=10649):
        return LogEntry(id=f"android-hang-{at}-{device}-{process}",
                        timestamp=self.T0 + timedelta(seconds=at), device_id=device,
                        process=process, level=LogLevel.ERROR,
                        message=f"{process} hung: its main thread took {ms} ms to handle a "
                                f"MotionEvent", source=LogSource.CRASH)

    async def _read(self, app, offset=0.0, seconds=30):
        from server.main import _android_hangs
        return await _android_hangs(app, SERIAL, PKG, self.T0.isoformat(),
                                    (self.T0 + timedelta(seconds=seconds)).isoformat(), offset)

    async def test_the_apps_hangs_in_the_window(self):
        app, buf = self._app()
        for e in (self._hang(5), self._hang(6, device="OTHER"), self._hang(7, process="com.other"),
                  self._hang(99),
                  LogEntry(id="android-crash-1", timestamp=self.T0 + timedelta(seconds=8),
                           device_id=SERIAL, process=PKG, level=LogLevel.FAULT,
                           message="crashed", source=LogSource.CRASH)):
            await buf.append(e)
        hangs, why = await self._read(app)
        assert why is None and [(h["start_s"], h["duration_ms"]) for h in hangs] == [(5.0, 10649.0)]

    async def test_logcat_not_captured_is_said_not_empty(self):
        app, _ = self._app()
        app.state.device_log_adapters = {}
        hangs, why = await self._read(app)
        assert hangs is None and "start_device_logging" in why

    async def test_logcat_filtered_to_another_app_is_not_zero_hangs(self):
        """It drops this app's hang reports, so none found means nothing
        (review)."""
        app, _ = self._app(process_filter="com.other")
        hangs, why = await self._read(app)
        assert hangs is None and "filtered to 'com.other'" in why

    async def test_logcat_filtered_to_this_app_is_fine(self):
        app, buf = self._app(process_filter=PKG)
        await buf.append(self._hang(5))
        hangs, why = await self._read(app)
        assert len(hangs) == 1 and why is None

    async def test_capture_begun_late_says_what_it_missed(self):
        app, buf = self._app(started_at=self.T0 + timedelta(seconds=10))
        await buf.append(self._hang(15))
        hangs, why = await self._read(app)
        assert len(hangs) == 1 and "began 10s into the profile" in why

    async def test_capture_stopped_says_so(self):
        app, buf = self._app(running=False)
        hangs, why = await self._read(app)
        assert hangs is None and "has stopped" in why
        await buf.append(self._hang(5))
        hangs, why = await self._read(app)
        assert len(hangs) == 1 and "stopped before the profile was read" in why

    async def test_a_device_clock_behind_the_host_does_not_lose_hangs(self):
        """logcat stamps the device's time; 20 s behind the host, a hang 5 s
        in was stamped before the window and lost (review)."""
        app, buf = self._app()
        await buf.append(self._hang(5 - 20))                # device time: 20 s behind
        hangs, _why = await self._read(app, offset=-20.0)
        assert [h["start_s"] for h in hangs] == [5.0]


# ── the routes ───────────────────────────────────────────────────────────────

@pytest.fixture
def app(state):
    from unittest.mock import AsyncMock as _AsyncMock

    from server.config import ServerConfig
    from server.device.controller import DeviceController
    from server.main import create_app
    from server.models import DeviceType
    app = create_app(config=ServerConfig(api_key="test-key-12345"),
                     enable_oslog=False, enable_crash=False, enable_proxy=False)
    ctrl = DeviceController()
    ctrl.resolve_udid = _AsyncMock(side_effect=lambda udid, **kw: udid)
    ctrl._ensure_device_type_cached = _AsyncMock()
    ctrl._device_type_cache[SERIAL] = DeviceType.ANDROID_DEVICE
    app.state.device_controller = ctrl
    app.state.adb = FakeAdb()
    app.state.profiles = _manager(app.state.adb, FakeTP("downloading"))
    return app


def _client(app):
    from httpx import ASGITransport, AsyncClient
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                       headers={"Authorization": "Bearer test-key-12345"})


class TestRoutes:
    async def test_an_android_start_says_what_the_summary_will_read(self, app):
        async with _client(app) as http:
            r = await http.post("/api/v1/profiles", json={"bundle_id": PKG, "udid": SERIAL,
                                                          "preset": "frames"})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["platform"] == "android" and body["template"] == "perfetto"
            assert body["trace_processor"]["state"] == "downloading"
            notes = " ".join(body["notes"])
            assert "not relaunched" in notes and "downloading" in notes
            assert "relaunch it" not in notes.lower()          # iOS's note is not said
            pid = body["id"]
            early = await http.get(f"/api/v1/profiles/{pid}/files/profile.pftrace")
            assert early.status_code in (404, 409)             # not offered while recording
            stopped = await http.post(f"/api/v1/profiles/{pid}/stop")
            assert stopped.json()["state"] == "finished"
            trace = await http.get(f"/api/v1/profiles/{pid}/files/profile.pftrace")
            assert trace.status_code == 200 and trace.content == app.state.adb.pull_bytes
            log = await http.get(f"/api/v1/profiles/{pid}/files/perfetto.log")
            assert log.status_code == 200 and "perfetto --detach" in log.text
            zipped = await http.get(f"/api/v1/profiles/{pid}/files/profile.trace.zip")
            assert zipped.status_code == 404                   # iOS's bundle, not Android's

    async def test_a_failed_profiles_trace_is_not_served_as_a_result(self, app):
        """A trace file on disk is offered only once the profile finished."""
        async with _client(app) as http:
            pid = (await http.post("/api/v1/profiles", json={"bundle_id": PKG,
                                                              "udid": SERIAL})).json()["id"]
            await http.post(f"/api/v1/profiles/{pid}/stop")
            profile = app.state.profiles.get(pid)
            assert profile.trace.is_file()
            profile.state = "failed"
            r = await http.get(f"/api/v1/profiles/{pid}/files/profile.pftrace")
            assert r.status_code == 409 and "failed" in r.json()["detail"]

    async def test_an_ios_preset_on_android_is_refused(self, app):
        async with _client(app) as http:
            r = await http.post("/api/v1/profiles", json={"bundle_id": PKG, "udid": SERIAL,
                                                          "preset": "hitches"})
        assert r.status_code == 400 and "not available on Android" in r.json()["detail"]


class TestToolVersions:
    @pytest.mark.parametrize("available", [True, False])
    async def test_quern_fetched_trace_processor_is_never_planned_for_upgrade(self, available):
        from server.tooling.tool_updates import plan_updates
        from server.tooling.tool_versions import ToolSite
        site = ToolSite(name="trace_processor", role="cli", source="quern", available=available,
                        version=tpm.VERSION if available else None)
        [update] = await plan_updates([site], pypi=AsyncMock(), brew=AsyncMock())
        assert update.action == "unmanaged" and "quern fetches" in update.reason
        assert ("first use" in update.reason) is (not available)
        assert "quern setup" not in update.reason            # setup does not install it

    async def test_collect_sites_reports_it(self, state, monkeypatch):
        from server.tooling import tool_versions as tv
        monkeypatch.setattr(tv, "probe_version", AsyncMock(return_value=(None, None)))
        monkeypatch.setattr(tv, "_attach_brew_provenance", AsyncMock())
        monkeypatch.setattr(tpm.config_mod, "read_user_config", lambda: {})
        tpm.binary_path().parent.mkdir(parents=True)
        tpm.binary_path().write_bytes(BINARY)
        tpm.binary_path().chmod(0o755)
        [site] = [s for s in await tv.collect_sites() if s.name == "trace_processor"]
        assert site.available and site.version == tpm.VERSION and site.source == "quern"


class TestAdbRunner:
    """The real runner, against a stand-in adb: stdin, colours, and a call
    that does not answer (no test reached it before, review)."""

    @pytest.fixture
    def adb_script(self, tmp_path):
        script = tmp_path / "adb"
        script.write_text(
            "#!/bin/sh\n"
            "case \"$*\" in\n"
            "  *slow*) exec sleep 30 ;;\n"
            "  *echo-stdin*) cat ; exit 3 ;;\n"
            "  *) printf '\\033[90mperfetto_cmd.cc:658\\033[0m Connected\\n' ;;\n"
            "esac\n")
        script.chmod(0o755)
        return str(script)

    async def test_stdin_reaches_it_and_its_exit_status_comes_back(self, adb_script):
        run = ad.adb_runner(lambda: adb_script)
        rc, out, _err = await run(SERIAL, "shell", "echo-stdin", stdin=b"config text")
        assert (rc, out) == (3, "config text")

    async def test_perfettos_colours_are_stripped(self, adb_script):
        rc, out, _err = await ad.adb_runner(lambda: adb_script)(SERIAL, "shell", "x")
        assert out.strip() == "perfetto_cmd.cc:658 Connected"

    async def test_a_call_that_does_not_answer_is_ended(self, adb_script):
        with pytest.raises(TimeoutError):
            await ad.adb_runner(lambda: adb_script)(SERIAL, "shell", "slow", timeout=0.3)

    async def test_no_adb_is_an_oserror(self):
        with pytest.raises(OSError, match="adb was not found"):
            await ad.adb_runner(lambda: None)(SERIAL, "shell", "x")
