"""Integration tests for the app state API endpoints."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from server.config import ServerConfig
from server.device.controller import DeviceController
from server.main import create_app
from server.models import DeviceError, DeviceState, DeviceType, DeviceInfo


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app():
    config = ServerConfig(api_key="test-key-12345")
    return create_app(config=config, enable_oslog=False, enable_crash=False, enable_proxy=False)


@pytest.fixture
def auth_headers():
    return {"Authorization": "Bearer test-key-12345"}


@pytest.fixture
def mock_controller(app):
    ctrl = MagicMock(spec=DeviceController)
    ctrl.resolve_udid = AsyncMock(return_value="AAAA-1111")
    ctrl._require_simulator = MagicMock()
    ctrl._is_physical = MagicMock(return_value=False)
    app.state.device_controller = ctrl
    return ctrl


# ---------------------------------------------------------------------------
# Checkpoint endpoints
# ---------------------------------------------------------------------------


class TestSaveEndpoint:
    async def test_save_endpoint(self, app, auth_headers, mock_controller):
        meta = {
            "label": "baseline",
            "bundle_id": "com.example.App",
            "captured_at": "2026-01-01T00:00:00+00:00",
            "udid": "AAAA-1111",
            "description": "",
            "containers": {"data": "/fake/path", "groups": {}},
        }
        with patch("server.api.app_state.save_state", AsyncMock(return_value=meta)):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/api/v1/device/app/state/save",
                    json={"bundle_id": "com.example.App", "label": "baseline"},
                    headers=auth_headers,
                )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "saved"
        assert data["meta"]["label"] == "baseline"

    async def test_save_endpoint_simulator_only(self, app, auth_headers, mock_controller):
        mock_controller._require_simulator = MagicMock(
            side_effect=DeviceError("save_app_state is only supported on simulators", tool="simctl")
        )
        with patch("server.api.app_state.save_state", AsyncMock()):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/api/v1/device/app/state/save",
                    json={"bundle_id": "com.example.App", "label": "baseline"},
                    headers=auth_headers,
                )
        assert resp.status_code == 500


class TestRestoreEndpoint:
    async def test_restore_endpoint(self, app, auth_headers, mock_controller):
        meta = {
            "label": "baseline",
            "bundle_id": "com.example.App",
            "captured_at": "2026-01-01T00:00:00+00:00",
            "udid": "AAAA-1111",
            "description": "",
            "containers": {"data": "/fake/path", "groups": {}},
        }
        with patch("server.api.app_state.restore_state", AsyncMock(return_value=meta)):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/api/v1/device/app/state/restore",
                    json={"bundle_id": "com.example.App", "label": "baseline"},
                    headers=auth_headers,
                )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "restored"

    async def test_restore_not_found(self, app, auth_headers, mock_controller):
        with patch(
            "server.api.app_state.restore_state",
            AsyncMock(side_effect=DeviceError("Checkpoint 'x' not found for com.example.App", tool="simctl")),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/api/v1/device/app/state/restore",
                    json={"bundle_id": "com.example.App", "label": "x"},
                    headers=auth_headers,
                )
        assert resp.status_code == 404


class TestListEndpoint:
    async def test_list_endpoint(self, app, auth_headers, mock_controller):
        states = [
            {"label": "alpha", "bundle_id": "com.example.App", "captured_at": "2026-01-01T00:00:00+00:00"},
        ]
        with patch("server.api.app_state.list_states", return_value=states):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get(
                    "/api/v1/device/app/state/list",
                    params={"bundle_id": "com.example.App"},
                    headers=auth_headers,
                )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["states"][0]["label"] == "alpha"


# ---------------------------------------------------------------------------
# Plist endpoints
# ---------------------------------------------------------------------------


class TestReadPlistEndpoint:
    async def test_read_plist_whole(self, app, auth_headers, mock_controller):
        plist_data = {"key1": "val1", "key2": 42}
        with (
            patch(
                "server.api.app_state.resolve_container",
                AsyncMock(return_value=MagicMock(__truediv__=lambda self, x: MagicMock(exists=lambda: True))),
            ),
            patch("server.api.app_state.read_plist", AsyncMock(return_value=plist_data)),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get(
                    "/api/v1/device/app/state/plist",
                    params={
                        "bundle_id": "com.example.App",
                        "container": "data",
                        "plist_path": "Library/Preferences/com.example.plist",
                    },
                    headers=auth_headers,
                )
        assert resp.status_code == 200
        data = resp.json()
        assert data["data"] == plist_data

    async def test_read_plist_single_key(self, app, auth_headers, mock_controller):
        plist_data = {"INTERNAL_isStagingServerEnvironmentKey": True}
        with (
            patch(
                "server.api.app_state.resolve_container",
                AsyncMock(return_value=MagicMock(__truediv__=lambda self, x: MagicMock(exists=lambda: True))),
            ),
            patch("server.api.app_state.read_plist", AsyncMock(return_value=plist_data)),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get(
                    "/api/v1/device/app/state/plist",
                    params={
                        "bundle_id": "com.example.App",
                        "container": "group.com.example",
                        "plist_path": "Library/Preferences/group.com.example.plist",
                        "key": "INTERNAL_isStagingServerEnvironmentKey",
                    },
                    headers=auth_headers,
                )
        assert resp.status_code == 200
        data = resp.json()
        assert data["key"] == "INTERNAL_isStagingServerEnvironmentKey"
        assert data["value"] is True


class TestSetPlistValueEndpoint:
    async def test_set_plist_value(self, app, auth_headers, mock_controller):
        with (
            patch(
                "server.api.app_state.resolve_container",
                AsyncMock(return_value=MagicMock(__truediv__=lambda self, x: MagicMock(exists=lambda: True))),
            ),
            patch("server.api.app_state.set_plist_value", AsyncMock()),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/api/v1/device/app/state/plist",
                    json={
                        "bundle_id": "com.example.App",
                        "container": "data",
                        "plist_path": "Library/Preferences/com.example.plist",
                        "key": "myFlag",
                        "value": False,
                    },
                    headers=auth_headers,
                )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["key"] == "myFlag"
        assert data["value"] is False
