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
            if self.pull_rc == 0:
                Path(args[2]).write_bytes(self.pull_bytes)
            return self.pull_rc, "", "" if self.pull_rc == 0 else "adb: error: remote object"
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
        for name, marker in (("process", "count(*)"), ("hangs", "wq:"), ("frames", "doFrame"),
                             ("cpu", "sum(s.dur)"), ("slices", "limit 10")):
            if marker in sql:
                if name in self.fail:
                    raise RuntimeError(f"trace_processor failed: {name} broke")
                return parse_csv(csv_fixture(name))
        raise AssertionError(sql)


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
        await ad.Device(adb, SERIAL).pull("a", tmp_path / "t.pftrace")
        assert (tmp_path / "t.pftrace").stat().st_size > 0
        assert adb.calls[-1].startswith("shell rm -f /data/misc/perfetto-traces/quern-a")

    @pytest.mark.parametrize("rc,data", [(1, b"x"), (0, b"")])
    async def test_a_pull_that_brought_nothing_fails(self, tmp_path, rc, data):
        adb = FakeAdb()
        adb.pull_rc, adb.pull_bytes = rc, data
        with pytest.raises(ad.PullFailed):
            await ad.Device(adb, SERIAL).pull("a", tmp_path / "t.pftrace")
        assert not any(c.startswith("shell rm") for c in adb.calls)   # kept on the device

    async def test_a_snapshot_of_an_app_not_running(self):
        adb = FakeAdb()
        adb.pid = ""
        snap = await ad.Device(adb, SERIAL).snapshot(PKG)
        assert snap["pid"] is None and snap["unavailable"]["cpu"] == "the app was not running"


# ── the summary ──────────────────────────────────────────────────────────────

class TestReducers:
    def test_the_hang_from_a_live_trace(self):
        hangs = ad.hangs_from_counters(parse_csv(csv_fixture("hangs")), PKG)
        [hang] = hangs
        assert hang["duration_ms"] == pytest.approx(10635.4, abs=0.1)
        assert hang["window"].startswith("com.quern.probe/")

    def test_other_windows_and_short_waits_are_not_the_apps_hangs(self):
        rows = [{"track": "wq:1 com.other/x.Main", "ts": 0, "value": 1, "until": 9e9,
                 "open": 0, "start_ts": 0},
                {"track": "wq:2 com.quern.probe/x.Main", "ts": 0, "value": 1, "until": 200e6,
                 "open": 0, "start_ts": 0},
                {"track": "wq:3 com.quern.probe/x.Main", "ts": 0, "value": 0, "until": 9e9,
                 "open": 0, "start_ts": 0}]
        assert ad.hangs_from_counters(rows, PKG) == []

    def test_a_cut_window_name_still_matches_a_long_package(self):
        long_pkg = "com.example.averyverylongapplication"
        # Cut as the measured name was: "wq:670dd51 com.quern.probe/com.quern.pr" is 39.
        track = "wq:670dd51 com.example.averyverylongapp"
        assert len(track) == 39
        assert ad.window_matches(track, long_pkg)
        assert not ad.window_matches("wq:1 com.example.a", long_pkg)    # too short to trust

    def test_a_hang_still_open_at_the_end_says_so(self):
        rows = [{"track": "wq:2 com.quern.probe/x", "ts": 1e9, "value": 1, "until": 3e9,
                 "open": 1, "start_ts": 0}]
        [hang] = ad.hangs_from_counters(rows, PKG)
        assert hang["start_s"] == 1.0 and "still unanswered" in hang["note"]

    def test_frames(self):
        f = ad.frames_from_durations([r["dur"] for r in parse_csv(csv_fixture("frames"))])
        assert f["count"] == 12 and f["source"] == "trace" and f["over_budget"] == 0
        assert ad.frames_from_durations([]) is None

    def test_cpu(self):
        c = ad.cpu_from_threads(parse_csv(csv_fixture("cpu")))
        assert c["threads"][0]["thread"] == "RenderThread" and c["cpu_ms"] > 0

    def test_snapshots(self):
        first = {"pid": 1, "ticks": 100, "uptime": 10.0,
                 "frames": {"total_frames": 50, "janky_frames": 2, "janky_method": "legacy"}}
        last = {"pid": 1, "ticks": 150, "uptime": 20.0,
                "frames": {"total_frames": 80, "janky_frames": 5, "janky_method": "legacy"}}
        assert ad.cpu_from_snapshots(first, last)[0]["cpu_percent"] == 5.0
        assert ad.frames_from_snapshots(first, last)[0]["janky"] == 3
        assert "restarted" in ad.cpu_from_snapshots(first, {**last, "pid": 2})[1]
        reset = {**last, "frames": {**last["frames"], "total_frames": 10}}
        assert "reset" in ad.frames_from_snapshots(first, reset)[1]


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

    async def test_one_query_failing_does_not_sink_the_others(self):
        s = await _summary(FakeTP(fail={"frames"}))
        assert s["hangs_count"] == 1 and s["cpu"]["source"] == "trace"
        # The trace could not say; gfxinfo could, and does.
        assert s["frames"]["source"] == "gfxinfo"

    async def test_an_unreadable_trace_is_said_as_such(self):
        s = await _summary(FakeTP(fail={"process"}))
        assert "trace_processor could not read the trace" in s["unavailable"]["trace"]

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

        async def logged(serial, package, started, ended):
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
    @staticmethod
    def _app(entries, running=True):
        from server.storage.ring_buffer import RingBuffer
        buf = RingBuffer(max_size=100)
        state = SimpleNamespace(device_log_adapters={SERIAL: SimpleNamespace(is_running=running)},
                                crash_buffer=buf)
        return SimpleNamespace(state=state), buf

    async def test_the_apps_hangs_in_the_window(self):
        from server.main import _android_hangs
        t0 = datetime(2026, 11, 2, 9, tzinfo=UTC)
        app, buf = self._app([])

        def hang(at, device=SERIAL, process=PKG, ms=10649):
            return LogEntry(id=f"android-hang-{at}", timestamp=t0 + timedelta(seconds=at),
                            device_id=device, process=process, level=LogLevel.ERROR,
                            message=f"{process} hung: its main thread took {ms} ms to handle "
                                    f"a MotionEvent", source=LogSource.CRASH)
        for e in (hang(5), hang(6, device="OTHER"), hang(7, process="com.other"), hang(99),
                  LogEntry(id="android-crash-1", timestamp=t0 + timedelta(seconds=8),
                           device_id=SERIAL, process=PKG, level=LogLevel.FAULT,
                           message="crashed", source=LogSource.CRASH)):
            await buf.append(e)
        hangs, why = await _android_hangs(app, SERIAL, PKG, t0.isoformat(),
                                          (t0 + timedelta(seconds=30)).isoformat())
        assert why is None and hangs == [{"start_s": 5.0, "duration_ms": 10649.0,
                                          "message": hangs[0]["message"]}]

    async def test_logcat_not_captured_is_said_not_empty(self):
        from server.main import _android_hangs
        app, _ = self._app([], running=False)
        hangs, why = await _android_hangs(app, SERIAL, PKG, "2026-11-02T09:00:00+00:00",
                                          "2026-11-02T09:01:00+00:00")
        assert hangs is None and "start_device_logging" in why



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
