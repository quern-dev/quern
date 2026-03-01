"""Integration tests for multi-device pool API endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from server.config import ServerConfig
from server.device.controller import DeviceController
from server.device.pool import DevicePool
from server.main import create_app
from server.models import DeviceInfo, DeviceState, DeviceType


@pytest.fixture
def app():
    """Create test app."""
    config = ServerConfig(api_key="test-key-12345")
    return create_app(
        config=config,
        enable_oslog=False,
        enable_crash=False,
        enable_proxy=False,
    )


@pytest.fixture
def auth_headers():
    return {"Authorization": "Bearer test-key-12345"}


@pytest.fixture
def mock_device_pool(app, tmp_path):
    """Set up a mock DevicePool with 4 devices."""
    ctrl = DeviceController()
    ctrl.simctl = AsyncMock()
    ctrl.simctl.boot = AsyncMock()
    ctrl.simctl.list_devices = AsyncMock(
        return_value=[
            DeviceInfo(
                udid="AAAA-1111",
                name="iPhone 16 Pro",
                state=DeviceState.BOOTED,
                device_type=DeviceType.SIMULATOR,
                os_version="iOS 18.2",
                runtime="com.apple.CoreSimulator.SimRuntime.iOS-18-2",
                device_family="iPhone",
            ),
            DeviceInfo(
                udid="BBBB-2222",
                name="iPhone 16 Pro",
                state=DeviceState.BOOTED,
                device_type=DeviceType.SIMULATOR,
                os_version="iOS 18.2",
                runtime="com.apple.CoreSimulator.SimRuntime.iOS-18-2",
                device_family="iPhone",
            ),
            DeviceInfo(
                udid="CCCC-3333",
                name="iPhone 16 Pro",
                state=DeviceState.SHUTDOWN,
                device_type=DeviceType.SIMULATOR,
                os_version="iOS 18.2",
                runtime="com.apple.CoreSimulator.SimRuntime.iOS-18-2",
                device_family="iPhone",
            ),
            DeviceInfo(
                udid="DDDD-4444",
                name="iPhone 15",
                state=DeviceState.BOOTED,
                device_type=DeviceType.SIMULATOR,
                os_version="iOS 17.5",
                runtime="com.apple.CoreSimulator.SimRuntime.iOS-17-5",
                device_family="iPhone",
            ),
        ]
    )
    ctrl.list_devices = ctrl.simctl.list_devices

    pool = DevicePool(ctrl)
    pool._pool_file = tmp_path / "device-pool.json"
    app.state.device_controller = ctrl
    app.state.device_pool = pool
    return pool


class TestPoolAPIIntegration:

    async def test_resolve_sets_active_device(self, app, auth_headers, mock_device_pool):
        """Resolve sets the active device, subsequent resolve with no params returns it."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Resolve a specific device
            resp = await client.post(
                "/api/v1/devices/resolve",
                json={"name": "iPhone 16 Pro"},
                headers=auth_headers,
            )
            assert resp.status_code == 200
            first_udid = resp.json()["udid"]
            assert resp.json()["active"] is True

            # Resolve with no params should return the same active device
            resp2 = await client.post(
                "/api/v1/devices/resolve",
                json={},
                headers=auth_headers,
            )
            assert resp2.status_code == 200
            assert resp2.json()["udid"] == first_udid

    async def test_ensure_devices_returns_active(self, app, auth_headers, mock_device_pool):
        """ensure_devices returns active_udid pointing to first device."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/devices/ensure",
                json={"count": 2, "name": "iPhone 16 Pro"},
                headers=auth_headers,
            )
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["devices"]) == 2
            assert "active_udid" in data
            assert data["active_udid"] == data["devices"][0]["udid"]

    async def test_resolve_no_match_returns_404(self, app, auth_headers, mock_device_pool):
        """resolve with impossible criteria returns 404 with diagnostic message."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/devices/resolve",
                json={"name": "iPad Pro"},
                headers=auth_headers,
            )
            assert resp.status_code == 404
            assert "iPad Pro" in resp.json()["detail"]
