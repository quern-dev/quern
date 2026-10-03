"""Video in a recording (#364, phase 3): a simulator's screen, joined to the trace.

quern-media is never run here: the recorder is driven with a fake process,
and the manager with a fake recorder. What is tested is what quern decides:
when a segment starts and ends, what its summary is read as, which actions
get a keyframe, and where each action lands in the movie.
"""

from __future__ import annotations

import asyncio
import json
import shutil
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

#: How long a resume may take before a test calls it hung. Only "returns" vs
#: "waits on the video forever" matters, so it is generous: 2s timed out on a
#: busy CI runner, in a resume that writes the state file on a worker thread.
RESUME_BOUND = 30.0


@pytest.fixture(autouse=True)
def _state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path / "state")


# ── the recorder, against a fake quern-media ─────────────────────────────────


class FakeProcess:
    """quern-media as quern sees it: lines in its log at start, and more --
    the summary -- when it is told to stop."""

    def __init__(self, lines=(), at_stop=(), exits_at_once=None, pid=4242):
        self.lines, self.at_stop = list(lines), list(at_stop)
        self.returncode = exits_at_once
        self.signals = []
        self.pid = pid
        self.log: Path | None = None

    def _write(self, lines):
        with open(self.log, "a") as f:
            f.writelines(line + "\n" for line in lines)

    def send_signal(self, sig):
        self.signals.append(sig)
        self._write(self.at_stop)
        self.returncode = 0

    def kill(self):
        self.signals.append("kill")
        self.returncode = -9

    async def wait(self):
        while self.returncode is None:
            await asyncio.sleep(0.01)
        return self.returncode


SUMMARY = "[record] 120 frames over 8.00s from host 612668.550500, 2 dropped -> /x.mp4"
STREAMING = f"[capture] streaming simulator {SIM}"


class TestTheRecorder:
    def _recorder(self, process, command_of=None):
        seen = []

        async def spawn(*argv, **kw):
            seen.append((argv, kw))
            process.log = Path(kw["stderr"].name)
            process._write(process.lines)
            return process

        async def binary():
            return Path("/bin/quern-media")
        return VideoRecorder(binary=binary, spawn=spawn, command_of=command_of), seen

    async def test_it_records_and_serves_on_loopback(self, tmp_path):
        recorder, seen = self._recorder(FakeProcess([STREAMING]))
        seg = await recorder.start(SIM, tmp_path / "video-1.mp4")
        [(argv, kw)] = seen
        assert argv[1:5] == ("--sim-udid", SIM, "--record", str(tmp_path / "video-1.mp4"))
        assert argv[5] == "--serve" and int(argv[6]) == seg.port
        assert seg.pid == 4242

    async def test_its_output_goes_to_a_file_never_a_pipe(self, tmp_path):
        """quern-media does not ignore SIGPIPE: with a pipe, a quern that
        died killed it at its next line, the one on the way to finalising,
        and the movie was unopenable. A file lets it outlive quern."""
        recorder, seen = self._recorder(FakeProcess([STREAMING]))
        await recorder.start(SIM, tmp_path / "video-1.mp4")
        [(_, kw)] = seen
        assert kw["stderr"] is not asyncio.subprocess.PIPE
        assert kw["stderr"].name == str(tmp_path / "video-1.log")

    async def test_stopping_finishes_the_movie_and_reads_its_summary(self, tmp_path):
        """SIGINT, which quern-media handles by writing the moov atom: killed
        without it, a movie is unopenable rather than shorter."""
        process = FakeProcess([STREAMING], at_stop=[SUMMARY])
        recorder, _ = self._recorder(process)
        seg = await recorder.start(SIM, tmp_path / "video-1.mp4")
        result = await recorder.stop(seg)
        assert process.signals == [signal.SIGINT]
        assert result["start_host_time"] == 612668.5505 and result["frames"] == 120
        assert result["duration_s"] == 8.0 and result["frames_dropped"] == 2
        assert "error" not in result and "exited_before_stop" not in result

    async def test_no_summary_is_said_not_joined(self, tmp_path):
        recorder, _ = self._recorder(FakeProcess([STREAMING], at_stop=["something else"]))
        seg = await recorder.start(SIM, tmp_path / "video-1.mp4")
        result = await recorder.stop(seg)
        assert result["start_host_time"] is None
        assert "gave no recording summary" in result["error"]
        assert "something else" in result["error"]

    async def test_a_summary_from_an_earlier_run_is_never_read(self, tmp_path):
        """The log is truncated at start: a stale summary would join this
        run's actions to another movie's clock."""
        (tmp_path / "video-1.log").write_text(SUMMARY + "\n")
        recorder, _ = self._recorder(FakeProcess([STREAMING]))
        seg = await recorder.start(SIM, tmp_path / "video-1.mp4")
        assert (await recorder.stop(seg))["start_host_time"] is None

    async def test_a_process_that_exits_at_once_is_a_start_failure(self, tmp_path):
        process = FakeProcess(["error: no booted simulator SIM-V", "", "USAGE", "  a", "  b",
                               "  c", "  d", "  e"], exits_at_once=2)
        recorder, _ = self._recorder(process)
        with pytest.raises(VideoError, match="exited \\(2\\).*no booted simulator") as e:
            await recorder.start(SIM, tmp_path / "video-1.mp4")
        assert "USAGE" not in str(e.value), "the error, not the usage printed after it"

    async def test_a_port_taken_in_between_is_tried_again_on_another(self, tmp_path):
        attempts = []

        async def spawn(*argv, **kw):
            attempts.append(int(argv[6]))
            process = (FakeProcess(["error: cannot bind port 1: Address already in use"],
                                   exits_at_once=2) if len(attempts) == 1
                       else FakeProcess([STREAMING]))
            process.log = Path(kw["stderr"].name)
            process._write(process.lines)
            return process

        async def binary():
            return Path("/bin/quern-media")
        seg = await VideoRecorder(binary=binary, spawn=spawn).start(SIM, tmp_path / "v.mp4")
        assert len(attempts) == 2 and seg.port == attempts[1]

    async def test_a_port_that_stays_taken_is_given_up_on(self, tmp_path):
        recorder, seen = self._recorder(FakeProcess(
            ["error: cannot bind port 1: Address already in use"], exits_at_once=2))
        with pytest.raises(VideoError, match="cannot bind port"):
            await recorder.start(SIM, tmp_path / "v.mp4")
        assert len(seen) == 3

    async def test_a_simulator_not_booted_is_a_start_failure(self, tmp_path):
        """quern-media binds its port before it finds the simulator is not
        booted, then exits: readiness is the capture, not the port (review).
        Here it is serving and has not exited yet when first looked at."""
        process = FakeProcess(["[http] serving on 127.0.0.1:1234"])
        recorder, _ = self._recorder(process)

        async def exits_later():
            await asyncio.sleep(0.25)
            process._write(["error: device is not booted"])
            process.returncode = 2
        task = asyncio.create_task(exits_later())
        with pytest.raises(VideoError, match="exited \\(2\\).*not booted"):
            await recorder.start(SIM, tmp_path / "video-1.mp4")
        await task

    async def test_a_start_that_times_out_stops_what_it_started(self, tmp_path, monkeypatch):
        """Never capturing and never exiting: given up on, and not left
        running to film a recording that was refused."""
        from server import recording_video
        monkeypatch.setattr(recording_video, "START_TIMEOUT", 0.3)
        process = FakeProcess()
        recorder, _ = self._recorder(process)
        with pytest.raises(VideoError, match="did not start recording"):
            await recorder.start(SIM, tmp_path / "video-1.mp4")
        assert process.signals == [signal.SIGINT]

    async def test_a_finish_that_hangs_is_killed_and_said(self, tmp_path, monkeypatch):
        from server import recording_video
        monkeypatch.setattr(recording_video, "STOP_TIMEOUT", 0.2)
        process = FakeProcess([STREAMING])
        recorder, _ = self._recorder(process)
        seg = await recorder.start(SIM, tmp_path / "video-1.mp4")
        process.send_signal = lambda sig: process.signals.append(sig)    # ignores it
        process._write([SUMMARY])          # said, but the moov atom never written
        result = await recorder.stop(seg)
        assert process.signals == [signal.SIGINT, "kill"]
        assert result["start_host_time"] is None and "was killed" in result["error"]

    async def test_one_that_exited_by_itself_ends_at_its_last_frame(self, tmp_path):
        """Stopped by something else, with a summary: its movie ends when it
        did, and nothing says when -- so the stop must not stand for it."""
        process = FakeProcess([STREAMING])
        recorder, _ = self._recorder(process)
        seg = await recorder.start(SIM, tmp_path / "video-1.mp4")
        process._write([SUMMARY])
        process.returncode = 0
        result = await recorder.stop(seg)
        assert process.signals == []
        assert result["exited_before_stop"] is True and result["start_host_time"] == 612668.5505
        assert "exited (0) before the recording stopped" in result["error"]

    def test_the_stop_time_is_not_the_end_of_a_movie_that_exited_early(self, tmp_path):
        t = datetime(2026, 10, 1, 12, tzinfo=UTC)
        _write(tmp_path, [
            {"type": "started", "at": t.isoformat(), "monotonic": 990.0, "udid": SIM},
            {"type": "video_started", "at": t.isoformat(), "monotonic": 991.0,
             "path": "/r/video-1.mp4", "segment": 1},
            {"type": "video_stopped", "at": t.isoformat(), "monotonic": 2000.0,
             "path": "/r/video-1.mp4", "start_host_time": 1000.0, "duration_s": 10.0,
             "exited_before_stop": True, "error": "exited"},
            {"type": "stopped", "at": t.isoformat(), "monotonic": 2001.0}])
        loaded = rec_mod.load(tmp_path)
        assert loaded.video_at(1005.0, 0) == {"path": "/r/video-1.mp4", "offset_s": 5.0}
        assert loaded.video_at(1500.0, 0) is None

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


class TestReaping:
    """A quern-media an earlier quern left running: finished, and read."""

    async def test_one_still_recording_is_stopped_and_its_summary_read(self, tmp_path,
                                                                        monkeypatch):
        movie = tmp_path / "video-1.mp4"
        (tmp_path / "video-1.log").write_text(STREAMING + "\n")
        alive = {"v": True}
        killed = []

        def fake_kill(pid, sig):
            killed.append((pid, sig))
            (tmp_path / "video-1.log").write_text(STREAMING + "\n" + SUMMARY + "\n")
            alive["v"] = False
        monkeypatch.setattr("server.recording_video.os.kill", fake_kill)
        recorder = VideoRecorder(binary=None, command_of=lambda pid: (
            f"/q/quern-media --sim-udid {SIM} --record {movie}" if alive["v"] else ""))
        result = await recorder.reap(77, movie)
        assert killed == [(77, signal.SIGINT)]
        assert result["start_host_time"] == 612668.5505

    async def test_one_that_finished_by_itself_is_read_not_lost(self, tmp_path):
        """quern was killed after its SIGINT and before it could wait: the
        movie was finalised and the summary is in the log (review)."""
        (tmp_path / "video-1.log").write_text(STREAMING + "\n" + SUMMARY + "\n")
        recorder = VideoRecorder(binary=None, command_of=lambda pid: "")
        result = await recorder.reap(77, tmp_path / "video-1.mp4")
        assert result["start_host_time"] == 612668.5505
        assert result["exited_before_stop"] is True, "ended when it exited, not now"

    async def test_one_gone_with_no_summary_is_lost(self, tmp_path):
        (tmp_path / "video-1.log").write_text(STREAMING + "\n")
        recorder = VideoRecorder(binary=None, command_of=lambda pid: "")
        assert await recorder.reap(77, tmp_path / "video-1.mp4") is None

    async def test_a_quern_media_recording_another_movie_is_left_alone(self, tmp_path,
                                                                        monkeypatch):
        """pid reused by quern's own preview, or another recording -- even one
        whose movie's path contains this one's."""
        killed = []
        monkeypatch.setattr("server.recording_video.os.kill", lambda *a: killed.append(a))
        movie = tmp_path / "a" / "video-1.mp4"
        for other in (f"/q/quern-media --sim-udid {SIM} --serve 8422",
                      f"/q/quern-media --sim-udid {SIM} --record /x{movie} --serve 1"):
            recorder = VideoRecorder(binary=None, command_of=lambda pid, c=other: c)
            assert await recorder.reap(77, movie) is None
        assert killed == []

    async def test_could_not_tell_while_waiting_is_not_read_as_gone(self, tmp_path,
                                                                     monkeypatch):
        movie = tmp_path / "video-1.mp4"
        log = tmp_path / "video-1.log"
        log.write_text(STREAMING + "\n")
        monkeypatch.setattr("server.recording_video.os.kill", lambda *a: None)
        mine = f"/q/quern-media --sim-udid {SIM} --record {movie} --serve 1"
        answers = iter(["mine", "error", "mine", "gone"])

        def command_of(pid):
            answer = next(answers)
            if answer == "error":
                raise OSError("ps timed out")
            if answer == "gone":
                log.write_text(STREAMING + "\n" + SUMMARY + "\n")
                return ""
            return mine
        result = await VideoRecorder(binary=None, command_of=command_of).reap(77, movie)
        assert result["start_host_time"] == 612668.5505

    async def test_a_pid_now_some_other_process_is_left_alone(self, tmp_path, monkeypatch):
        killed = []
        monkeypatch.setattr("server.recording_video.os.kill", lambda *a: killed.append(a))
        recorder = VideoRecorder(binary=None, command_of=lambda pid: "/usr/bin/vim notes")
        assert await recorder.reap(77, tmp_path / "video-1.mp4") is None
        assert killed == []

    async def test_could_not_tell_is_raised_not_read_as_gone(self, tmp_path):
        def broken(pid):
            raise OSError("ps timed out")
        with pytest.raises(OSError):
            await VideoRecorder(binary=None, command_of=broken).reap(77, tmp_path / "v.mp4")


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
    def __init__(self, fail_start=False, start_host_time=1000.0, stop_error=None,
                 stop_takes=0.0, reaped=None):
        self.fail_start = fail_start
        self.stop_error = stop_error
        self.stop_takes = stop_takes
        self.reaped, self.reap_calls = reaped, []
        self.start_host_time = start_host_time
        self.started, self.stopped, self.keyframes = [], [], []

    async def start(self, udid, path):
        if self.fail_start:
            path.with_suffix(".log").write_text("error: no booted simulator\n")
            raise VideoError("no booted simulator")
        self.started.append(path)
        await asyncio.sleep(0)
        return Segment(path=path, udid=udid, process=FakeProcess(pid=1000 + len(self.started)))

    async def reap(self, pid, movie):
        self.reap_calls.append((pid, str(movie)))
        return self.reaped

    async def keyframe(self, seg):
        self.keyframes.append(seg.path)
        return True

    async def stop(self, seg):
        self.stopped.append(seg.path)
        await asyncio.sleep(self.stop_takes)
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
        assert manager.get(rec.id).complete is False, "lost video is not complete"
        assert manager.get(rec.id).summary()["video_lost"] is True
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
        await asyncio.wait_for(second.resume_all(), RESUME_BOUND)
        await second.stop(rec.id)
        assert video.started == [tmp_path / "r" / "video-1.mp4", tmp_path / "r" / "video-2.mp4"]
        manifest = json.loads((tmp_path / "r" / "manifest.json").read_text())
        assert [s["segment"] for s in manifest["video"]] == [1, 2]

    async def test_a_segment_that_will_not_resume_is_said_and_the_run_goes_on(self, tmp_path):
        first = Sources().manager(FakeVideo())
        rec = await first.start(SIM, str(tmp_path / "r"), Filters(video=True))
        await first.shutdown()
        second = Sources().manager(FakeVideo(fail_start=True))
        assert await asyncio.wait_for(second.resume_all(), RESUME_BOUND) == [rec.id]
        await second.get(rec.id)._resuming
        assert any("video segment 2: could not be started" in w
                   for w in second.get(rec.id).warnings)
        assert second._filming == {}, "a movie that never started holds no screen"
        done = await second.stop(rec.id)
        assert done.complete is False, "a run missing part of its movie is not complete"
        [failed] = [e for e in _events(tmp_path / "r")
                    if e["type"] == "video_stopped" and e["segment"] == 2]
        assert failed["start_host_time"] is None and "could not be started" in failed["error"]

    async def test_every_action_gets_a_keyframe_though_ids_are_reused(self, tmp_path):
        """Each scope freed before the next is made, as in production: CPython
        hands the next one the same address. Keyed on id(), 1 of 50 got a
        keyframe (review)."""
        video = FakeVideo()
        manager = Sources().manager(video)
        rec = await manager.start(SIM, str(tmp_path / "r"), Filters(video=True))
        for _ in range(50):
            _act()
            await _settle()
        assert len(video.keyframes) == 50
        await manager.stop(rec.id)

    async def test_a_second_stop_waits_for_the_first(self, tmp_path):
        """A client retrying while the movie finalises must not get `stopped`
        written ahead of `video_stopped` and a complete it does not have."""
        manager = Sources().manager(FakeVideo(stop_takes=0.2))
        rec = await manager.start(SIM, str(tmp_path / "r"), Filters(video=True))
        await asyncio.gather(manager.stop(rec.id), manager.stop(rec.id))
        kinds = [e["type"] for e in _events(tmp_path / "r")]
        assert kinds.count("stopped") == 1
        assert kinds.index("video_stopped") < kinds.index("stopped")

    async def test_two_starts_into_one_directory_film_once(self, tmp_path):
        """The loser must not start quern-media on the winner's movie, which
        it would replace (review)."""
        video = FakeVideo()
        manager = Sources().manager(video)
        results = await asyncio.gather(
            manager.start(SIM, str(tmp_path / "r"), Filters(video=True)),
            manager.start(SIM, str(tmp_path / "r"), Filters(video=True)),
            return_exceptions=True)
        assert sum(isinstance(r, RecordingError) for r in results) == 1
        assert video.started == [tmp_path / "r" / "video-1.mp4"]

    async def test_one_simulator_is_filmed_by_one_recording(self, tmp_path):
        manager = Sources().manager(FakeVideo())
        first = await manager.start(SIM, str(tmp_path / "a"), Filters(video=True))
        with pytest.raises(RecordingError, match="already being filmed"):
            await manager.start(SIM, str(tmp_path / "b"), Filters(video=True))
        await manager.stop(first.id)
        second = await manager.start(SIM, str(tmp_path / "c"), Filters(video=True))
        await manager.stop(second.id)

    async def test_a_refused_start_leaves_no_recording_behind(self, tmp_path):
        manager = Sources().manager(FakeVideo(fail_start=True))
        with pytest.raises(RecordingError):
            await manager.start(SIM, str(tmp_path / "r"), Filters(video=True))
        assert list((tmp_path / "r").iterdir()) == []      # its log too
        manager._video = FakeVideo()
        rec = await manager.start(SIM, str(tmp_path / "r"), Filters(video=True))
        await manager.stop(rec.id)

    async def test_stopping_an_interrupted_recording_keeps_its_video(self, tmp_path):
        first = Sources().manager(FakeVideo())
        rec = await first.start(SIM, str(tmp_path / "r"), Filters(video=True))
        await first.shutdown()
        second = Sources().manager(FakeVideo())
        moved = tmp_path / "moved"
        (tmp_path / "r").rename(moved)
        await asyncio.wait_for(second.resume_all(), RESUME_BOUND)       # its directory is gone
        moved.rename(tmp_path / "r")
        done = await second.stop(rec.id)
        assert done.complete is False
        [seg] = json.loads((tmp_path / "r" / "manifest.json").read_text())["video"]
        assert seg["start_host_time"] == 1000.0

    async def test_a_movie_left_recording_is_finished_by_the_next_quern(self, tmp_path):
        """quern killed before it could stop quern-media: the next one stops
        it, reads its summary, and the movie joins the run it was made in."""
        first = Sources().manager(FakeVideo())
        rec = await first.start(SIM, str(tmp_path / "r"), Filters(video=True))
        await first._flush(rec)                        # and then quern is killed
        reaped = {"path": str(tmp_path / "r" / "video-1.mp4"), "start_host_time": 1000.0,
                  "duration_s": 30.0, "frames": 9}
        video = FakeVideo(reaped=reaped)
        second = Sources().manager(video)
        await asyncio.wait_for(second.resume_all(), RESUME_BOUND)
        await second.get(rec.id)._resuming
        assert video.reap_calls == [(1001, str(tmp_path / "r" / "video-1.mp4"))]
        await second.stop(rec.id)
        loaded = rec_mod.load(tmp_path / "r")
        first_seg = next(v for v in loaded.video if v["segment"] == 1)
        assert first_seg["run"] == 0 and first_seg["start_host_time"] == 1000.0
        assert [v["segment"] for v in loaded.video] == [1, 2]

    async def test_a_directory_now_holding_another_recording_is_not_resumed(self, tmp_path):
        """Interrupted because its directory went; the directory came back
        with a recording started since. Resumed, it wrote into that one's
        file and took the simulator (live)."""
        first = Sources().manager(FakeVideo())
        old = await first.start(SIM, str(tmp_path / "r"), Filters(video=True))
        await first.shutdown()
        shutil.rmtree(tmp_path / "r")
        second = Sources().manager(FakeVideo())
        await asyncio.wait_for(second.resume_all(), RESUME_BOUND)   # gone: interrupted
        new = await second.start(SIM, str(tmp_path / "r"), Filters(video=True))
        await second.shutdown()
        third = Sources().manager(FakeVideo())
        assert await asyncio.wait_for(third.resume_all(), RESUME_BOUND) == [new.id]
        assert third.get(old.id).state == "interrupted"
        assert f"now holds recording {new.id}" in third.get(old.id).error
        await third.get(new.id)._resuming
        assert third.get(new.id)._segment is not None, "the new one films"
        before = (tmp_path / "r" / "events.jsonl").read_text()
        await third.stop(old.id)
        assert (tmp_path / "r" / "events.jsonl").read_text() == before
        assert third.get(old.id).complete is False
        await third.stop(new.id)
        assert {e.get("recording") for e in _events(tmp_path / "r")
                if e["type"] == "started"} == {new.id}

    async def test_a_movie_left_unfinished_and_not_running_is_lost(self, tmp_path):
        first = Sources().manager(FakeVideo())
        rec = await first.start(SIM, str(tmp_path / "r"), Filters(video=True))
        await first._flush(rec)
        second = Sources().manager(FakeVideo(reaped=None))
        await asyncio.wait_for(second.resume_all(), RESUME_BOUND)
        await second.get(rec.id)._resuming
        done = await second.stop(rec.id)
        assert done.complete is False
        lost = next(v for v in rec_mod.load(tmp_path / "r").video if v["segment"] == 1)
        assert "without finishing it" in lost["error"]

    async def test_resuming_does_not_wait_for_video(self, tmp_path):
        """Starting quern-media can mean building it; server startup must not
        wait on that (review)."""
        first = Sources().manager(FakeVideo())
        rec = await first.start(SIM, str(tmp_path / "r"), Filters(video=True))
        await first.shutdown()
        gate = asyncio.Event()
        video = FakeVideo()
        real_start = video.start

        async def slow_start(udid, path):
            await gate.wait()
            return await real_start(udid, path)
        video.start = slow_start
        second = Sources().manager(video)
        assert await asyncio.wait_for(second.resume_all(), RESUME_BOUND) == [rec.id]
        assert video.started == []
        stopping = asyncio.create_task(second.stop(rec.id))
        await _settle()
        gate.set()
        await stopping
        kinds = [(e["type"], e.get("segment")) for e in _events(tmp_path / "r")]
        assert kinds.index(("video_started", 2)) < kinds.index(("stopped", None)), \
            "the stop waited for the movie starting, and then finished it"
        assert ("video_stopped", 2) in kinds

    async def test_a_stop_waits_for_the_last_runs_movie_to_be_finished(self, tmp_path):
        """Stopped while the next quern is still finishing the movie the last
        one left running: what that finish says must be in the file, before
        `stopped`, not queued after the writer has gone."""
        first = Sources().manager(FakeVideo())
        rec = await first.start(SIM, str(tmp_path / "r"), Filters(video=True))
        await first._flush(rec)                        # and then quern is killed
        gate = asyncio.Event()
        video = FakeVideo(reaped={"path": str(tmp_path / "r" / "video-1.mp4"),
                                  "start_host_time": 1000.0})
        real_reap = video.reap

        async def slow_reap(pid, movie):
            await gate.wait()
            return await real_reap(pid, movie)
        video.reap = slow_reap
        second = Sources().manager(video)
        await asyncio.wait_for(second.resume_all(), RESUME_BOUND)
        stopping = asyncio.create_task(second.stop(rec.id))
        await _settle()
        gate.set()
        await stopping
        kinds = [(e["type"], e.get("segment")) for e in _events(tmp_path / "r")]
        assert ("video_stopped", 1) in kinds
        assert kinds.index(("video_stopped", 1)) < kinds.index(("stopped", None))

    def test_video_asked_for_and_none_recorded_is_not_complete(self, tmp_path):
        rec = rec_mod.Recording(id="r", udid=SIM, dir=tmp_path, filters=Filters(video=True),
                                started_at=datetime(2026, 10, 1, tzinfo=UTC), state="stopped")
        assert rec.complete is False
        rec.video_segments = [{"segment": 1, "start_host_time": 1000.0}]
        assert rec.complete is True

    async def test_the_pid_is_on_disk_before_start_returns(self, tmp_path):
        """Held for the next flush, a kill in that second left quern-media
        filming with nothing anywhere naming it (review)."""
        manager = Sources().manager(FakeVideo())
        rec = await manager.start(SIM, str(tmp_path / "r"), Filters(video=True))
        [line] = [e for e in _events(tmp_path / "r") if e["type"] == "video_started"]
        assert line["pid"] == 1001
        await manager.stop(rec.id)

    async def test_a_refused_start_waits_for_quern_media_to_go(self, tmp_path, monkeypatch):
        """The pid could not be written, so the start is refused -- but the
        screen and the files go only once quern-media has: cancelling that
        wait left it filming with nothing naming it (CodeRabbit)."""
        monkeypatch.setattr(rec_mod, "CANCEL_WAIT", 0.02)
        video = FakeVideo(stop_takes=0.3)
        done = []
        real_stop = video.stop

        async def stop(seg):
            result = await real_stop(seg)
            done.append(seg.path)
            return result
        video.stop = stop
        real_append = rec_mod._append

        def append(path, lines, sync=False):
            if any('"video_started"' in line for line in lines):
                raise OSError(28, "No space left on device")
            return real_append(path, lines, sync)
        monkeypatch.setattr(rec_mod, "_append", append)
        manager = Sources().manager(video)
        with pytest.raises(RecordingError, match="No space left"):
            await manager.start(SIM, str(tmp_path / "r"), Filters(video=True))
        assert done == [tmp_path / "r" / "video-1.mp4"], "stopped before the refusal"
        assert manager._filming == {} and not (tmp_path / "r" / "events.jsonl").exists()

    async def test_a_resumed_segments_pid_is_on_disk_at_once(self, tmp_path):
        first = Sources().manager(FakeVideo())
        rec = await first.start(SIM, str(tmp_path / "r"), Filters(video=True))
        await first.shutdown()
        second = Sources().manager(FakeVideo())
        await asyncio.wait_for(second.resume_all(), RESUME_BOUND)
        await second.get(rec.id)._resuming
        assert any(e["type"] == "video_started" and e["segment"] == 2
                   for e in _events(tmp_path / "r"))
        await second.stop(rec.id)

    async def test_shutdown_is_bounded_while_a_resume_reaps(self, tmp_path, monkeypatch):
        """`quern stop` kills the server after 5s; a reap can take a minute.
        Shutdown gives up on it, and starts no new movie after (review)."""
        monkeypatch.setattr(rec_mod, "PAUSE_WAIT", 0.2)
        monkeypatch.setattr(rec_mod, "CANCEL_WAIT", 0.1)
        first = Sources().manager(FakeVideo())
        rec = await first.start(SIM, str(tmp_path / "r"), Filters(video=True))
        await first._flush(rec)                        # and then quern is killed
        video = FakeVideo()

        async def forever(pid, movie):
            await asyncio.Event().wait()
        video.reap = forever
        second = Sources().manager(video)
        await asyncio.wait_for(second.resume_all(), RESUME_BOUND)
        # The reap below never ends: any bound catches an unbounded shutdown.
        await asyncio.wait_for(second.shutdown(), 10.0)
        await _settle()
        assert video.started == [], "no new movie once quern is stopping"
        assert any(e["type"] == "paused" for e in _events(tmp_path / "r"))

    async def test_a_start_after_pausing_begins_is_never_made(self, tmp_path):
        first = Sources().manager(FakeVideo())
        rec = await first.start(SIM, str(tmp_path / "r"), Filters(video=True))
        await first._flush(rec)                        # and then quern is killed
        gate = asyncio.Event()
        video = FakeVideo()

        async def slow_reap(pid, movie):
            await gate.wait()
        video.reap = slow_reap
        second = Sources().manager(video)
        await asyncio.wait_for(second.resume_all(), RESUME_BOUND)
        second.get(rec.id)._pausing = True
        gate.set()
        await second.get(rec.id)._resuming
        assert video.started == []

    async def test_a_line_of_the_wrong_shape_stops_no_resume(self, tmp_path):
        """It raised out of the tally, and every recording after it went
        unresumed -- and the next save forgot them (review)."""
        first = Sources().manager()
        a = await first.start(SIM, str(tmp_path / "a"), Filters())
        b = await first.start("SIM-W", str(tmp_path / "b"), Filters())
        await first.shutdown()
        with open(tmp_path / "a" / "events.jsonl", "a") as f:
            f.write('{"type":"dropped","at":"2026-10-01T12:00:00+00:00","what":"flow",'
                    '"count":"many"}\n{"type":["x"],"at":"2026-10-01T12:00:00+00:00"}\n')
        second = Sources().manager()
        resumed = await asyncio.wait_for(second.resume_all(), RESUME_BOUND)
        assert sorted(resumed) == sorted([a.id, b.id])
        await second.shutdown()

    async def test_a_reap_that_ends_after_the_recording_failed_is_kept(self, tmp_path):
        first = Sources().manager(FakeVideo())
        rec = await first.start(SIM, str(tmp_path / "r"), Filters(video=True))
        await first._flush(rec)
        gate = asyncio.Event()
        video = FakeVideo()

        async def slow_reap(pid, movie):
            await gate.wait()
            return {"path": str(movie), "start_host_time": 1000.0, "duration_s": 5.0}
        video.reap = slow_reap
        second = Sources().manager(video)
        await asyncio.wait_for(second.resume_all(), RESUME_BOUND)
        resumed = second.get(rec.id)
        await second._fail(resumed, "disk full")
        gate.set()
        await resumed._resuming
        manifest = json.loads((tmp_path / "r" / "manifest.json").read_text())
        assert any(s.get("start_host_time") == 1000.0 for s in manifest["video"])
        assert video.started == [], "nothing filmed for a failed recording"

    async def test_a_failure_while_the_next_movie_starts_finishes_it(self, tmp_path):
        first = Sources().manager(FakeVideo())
        rec = await first.start(SIM, str(tmp_path / "r"), Filters(video=True))
        await first.shutdown()
        gate = asyncio.Event()
        video = FakeVideo()
        real_start = video.start

        async def slow_start(udid, path):
            await gate.wait()
            return await real_start(udid, path)
        video.start = slow_start
        second = Sources().manager(video)
        await asyncio.wait_for(second.resume_all(), RESUME_BOUND)
        await _settle()
        await second._fail(second.get(rec.id), "disk full")
        gate.set()
        await second.get(rec.id)._resuming
        assert video.stopped == [tmp_path / "r" / "video-2.mp4"]
        assert second._filming == {}, "the screen is free again"

    async def test_a_resumed_recording_holds_its_screen(self, tmp_path):
        first = Sources().manager(FakeVideo())
        rec = await first.start(SIM, str(tmp_path / "r"), Filters(video=True))
        await first.shutdown()
        second = Sources().manager(FakeVideo())
        await asyncio.wait_for(second.resume_all(), RESUME_BOUND)
        await second.get(rec.id)._resuming
        with pytest.raises(RecordingError, match="already being filmed"):
            await second.start(SIM, str(tmp_path / "other"), Filters(video=True))
        await second.stop(rec.id)

    async def test_shutdown_waits_for_a_failed_recordings_movie(self, tmp_path):
        video = FakeVideo(stop_takes=0.2)
        manager = Sources().manager(video)
        rec = await manager.start(SIM, str(tmp_path / "r"), Filters(video=True))
        await manager._fail(rec, "disk full")
        await manager.shutdown()
        # Read from the manifest, written only once the movie has finished:
        # `stopped` is noted as stopping begins, so it would pass regardless.
        [seg] = json.loads((tmp_path / "r" / "manifest.json").read_text())["video"]
        assert seg["start_host_time"] == 1000.0

    async def test_shutdown_finishes_every_movie_at_once(self, tmp_path):
        manager = Sources().manager(FakeVideo(stop_takes=0.4))
        await manager.start(SIM, str(tmp_path / "a"), Filters(video=True))
        await manager.start("SIM-W", str(tmp_path / "b"), Filters(video=True))
        began = asyncio.get_running_loop().time()
        await manager.shutdown()
        assert asyncio.get_running_loop().time() - began < 0.7

    async def test_a_failed_recordings_movie_is_finished_and_kept(self, tmp_path):
        video = FakeVideo(stop_takes=0.05)
        manager = Sources().manager(video)
        rec = await manager.start(SIM, str(tmp_path / "r"), Filters(video=True))
        await manager._fail(rec, "disk full")
        await asyncio.wait(list(manager._background))
        assert video.stopped == [tmp_path / "r" / "video-1.mp4"]
        [seg] = json.loads((tmp_path / "r" / "manifest.json").read_text())["video"]
        assert seg["start_host_time"] == 1000.0


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

    def test_video_on_a_device_quern_does_not_know_is_refused(self, tmp_path):
        class Controller:
            async def _ensure_device_type_cached(self, udid):
                return None

            def _device_type(self, udid):
                return None

        app = FastAPI()
        app.include_router(recordings_router)
        app.state.recordings = Sources().manager(FakeVideo())
        app.state.device_controller = Controller()
        with TestClient(app) as client:
            r = client.post("/api/v1/recordings", json={
                "udid": "NOPE", "output_dir": str(tmp_path / "r"), "video": True})
        assert r.status_code == 400 and "not a device quern knows" in r.json()["detail"]


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

