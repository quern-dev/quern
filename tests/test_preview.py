"""Tests for server/device/preview.py — screen-mirror build and event handling."""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections import deque

import pytest

from server.device.controller import DeviceError
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

        source = tmp_path / "main.swift"
        source.write_text("// source")
        import os
        os.utime(source, (1, 1))  # older than the binary

        monkeypatch.setattr(preview, "_find_source", lambda: source)
        # Pinned empty so freshness is not compared against the real
        # JPEGFraming.swift. Unpinned, this does not fail on its mtime -- it
        # falls off the fast path and invokes the real /usr/bin/swiftc, which
        # succeeds on a comment-only source and passes. Quietly running a
        # compiler is worse than failing.
        monkeypatch.setattr(preview, "_SHARED_SOURCE_CANDIDATES", [])
        monkeypatch.setattr(preview, "bundle_paths", lambda: (bundle, binary))

        # This test is about the fast path, so reaching a compiler at all is a
        # failure of the test rather than a slower route to the same answer.
        def no_compiler(*a, **kw):  # noqa: ANN002, ANN003
            raise AssertionError("the fast path shelled out to a compiler")

        monkeypatch.setattr(preview.subprocess, "run", no_compiler)

        assert not (bundle / "Contents" / "Info.plist").exists()
        preview.build_preview_bundle()
        assert (bundle / "Contents" / "Info.plist").exists(), "bundle was not repaired"

    def test_a_stuck_compiler_fails_instead_of_hanging(self, tmp_path, monkeypatch):
        """Unbounded, a stuck swiftc hangs `quern setup` with no output and no
        way to tell it apart from a hang in Quern itself."""
        from server.device import preview

        bundle = tmp_path / "Quern Preview.app"
        binary = bundle / "Contents" / "MacOS" / "ios-preview"
        source = tmp_path / "main.swift"
        source.write_text("// source")

        monkeypatch.setattr(preview, "_find_source", lambda: source)
        # Pinned empty so freshness is not compared against the real
        # JPEGFraming.swift. Unpinned, this does not fail on its mtime -- it
        # falls off the fast path and invokes the real /usr/bin/swiftc, which
        # succeeds on a comment-only source and passes. Quietly running a
        # compiler is worse than failing.
        monkeypatch.setattr(preview, "_SHARED_SOURCE_CANDIDATES", [])
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
        source = tmp_path / "main.swift"
        source.write_text("// source")
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured.update(kwargs)
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.write_text("x")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(preview, "_find_source", lambda: source)
        # Pinned empty so freshness is not compared against the real
        # JPEGFraming.swift. Unpinned, this does not fail on its mtime -- it
        # falls off the fast path and invokes the real /usr/bin/swiftc, which
        # succeeds on a comment-only source and passes. Quietly running a
        # compiler is worse than failing.
        monkeypatch.setattr(preview, "_SHARED_SOURCE_CANDIDATES", [])
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

    def test_an_ambiguous_name_is_refused_rather_than_guessed(self):
        """Two phones of one model share a localizedName. Returning the first
        meant a request for phone B opened phone A — silently, and the same
        way every time. There is nothing to disambiguate with: CoreMediaIO's
        uniqueID is neither the hardware UDID nor the CoreDevice UUID, so no
        caller can supply the right id for a name."""
        mgr = self._available(("iPhone 15 Pro", "AAA"), ("iPhone 15 Pro", "BBB"))
        with pytest.raises(RuntimeError, match="are called 'iPhone 15 Pro'"):
            mgr._resolve_device("iPhone 15 Pro")

        # Each is still reachable by its own id.
        assert mgr._resolve_device("AAA").cmio_id == "AAA"
        assert mgr._resolve_device("BBB").cmio_id == "BBB"

    def test_a_unique_name_still_resolves(self):
        mgr = self._available(("iPhone 11", "AAA"), ("iPhone 15 Pro", "BBB"))
        assert mgr._resolve_device("iPhone 11").cmio_id == "AAA"

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


class TestSharedSourceFreshness:
    def test_editing_the_shared_parser_rebuilds_the_app(self, tmp_path, monkeypatch):
        """The binary is compiled from the script *and* the shared frame
        parser. Comparing the binary against the script alone would leave an
        edit to the parser silently not taking — the app keeps running the
        previous build and nothing says so."""
        from server.device import preview

        bundle = tmp_path / "Quern Preview.app"
        binary = bundle / "Contents" / "MacOS" / "ios-preview"
        binary.parent.mkdir(parents=True)
        binary.write_text("compiled")

        source = tmp_path / "main.swift"
        source.write_text("// source")
        os.utime(source, (1, 1))  # older than the binary

        shared = tmp_path / "JPEGFraming.swift"
        shared.write_text("// parser")  # newer than the binary

        compiled: list = []

        def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003
            compiled.append(cmd)
            binary.write_text("recompiled")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(preview, "_find_source", lambda: source)
        monkeypatch.setattr(preview, "_SHARED_SOURCE_CANDIDATES", [shared])
        monkeypatch.setattr(preview, "bundle_paths", lambda: (bundle, binary))
        monkeypatch.setattr(preview.shutil, "which", lambda _: "/usr/bin/swiftc")
        monkeypatch.setattr(preview.subprocess, "run", fake_run)

        preview.build_preview_bundle()

        assert compiled, "a newer shared source did not trigger a rebuild"
        assert str(shared) in compiled[0], "the shared source was not compiled in"
        assert str(source) in compiled[0]


class TestSharedSourcesAreRequired:
    def test_a_missing_shared_source_names_itself(self, tmp_path, monkeypatch):
        """Filtering to the files that exist compiled the script alone, and
        swiftc then blamed main.swift for a symbol whose file was gone —
        sending the reader to the one file that was fine."""
        from server.device import preview

        missing = tmp_path / "JPEGFraming.swift"  # never created
        monkeypatch.setattr(preview, "_SHARED_SOURCE_CANDIDATES", [missing])

        with pytest.raises(RuntimeError, match="JPEGFraming.swift"):
            preview._shared_sources()

    def test_the_real_shared_sources_all_exist(self):
        """The paths are hardcoded, so a rename inside the Swift package
        breaks the app build. Nothing else in CI compiles that combination."""
        from server.device import preview

        for path in preview._SHARED_SOURCE_CANDIDATES:
            assert path.exists(), f"{path} is referenced by the build but absent"


class TestRemoveByLabel:
    """`add` accepts three identifier forms; `remove` accepted two.

    A simulator is filed under its udid with the simulator name as a label.
    `_resolve_device` searches CoreMediaIO devices only, so it cannot see that
    label -- `remove("iPhone 16 Pro")` fell through to `_stop_stream` with a
    display name for a key, which matches nothing. It returned normally with
    the window still open and quern-media still holding the framebuffer.
    """

    @staticmethod
    def _with_simulator(*previews):
        """A manager with a live subprocess and the given previews active."""
        from server.device import preview as preview_mod

        mgr = PreviewManager()
        mgr._process = _LiveStreamProcess()
        for i, (key, label) in enumerate(previews):
            mgr._active[key] = preview_mod.ActivePreview(
                name=key, position=i, kind="simulator",
                stream_port=preview_mod.STREAM_BASE_PORT + i, label=label,
            )
            mgr._positions.add(i)
        return mgr

    def test_a_simulator_preview_is_removed_by_its_name(self, monkeypatch):
        udid = "11111111-2222-3333-4444-555555555555"
        mgr = self._with_simulator((udid, "iPhone 16 Pro"))

        sent: list[dict] = []
        stopped: list[str] = []

        async def _send(cmd):
            sent.append(cmd)
            mgr._dispatch_event(
                {"event": "removed", "key": cmd["key"], "id": cmd["id"]}
            )

        async def _stop_stream(key):
            stopped.append(key)

        monkeypatch.setattr(mgr, "_send", _send)
        monkeypatch.setattr(mgr, "_stop_stream", _stop_stream)

        asyncio.run(mgr.remove("iPhone 16 Pro"))

        # Asserted on the key that went to the app, not merely on `_active`
        # shrinking. The bug returned normally and left the window open, so
        # "no exception" is exactly what it looked like.
        assert [c["key"] for c in sent] == [udid]
        assert udid not in mgr._active
        assert stopped == [udid]

    def test_a_simulator_preview_is_still_removable_by_udid(self, monkeypatch):
        """The form that already worked, kept honest."""
        udid = "11111111-2222-3333-4444-555555555555"
        mgr = self._with_simulator((udid, "iPhone 16 Pro"))

        sent: list[dict] = []

        async def _send(cmd):
            sent.append(cmd)
            mgr._dispatch_event(
                {"event": "removed", "key": cmd["key"], "id": cmd["id"]}
            )

        monkeypatch.setattr(mgr, "_send", _send)
        monkeypatch.setattr(mgr, "_stop_stream", _noop_stop_stream)

        asyncio.run(mgr.remove(udid))
        assert [c["key"] for c in sent] == [udid]

    def test_an_ambiguous_label_is_refused_rather_than_guessed(self):
        """Two simulators of one model share a name, and there is no third
        thing to arbitrate with -- the same reason `_resolve_device` refuses.
        Removing the first would close a window the caller did not name."""
        mgr = self._with_simulator(
            ("udid-a", "iPhone 16 Pro"), ("udid-b", "iPhone 16 Pro")
        )
        with pytest.raises(RuntimeError, match="are called 'iPhone 16 Pro'"):
            asyncio.run(mgr.remove("iPhone 16 Pro"))

        assert set(mgr._active) == {"udid-a", "udid-b"}

    def test_a_capture_device_still_wins_over_a_matching_label(self, monkeypatch):
        """`add` resolves a capture device before it considers a simulator, so
        `remove` must too, or one string names two different windows."""
        from server.device import preview as preview_mod

        mgr = self._with_simulator(("udid-sim", "iPhone 11"))
        mgr._available = [preview_mod.PreviewDeviceInfo(name="iPhone 11", cmio_id="CMIO")]
        mgr._active["CMIO"] = preview_mod.ActivePreview(
            name="CMIO", position=9, kind="device", label="iPhone 11"
        )

        sent: list[dict] = []

        async def _send(cmd):
            sent.append(cmd)
            mgr._dispatch_event(
                {"event": "removed", "key": cmd["key"], "id": cmd["id"]}
            )

        monkeypatch.setattr(mgr, "_send", _send)
        monkeypatch.setattr(mgr, "_stop_stream", _noop_stop_stream)

        asyncio.run(mgr.remove("iPhone 11"))
        assert [c["key"] for c in sent] == ["CMIO"]
        assert "udid-sim" in mgr._active, "removed the simulator instead"


async def _noop_stop_stream(key):
    pass


class TestSimulatorPreviewRouting:
    """`POST /device/preview/start` must reach the simulator path.

    `PreviewManager.add` grew simulator support, and this route is the only
    HTTP way in -- but it refused a simulator udid with a 400 before ever
    calling it, so the capability shipped unreachable. `preview_stop` never
    had the matching gate, which is what makes the asymmetry a bug rather
    than a boundary: stopping a simulator preview was reachable while
    starting one was not.
    """

    @staticmethod
    def _request(pm, *, physical: bool):
        """A stand-in for the FastAPI Request the route reads app state from."""

        class _Controller:
            def _is_android(self, _udid):
                return False

            def _is_physical(self, _udid):
                return physical

            async def resolve_udid(self, udid):
                return udid

            async def list_devices(self):
                raise AssertionError(
                    "the simulator path must not round-trip the udid through a "
                    "CoreMediaIO device name"
                )

        class _State:
            device_controller = _Controller()
            preview_manager = pm
            scrcpy_preview = None

        class _App:
            state = _State()

        class _Request:
            app = _App()

        return _Request()

    def test_a_simulator_udid_opens_a_preview(self):
        from server.api.device import PreviewStartRequest, preview_start
        from server.device import preview as preview_mod

        udid = "11111111-2222-3333-4444-555555555555"
        added: list[str] = []

        class _PM:
            async def add(self, name):
                added.append(name)
                return preview_mod.ActivePreview(
                    name=udid, position=0, kind="simulator",
                    stream_port=preview_mod.STREAM_BASE_PORT,
                    label="iPhone 16 Pro",
                )

        result = asyncio.run(
            preview_start(
                self._request(_PM(), physical=False),
                PreviewStartRequest(udid=udid),
            )
        )

        # The udid, not a name. Resolving it to a display name and handing
        # that to `add` is the lossy round-trip the physical path needs and
        # the simulator path must not do -- a simulator is filed under its
        # udid.
        assert added == [udid]
        assert result["status"] == "added"
        assert result["name"] == "iPhone 16 Pro"
        assert result["platform"] == "ios"

    def test_a_physical_udid_still_resolves_through_a_device_name(self):
        """The existing path, kept honest: CoreMediaIO matches on a name."""
        from server.api.device import PreviewStartRequest, preview_start
        from server.device import preview as preview_mod

        udid = "00008030-000123456789002E"
        added: list[str] = []

        class _Device:
            def __init__(self):
                self.udid = udid
                self.name = "Jerimiah's iPhone"

        class _PM:
            async def add(self, name):
                added.append(name)
                return preview_mod.ActivePreview(
                    name="CMIO-ID", position=0, label="Jerimiah's iPhone"
                )

        request = self._request(_PM(), physical=True)

        async def _list_devices():
            return [_Device()]

        request.app.state.device_controller.list_devices = _list_devices

        result = asyncio.run(
            preview_start(request, PreviewStartRequest(udid=udid))
        )
        assert added == ["Jerimiah's iPhone"]
        assert result["status"] == "added"


class TestSimulatorStopRouting:
    """`POST /device/preview/stop`, the sibling of `TestSimulatorPreviewRouting`.

    The simulator fix landed on `preview_start` and not here, so this route
    went on resolving the udid to a display name and handing that to
    `pm.remove`. `PreviewManager.remove` resolves capture devices before
    labels, deliberately -- so with a phone and a simulator of the same name
    both previewed, stopping the simulator closed the phone's window and
    reported success.
    """

    @staticmethod
    def _request(pm, *, physical: bool, devices=None, list_raises=False):
        class _Controller:
            def _is_android(self, _udid):
                return False

            def _is_physical(self, _udid):
                return physical

            async def resolve_udid(self, udid):
                return udid

            async def list_devices(self):
                if list_raises:
                    raise DeviceError("simctl unavailable")
                return devices or []

        class _State:
            device_controller = _Controller()
            preview_manager = pm
            scrcpy_preview = None

        class _App:
            state = _State()

        class _Request:
            app = _App()

        return _Request()

    @staticmethod
    def _pm(removed, raises=None):
        class _PM:
            async def remove(self, name):
                if raises is not None:
                    raise raises
                removed.append(name)

        return _PM()

    def test_a_simulator_is_stopped_by_its_udid(self):
        from server.api.device import PreviewStopRequest, preview_stop

        udid = "11111111-2222-3333-4444-555555555555"
        removed: list[str] = []

        result = asyncio.run(
            preview_stop(
                self._request(self._pm(removed), physical=False),
                PreviewStopRequest(udid=udid),
            )
        )
        # The udid, not a name. A name here lands on a capture device of the
        # same name and closes the wrong window.
        assert removed == [udid]
        assert result["status"] == "removed"

    def test_a_simulator_stop_does_not_depend_on_the_device_list(self):
        """`list_devices` raising used to 404 a udid that is already the key."""
        from server.api.device import PreviewStopRequest, preview_stop

        udid = "11111111-2222-3333-4444-555555555555"
        removed: list[str] = []

        result = asyncio.run(
            preview_stop(
                self._request(self._pm(removed), physical=False, list_raises=True),
                PreviewStopRequest(udid=udid),
            )
        )
        assert removed == [udid]
        assert result["status"] == "removed"

    def test_an_ambiguous_removal_is_a_reported_error_not_a_bare_500(self):
        """`preview_start` wraps RuntimeError; this route did not, so an
        ambiguous label reached FastAPI as a 500 with no detail -- and the
        message tells the caller to use the key they had just passed."""
        from fastapi import HTTPException

        from server.api.device import PreviewStopRequest, preview_stop

        udid = "11111111-2222-3333-4444-555555555555"
        pm = self._pm([], raises=RuntimeError("2 previews are called 'iPhone 16 Pro'"))

        try:
            asyncio.run(
                preview_stop(
                    self._request(pm, physical=False),
                    PreviewStopRequest(udid=udid),
                )
            )
        except HTTPException as exc:
            assert exc.status_code == 500
            assert "iPhone 16 Pro" in str(exc.detail)
        else:
            raise AssertionError("expected an HTTPException carrying the reason")

    def test_a_physical_device_still_stops_through_its_name(self):
        from server.api.device import PreviewStopRequest, preview_stop

        udid = "00008030-000123456789002E"
        removed: list[str] = []

        class _Device:
            def __init__(self):
                self.udid = udid
                self.name = "Jerimiah's iPhone"

        result = asyncio.run(
            preview_stop(
                self._request(
                    self._pm(removed), physical=True, devices=[_Device()]
                ),
                PreviewStopRequest(udid=udid),
            )
        )
        assert removed == ["Jerimiah's iPhone"]
        assert result["status"] == "removed"


class TestSimulatorAddSafety:
    """`add` grew a simulator branch; it did not grow the rules the rest of
    the identity handling follows."""

    def test_two_booted_simulators_of_one_name_are_refused(self, monkeypatch):
        """Cloning a device is the ordinary way to get two of one name.
        Opening whichever simctl listed first is the defect `_resolve_device`
        and `_key_for_label` both refuse to commit -- and it was asymmetric
        too: `add` picked one while `remove` raised once both were active."""
        from server.device import preview as preview_mod

        mgr = PreviewManager()

        async def _no_process():
            return None

        async def _booted():
            return [("udid-a", "iPhone 16 Pro"), ("udid-b", "iPhone 16 Pro")]

        async def _never(*_a, **_k):
            raise AssertionError("add_simulator must not be reached")

        monkeypatch.setattr(mgr, "_ensure_process", _no_process)
        monkeypatch.setattr(preview_mod, "booted_simulators", _booted)
        monkeypatch.setattr(mgr, "add_simulator", _never)

        with pytest.raises(RuntimeError, match="are called 'iPhone 16 Pro'"):
            asyncio.run(mgr.add("iPhone 16 Pro"))

    def test_a_udid_beats_another_simulators_name(self, monkeypatch):
        """Names are checked only after every udid, so a name colliding with
        some other simulator's udid cannot win."""
        from server.device import preview as preview_mod

        mgr = PreviewManager()
        picked: list[str] = []

        async def _no_process():
            return None

        async def _booted():
            # The first entry is *named* the same string that is the second
            # entry's udid. Scanning entry-by-entry matches the name first.
            return [("udid-a", "udid-b"), ("udid-b", "iPhone 16 Pro")]

        async def _add_sim(udid, title=None):
            picked.append(udid)
            return preview_mod.ActivePreview(name=udid, position=0, kind="simulator")

        monkeypatch.setattr(mgr, "_ensure_process", _no_process)
        monkeypatch.setattr(preview_mod, "booted_simulators", _booted)
        monkeypatch.setattr(mgr, "add_simulator", _add_sim)

        asyncio.run(mgr.add("udid-b"))
        assert picked == ["udid-b"], "a name matched ahead of a udid"

    def test_two_concurrent_adds_for_one_simulator_start_one_stream(
        self, monkeypatch
    ):
        """The check sat before an await on a build that takes seconds, so
        both callers passed it -- an agent retrying after a client timeout is
        enough. The second overwrote `_streams[udid]`, stranding the first
        `quern-media` where `_stop_stream`, `_terminate_streams` and `stop()`
        could not reach it: it held its port and the framebuffer subscription
        until the server exited.
        """
        from server.device import preview as preview_mod

        udid = "11111111-2222-3333-4444-555555555555"
        started: list[int] = []

        async def run():
            mgr = PreviewManager()
            mgr._process = _LiveStreamProcess()

            release = asyncio.Event()

            async def _no_process():
                return None

            async def _build():
                # Both callers are inside here at once, which is the window.
                await release.wait()
                return "/tmp/quern-media"

            async def _start_stream(key, binary, port):
                started.append(port)
                stream = preview_mod._StreamProcess(
                    process=_LiveStreamProcess(), port=port, log=deque(maxlen=20),
                )
                # The real one registers here, which is the assignment the
                # second caller used to overwrite. A stub that skips it cannot
                # show the stranding.
                mgr._streams[key] = stream
                return stream

            async def _wait_until_serving(_key, _stream):
                return None

            async def _send(cmd):
                mgr._dispatch_event(
                    {"event": "added", "key": cmd["key"], "id": cmd["id"]}
                )

            monkeypatch.setattr(mgr, "_ensure_process", _no_process)
            monkeypatch.setattr(preview_mod, "build_media_engine", _build)
            monkeypatch.setattr(mgr, "_start_stream", _start_stream)
            monkeypatch.setattr(mgr, "_wait_until_serving", _wait_until_serving)
            monkeypatch.setattr(mgr, "_send", _send)

            first = asyncio.create_task(mgr.add_simulator(udid, title="iPhone 16 Pro"))
            second = asyncio.create_task(mgr.add_simulator(udid, title="iPhone 16 Pro"))
            await asyncio.sleep(0)
            release.set()
            return await asyncio.gather(first, second), mgr

        (a, b), mgr = asyncio.run(run())

        # Asserted on the streams actually started, not on the two results
        # being equal: the buggy version returned two records that compared
        # fine while a second quern-media ran untracked.
        assert len(started) == 1, f"started {len(started)} streams for one simulator"
        assert len(mgr._streams) == 1
        assert len(mgr._positions) == 1
        assert a.name == b.name == udid
