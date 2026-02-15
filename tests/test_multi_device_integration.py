"""Integration tests for multi-device pool API endpoints."""

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


class TestPoolAPIIntegration:

    async def test_claim_resolve_release_cycle(self, app, auth_headers, mock_device_pool):
        """Full lifecycle: claim -> resolve (different session) -> release."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Claim
            resp = await client.post(
                "/api/v1/devices/claim",
                json={"session_id": "int-test", "name": "iPhone 16 Pro"},
                headers=auth_headers,
            )
            assert resp.status_code == 200
            udid = resp.json()["device"]["udid"]

            # Resolve should find a different device for another session
            resp2 = await client.post(
                "/api/v1/devices/resolve",
                json={"name": "iPhone 16 Pro", "session_id": "int-test-2"},
                headers=auth_headers,
            )
            assert resp2.status_code == 200
            assert resp2.json()["udid"] != udid

            # Release first device
            resp3 = await client.post(
                "/api/v1/devices/release",
                json={"udid": udid, "session_id": "int-test"},
                headers=auth_headers,
            )
            assert resp3.status_code == 200

    async def test_ensure_devices_claims_all(self, app, auth_headers, mock_device_pool):
        """ensure_devices with session_id claims all returned devices."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/devices/ensure",
                json={"count": 2, "name": "iPhone 16 Pro", "session_id": "bulk-test"},
                headers=auth_headers,
            )
            assert resp.status_code == 200
            devices = resp.json()["devices"]
            assert len(devices) == 2

            # Verify all are claimed via pool listing
            pool_resp = await client.get(
                "/api/v1/devices/pool?claimed=claimed",
                headers=auth_headers,
            )
            claimed = pool_resp.json()["devices"]
            claimed_udids = {d["udid"] for d in claimed}
            for d in devices:
                assert d["udid"] in claimed_udids

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
