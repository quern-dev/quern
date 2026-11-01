"""Gauges on recordings (#245, phase 2): the summariser, the sampler a
recording runs, and the trace that reads it -- live and from the file.

The gauge source is a fake fed from a queue, so each test says exactly when a
sample arrives and what it holds; the device side is tested on its own below
with pymobiledevice3 and the backends faked.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from server import gauges as gauges_mod
from server import logging_ext
from server.device import performance
from server.gauges import Sample, parse_sample, summarise
from server.models import DeviceError, InvalidDeviceRequestError, PerformanceSnapshot
from server.recording import recorder as rec_mod
from server.recording.recorder import Filters, RecordingError, RecordingManager
from tests.test_recording import SIM, Sources, _action, _app, _events, _now, _settle

T0 = datetime(2026, 11, 1, 12, tzinfo=UTC)


def _s(start_s: float, end_s: float, **kw) -> Sample:
    kw.setdefault("running", True)
    return Sample(start=T0 + timedelta(seconds=start_s), end=T0 + timedelta(seconds=end_s), **kw)


def _summary(samples, start_s=10.0, end_s=11.0, grace_s=3.0, **kw):
    return summarise(samples, T0 + timedelta(seconds=start_s), T0 + timedelta(seconds=end_s),
                     grace=timedelta(seconds=grace_s), **kw)


# ── the summariser ───────────────────────────────────────────────────────────

class TestSummarise:
    def test_peaks_over_the_samples_overlapping_the_action_and_its_grace(self):
        samples = [
            _s(8, 9, cpu_percent=99.0, memory_bytes=999),          # before: not in it
            _s(9, 10.5, cpu_percent=20.0, memory_bytes=100, threads=10),
            _s(10.5, 12, cpu_percent=80.0, memory_bytes=300, threads=14),
            _s(12, 14, cpu_percent=40.0, memory_bytes=200, threads=12),   # in the grace
            _s(14.5, 16, cpu_percent=99.0, memory_bytes=999),      # after the grace
        ]
        g = _summary(samples)
        assert g["samples"] == 3
        assert g["cpu_percent"] == {"peak": 80.0, "mean": 46.7}
        assert g["memory_bytes"] == {"peak": 300, "first": 100, "last": 200}
        assert g["threads_peak"] == 14
        assert g["sample_seconds"] == 2.0
        assert g["window"] == {"from": (T0 + timedelta(seconds=10)).isoformat(),
                               "to": (T0 + timedelta(seconds=14)).isoformat()}
        assert g["covered"]["from"] == (T0 + timedelta(seconds=9)).isoformat()

    def test_zero_is_a_reading(self):
        g = _summary([_s(9, 12, cpu_percent=0.0, memory_bytes=0, threads=0),
                      _s(12, 15, cpu_percent=0.0, memory_bytes=0, threads=0)])
        assert g["cpu_percent"] == {"peak": 0.0, "mean": 0.0}
        assert g["memory_bytes"]["peak"] == 0 and g["threads_peak"] == 0
        assert "cpu_percent" not in g["unavailable"]

    def test_no_sample_covering_it_is_could_not_tell_with_the_reason(self):
        g = _summary([_s(0, 1, cpu_percent=5.0)], uncovered="the recording stopped")
        assert g["samples"] == 0 and g["cpu_percent"] is None and g["memory_bytes"] is None
        assert set(g["unavailable"]) == set(gauges_mod.METRICS)
        assert all(v == "the recording stopped" for v in g["unavailable"].values())
        assert _summary([])["unavailable"]["cpu_percent"] == "no gauge sample overlaps this action"

    def test_a_null_says_why_each_reason_once(self):
        nope = {"cpu_percent": "the app is not running", "memory_bytes": "the app is not running"}
        g = _summary([_s(9, 12, running=False, unavailable=nope),
                      _s(12, 15, running=False, unavailable=nope),
                      _s(12, 15, running=None, error="device offline")])
        assert g["cpu_percent"] is None
        assert g["unavailable"]["cpu_percent"] == "the app is not running; device offline"

    def test_partial_coverage_and_a_part_not_running_are_caveats(self):
        g = _summary([_s(11, 12, cpu_percent=5.0, running=True),
                      _s(12, 13, running=False)])
        assert any("do not span the whole window" in c for c in g["caveats"])
        assert any("not running for part" in c for c in g["caveats"])
        whole = _summary([_s(9, 12, cpu_percent=5.0), _s(12, 15, cpu_percent=6.0)])
        assert whole["caveats"] == []

    def test_display_fps(self):
        g = _summary([_s(9, 12, display_fps=60.0), _s(12, 15, display_fps=42.5)])
        assert g["display_fps"] == {"min": 42.5, "mean": 51.2}


class TestFrames:
    @staticmethod
    def _f(total, janky=None, method="deadline"):
        return {"total_frames": total, "janky_frames": janky, "janky_method": method}

    def test_frames_are_the_difference_from_the_last_reading_before(self):
        g = _summary([_s(6, 9, frames=self._f(100, 5)),       # the baseline, before
                      _s(9, 12, frames=self._f(130, 8)),
                      _s(12, 15, frames=self._f(190, 20))])
        assert g["frames"]["total"] == 90 and g["frames"]["janky"] == 15
        assert g["frames"]["janky_method"] == "deadline" and "note" not in g["frames"]

    def test_without_a_reading_before_it_counts_from_the_first_and_says_so(self):
        g = _summary([_s(9, 12, frames=self._f(130, 8)), _s(12, 15, frames=self._f(190, 20))])
        assert g["frames"]["total"] == 60 and "not included" in g["frames"]["note"]

    def test_one_reading_is_not_a_difference(self):
        g = _summary([_s(9, 15, frames=self._f(130, 8))])
        assert g["frames"] is None and "need two" in g["unavailable"]["frames"]

    def test_a_count_that_goes_down_means_nothing(self):
        g = _summary([_s(6, 9, frames=self._f(500)), _s(9, 15, frames=self._f(20))])
        assert g["frames"] is None and "restarted" in g["unavailable"]["frames"]

    def test_janky_counted_two_ways_is_not_subtracted(self):
        g = _summary([_s(6, 9, frames=self._f(100, 5, "legacy")),
                      _s(9, 15, frames=self._f(160, 9, "deadline"))])
        assert g["frames"]["total"] == 60 and g["frames"]["janky"] is None

    def test_a_platform_without_frames_says_so(self):
        g = _summary([_s(9, 15, unavailable={"frames": "Android only"})])
        assert g["unavailable"]["frames"] == "Android only"


class TestParse:
    def test_round_trip(self):
        line = gauges_mod.sample_line(
            {"running": True, "cpu_percent": 12.5, "memory_bytes": 10, "threads": 3,
             "frames": None, "display_fps": None, "unavailable": {"frames": "Android only"},
             "udid": "not kept"}, T0, T0 + timedelta(seconds=1))
        assert "udid" not in line
        s = parse_sample(json.loads(json.dumps(line)))
        assert (s.cpu_percent, s.memory_bytes, s.threads, s.running) == (12.5, 10, 3, True)
        assert s.unavailable == {"frames": "Android only"} and s.seconds == 1.0

    @pytest.mark.parametrize("data", [None, "x", {}, {"from": "nope", "to": "nope"},
                                      {"from": T0.isoformat()},
                                      {"from": (T0 + timedelta(seconds=1)).isoformat(),
                                       "to": T0.isoformat()}])
    def test_junk_is_not_a_sample(self, data):
        assert parse_sample(data) is None

    def test_a_bool_is_not_a_reading(self):
        s = parse_sample({"from": T0.isoformat(), "to": T0.isoformat(), "cpu_percent": True,
                          "running": "yes"})
        assert s.cpu_percent is None and s.running is None

    def test_a_failed_line_covers_its_window_and_names_every_metric(self):
        s = parse_sample(gauges_mod.failed_line("offline", T0, T0 + timedelta(seconds=4)))
        assert s.error == "offline" and s.running is None
        assert set(s.unavailable) == set(gauges_mod.METRICS)


# ── the recording's sampler ──────────────────────────────────────────────────

class FakeGauges:
    """A gauge source fed by the test: `push` a snapshot dict, or an
    exception to raise from the stream."""

    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue()
        self.calls: list[tuple] = []
        self.closed = 0
        self.saw_action: list = []

    def __call__(self, udid, app, interval):
        self.calls.append((udid, app, interval))
        return self._stream()

    async def _stream(self):
        try:
            while True:
                item = await self.queue.get()
                self.saw_action.append(logging_ext._CURRENT.get())
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            self.closed += 1

    def push(self, **snap):
        snap.setdefault("running", True)
        snap.setdefault("sample_seconds", 2.0)
        snap.setdefault("unavailable", {})
        self.queue.put_nowait(snap)


def _manager(src: Sources, gauges=None) -> RecordingManager:
    return RecordingManager(server_buffer=src.server, ring_buffer=src.ring,
                            crash_buffer=src.crash, flow_store=src.flows,
                            ip_map=lambda: {}, gauges=gauges)


async def _until(predicate, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def _gauge_lines(directory):
    return [e for e in _events(directory) if e["type"] == "gauge"]


class TestTheSampler:
    async def test_samples_are_written_counted_and_read_back(self, tmp_path):
        src, fake = Sources(), FakeGauges()
        m = _manager(src, fake)
        rec = await m.start(SIM, str(tmp_path / "r"), Filters(gauges="com.x",
                                                              gauge_interval_s=2.0))
        fake.push(cpu_percent=12.5, memory_bytes=1000, threads=4)
        fake.push(cpu_percent=30.0, memory_bytes=1500, threads=5)
        await _until(lambda: rec.counts.get("gauge") == 2)
        await m.stop(rec.id)
        assert fake.calls == [(SIM, "com.x", 2.0)] and fake.closed == 1
        lines = _gauge_lines(tmp_path / "r")
        assert [g["data"]["cpu_percent"] for g in lines] == [12.5, 30.0]
        window = datetime.fromisoformat(lines[0]["data"]["to"]) - datetime.fromisoformat(
            lines[0]["data"]["from"])
        assert window == timedelta(seconds=2)
        loaded = rec_mod.load(tmp_path / "r")
        assert loaded.gauge_app == "com.x" and loaded.gauge_interval_s == 2.0
        assert [g.memory_bytes for g in loaded.gauges] == [1000, 1500]
        assert rec.summary()["counts"]["gauge"] == 2
        assert rec.summary()["filters"]["gauges"] == "com.x"

    async def test_a_recording_without_gauges_samples_nothing(self, tmp_path):
        src, fake = Sources(), FakeGauges()
        m = _manager(src, fake)
        rec = await m.start(SIM, str(tmp_path / "r"), Filters())
        await _settle()
        await m.stop(rec.id)
        assert fake.calls == [] and "gauge" not in rec.counts
        assert rec_mod.load(tmp_path / "r").gauge_app is None

    async def test_no_line_after_stopped(self, tmp_path):
        src, fake = Sources(), FakeGauges()
        m = _manager(src, fake)
        rec = await m.start(SIM, str(tmp_path / "r"), Filters(gauges="com.x"))
        fake.push(cpu_percent=1.0)
        await _until(lambda: rec.counts.get("gauge") == 1)
        await m.stop(rec.id)
        fake.push(cpu_percent=2.0)
        await _settle()
        kinds = [e["type"] for e in _events(tmp_path / "r")]
        assert kinds[-1] == "stopped" and kinds.count("gauge") == 1

    async def test_the_sampler_is_not_inside_the_action_that_started_it(self, tmp_path):
        """A task copies its creator's context: unguarded, every sample's
        device lookup was reported as the start_recording action's device."""
        src, fake = Sources(), FakeGauges()
        m = _manager(src, fake)
        token = logging_ext.set_current_action(SimpleNamespace(name="start_recording"))
        try:
            rec = await m.start(SIM, str(tmp_path / "r"), Filters(gauges="com.x"))
        finally:
            logging_ext.reset_current_action(token)
        fake.push(cpu_percent=1.0)
        await _until(lambda: rec.counts.get("gauge") == 1)
        await m.stop(rec.id)
        assert fake.saw_action == [None]

    async def test_a_failing_device_is_said_and_asked_again(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rec_mod, "GAUGE_RETRY_S", 0.05)
        src, fake = Sources(), FakeGauges()
        m = _manager(src, fake)
        rec = await m.start(SIM, str(tmp_path / "r"), Filters(gauges="com.x"))
        fake.push(cpu_percent=1.0)
        fake.queue.put_nowait(DeviceError("the phone went away", tool="pymobiledevice3"))
        await _until(lambda: len(fake.calls) == 2)
        fake.push(cpu_percent=3.0)
        await _until(lambda: rec.counts.get("gauge") == 4)
        assert rec.state == "recording"
        await m.stop(rec.id)
        data = [g["data"] for g in _gauge_lines(tmp_path / "r")]
        assert [d.get("cpu_percent") for d in data] == [1.0, None, None, 3.0]
        assert "the phone went away" in data[1]["error"]
        assert "retried after: the phone went away" in data[2]["error"]
        # The failure and the pause tile the time from the last good sample.
        assert data[1]["from"] == data[0]["to"] and data[2]["from"] == data[1]["to"]

    async def test_a_source_that_cannot_even_start_is_said_too(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rec_mod, "GAUGE_RETRY_S", 0.05)

        def broken(udid, app, interval):
            raise AttributeError("'NoneType' object has no attribute 'gauge_stream'")
        src = Sources()
        m = _manager(src, broken)
        rec = await m.start(SIM, str(tmp_path / "r"), Filters(gauges="com.x"))
        await _until(lambda: rec.counts.get("gauge", 0) >= 2)
        await m.stop(rec.id)
        assert rec.state == "stopped"
        assert "gauge_stream" in _gauge_lines(tmp_path / "r")[0]["data"]["error"]

    async def test_the_retry_backs_off(self, tmp_path, monkeypatch):
        sleeps = []
        real_sleep = asyncio.sleep

        async def sleep(seconds, *a):
            sleeps.append(seconds)
            await real_sleep(0)
        monkeypatch.setattr(rec_mod.asyncio, "sleep", sleep)

        def broken(udid, app, interval):
            raise OSError("no")
        m = _manager(Sources(), broken)
        rec = await m.start(SIM, str(tmp_path / "r"), Filters(gauges="com.x"))
        await _until(lambda: len(sleeps) >= 6)
        await m.stop(rec.id)
        assert sleeps[:6] == [2.0, 4.0, 8.0, 16.0, 30.0, 30.0]


class TestOnePerDevice:
    async def test_a_second_gauged_recording_of_a_device_is_refused(self, tmp_path):
        src, fake = Sources(), FakeGauges()
        m = _manager(src, fake)
        first = await m.start(SIM, str(tmp_path / "a"), Filters(gauges="com.x"))
        with pytest.raises(RecordingError, match="already sampled"):
            await m.check_start(SIM, str(tmp_path / "b"), Filters(gauges="com.y"))
        with pytest.raises(RecordingError, match="already sampled"):
            await m.start(SIM, str(tmp_path / "b"), Filters(gauges="com.y"))
        plain = await m.start(SIM, str(tmp_path / "c"), Filters())    # no gauges: fine
        other = await m.start("SIM-B", str(tmp_path / "d"), Filters(gauges="com.x"))
        await m.stop(first.id)
        again = await m.start(SIM, str(tmp_path / "e"), Filters(gauges="com.x"))
        for rec in (plain, other, again):
            await m.stop(rec.id)
        assert m._gauging == {}

    async def test_a_refused_start_gives_the_device_back(self, tmp_path):
        src, fake = Sources(), FakeGauges()
        m = _manager(src, fake)
        (tmp_path / "taken").mkdir()
        (tmp_path / "taken" / "events.jsonl").write_text("{}\n")
        with pytest.raises(RecordingError, match="already exists"):
            await m.start(SIM, str(tmp_path / "taken"), Filters(gauges="com.x"))
        assert m._gauging == {}
        rec = await m.start(SIM, str(tmp_path / "ok"), Filters(gauges="com.x"))
        await m.stop(rec.id)

    async def test_video_refused_gives_the_device_back(self, tmp_path):
        m = _manager(Sources(), FakeGauges())         # no video on this server
        with pytest.raises(RecordingError, match="video"):
            await m.start(SIM, str(tmp_path / "r"), Filters(gauges="com.x", video=True))
        assert m._gauging == {}

    async def test_a_failed_recording_gives_the_device_back(self, tmp_path):
        src, fake = Sources(), FakeGauges()
        m = _manager(src, fake)
        rec = await m.start(SIM, str(tmp_path / "r"), Filters(gauges="com.x"))
        await m._fail(rec, "disk full")
        assert m._gauging == {} and m.live_gauges(SIM) is None

    async def test_a_server_that_cannot_sample_refuses(self, tmp_path):
        m = _manager(Sources(), None)
        with pytest.raises(RecordingError, match="cannot be sampled"):
            await m.check_start(SIM, None, Filters(gauges="com.x"))
        with pytest.raises(RecordingError, match="cannot be sampled"):
            await m.start(SIM, str(tmp_path / "r"), Filters(gauges="com.x"))
        assert not (tmp_path / "r" / "events.jsonl").exists()


class TestFilters:
    @pytest.mark.parametrize("app", ["com.x; reboot", "", "a b", "$(id)"])
    def test_an_app_id_that_could_reach_a_shell_is_refused(self, app):
        with pytest.raises(RecordingError, match="bundle id or package"):
            Filters(gauges=app)

    @pytest.mark.parametrize("interval", [0.4, 10.5, True, "1"])
    def test_the_interval_is_bounded(self, interval):
        with pytest.raises(RecordingError, match="gauge_interval_s"):
            Filters(gauges="com.x", gauge_interval_s=interval)

    def test_round_trip_and_an_older_manifest(self):
        f = Filters(gauges="com.x", gauge_interval_s=2.5)
        assert Filters.from_dict(f.as_dict()) == f
        old = Filters.from_dict({"kinds": ["actions"]})
        assert old.gauges is None and old.gauge_interval_s == gauges_mod.INTERVAL_DEFAULT_S


class TestRestart:
    async def test_a_resumed_recording_samples_again_and_holds_the_device(self, tmp_path):
        src, fake = Sources(), FakeGauges()
        m = _manager(src, fake)
        rec = await m.start(SIM, str(tmp_path / "r"), Filters(gauges="com.x"))
        fake.push(cpu_percent=1.0)
        await _until(lambda: rec.counts.get("gauge") == 1)
        await m.shutdown()
        assert fake.closed == 1

        fake2 = FakeGauges()
        m2 = _manager(src, fake2)
        assert await m2.resume_all() == [rec.id]
        again = m2.get(rec.id)
        assert again.counts.get("gauge") == 1             # read back from the file
        assert m2._gauging == {SIM: rec.id}
        fake2.push(cpu_percent=2.0)
        await _until(lambda: again.counts.get("gauge") == 2)
        with pytest.raises(RecordingError, match="already sampled"):
            await m2.start(SIM, str(tmp_path / "x"), Filters(gauges="com.x"))
        await m2.stop(rec.id)
        loaded = rec_mod.load(tmp_path / "r")
        assert [g.cpu_percent for g in loaded.gauges] == [1.0, 2.0]
        # The downtime is a hole for gauges as for everything else.
        assert rec_mod.holes_in(loaded, {"gauge"}, None, None)


# ── the trace ────────────────────────────────────────────────────────────────

def _gauged_app(src: Sources, fake: FakeGauges):
    app = _app(src)
    app.state.recordings = _manager(src, fake)
    return app


class TestTheTrace:
    def test_live_each_action_on_the_sampled_device_gets_its_gauges(self, tmp_path):
        src, fake = Sources(), FakeGauges()
        with TestClient(_gauged_app(src, fake)) as client:
            started = client.post("/api/v1/recordings", json={
                "udid": SIM, "output_dir": str(tmp_path / "r"), "gauges": "com.x",
                "gauge_interval_s": 2}).json()
            assert started["filters"]["gauges"] == "com.x"

            async def feed():
                await src.server.append(_action("tap", at=_now(), duration_ms=500))
                await src.server.append(_action("tap", udid="SIM-B", at=_now()))
                fake.push(cpu_percent=55.0, memory_bytes=2048, threads=9)
                rec = app_rec()
                await _until(lambda: rec.counts.get("gauge") == 1)
            app_rec = lambda: client.app.state.recordings.get(started["id"])  # noqa: E731
            client.portal.call(feed)
            trace = client.get("/api/v1/trace").json()
            by_udid = {a["udid"]: a for a in trace["actions"]}
            mine = by_udid[SIM]["gauges"]
            assert mine["samples"] == 1 and mine["cpu_percent"]["peak"] == 55.0
            assert mine["memory_bytes"]["peak"] == 2048 and mine["threads_peak"] == 9
            assert by_udid["SIM-B"]["gauges"] is None
            assert trace["gauges"] == {SIM: {"app": "com.x", "interval_s": 2.0,
                                             "recording": started["id"], "live_samples": 1}}
            client.post(f"/api/v1/recordings/{started['id']}/stop")
            after = client.get("/api/v1/trace").json()
            assert after["gauges"] == {} and all(a["gauges"] is None for a in after["actions"])

    def test_live_an_action_before_the_first_sample_says_where_to_look(self, tmp_path):
        src, fake = Sources(), FakeGauges()
        with TestClient(_gauged_app(src, fake)) as client:
            rid = client.post("/api/v1/recordings", json={
                "udid": SIM, "output_dir": str(tmp_path / "r"), "gauges": "com.x"}).json()["id"]

            async def feed():
                await src.server.append(_action("tap", at=_now(-60), duration_ms=500))
            client.portal.call(feed)
            [action] = client.get("/api/v1/trace", params={"since": _now(-120).isoformat()},
                                  ).json()["actions"]
            assert action["gauges"]["samples"] == 0
            assert "no gauge sample yet" in action["gauges"]["unavailable"]["cpu_percent"]
            client.post(f"/api/v1/recordings/{rid}/stop")

    def test_over_the_recording_after_it_stopped(self, tmp_path):
        src, fake = Sources(), FakeGauges()
        with TestClient(_gauged_app(src, fake)) as client:
            rid = client.post("/api/v1/recordings", json={
                "udid": SIM, "output_dir": str(tmp_path / "r"), "gauges": "com.x"}).json()["id"]
            rec = client.app.state.recordings.get(rid)

            async def feed():
                await src.server.append(_action("tap", at=_now(), duration_ms=500))
                fake.push(cpu_percent=70.0, memory_bytes=4096,
                          frames={"total_frames": 10, "janky_frames": 1,
                                  "janky_method": "deadline"})
                await _until(lambda: rec.counts.get("gauge") == 1)
                await _settle()
            client.portal.call(feed)
            client.post(f"/api/v1/recordings/{rid}/stop")
            trace = client.get("/api/v1/trace", params={"recording": rid}).json()
            [action] = trace["actions"]
            assert action["gauges"]["cpu_percent"]["peak"] == 70.0
            assert "need two" in action["gauges"]["unavailable"]["frames"]
            [(udid, described)] = trace["gauges"].items()
            assert udid == SIM and described["app"] == "com.x" and described["samples"] == 1
            page = client.get("/api/v1/recordings/events", params={
                "recording": rid, "kinds": "gauges", "detail": "summary"}).json()
            assert [e["data"]["cpu_percent"] for e in page["events"]
                    if e["type"] == "gauge"] == [70.0]
            default = client.get("/api/v1/recordings/events", params={"recording": rid}).json()
            assert all(e["type"] != "gauge" for e in default["events"])

    def test_over_a_recording_an_action_in_a_hole_names_it(self, tmp_path):
        src, fake = Sources(), FakeGauges()
        with TestClient(_gauged_app(src, fake)) as client:
            rid = client.post("/api/v1/recordings", json={
                "udid": SIM, "output_dir": str(tmp_path / "r"), "gauges": "com.x"}).json()["id"]

            rec = client.app.state.recordings.get(rid)

            async def feed():
                await src.server.append(_action("tap", at=_now(), duration_ms=500))
                await _settle()
                await client.app.state.recordings._flush(rec)     # not a second's wait
            client.portal.call(feed)
            # Read while it runs: the end of the file is a hole still being written.
            [action] = client.get("/api/v1/trace", params={"recording": rid}).json()["actions"]
            assert "still recording" in action["gauges"]["unavailable"]["cpu_percent"]
            client.post(f"/api/v1/recordings/{rid}/stop")

    def test_a_recording_without_gauges_gives_none(self, tmp_path):
        src = Sources()
        with TestClient(_app(src)) as client:
            rid = client.post("/api/v1/recordings", json={
                "udid": SIM, "output_dir": str(tmp_path / "r")}).json()["id"]
            client.portal.call(lambda: src.server.append(_action("tap", at=_now())))
            client.portal.call(_settle)
            client.post(f"/api/v1/recordings/{rid}/stop")
            trace = client.get("/api/v1/trace", params={"recording": rid}).json()
            assert trace["gauges"] == {} and trace["actions"][0]["gauges"] is None

    def test_the_route_refuses_bad_gauge_arguments(self, tmp_path):
        with TestClient(_gauged_app(Sources(), FakeGauges())) as client:
            for body in ({"gauges": "com.x; reboot"}, {"gauges": "com.x", "gauge_interval_s": 0.1},
                         {"gauges": "com.x", "gauge_interval_s": 11}):
                r = client.post("/api/v1/recordings", json={
                    "udid": SIM, "output_dir": str(tmp_path / "r"), **body})
                assert r.status_code == 422, body
            rid = client.post("/api/v1/recordings", json={
                "udid": SIM, "output_dir": str(tmp_path / "a"), "gauges": "com.x"}).json()["id"]
            second = client.post("/api/v1/recordings", json={
                "udid": SIM, "output_dir": str(tmp_path / "b"), "gauges": "com.y"})
            assert second.status_code == 400 and "already sampled" in second.json()["detail"]
            client.post(f"/api/v1/recordings/{rid}/stop")


# ── the device side ──────────────────────────────────────────────────────────

class TestTheControllerStream:
    @staticmethod
    def _controller(kind):
        from server.device.controller import DeviceController
        from server.models import DeviceType
        ctrl = DeviceController()
        ctrl.resolve_udid = AsyncMock(side_effect=lambda udid, **kw: udid)
        ctrl._device_type_cache[SIM] = kind
        return ctrl, DeviceType

    async def test_an_app_not_running_waits_out_the_interval(self, monkeypatch):
        from server.models import DeviceType
        ctrl, _ = self._controller(DeviceType.SIMULATOR)
        calls = []

        async def snapshot(simctl, udid, app, seconds):
            calls.append(seconds)
            return PerformanceSnapshot(udid=udid, bundle_id=app, platform="ios_simulator",
                                       running=False, sample_seconds=seconds)
        monkeypatch.setattr(performance, "simulator_snapshot", snapshot)
        sleeps = []

        async def sleep(seconds):
            sleeps.append(seconds)
        monkeypatch.setattr("server.device.controller.asyncio.sleep", sleep)
        stream = ctrl.gauge_stream(SIM, "com.x", 1.5)
        async with contextlib.aclosing(stream):
            for _ in range(3):
                assert (await anext(stream)).running is False
        assert calls == [1.5, 1.5, 1.5] and sleeps == [1.5, 1.5]

    async def test_a_running_app_is_sampled_back_to_back(self, monkeypatch):
        from server.models import DeviceType
        ctrl, _ = self._controller(DeviceType.ANDROID_EMULATOR)

        async def snapshot(adb, serial, app, seconds):
            return PerformanceSnapshot(udid=serial, bundle_id=app, platform="android",
                                       running=True, sample_seconds=seconds, cpu_percent=3.0)
        monkeypatch.setattr(performance, "android_snapshot", snapshot)
        sleep = AsyncMock()
        monkeypatch.setattr("server.device.controller.asyncio.sleep", sleep)
        stream = ctrl.gauge_stream(SIM, "com.x", 1.0)
        async with contextlib.aclosing(stream):
            assert (await anext(stream)).cpu_percent == 3.0
            await anext(stream)
        sleep.assert_not_awaited()

    async def test_an_app_id_that_could_reach_a_shell_is_refused(self):
        from server.models import DeviceType
        ctrl, _ = self._controller(DeviceType.ANDROID_EMULATOR)
        with pytest.raises(InvalidDeviceRequestError):
            await anext(ctrl.gauge_stream(SIM, "com.x; reboot", 1.0))
        ctrl.resolve_udid.assert_not_awaited()


class _FakeSysmontap:
    ticks: list = []

    def __init__(self):
        self.interval = None

    @classmethod
    async def create(cls, dvt, interval):
        s = cls()
        s.interval = interval
        dvt.sysmon_intervals.append(interval)
        return s

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def iter_processes(self):
        while self.ticks:
            yield self.ticks.pop(0)


def _tick(pid, user, system, age, mem=1000, threads=7):
    return [{"pid": pid, "cpuTotalUser": user, "cpuTotalSystem": system, "procAge": age,
             "physFootprint": mem, "threadCount": threads}]


def _fake_lib(pids, ticks, fps_events=None, fps_error=None):
    dvt = SimpleNamespace(sysmon_intervals=[])
    rsd = SimpleNamespace(connect=AsyncMock(), close=AsyncMock())

    class Dvt:
        def __init__(self, _rsd):
            pass

        async def __aenter__(self):
            return dvt

        async def __aexit__(self, *a):
            return False

    class PC:
        def __init__(self, _dvt):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def process_identifier_for_bundle_identifier(self, _bundle):
            return pids.pop(0) if pids else 0

    class Graphics:
        def __init__(self, _dvt):
            pass

        async def __aenter__(self):
            if fps_error:
                raise fps_error
            return self

        async def __aexit__(self, *a):
            return False

        async def __aiter__(self):
            for ev in fps_events or []:
                yield ev
            await asyncio.Event().wait()            # a live feed never ends by itself

    _FakeSysmontap.ticks = list(ticks)
    lib = SimpleNamespace(RemoteServiceDiscoveryService=lambda _addr: rsd, DvtProvider=Dvt,
                          ProcessControl=PC, Graphics=Graphics, Sysmontap=_FakeSysmontap)
    return lib, rsd, dvt


class TestThePhoneStream:
    @pytest.fixture(autouse=True)
    def _tunnel(self, monkeypatch):
        monkeypatch.setattr(performance, "_device_address", AsyncMock(return_value=("::1", 1)))
        monkeypatch.setattr(performance, "_NOT_RUNNING_POLL_S", 0.0)

    async def test_one_sample_per_tick_on_one_connection(self, monkeypatch):
        ns = 10**9
        lib, rsd, dvt = _fake_lib(
            pids=[42], ticks=[_tick(42, 0, 0, 1 * ns), _tick(42, ns // 2, 0, 2 * ns, mem=2000),
                              _tick(42, ns, ns // 2, 3 * ns, mem=3000)],
            fps_events=[{"CoreAnimationFramesPerSecond": 1.0},
                        {"CoreAnimationFramesPerSecond": 58.0}])
        monkeypatch.setattr(performance, "_pmd3", lambda: lib)
        stream = performance.device_stream("UUID", "com.x", 1.0)
        async with contextlib.aclosing(stream):
            first = await anext(stream)
            await asyncio.sleep(0.01)           # let the FPS feed read its second event
            second = await anext(stream)
        assert (first.cpu_percent, first.memory_bytes, first.threads) == (50.0, 2000, 7)
        assert first.sample_seconds == 1.0 and first.running
        assert second.cpu_percent == 100.0 and second.memory_bytes == 3000
        assert second.display_fps == 58.0
        assert dvt.sysmon_intervals == [1000]
        rsd.connect.assert_awaited_once()
        rsd.close.assert_awaited_once()

    async def test_an_app_not_running_is_polled_until_it_starts(self, monkeypatch):
        ns = 10**9
        lib, rsd, _ = _fake_lib(pids=[0, 0, 42],
                                ticks=[_tick(42, 0, 0, ns), _tick(42, ns, 0, 2 * ns)])
        monkeypatch.setattr(performance, "_pmd3", lambda: lib)
        stream = performance.device_stream("UUID", "com.x", 1.0)
        async with contextlib.aclosing(stream):
            got = [await anext(stream) for _ in range(3)]
        assert [s.running for s in got] == [False, False, True]
        assert got[0].unavailable["cpu_percent"] == performance.NOT_RUNNING
        assert got[2].cpu_percent == 100.0
        rsd.connect.assert_awaited_once()

    async def test_an_app_that_exits_is_looked_for_again(self, monkeypatch):
        ns = 10**9
        lib, _, _ = _fake_lib(pids=[42, 0], ticks=[_tick(42, 0, 0, ns), _tick(42, ns, 0, 2 * ns),
                                                   [{"pid": 7}]])
        monkeypatch.setattr(performance, "_pmd3", lambda: lib)
        stream = performance.device_stream("UUID", "com.x", 1.0)
        async with contextlib.aclosing(stream):
            assert (await anext(stream)).running is True
            assert (await anext(stream)).running is False

    async def test_fps_that_cannot_be_read_says_why_and_costs_nothing_else(self, monkeypatch):
        ns = 10**9
        lib, _, _ = _fake_lib(pids=[42], ticks=[_tick(42, 0, 0, ns), _tick(42, ns, 0, 2 * ns),
                                                _tick(42, 2 * ns, 0, 3 * ns)],
                              fps_error=RuntimeError("no graphics"))
        monkeypatch.setattr(performance, "_pmd3", lambda: lib)
        stream = performance.device_stream("UUID", "com.x", 1.0)
        async with contextlib.aclosing(stream):
            # The feed starts with the stream: the first sample can predate
            # its first answer, and says so.
            assert "no FPS reading yet" in (await anext(stream)).unavailable["display_fps"]
            await asyncio.sleep(0.01)
            snap = await anext(stream)
        assert snap.cpu_percent == 100.0 and snap.display_fps is None
        assert "no graphics" in snap.unavailable["display_fps"]

    async def test_a_broken_connection_is_a_device_error(self, monkeypatch):
        lib, rsd, _ = _fake_lib(pids=[42], ticks=[])
        rsd.connect = AsyncMock(side_effect=ConnectionResetError("reset"))
        monkeypatch.setattr(performance, "_pmd3", lambda: lib)
        with pytest.raises(DeviceError, match="reset"):
            await anext(performance.device_stream("UUID", "com.x", 1.0))
        rsd.close.assert_awaited_once()
