"""Tests for server/device/preview.py — screen-mirror build and event handling."""

from __future__ import annotations

import asyncio
import subprocess

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

    def test_a_reply_with_no_id_is_still_accepted(self):
        """An older subprocess does not echo ids, and refusing those would hang
        every call against a binary the user has not rebuilt yet."""
        loop, mgr, fut = self._pending("add", "c2")
        try:
            mgr._dispatch_event({"event": "added", "name": "iPhone 11"})
            assert fut.result() is True
        finally:
            loop.close()

    def test_commands_get_distinct_ids(self):
        mgr = PreviewManager()
        ids = {mgr._next_command_id() for _ in range(50)}
        assert len(ids) == 50
