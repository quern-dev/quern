"""API tests for resolution protocol endpoints (Phase 4b-gamma)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from server.config import ServerConfig
from server.device.controller import DeviceController
from server.device.pool import DevicePool
from server.main import create_app
from server.models import DeviceError, DeviceInfo, DeviceState, DeviceType


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
    """Set up a mock DevicePool with 5 devices."""
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
            ),
            DeviceInfo(
                udid="BBBB-2222",
                name="iPhone 16 Pro",
                state=DeviceState.BOOTED,
                device_type=DeviceType.SIMULATOR,
                os_version="iOS 18.2",
                runtime="com.apple.CoreSimulator.SimRuntime.iOS-18-2",
            ),
            DeviceInfo(
                udid="CCCC-3333",
                name="iPhone 16 Pro",
                state=DeviceState.SHUTDOWN,
                device_type=DeviceType.SIMULATOR,
                os_version="iOS 18.2",
                runtime="com.apple.CoreSimulator.SimRuntime.iOS-18-2",
            ),
            DeviceInfo(
                udid="DDDD-4444",
                name="iPhone 15",
                state=DeviceState.BOOTED,
                device_type=DeviceType.SIMULATOR,
                os_version="iOS 17.5",
                runtime="com.apple.CoreSimulator.SimRuntime.iOS-17-5",
            ),
        ]
    )
    ctrl.list_devices = ctrl.simctl.list_devices

    pool = DevicePool(ctrl)
    pool._pool_file = tmp_path / "device-pool.json"
    app.state.device_controller = ctrl
    app.state.device_pool = pool
    return pool


class TestResolveEndpoint:
    """Test POST /api/v1/devices/resolve."""

    async def test_resolve_by_name(self, app, auth_headers, mock_device_pool):
        """Resolve with name returns a matching device."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/devices/resolve",
                headers=auth_headers,
                json={"name": "iPhone 16 Pro"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "udid" in data
        assert data["udid"] in ("AAAA-1111", "BBBB-2222")
        assert data["name"] == "iPhone 16 Pro"
        assert "waited_seconds" in data

    async def test_resolve_by_os_version(self, app, auth_headers, mock_device_pool):
        """Resolve with OS version filter returns matching device."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/devices/resolve",
                headers=auth_headers,
                json={"os_version": "17"},
            )
        assert resp.status_code == 200
        assert resp.json()["udid"] == "DDDD-4444"

    async def test_resolve_with_claim(self, app, auth_headers, mock_device_pool):
        """Resolve with session_id claims the device."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/devices/resolve",
                headers=auth_headers,
                json={"name": "iPhone 16 Pro", "session_id": "my-session"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["claimed_by"] == "my-session"

    async def test_resolve_no_match_returns_404(self, app, auth_headers, mock_device_pool):
        """Resolve with impossible criteria returns 404."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/devices/resolve",
                headers=auth_headers,
                json={"name": "iPad Pro"},
            )
        assert resp.status_code == 404
        assert "No device matching" in resp.json()["detail"]

    async def test_resolve_no_args(self, app, auth_headers, mock_device_pool):
        """Resolve with empty body returns any booted device."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/devices/resolve",
                headers=auth_headers,
                json={},
            )
        assert resp.status_code == 200
        assert resp.json()["state"] == "booted"


class TestEnsureEndpoint:
    """Test POST /api/v1/devices/ensure."""

    async def test_ensure_enough_booted(self, app, auth_headers, mock_device_pool):
        """Ensure with available devices returns immediately."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/devices/ensure",
                headers=auth_headers,
                json={"count": 2, "name": "iPhone 16 Pro"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["devices"]) == 2
        assert data["total_available"] == 2

    async def test_ensure_boots_additional(self, app, auth_headers, mock_device_pool):
        """Ensure boots shutdown devices to meet count."""
        # Make boot succeed and simctl return CCCC as booted
        original_list = mock_device_pool.controller.simctl.list_devices
        call_count = [0]

        async def list_with_boot(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] > 2:
                return [
                    DeviceInfo(
                        udid="CCCC-3333", name="iPhone 16 Pro", state=DeviceState.BOOTED,
                        os_version="iOS 18.2", runtime="...", is_available=True,
                    ),
                ]
            return await original_list(*args, **kwargs)

        mock_device_pool.controller.simctl.list_devices = AsyncMock(side_effect=list_with_boot)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/devices/ensure",
                headers=auth_headers,
                json={"count": 3, "name": "iPhone 16 Pro", "auto_boot": True},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["devices"]) == 3

    async def test_ensure_not_enough_returns_error(self, app, auth_headers, mock_device_pool):
        """Ensure with impossible count returns error."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/devices/ensure",
                headers=auth_headers,
                json={"count": 10, "name": "iPhone 16 Pro"},
            )
        assert resp.status_code == 500 or resp.status_code == 404
        # The error message should mention "Need 10"
        assert "Need 10" in resp.json()["detail"] or "10" in resp.json()["detail"]

    async def test_ensure_with_session_claims(self, app, auth_headers, mock_device_pool):
        """Ensure with session_id claims all devices."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/devices/ensure",
                headers=auth_headers,
                json={"count": 2, "name": "iPhone 16 Pro", "session_id": "test-run"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == "test-run"
        assert len(data["devices"]) == 2
