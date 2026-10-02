"""Video in a recording (#364, phase 3): a simulator's screen, joined to the trace.

quern-media is never run here: the recorder is driven with a fake process,
and the manager with a fake recorder. What is tested is what quern decides:
when a segment starts and ends, what its summary is read as, which actions
get a keyframe, and where each action lands in the movie.
"""

from __future__ import annotations

import asyncio
import json
import signal
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import config as config_mod
from server import logging_ext
from server import recording as rec_mod
from server.api.actions import ActionScope
from server.api.recordings import router as recordings_router
from server.api.trace import router as trace_router
from server.models import DeviceType, LogEntry, LogLevel, LogSource
from server.proxy.flow_store import FlowStore
from server.recording import Filters, RecordingError, RecordingManager
from server.recording_video import Segment, VideoError, VideoRecorder
from server.storage.ring_buffer import RingBuffer

SIM = "SIM-V"


@pytest.fixture(autouse=True)
def _state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path / "state")


# ── the recorder, against a fake quern-media ─────────────────────────────────


class FakeStderr:
    def __init__(self, lines):
        self._lines = [line.encode() + b"\n" for line in lines]

    async def readline(self):
        await asyncio.sleep(0)
        return self._lines.pop(0) if self._lines else b""


class FakeProcess:
    def __init__(self, lines=(), exits_at_once=None):
        self.stderr = FakeStderr(list(lines))
        self.returncode = exits_at_once
        self.signals = []

    def send_signal(self, sig):
        self.signals.append(sig)
        self.returncode = 0

    def kill(self):
        self.signals.append("kill")
        self.returncode = -9

    async def wait(self):
        return self.returncode


SUMMARY = "[record] 120 frames over 8.00s from host 612668.550500, 2 dropped -> /x.mp4"


class TestTheRecorder:
    async def _recorder(self, process, monkeypatch, serving=True):
        seen = []

        async def spawn(*argv, **kw):
            seen.append(argv)
            return process

        async def binary():
            return Path("/bin/quern-media")
        recorder = VideoRecorder(binary=binary, spawn=spawn)
        if serving:
            async def ok(self, seg):
                return None
            monkeypatch.setattr(VideoRecorder, "_wait_serving", ok)
        return recorder, seen

    async def test_it_records_and_serves_on_loopback(self, tmp_path, monkeypatch):
        recorder, seen = await self._recorder(FakeProcess([SUMMARY]), monkeypatch)
        seg = await recorder.start(SIM, tmp_path / "video-1.mp4")
        [argv] = seen
        assert argv[1:5] == ("--sim-udid", SIM, "--record", str(tmp_path / "video-1.mp4"))
        assert argv[5] == "--serve" and int(argv[6]) == seg.port

    async def test_stopping_finishes_the_movie_and_reads_its_summary(self, tmp_path,
                                                                      monkeypatch):
        """SIGINT, which quern-media handles by writing the moov atom: killed
        without it, a movie is unopenable rather than shorter."""
        process = FakeProcess([SUMMARY])
        recorder, _ = await self._recorder(process, monkeypatch)
        seg = await recorder.start(SIM, tmp_path / "video-1.mp4")
        await asyncio.sleep(0.01)
        result = await recorder.stop(seg)
        assert process.signals == [signal.SIGINT]
        assert result["start_host_time"] == 612668.5505 and result["frames"] == 120
        assert result["duration_s"] == 8.0 and result["frames_dropped"] == 2

    async def test_no_summary_is_said_not_joined(self, tmp_path, monkeypatch):
        recorder, _ = await self._recorder(FakeProcess(["something else"]), monkeypatch)
        seg = await recorder.start(SIM, tmp_path / "video-1.mp4")
        result = await recorder.stop(seg)
        assert result["start_host_time"] is None
        assert "gave no recording summary" in result["error"]
        assert "something else" in result["error"]

    async def test_a_process_that_exits_at_once_is_a_start_failure(self, tmp_path, monkeypatch):
        process = FakeProcess(["error: no booted simulator SIM-V"], exits_at_once=2)
        recorder, _ = await self._recorder(process, monkeypatch, serving=False)
        with pytest.raises(VideoError, match="exited \\(2\\).*no booted simulator"):
            await recorder.start(SIM, tmp_path / "video-1.mp4")

    async def test_a_keyframe_is_a_post_to_its_server(self):
        """A real loopback listener standing in for quern-media's /keyframe."""
        got = []

        async def handle(reader, writer):
            got.append(await reader.readline())
            await reader.read(200)
            writer.write(b"HTTP/1.1 204 No Content\r\n\r\n")
            await writer.drain()
            writer.close()
        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        seg = Segment(path=Path("/x.mp4"), udid=SIM, process=FakeProcess(), port=port)
        recorder = VideoRecorder(binary=None)
        assert await recorder.keyframe(seg) is True
        assert got and got[0].startswith(b"POST /keyframe")
        server.close()
        await server.wait_closed()
        assert await recorder.keyframe(seg) is False
        assert (seg.keyframes_requested, seg.keyframes_failed) == (2, 1)


    async def test_a_keyframe_refused_is_counted_as_failed(self):
        async def handle(reader, writer):
            await reader.read(200)
            writer.write(b"HTTP/1.1 500 Internal Server Error\r\n\r\n")
            await writer.drain()
            writer.close()
        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        seg = Segment(path=Path("/x.mp4"), udid=SIM, process=FakeProcess(),
                      port=server.sockets[0].getsockname()[1])
        try:
            assert await VideoRecorder(binary=None).keyframe(seg) is False
        finally:
            server.close()
            await server.wait_closed()
        assert (seg.keyframes_requested, seg.keyframes_failed) == (1, 1)

    async def test_no_keyframe_is_asked_of_a_process_that_has_exited(self):
        seg = Segment(path=Path("/x.mp4"), udid=SIM, process=FakeProcess(exits_at_once=1),
                      port=1)
        assert await VideoRecorder(binary=None).keyframe(seg) is False
        assert seg.keyframes_requested == 0

    async def test_a_start_that_times_out_stops_what_it_started(self, tmp_path, monkeypatch):
        """Nothing is listening on its port, and it never exits: given up on,
        and not left running to film a recording that was refused."""
        from server import recording_video
        monkeypatch.setattr(recording_video, "START_TIMEOUT", 0.3)
        process = FakeProcess()
        recorder, _ = await self._recorder(process, monkeypatch, serving=False)
        with pytest.raises(VideoError, match="did not start recording"):
            await recorder.start(SIM, tmp_path / "video-1.mp4")
        assert process.signals == [signal.SIGINT]


def test_a_listener_that_raises_never_reaches_the_action():
    called = []

    def bad(udid, action):
        raise RuntimeError("boom")

    def good(udid, action):
        called.append(udid)
    logging_ext.add_action_device_listener(bad)
    logging_ext.add_action_device_listener(good)
    try:
        _act()
    finally:
        logging_ext.remove_action_device_listener(bad)
        logging_ext.remove_action_device_listener(good)
    assert called == [SIM]


async def test_resolving_a_device_tells_the_listeners():
    """The hook is only as good as its call site: `resolve_udid` is where
    every action learns its device."""
    from server.device.controller import DeviceController
    controller = DeviceController()

    async def resolved(udid=None, *, set_active=True):
        return SIM
    controller._resolve_udid = resolved
    seen = []

    def listener(udid, action):
        seen.append(udid)
    logging_ext.add_action_device_listener(listener)
    scope = ActionScope("tap_element", "device.action")
    token = logging_ext.set_current_action(scope)
    try:
        await controller.resolve_udid()
    finally:
        logging_ext.reset_current_action(token)
        logging_ext.remove_action_device_listener(listener)
    assert seen == [SIM]


# ── the manager, against a fake recorder ────────────────────────────────────


class FakeVideo:
    def __init__(self, fail_start=False, start_host_time=1000.0, stop_error=None):
        self.fail_start = fail_start
        self.stop_error = stop_error
        self.start_host_time = start_host_time
        self.started, self.stopped, self.keyframes = [], [], []

    async def start(self, udid, path):
        if self.fail_start:
            raise VideoError("no booted simulator")
        self.started.append(path)
        return Segment(path=path, udid=udid)

    async def keyframe(self, seg):
        self.keyframes.append(seg.path)
        return True

    async def stop(self, seg):
        self.stopped.append(seg.path)
        if self.stop_error:
            return {"path": str(seg.path), "start_host_time": None, "error": self.stop_error}
        return {"path": str(seg.path), "start_host_time": self.start_host_time,
                "duration_s": 600.0, "frames": 10, "frames_dropped": 0}


class Sources:
    def __init__(self):
        self.server = RingBuffer(max_size=1000)
        self.ring = RingBuffer(max_size=1000)
        self.crash = RingBuffer(max_size=1000)
        self.flows = FlowStore()

    def manager(self, video=None) -> RecordingManager:
        return RecordingManager(server_buffer=self.server, ring_buffer=self.ring,
                                crash_buffer=self.crash, flow_store=self.flows, video=video)


def _events(directory: Path) -> list[dict]:
    return [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


def _act(udid=SIM):
    """An action learning its device, as `resolve_udid` reports it."""
    scope = ActionScope("tap_element", "device.action")
    token = logging_ext.set_current_action(scope)
    try:
        logging_ext.note_action_device(udid)
    finally:
        logging_ext.reset_current_action(token)
    return scope


class TestTheManager:
    async def test_a_recording_with_video_films_and_finishes(self, tmp_path):
        video = FakeVideo()
        manager = Sources().manager(video)
        rec = await manager.start(SIM, str(tmp_path / "r"), Filters(video=True))
        assert video.started == [tmp_path / "r" / "video-1.mp4"]
        await manager.stop(rec.id)
        assert video.stopped == [tmp_path / "r" / "video-1.mp4"]
        kinds = [e["type"] for e in _events(tmp_path / "r")]
        assert kinds.index("video_started") < kinds.index("video_stopped") < kinds.index("stopped")
        manifest = json.loads((tmp_path / "r" / "manifest.json").read_text())
        [seg] = manifest["video"]
        assert seg["start_host_time"] == 1000.0 and seg["segment"] == 1

    async def test_a_segment_that_did_not_finish_cleanly_is_a_warning(self, tmp_path):
        manager = Sources().manager(FakeVideo(stop_error="quern-media gave no recording "
                                                         "summary: killed"))
        rec = await manager.start(SIM, str(tmp_path / "r"), Filters(video=True))
        await manager.stop(rec.id)
        assert any("video segment 1" in w and "no recording summary" in w
                   for w in manager.get(rec.id).warnings)
        [seg] = json.loads((tmp_path / "r" / "manifest.json").read_text())["video"]
        assert seg["start_host_time"] is None and "no recording summary" in seg["error"]

    async def test_no_video_asked_is_null_not_empty(self, tmp_path):
        manager = Sources().manager(FakeVideo())
        rec = await manager.start(SIM, str(tmp_path / "r"), Filters())
        await manager.stop(rec.id)
        assert json.loads((tmp_path / "r" / "manifest.json").read_text())["video"] is None

    async def test_video_that_cannot_start_refuses_the_recording(self, tmp_path):
        """Asked for and impossible is a refusal, not a run that quietly has
        no movie -- found out at the end of a 90-minute build."""
        manager = Sources().manager(FakeVideo(fail_start=True))
        with pytest.raises(RecordingError, match="video could not be started"):
            await manager.start(SIM, str(tmp_path / "r"), Filters(video=True))
        assert not (tmp_path / "r" / "events.jsonl").exists()
        assert manager.list() == []

    async def test_a_server_without_video_refuses_it(self, tmp_path):
        with pytest.raises(RecordingError, match="cannot be recorded on this server"):
            await Sources().manager(None).start(SIM, str(tmp_path / "r"), Filters(video=True))

    async def test_each_action_on_the_device_gets_one_keyframe(self, tmp_path):
        video = FakeVideo()
        manager = Sources().manager(video)
        rec = await manager.start(SIM, str(tmp_path / "r"), Filters(video=True))
        scope = _act()
        token = logging_ext.set_current_action(scope)
        try:
            logging_ext.note_action_device(SIM)          # the same action asking again
        finally:
            logging_ext.reset_current_action(token)
        _act()
        _act(udid="ANOTHER-DEVICE")
        await _settle()
        assert len(video.keyframes) == 2
        await manager.stop(rec.id)

    async def test_no_action_running_asks_for_nothing(self, tmp_path):
        video = FakeVideo()
        manager = Sources().manager(video)
        rec = await manager.start(SIM, str(tmp_path / "r"), Filters(video=True))
        logging_ext.note_action_device(SIM)
        await _settle()
        assert video.keyframes == []
        await manager.stop(rec.id)

    async def test_a_restart_starts_a_new_segment(self, tmp_path):
        video = FakeVideo()
        first = Sources().manager(video)
        rec = await first.start(SIM, str(tmp_path / "r"), Filters(video=True))
        await first.shutdown()
        second = Sources().manager(video)
        await second.resume_all()
        await second.stop(rec.id)
        assert video.started == [tmp_path / "r" / "video-1.mp4", tmp_path / "r" / "video-2.mp4"]
        manifest = json.loads((tmp_path / "r" / "manifest.json").read_text())
        assert [s["segment"] for s in manifest["video"]] == [1, 2]

    async def test_a_segment_that_will_not_resume_is_said_and_the_run_goes_on(self, tmp_path):
        first = Sources().manager(FakeVideo())
        rec = await first.start(SIM, str(tmp_path / "r"), Filters(video=True))
        await first.shutdown()
        second = Sources().manager(FakeVideo(fail_start=True))
        assert await second.resume_all() == [rec.id]
        assert any("video segment 2 could not be started" in w
                   for w in second.get(rec.id).warnings)
        await second.stop(rec.id)
        assert any(e["type"] == "warning" and "video segment 2" in e["message"]
                   for e in _events(tmp_path / "r"))


# ── the join ─────────────────────────────────────────────────────────────────


def _write(directory: Path, lines: list[dict]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "events.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))


def _action_line(at: datetime, started_monotonic: float) -> dict:
    entry = LogEntry(id=uuid.uuid4().hex, timestamp=at, device_id="server",
                     process="server.api.actions", category="device.action",
                     level=LogLevel.INFO, message="tap ok", source=LogSource.SERVER,
                     action="tap_element", udid=SIM, duration_ms=500, outcome="ok",
                     started_monotonic=started_monotonic)
    return {"type": "action", "at": at.isoformat(), "monotonic": started_monotonic,
            "data": entry.model_dump(mode="json")}


class TestTheJoin:
    def test_an_action_lands_at_its_offset_in_its_runs_movie(self, tmp_path):
        t = datetime(2026, 10, 1, 12, tzinfo=UTC)
        _write(tmp_path, [
            {"type": "started", "at": t.isoformat(), "monotonic": 990.0, "udid": SIM,
             "format_version": 2},
            {"type": "video_started", "at": t.isoformat(), "monotonic": 991.0,
             "path": "/r/video-1.mp4", "segment": 1},
            _action_line(t, 1012.25),
            {"type": "video_stopped", "at": t.isoformat(), "monotonic": 1100.0,
             "path": "/r/video-1.mp4", "start_host_time": 1000.0, "duration_s": 100.0},
            {"type": "stopped", "at": t.isoformat(), "monotonic": 1101.0}])
        loaded = rec_mod.load(tmp_path)
        [action] = loaded.actions
        assert loaded.video_at(action.started_monotonic, loaded.runs[action.id]) == {
            "path": "/r/video-1.mp4", "offset_s": 12.25}
        assert loaded.video_at(999.0, 0) is None, "before the first frame is not in it"

    def test_a_reboot_never_joins_the_wrong_movie(self, tmp_path):
        """After a reboot monotonic starts again: an action at 12.0 in the
        second run must not land 12s into... nothing -- and must never land in
        the first run's movie whose numbers happen to cover it."""
        t = datetime(2026, 10, 1, 12, tzinfo=UTC)
        _write(tmp_path, [
            {"type": "started", "at": t.isoformat(), "monotonic": 5.0, "udid": SIM,
             "format_version": 2},
            {"type": "video_started", "at": t.isoformat(), "monotonic": 6.0,
             "path": "/r/video-1.mp4", "segment": 1},
            {"type": "video_stopped", "at": t.isoformat(), "monotonic": 50.0,
             "path": "/r/video-1.mp4", "start_host_time": 6.0, "duration_s": 44.0},
            {"type": "paused", "at": t.isoformat(), "monotonic": 51.0},
            {"type": "resumed", "at": (t + timedelta(hours=1)).isoformat(), "monotonic": 3.0,
             "gap": {"from": t.isoformat(), "to": (t + timedelta(hours=1)).isoformat(),
                     "reason": "quern was not running"}},
            _action_line(t + timedelta(hours=1), 12.0),
            {"type": "stopped", "at": t.isoformat(), "monotonic": 20.0}])
        loaded = rec_mod.load(tmp_path)
        [action] = loaded.actions
        assert loaded.runs[action.id] == 1
        assert loaded.video_at(action.started_monotonic, 1) is None

    def test_a_segment_still_recording_has_no_join_yet(self, tmp_path):
        t = datetime(2026, 10, 1, 12, tzinfo=UTC)
        _write(tmp_path, [
            {"type": "started", "at": t.isoformat(), "monotonic": 990.0, "udid": SIM},
            {"type": "video_started", "at": t.isoformat(), "monotonic": 991.0,
             "path": "/r/video-1.mp4", "segment": 1}])
        loaded = rec_mod.load(tmp_path, live=True)
        assert loaded.video == [{"path": "/r/video-1.mp4", "run": 0, "start_host_time": None,
                                 "recording": True}]
        assert loaded.video_at(995.0, 0) is None

    def test_a_segment_quern_never_finished_is_said_not_recording(self, tmp_path):
        """quern died with segment 1 open: no `video_stopped`, no summary, no
        moov atom. On resume it is lost, not "still recording" -- and the
        second run's open segment, live, still is."""
        t = datetime(2026, 10, 1, 12, tzinfo=UTC)
        _write(tmp_path, [
            {"type": "started", "at": t.isoformat(), "monotonic": 990.0, "udid": SIM},
            {"type": "video_started", "at": t.isoformat(), "monotonic": 991.0,
             "path": "/r/video-1.mp4", "segment": 1},
            {"type": "resumed", "at": t.isoformat(), "monotonic": 5.0},
            {"type": "video_started", "at": t.isoformat(), "monotonic": 6.0,
             "path": "/r/video-2.mp4", "segment": 2}])
        first, second = rec_mod.load(tmp_path, live=True).video
        assert first["path"] == "/r/video-1.mp4" and "recording" not in first
        assert "quern stopped without finishing it" in first["error"]
        assert second == {"path": "/r/video-2.mp4", "run": 1, "start_host_time": None,
                          "recording": True}
        [_, after] = rec_mod.load(tmp_path).video
        assert "recording" not in after and "ended without finishing" in after["error"]

    def test_a_failed_recordings_segment_is_never_recording(self, tmp_path):
        t = datetime(2026, 10, 1, 12, tzinfo=UTC)
        _write(tmp_path, [
            {"type": "started", "at": t.isoformat(), "monotonic": 990.0, "udid": SIM},
            {"type": "video_started", "at": t.isoformat(), "monotonic": 991.0,
             "path": "/r/video-1.mp4", "segment": 1},
            {"type": "failed", "at": t.isoformat(), "monotonic": 992.0, "error": "disk full"}])
        [seg] = rec_mod.load(tmp_path, live=True).video
        assert "recording" not in seg and "the recording failed (disk full)" in seg["error"]

    def test_each_flow_joins_the_movie_of_its_own_run(self):
        """A flow is joined by its own run, not its action's."""
        from server.api import trace as trace_mod
        loaded = rec_mod.Loaded(
            actions=[], flows=[], logs=[], udid=SIM, holes=[], stopped=True,
            unreadable_lines=0, clock_anchors=[], monotonic_resets=0, warnings=[],
            video=[{"path": "/v1.mp4", "run": 0, "start_host_time": 1000.0,
                    "ended_monotonic": 1100.0},
                   {"path": "/v2.mp4", "run": 1, "start_host_time": 10.0,
                    "ended_monotonic": 2000.0}],
            runs={"a": 0, "f": 1})

        class Attribution:
            class action:
                id = "a"
        row = trace_mod._with_video({"started_monotonic": 1012.0,
                                     "flows": [{"id": "f", "started_monotonic": 1013.0}]},
                                    Attribution, loaded)
        assert row["video"] == {"path": "/v1.mp4", "offset_s": 12.0}
        assert row["flows"][0]["video"] == {"path": "/v2.mp4", "offset_s": 1003.0}

    def test_the_trace_over_a_recording_carries_the_join(self, tmp_path):
        t = datetime(2026, 10, 1, 12, tzinfo=UTC)
        _write(tmp_path / "r", [
            {"type": "started", "at": t.isoformat(), "monotonic": 990.0, "udid": SIM,
             "format_version": 2},
            {"type": "video_started", "at": t.isoformat(), "monotonic": 991.0,
             "path": "/r/video-1.mp4", "segment": 1},
            _action_line(t, 1012.25),
            {"type": "video_stopped", "at": t.isoformat(), "monotonic": 1100.0,
             "path": "/r/video-1.mp4", "start_host_time": 1000.0, "duration_s": 100.0},
            {"type": "stopped", "at": t.isoformat(), "monotonic": 1101.0}])
        app = FastAPI()
        app.include_router(trace_router)
        with TestClient(app) as client:
            trace = client.get("/api/v1/trace", params={"recording": str(tmp_path / "r")}).json()
        [action] = trace["actions"]
        assert action["video"] == {"path": "/r/video-1.mp4", "offset_s": 12.25}
        assert trace["recording"]["video"][0]["start_host_time"] == 1000.0


class TestTheRoute:
    def test_video_on_a_device_that_is_not_a_simulator_is_refused(self, tmp_path):
        class Controller:
            async def _ensure_device_type_cached(self, udid):
                return None

            def _device_type(self, udid):
                return DeviceType.ANDROID_EMULATOR

        app = FastAPI()
        app.include_router(recordings_router)
        src = Sources()
        app.state.recordings = src.manager(FakeVideo())
        app.state.device_controller = Controller()
        with TestClient(app) as client:
            r = client.post("/api/v1/recordings", json={
                "udid": "emulator-5554", "output_dir": str(tmp_path / "r"), "video": True})
        assert r.status_code == 400 and "video records simulators" in r.json()["detail"]


def test_an_action_after_the_last_frame_is_still_in_the_movie(tmp_path):
    """The movie runs to the stop, past its last frame (measured: 9.09s of
    movie, 6.65s of frames). An idle screen composites nothing, so the tail
    can be long."""
    t = datetime(2026, 10, 1, 12, tzinfo=UTC)
    _write(tmp_path, [
        {"type": "started", "at": t.isoformat(), "monotonic": 990.0, "udid": SIM},
        {"type": "video_started", "at": t.isoformat(), "monotonic": 991.0,
         "path": "/r/video-1.mp4", "segment": 1},
        _action_line(t, 1008.0),
        {"type": "video_stopped", "at": t.isoformat(), "monotonic": 1010.0,
         "path": "/r/video-1.mp4", "start_host_time": 1000.0, "duration_s": 6.65},
        {"type": "stopped", "at": t.isoformat(), "monotonic": 1011.0}])
    loaded = rec_mod.load(tmp_path)
    [action] = loaded.actions
    assert loaded.video_at(action.started_monotonic, 0) == {"path": "/r/video-1.mp4",
                                                            "offset_s": 8.0}
    assert loaded.video_at(1010.5, 0) is None, "after the stop is not in the movie"

