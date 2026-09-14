"""Tests for server/device/preview.py — screen-mirror build and event handling."""

from __future__ import annotations

import asyncio
import subprocess
from collections import deque

import pytest

from server.device.preview import PreviewManager


class TestBuildBundle:
    def test_a_stale_bundle_is_repaired_on_the_fast_path(self, tmp_path, monkeypatch):
        """The freshness test only asks about the binary. A run that compiled
        and then failed to finish the bundle leaves a binary newer than the
        source, so every later call would return it and report success for an
        app macOS cannot launch."""
        from server.device import preview

        bundle = tmp_path / "Quern Preview.app"
        binary = bundle / "Contents" / "MacOS" / "ios-preview"
        binary.parent.mkdir(parents=True)
        binary.write_text("compiled")

        source = tmp_path / "ios-preview.swift"
        source.write_text("// source")
        import os
        os.utime(source, (1, 1))  # older than the binary

        monkeypatch.setattr(preview, "_find_source", lambda: source)
        monkeypatch.setattr(preview, "bundle_paths", lambda: (bundle, binary))

        assert not (bundle / "Contents" / "Info.plist").exists()
        preview.build_preview_bundle()
        assert (bundle / "Contents" / "Info.plist").exists(), "bundle was not repaired"

    def test_a_stuck_compiler_fails_instead_of_hanging(self, tmp_path, monkeypatch):
        """Unbounded, a stuck swiftc hangs `quern setup` with no output and no
        way to tell it apart from a hang in Quern itself."""
        from server.device import preview

        bundle = tmp_path / "Quern Preview.app"
        binary = bundle / "Contents" / "MacOS" / "ios-preview"
        source = tmp_path / "ios-preview.swift"
        source.write_text("// source")

        monkeypatch.setattr(preview, "_find_source", lambda: source)
        monkeypatch.setattr(preview, "bundle_paths", lambda: (bundle, binary))
        monkeypatch.setattr(preview.shutil, "which", lambda _: "/usr/bin/swiftc")
        monkeypatch.setattr(
            preview.subprocess, "run",
            lambda *a, **kw: (_ for _ in ()).throw(
                subprocess.TimeoutExpired(cmd="swiftc", timeout=60)
            ),
        )

        with pytest.raises(RuntimeError, match="did not finish within 60s"):
            preview.build_preview_bundle()

    def test_the_compile_is_actually_given_a_timeout(self, tmp_path, monkeypatch):
        """A timeout that is never passed to subprocess.run protects nothing."""
        from server.device import preview

        bundle = tmp_path / "Quern Preview.app"
        binary = bundle / "Contents" / "MacOS" / "ios-preview"
        source = tmp_path / "ios-preview.swift"
        source.write_text("// source")
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured.update(kwargs)
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.write_text("x")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(preview, "_find_source", lambda: source)
        monkeypatch.setattr(preview, "bundle_paths", lambda: (bundle, binary))
        monkeypatch.setattr(preview.shutil, "which", lambda _: "/usr/bin/swiftc")
        monkeypatch.setattr(preview.subprocess, "run", fake_run)

        preview.build_preview_bundle()
        assert captured.get("timeout"), "swiftc was launched with no timeout"


class TestDisconnectEvent:
    """`_pending` holds futures for both add and remove, so a disconnect has to
    settle them differently."""

    def _manager_with_pending(self, op: str, cid: str = "c1"):
        loop = asyncio.new_event_loop()
        mgr = PreviewManager()
        fut = loop.create_future()
        mgr._pending["iPhone 11"] = (cid, op, fut)
        return loop, mgr, fut

    def test_a_disconnect_fails_an_in_flight_add(self):
        """Completing it successfully would have add() record a preview for an
        unplugged device and reserve a window position for it."""
        loop, mgr, fut = self._manager_with_pending("add")
        try:
            mgr._dispatch_event({"event": "disconnected", "name": "iPhone 11"})
            assert fut.done()
            with pytest.raises(RuntimeError, match="disconnected before"):
                fut.result()
        finally:
            loop.close()

    def test_a_disconnect_completes_an_in_flight_remove(self):
        """A remove got what it asked for: the preview is gone."""
        loop, mgr, fut = self._manager_with_pending("remove")
        try:
            mgr._dispatch_event({"event": "disconnected", "name": "iPhone 11"})
            assert fut.result() is True
        finally:
            loop.close()

    def test_a_disconnect_drops_the_device_from_available(self):
        """The server would otherwise advertise an unplugged phone until
        something forced a refresh."""
        from server.device.preview import PreviewDeviceInfo

        mgr = PreviewManager()
        mgr._available = [PreviewDeviceInfo(name="iPhone 11", cmio_id="A")]
        mgr._dispatch_event({"event": "disconnected", "name": "iPhone 11"})
        assert mgr._available == []

    def test_a_connect_adds_the_device_without_opening_anything(self):
        """In interactive mode the server decides what is on screen."""
        mgr = PreviewManager()
        mgr._dispatch_event({"event": "connected", "name": "iPhone 11", "id": "A"})
        assert [d.name for d in mgr._available] == ["iPhone 11"]
        assert mgr._active == {}


class TestCommandCorrelation:
    """A write that times out was still delivered and may still run, so its
    late reply must not be matched against whatever holds that name next."""

    def _pending(self, op: str, cid: str):
        loop = asyncio.new_event_loop()
        mgr = PreviewManager()
        fut = loop.create_future()
        mgr._pending["iPhone 11"] = (cid, op, fut)
        return loop, mgr, fut

    def test_a_late_reply_does_not_settle_a_newer_command(self):
        """The exact sequence: remove() times out, add() takes its place, then
        the delayed `removed` arrives. Resolving it would have add() record a
        preview the subprocess had already torn down."""
        loop, mgr, add_fut = self._pending("add", "c2")
        try:
            mgr._dispatch_event(
                {"event": "removed", "name": "iPhone 11", "id": "c1"}
            )
            assert not add_fut.done(), "a stale reply settled the new command"
            assert "iPhone 11" in mgr._pending, "the new command was discarded"
        finally:
            loop.close()

    def test_the_matching_reply_settles_it(self):
        loop, mgr, fut = self._pending("add", "c2")
        try:
            mgr._dispatch_event({"event": "added", "name": "iPhone 11", "id": "c2"})
            assert fut.result() is True
        finally:
            loop.close()

    def test_a_reply_with_no_id_is_refused(self):
        """Accepting id-less replies was meant to tolerate an older subprocess,
        but it reopened the same hole from the other side: a late id-less
        `removed` would still settle a newer add. The subprocess is compiled
        from source shipping with this server, so a binary that cannot echo an
        id is one this code never sent an id to."""
        loop, mgr, fut = self._pending("add", "c2")
        try:
            mgr._dispatch_event({"event": "added", "name": "iPhone 11"})
            assert not fut.done(), "an id-less reply settled an id-bearing command"
        finally:
            loop.close()

    def test_commands_get_distinct_ids(self):
        mgr = PreviewManager()
        ids = {mgr._next_command_id() for _ in range(50)}
        assert len(ids) == 50


class TestAddAcknowledgement:
    def test_a_window_closed_before_ack_fails_the_add(self):
        """The subprocess acknowledges an add a second after starting capture,
        and the window can be closed inside that second. Acknowledging anyway
        left the server holding an active preview with no window, and refusing
        fresh adds for that device because it believed one was running."""
        loop = asyncio.new_event_loop()
        mgr = PreviewManager()
        fut = loop.create_future()
        mgr._pending["iPhone 11"] = ("c1", "add", fut)
        try:
            mgr._dispatch_event({
                "event": "add_failed",
                "name": "iPhone 11",
                "error": "Window closed before the preview was acknowledged",
                "id": "c1",
            })
            assert fut.done()
            with pytest.raises(RuntimeError, match="Window closed"):
                fut.result()
            assert mgr._active == {}
        finally:
            loop.close()


class _FakeStreamProcess:
    """A stand-in for quern-media that never touches a simulator."""

    def __init__(self, stderr_lines=(), exit_code=2):
        self._lines = list(stderr_lines)
        self._exit_code = exit_code
        self._eof = False
        self.terminated = False
        self.killed = False
        self.stderr = self

    @property
    def returncode(self):
        # Only reports an exit once its output has been drained, so a test
        # sees the same ordering as a real process: the reason arrives before
        # the exit is observable.
        return self._exit_code if self._eof else None

    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0).encode() + b"\n"
        self._eof = True
        return b""

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self._exit_code


class _LiveStreamProcess(_FakeStreamProcess):
    """One that stays up until something stops it."""

    def __init__(self):
        super().__init__(exit_code=0)
        self._stopped = False

    @property
    def returncode(self):
        return -15 if self._stopped else None

    async def readline(self) -> bytes:
        await asyncio.sleep(3600)
        return b""

    def terminate(self) -> None:
        self.terminated = True
        self._stopped = True


class TestSimulatorStreams:
    """A simulator is not a CoreMediaIO device, so its preview is an MJPEG
    stream from quern-media. The window and that subprocess have to live and
    die together."""

    @staticmethod
    def _manager(monkeypatch, process):
        from server.device import preview

        mgr = PreviewManager()

        async def _no_process():
            return None

        async def _spawn(*_args, **_kwargs):
            return process

        monkeypatch.setattr(mgr, "_ensure_process", _no_process)
        monkeypatch.setattr(preview, "build_media_engine", lambda: "/tmp/quern-media")
        monkeypatch.setattr(preview.asyncio, "create_subprocess_exec", _spawn)
        return mgr

    def test_a_stream_that_dies_before_serving_reports_why(self, monkeypatch):
        """A dead subprocess and a slow one look identical from the port. A
        timeout for a simulator that was never booted sends the reader
        somewhere the fault is not, so the exit is reported with its output."""
        process = _FakeStreamProcess(
            stderr_lines=["[capture] no such simulator"], exit_code=2
        )
        mgr = self._manager(monkeypatch, process)

        with pytest.raises(RuntimeError, match="no such simulator"):
            asyncio.run(mgr.add_simulator("F5AF3736-DEAD-BEEF"))

    def test_a_failed_add_leaves_no_quern_media_behind(self, monkeypatch):
        """A survivor would hold both the port and the framebuffer
        subscription against the next attempt, which would then fail for a
        reason that has nothing to do with why this one did."""
        process = _FakeStreamProcess(stderr_lines=["boom"], exit_code=2)
        mgr = self._manager(monkeypatch, process)

        with pytest.raises(RuntimeError):
            asyncio.run(mgr.add_simulator("F5AF3736-DEAD-BEEF"))

        assert mgr._streams == {}, "a failed add kept its stream"
        assert mgr._active == {}, "a failed add recorded a preview"

    def test_removing_a_preview_stops_its_stream(self, monkeypatch):
        """Otherwise quern-media keeps encoding frames for a window that has
        gone, holding the simulator framebuffer open."""
        process = _LiveStreamProcess()

        async def run():
            from server.device import preview

            mgr = PreviewManager()
            mgr._streams["SIM"] = preview._StreamProcess(
                process=process, port=8422, log=deque(maxlen=20)
            )
            await mgr._stop_stream("SIM")

        asyncio.run(run())
        assert process.terminated, "the stream outlived its preview"

    def test_a_window_closed_by_the_user_stops_its_stream(self, monkeypatch):
        """Nothing else notices a window the user closed, so the stream would
        run until the server stopped."""
        process = _LiveStreamProcess()

        async def run():
            from server.device import preview

            mgr = PreviewManager()
            mgr._streams["SIM"] = preview._StreamProcess(
                process=process, port=8422, log=deque(maxlen=20)
            )
            mgr._dispatch_event({"event": "window_closed", "name": "SIM"})
            # The handler is synchronous and schedules the teardown.
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        asyncio.run(run())
        assert process.terminated, "closing the window left the stream running"

    def test_a_second_stream_does_not_reuse_the_first_port(self, monkeypatch):
        """Both would bind the same port and the second would fail to serve."""
        from server.device import preview

        mgr = PreviewManager()
        mgr._streams["A"] = preview._StreamProcess(
            process=_LiveStreamProcess(), port=preview.STREAM_BASE_PORT,
            log=deque(maxlen=20),
        )
        chosen = preview.find_available_port(
            preview.STREAM_BASE_PORT,
            exclude={s.port for s in mgr._streams.values()},
        )
        assert chosen != preview.STREAM_BASE_PORT
