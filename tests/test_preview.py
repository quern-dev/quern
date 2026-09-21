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


#: A CoreMediaIO unique ID. Sessions are keyed by this, never by the name --
#: two phones of the same model report the same name.
IPHONE_KEY = "A65275E0-4D75-494B-A89E-378F7EABA35D"


class TestDisconnectEvent:
    """`_pending` holds futures for both add and remove, so a disconnect has to
    settle them differently."""

    def _manager_with_pending(self, op: str, cid: str = "c1"):
        loop = asyncio.new_event_loop()
        mgr = PreviewManager()
        fut = loop.create_future()
        mgr._pending[IPHONE_KEY] = (cid, op, fut)
        return loop, mgr, fut

    def test_a_disconnect_fails_an_in_flight_add(self):
        """Completing it successfully would have add() record a preview for an
        unplugged device and reserve a window position for it."""
        loop, mgr, fut = self._manager_with_pending("add")
        try:
            mgr._dispatch_event(
                {"event": "disconnected", "key": IPHONE_KEY, "name": "iPhone 11"}
            )
            assert fut.done()
            with pytest.raises(RuntimeError, match="disconnected before"):
                fut.result()
        finally:
            loop.close()

    def test_a_disconnect_completes_an_in_flight_remove(self):
        """A remove got what it asked for: the preview is gone."""
        loop, mgr, fut = self._manager_with_pending("remove")
        try:
            mgr._dispatch_event(
                {"event": "disconnected", "key": IPHONE_KEY, "name": "iPhone 11"}
            )
            assert fut.result() is True
        finally:
            loop.close()

    def test_a_disconnect_drops_the_device_from_available(self):
        """The server would otherwise advertise an unplugged phone until
        something forced a refresh."""
        from server.device.preview import PreviewDeviceInfo

        mgr = PreviewManager()
        mgr._available = [PreviewDeviceInfo(name="iPhone 11", cmio_id=IPHONE_KEY)]
        mgr._dispatch_event(
            {"event": "disconnected", "key": IPHONE_KEY, "name": "iPhone 11"}
        )
        assert mgr._available == []

    def test_a_connect_adds_the_device_without_opening_anything(self):
        """In interactive mode the server decides what is on screen."""
        mgr = PreviewManager()
        mgr._dispatch_event(
            {"event": "connected", "key": IPHONE_KEY, "name": "iPhone 11"}
        )
        assert [d.name for d in mgr._available] == ["iPhone 11"]
        assert [d.cmio_id for d in mgr._available] == [IPHONE_KEY]
        assert mgr._active == {}


class TestCommandCorrelation:
    """A write that times out was still delivered and may still run, so its
    late reply must not be matched against whatever holds that name next."""

    def _pending(self, op: str, cid: str):
        loop = asyncio.new_event_loop()
        mgr = PreviewManager()
        fut = loop.create_future()
        mgr._pending[IPHONE_KEY] = (cid, op, fut)
        return loop, mgr, fut

    def test_a_late_reply_does_not_settle_a_newer_command(self):
        """The exact sequence: remove() times out, add() takes its place, then
        the delayed `removed` arrives. Resolving it would have add() record a
        preview the subprocess had already torn down."""
        loop, mgr, add_fut = self._pending("add", "c2")
        try:
            mgr._dispatch_event(
                {"event": "removed", "key": IPHONE_KEY, "id": "c1"}
            )
            assert not add_fut.done(), "a stale reply settled the new command"
            assert IPHONE_KEY in mgr._pending, "the new command was discarded"
        finally:
            loop.close()

    def test_the_matching_reply_settles_it(self):
        loop, mgr, fut = self._pending("add", "c2")
        try:
            mgr._dispatch_event({"event": "added", "key": IPHONE_KEY, "id": "c2"})
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
            mgr._dispatch_event({"event": "added", "key": IPHONE_KEY})
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
        mgr._pending[IPHONE_KEY] = ("c1", "add", fut)
        try:
            mgr._dispatch_event({
                "event": "add_failed",
                "key": IPHONE_KEY,
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

        async def _build():
            return "/tmp/quern-media"

        async def _refuse(*_args, **_kwargs):
            # Stubbed because _wait_until_serving otherwise dials loopback for
            # real, and CONTRIBUTING is explicit that tests do not touch the
            # machine. Being honest about the blast radius: the dial targets a
            # port find_available_port just found free, so in practice it gets
            # ECONNREFUSED anyway and the tests pass either way. This removes
            # the dependency on that reasoning staying true, not an observed
            # failure.
            raise ConnectionRefusedError("nothing is listening (stubbed)")

        monkeypatch.setattr(mgr, "_ensure_process", _no_process)
        monkeypatch.setattr(preview, "build_media_engine", _build)
        monkeypatch.setattr(preview.asyncio, "create_subprocess_exec", _spawn)
        monkeypatch.setattr(preview.asyncio, "open_connection", _refuse)
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
            mgr._dispatch_event({"event": "window_closed", "key": "SIM"})
            # The handler is synchronous and schedules the teardown.
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        asyncio.run(run())
        assert process.terminated, "closing the window left the stream running"

    def test_a_second_stream_does_not_reuse_the_first_port(self, monkeypatch):
        """Both would bind the same port and the second would fail to serve.

        Driven through add_simulator rather than by calling the port helper
        directly: the thing that can regress is add_simulator forgetting to
        pass the exclude set, which a direct call cannot catch.
        """
        from server.device import preview

        mgr = PreviewManager()
        mgr._streams["A"] = preview._StreamProcess(
            process=_LiveStreamProcess(), port=preview.STREAM_BASE_PORT,
            log=deque(maxlen=20),
        )

        launched: dict = {}
        asked: dict = {}

        real_find = preview.find_available_port

        def _find(preferred, **kwargs):
            asked.update(kwargs)
            return real_find(preferred, **kwargs)

        async def _no_process():
            return None

        async def _build():
            return "/tmp/quern-media"

        async def _spawn(*args, **_kwargs):
            launched["port"] = int(args[-1])
            return _LiveStreamProcess()

        async def _connect(*_args, **_kwargs):
            class _Writer:
                def close(self):
                    pass

                async def wait_closed(self):
                    pass

            return None, _Writer()

        async def _send(cmd):
            mgr._dispatch_event(
                {"event": "added", "key": cmd["key"], "id": cmd["id"]}
            )

        monkeypatch.setattr(mgr, "_ensure_process", _no_process)
        monkeypatch.setattr(preview, "build_media_engine", _build)
        monkeypatch.setattr(preview.asyncio, "create_subprocess_exec", _spawn)
        monkeypatch.setattr(preview.asyncio, "open_connection", _connect)
        monkeypatch.setattr(mgr, "_send", _send)
        monkeypatch.setattr(preview, "find_available_port", _find)

        record = asyncio.run(mgr.add_simulator("SIM2"))

        # Asserted on the exclude set rather than on the resulting number.
        # find_available_port probes by real bind(), so on a host where
        # something already holds STREAM_BASE_PORT the assertion
        # `launched["port"] != STREAM_BASE_PORT` is satisfied by the host and
        # passes with the exclude set removed entirely -- verified.
        assert asked["exclude"] == {preview.STREAM_BASE_PORT}
        assert record.stream_port == launched["port"]


class TestIdentityResolution:
    """Sessions are keyed by CoreMediaIO unique ID. A name is input, not an
    identity: two phones of the same model report the same one."""

    @staticmethod
    def _available(*pairs):
        from server.device.preview import PreviewDeviceInfo

        mgr = PreviewManager()
        mgr._available = [PreviewDeviceInfo(name=n, cmio_id=i) for n, i in pairs]
        return mgr

    def test_two_phones_of_one_model_stay_distinct(self):
        """Keyed by name, the second could not be previewed at all -- the
        table already held that name -- and unplugging either closed the
        other's window."""
        mgr = self._available(("iPhone 15 Pro", "AAA"), ("iPhone 15 Pro", "BBB"))
        assert mgr._resolve_device("AAA").cmio_id == "AAA"
        assert mgr._resolve_device("BBB").cmio_id == "BBB"

    def test_an_id_beats_a_name_that_collides_with_it(self):
        """Resolving names first would hand back the wrong device outright."""
        mgr = self._available(("BBB", "AAA"), ("iPhone 11", "BBB"))
        assert mgr._resolve_device("BBB").cmio_id == "BBB"

    def test_a_name_still_resolves(self):
        """It is what a person reads off the menu."""
        mgr = self._available(("iPhone 11", "AAA"))
        assert mgr._resolve_device("iPhone 11").cmio_id == "AAA"

    def test_two_phones_of_one_model_get_separate_sessions(self, monkeypatch):
        """The property the re-key exists for, asserted on the session table
        rather than on the lookup helper.

        Resolving the right device is not enough: `_active`, `_pending` and
        `_positions` have to be keyed by id too. Keyed by name, the second add
        finds the name already taken and hands back the first phone's preview,
        and unplugging either closes the other's window. Verified that this
        fails when `_add_device` keys on `device.name`.
        """
        from server.device import preview
        from server.device.preview import PreviewDeviceInfo

        mgr = PreviewManager()
        mgr._available = [
            PreviewDeviceInfo(name="iPhone 15 Pro", cmio_id="AAA"),
            PreviewDeviceInfo(name="iPhone 15 Pro", cmio_id="BBB"),
        ]
        sent: list = []

        async def _no_process():
            return None

        async def _send(cmd):
            sent.append(cmd)
            mgr._dispatch_event(
                {"event": "added", "key": cmd["key"], "id": cmd["id"]}
            )

        monkeypatch.setattr(preview, "ADD_STAGGER_SECONDS", 0)
        monkeypatch.setattr(mgr, "_ensure_process", _no_process)
        monkeypatch.setattr(mgr, "_send", _send)

        async def run():
            await mgr.add("AAA")
            await mgr.add("BBB")

        asyncio.run(run())

        assert set(mgr._active) == {"AAA", "BBB"}, (
            f"both phones should hold a session, got {sorted(mgr._active)}"
        )
        assert [c["key"] for c in sent] == ["AAA", "BBB"]
        assert len(mgr._positions) == 2, "the second window reused the first's slot"

    def test_add_routes_a_simulator_udid_to_a_stream(self, monkeypatch):
        """One entry point for both kinds: a udid is not a capture device, and
        the caller should not have to know which call to make."""
        from server.device import preview
        from server.device.preview import ActivePreview

        mgr = PreviewManager()
        seen: dict = {}

        async def _no_process():
            return None

        async def _booted():
            return [("SIM-UDID", "iPhone 16 Pro")]

        async def _add_sim(udid, title=None):
            seen.update(udid=udid, title=title)
            return ActivePreview(name=udid, position=0, kind="simulator")

        monkeypatch.setattr(mgr, "_ensure_process", _no_process)
        monkeypatch.setattr(preview, "booted_simulators", _booted)
        monkeypatch.setattr(mgr, "add_simulator", _add_sim)

        asyncio.run(mgr.add("SIM-UDID"))
        assert seen == {"udid": "SIM-UDID", "title": "iPhone 16 Pro"}

    def test_an_unknown_identifier_says_it_is_neither(self, monkeypatch):
        """"Device not found" sent the reader looking at the USB cable for a
        simulator that simply was not booted."""
        from server.device import preview

        mgr = PreviewManager()

        async def _no_process():
            return None

        async def _booted():
            return []

        monkeypatch.setattr(mgr, "_ensure_process", _no_process)
        monkeypatch.setattr(preview, "booted_simulators", _booted)

        with pytest.raises(RuntimeError, match="not a connected device or a booted"):
            asyncio.run(mgr.add("nothing-like-this"))


class TestTeardownFailureReporting:
    def test_a_failing_teardown_is_logged_not_swallowed(self, caplog):
        """Driven through the real window_closed path, not by attaching the
        callback by hand.

        A version that built its own task and attached
        `_report_background_failure` itself passed with every production
        wiring deleted -- it only ever tested the one static method. This
        fails unless the handler actually attaches the callback.
        """
        import logging

        from server.device import preview

        async def run():
            mgr = PreviewManager()

            async def boom(_key):
                raise OSError("terminate failed")

            mgr._streams["SIM"] = preview._StreamProcess(
                process=_LiveStreamProcess(), port=8422, log=deque(maxlen=20)
            )
            mgr._stop_stream = boom
            mgr._dispatch_event({"event": "window_closed", "key": "SIM"})
            await asyncio.sleep(0.05)

        with caplog.at_level(logging.ERROR):
            asyncio.run(run())

        # Matched on this handler's own wording, not on the task name alone.
        # With no callback attached, asyncio logs "Task exception was never
        # retrieved" when the task is collected, and that message embeds the
        # task repr -- including name='stop-stream[SIM]'. So a check for the
        # task name passed with every production wiring deleted, which is the
        # proxy-assertion trap this test exists to escape. "Background task"
        # appears only in our message.
        #
        # Deliberately not matched on the logger name. CI failed this while
        # it passed locally, reporting the records as `server.device.preview`
        # against a source that named the logger `quern-debug-server.preview`.
        # The cause: Actions checks out the PR *merged with its base*, and
        # main had renamed the logger to `getLogger(__name__)` as part of
        # retiring the "debug server" name. So CI was running main's rename
        # with this branch's test. Matching on the message survives a rename;
        # matching on the name would break again at the next one.
        reported = [
            r for r in caplog.records
            if "Background task" in r.getMessage()
            and "stop-stream[SIM]" in r.getMessage()
        ]
        assert reported, (
            "the teardown failure was not reported by the manager; "
            f"records seen: {[(r.name, r.getMessage()[:60]) for r in caplog.records]}"
        )
