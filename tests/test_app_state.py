"""Unit tests for server/device/app_state.py."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

import server.device.app_state as app_state_module
from server.device.app_state import (
    delete_state,
    get_app_groups,
    list_states,
    resolve_container,
    restore_state,
    save_state,
)
from server.models import DeviceError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_proc(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""):
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


# ---------------------------------------------------------------------------
# get_app_groups
# ---------------------------------------------------------------------------


class TestGetAppGroups:
    async def test_finds_matching_group(self, tmp_path, monkeypatch):
        # Build a fake AppGroup directory structure
        app_group_root = (
            tmp_path / "Library" / "Developer" / "CoreSimulator"
            / "Devices" / "TEST-UDID" / "data" / "Containers" / "Shared" / "AppGroup"
        )
        container = app_group_root / "ABCD-1234"
        container.mkdir(parents=True)
        meta = {"MCMMetadataIdentifier": "group.com.example.shared"}
        plist_path = container / ".com.apple.mobile_container_manager.metadata.plist"
        plist_path.write_text("")  # exists check passes

        # Patch Path.home() and plutil call
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

        plist_json = json.dumps(meta).encode()
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=_mock_proc(0, stdout=plist_json),
        ):
            groups = await get_app_groups("TEST-UDID", "com.example.App")

        assert "group.com.example.shared" in groups
        assert groups["group.com.example.shared"] == container

    async def test_returns_empty_when_no_groups_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        groups = await get_app_groups("NO-UDID", "com.example.App")
        assert groups == {}

    async def test_skips_non_group_identifiers(self, tmp_path, monkeypatch):
        app_group_root = (
            tmp_path / "Library" / "Developer" / "CoreSimulator"
            / "Devices" / "TEST-UDID" / "data" / "Containers" / "Shared" / "AppGroup"
        )
        container = app_group_root / "WXYZ-5678"
        container.mkdir(parents=True)
        plist_path = container / ".com.apple.mobile_container_manager.metadata.plist"
        plist_path.write_text("")

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        non_group_meta = json.dumps({"MCMMetadataIdentifier": "com.example.App"}).encode()
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=_mock_proc(0, stdout=non_group_meta),
        ):
            groups = await get_app_groups("TEST-UDID", "com.example.App")
        assert groups == {}


# ---------------------------------------------------------------------------
# save_state
# ---------------------------------------------------------------------------


class TestSaveState:
    async def test_save_state_copies_containers(self, tmp_path):
        """save_state creates checkpoint dir with data-container/ and metadata."""
        # Set APP_STATES_DIR to tmp_path
        original_dir = app_state_module.APP_STATES_DIR
        app_state_module.APP_STATES_DIR = tmp_path / "app-states"

        try:
            # Create a fake data container
            fake_data = tmp_path / "sim-data"
            fake_data.mkdir()
            (fake_data / "Library").mkdir()
            (fake_data / "Library" / "prefs.plist").write_text("fake")

            with (
                patch(
                    "server.device.app_state.get_data_container",
                    AsyncMock(return_value=fake_data),
                ),
                patch(
                    "server.device.app_state.get_app_groups",
                    AsyncMock(return_value={}),
                ),
                patch("server.device.app_state._terminate_app", AsyncMock()),
            ):
                meta = await save_state("TEST-UDID", "com.example.App", "baseline")

            checkpoint = app_state_module.APP_STATES_DIR / "com.example.App" / "baseline"
            assert checkpoint.exists()
            assert (checkpoint / ".quern-meta.json").exists()
            assert (checkpoint / "data-container").exists()
            assert meta["label"] == "baseline"
            assert meta["bundle_id"] == "com.example.App"
        finally:
            app_state_module.APP_STATES_DIR = original_dir

    async def test_save_state_terminates_app_first(self, tmp_path):
        """save_state must terminate the app before copying."""
        original_dir = app_state_module.APP_STATES_DIR
        app_state_module.APP_STATES_DIR = tmp_path / "app-states"
        call_order = []

        async def fake_terminate(udid, bundle_id):
            call_order.append("terminate")

        async def fake_get_data(udid, bundle_id):
            call_order.append("copy")
            d = tmp_path / "sim-data"
            d.mkdir(exist_ok=True)
            return d

        try:
            with (
                patch("server.device.app_state._terminate_app", side_effect=fake_terminate),
                patch("server.device.app_state.get_data_container", side_effect=fake_get_data),
                patch("server.device.app_state.get_app_groups", AsyncMock(return_value={})),
            ):
                await save_state("TEST-UDID", "com.example.App", "test_label")

            assert call_order[0] == "terminate"
            assert call_order[1] == "copy"
        finally:
            app_state_module.APP_STATES_DIR = original_dir


# ---------------------------------------------------------------------------
# restore_state
# ---------------------------------------------------------------------------


class TestRestoreState:
    async def test_restore_state_re_resolves_uuids(self, tmp_path):
        """restore_state uses live container paths, not paths stored in metadata."""
        original_dir = app_state_module.APP_STATES_DIR
        app_state_module.APP_STATES_DIR = tmp_path / "app-states"

        try:
            # Create checkpoint with data-container
            checkpoint = app_state_module.APP_STATES_DIR / "com.example.App" / "snap1"
            checkpoint.mkdir(parents=True)
            data_src = checkpoint / "data-container"
            data_src.mkdir()
            (data_src / "someFile.txt").write_text("checkpoint data")

            meta = {
                "label": "snap1",
                "bundle_id": "com.example.App",
                "udid": "OLD-UDID",
                "captured_at": "2026-01-01T00:00:00+00:00",
                "containers": {"data": "/old/uuid/path", "groups": {}},
                "description": "",
            }
            (checkpoint / ".quern-meta.json").write_text(json.dumps(meta))

            # The "live" path has a different UUID (simulating rotation)
            live_data = tmp_path / "live-sim-data"
            live_data.mkdir()
            (live_data / "oldFile.txt").write_text("old data")

            resolved_udid_used = []

            async def fake_get_data(udid, bundle_id):
                resolved_udid_used.append(udid)
                return live_data

            with (
                patch("server.device.app_state._terminate_app", AsyncMock()),
                patch("server.device.app_state.get_data_container", side_effect=fake_get_data),
                patch("server.device.app_state.get_app_groups", AsyncMock(return_value={})),
            ):
                await restore_state("NEW-UDID", "com.example.App", "snap1")

            # Live path used for restore (not stored path)
            assert resolved_udid_used[0] == "NEW-UDID"
            # Old file wiped, checkpoint data copied
            assert not (live_data / "oldFile.txt").exists()
            assert (live_data / "someFile.txt").exists()
        finally:
            app_state_module.APP_STATES_DIR = original_dir

    async def test_restore_raises_if_not_found(self, tmp_path):
        original_dir = app_state_module.APP_STATES_DIR
        app_state_module.APP_STATES_DIR = tmp_path / "app-states"
        try:
            with pytest.raises(DeviceError, match="not found"):
                await restore_state("UDID", "com.example.App", "nonexistent")
        finally:
            app_state_module.APP_STATES_DIR = original_dir


# ---------------------------------------------------------------------------
# list_states
# ---------------------------------------------------------------------------


class TestListStates:
    def test_list_states_returns_metadata(self, tmp_path):
        original_dir = app_state_module.APP_STATES_DIR
        app_state_module.APP_STATES_DIR = tmp_path / "app-states"

        try:
            bundle_dir = app_state_module.APP_STATES_DIR / "com.example.App"
            for label, ts in [("alpha", "2026-01-01T00:00:00+00:00"), ("beta", "2026-02-01T00:00:00+00:00")]:
                d = bundle_dir / label
                d.mkdir(parents=True)
                meta = {"label": label, "bundle_id": "com.example.App", "captured_at": ts}
                (d / ".quern-meta.json").write_text(json.dumps(meta))

            results = list_states("com.example.App")
            assert len(results) == 2
            # Sorted newest first
            assert results[0]["label"] == "beta"
            assert results[1]["label"] == "alpha"
        finally:
            app_state_module.APP_STATES_DIR = original_dir

    def test_list_states_empty_for_unknown_bundle(self, tmp_path):
        original_dir = app_state_module.APP_STATES_DIR
        app_state_module.APP_STATES_DIR = tmp_path / "app-states"
        try:
            assert list_states("com.unknown.App") == []
        finally:
            app_state_module.APP_STATES_DIR = original_dir
