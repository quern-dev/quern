"""Integration tests for device pool API endpoints."""

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
    """Create test app with device pool enabled."""
    config = ServerConfig(api_key="test-key-12345")
    return create_app(
        config=config,
        enable_oslog=False,
        enable_crash=False,
        enable_proxy=False,
    )


@pytest.fixture
def auth_headers():
    """Auth headers for API requests."""
    return {"Authorization": "Bearer test-key-12345"}


@pytest.fixture
def mock_device_pool(app, tmp_path):
    """Set up a mock DevicePool with test data."""
    # Create mock controller
    ctrl = DeviceController()
    ctrl.list_devices = AsyncMock(
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
                name="iPhone 15",
                state=DeviceState.SHUTDOWN,
                device_type=DeviceType.SIMULATOR,
                os_version="iOS 17.5",
                runtime="com.apple.CoreSimulator.SimRuntime.iOS-17-5",
                device_family="iPhone",
            ),
            DeviceInfo(
                udid="CCCC-3333",
                name="iPad Pro 13-inch (M4)",
                state=DeviceState.BOOTED,
                device_type=DeviceType.SIMULATOR,
                os_version="iOS 18.2",
                runtime="com.apple.CoreSimulator.SimRuntime.iOS-18-2",
                device_family="iPad",
            ),
        ]
    )

    # Create pool with temp state file
    pool = DevicePool(ctrl)
    pool._pool_file = tmp_path / "device-pool.json"
    app.state.device_controller = ctrl
    app.state.device_pool = pool
    return pool


class TestDevicePoolAPI:
    """Test device pool API endpoints."""

    async def test_refresh_endpoint(self, app, auth_headers, mock_device_pool):
        """Refresh endpoint works."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/devices/refresh",
                headers=auth_headers,
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "refreshed"
            assert "device_count" in data
            assert isinstance(data["device_count"], int)

    async def test_resolve_returns_active_field(self, app, auth_headers, mock_device_pool):
        """Resolve endpoint returns active=true in response."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/devices/resolve",
                headers=auth_headers,
                json={"name": "iPhone 16 Pro"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["active"] is True
            assert data["udid"] == "AAAA-1111"
            assert data["name"] == "iPhone 16 Pro"

    async def test_ensure_returns_active_udid(self, app, auth_headers, mock_device_pool):
        """Ensure endpoint returns active_udid pointing to first device."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/devices/ensure",
                headers=auth_headers,
                json={"count": 1, "name": "iPhone 16 Pro"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert "active_udid" in data
            assert data["active_udid"] == data["devices"][0]["udid"]

    async def test_resolve_no_match_returns_404(self, app, auth_headers, mock_device_pool):
        """Resolve with no matching device returns 404."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/devices/resolve",
                headers=auth_headers,
                json={"name": "Android Phone"},
            )
            assert resp.status_code == 404

    async def test_list_devices_with_name_filter(self, app, auth_headers, mock_device_pool):
        """List devices endpoint filters by name."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/device/list",
                headers=auth_headers,
                params={"name": "iPad"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["devices"]) == 1
            assert "iPad" in data["devices"][0]["name"]

    async def test_list_devices_with_os_version_filter(self, app, auth_headers, mock_device_pool):
        """List devices endpoint filters by OS version."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/device/list",
                headers=auth_headers,
                params={"os_version": "17.5"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["devices"]) == 1
            assert data["devices"][0]["name"] == "iPhone 15"

    async def test_list_devices_with_device_family_filter(self, app, auth_headers, mock_device_pool):
        """List devices endpoint filters by device family."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/device/list",
                headers=auth_headers,
                params={"device_family": "iPad"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["devices"]) == 1
            assert data["devices"][0]["device_family"] == "iPad"
