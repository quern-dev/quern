"""Integration tests for device pool API endpoints."""

from __future__ import annotations

from datetime import datetime, timedelta
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
            ),
            DeviceInfo(
                udid="BBBB-2222",
                name="iPhone 15",
                state=DeviceState.SHUTDOWN,
                device_type=DeviceType.SIMULATOR,
                os_version="iOS 17.5",
                runtime="com.apple.CoreSimulator.SimRuntime.iOS-17-5",
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

    async def test_list_pool(self, app, auth_headers, mock_device_pool):
        """List pool endpoint returns device list."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/devices/pool", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert "devices" in data
        assert "total" in data
        assert isinstance(data["devices"], list)
        assert data["total"] == len(data["devices"])

    async def test_list_pool_with_state_filter(self, app, auth_headers, mock_device_pool):
        """List pool with state filter works."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/devices/pool", headers=auth_headers, params={"state": "booted"}
            )

        assert resp.status_code == 200
        data = resp.json()
        # All returned devices should be booted
        for device in data["devices"]:
            assert device["state"] == "booted"

    async def test_list_pool_with_claimed_filter(self, app, auth_headers, mock_device_pool):
        """List pool with claimed filter works."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # First claim a device
            await client.post(
                "/api/v1/devices/claim",
                headers=auth_headers,
                json={"session_id": "test-session", "name": "iPhone"},
            )

            # Then filter for claimed devices
            resp = await client.get(
                "/api/v1/devices/pool",
                headers=auth_headers,
                params={"claimed": "claimed"},
            )

        assert resp.status_code == 200
        data = resp.json()
        # All returned devices should be claimed
        for device in data["devices"]:
            assert device["claim_status"] == "claimed"

    async def test_list_pool_with_device_type_filter(self, app, auth_headers, mock_device_pool):
        """List pool with device_type filter works."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/devices/pool",
                headers=auth_headers,
                params={"device_type": "simulator"},
            )

        assert resp.status_code == 200
        data = resp.json()
        for device in data["devices"]:
            assert device["device_type"] == "simulator"

    async def test_claim_and_release_flow(self, app, auth_headers, mock_device_pool):
        """Full claim and release flow works."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Claim
            resp = await client.post(
                "/api/v1/devices/claim",
                headers=auth_headers,
                json={"session_id": "test-session", "name": "iPhone"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "claimed"
            assert "device" in data
            udid = data["device"]["udid"]
            assert data["device"]["claim_status"] == "claimed"
            assert data["device"]["claimed_by"] == "test-session"

            # Release
            resp = await client.post(
                "/api/v1/devices/release",
                headers=auth_headers,
                json={"udid": udid, "session_id": "test-session"},
            )
            assert resp.status_code == 200
            assert resp.json()["status"] == "released"

            # Verify it's available again
            resp = await client.get("/api/v1/devices/pool", headers=auth_headers)
            devices = resp.json()["devices"]
            claimed_device = next((d for d in devices if d["udid"] == udid), None)
            assert claimed_device is not None
            assert claimed_device["claim_status"] == "available"

    async def test_claim_by_udid(self, app, auth_headers, mock_device_pool):
        """Claiming by specific UDID works."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Get a device UDID first
            resp = await client.get("/api/v1/devices/pool", headers=auth_headers)
            devices = resp.json()["devices"]
            if not devices:
                pytest.skip("No devices available")

            udid = devices[0]["udid"]

            # Claim by UDID
            resp = await client.post(
                "/api/v1/devices/claim",
                headers=auth_headers,
                json={"session_id": "test", "udid": udid},
            )
            assert resp.status_code == 200
            assert resp.json()["device"]["udid"] == udid

    async def test_claim_already_claimed_returns_409(self, app, auth_headers, mock_device_pool):
        """Claiming already claimed device returns 409."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # First claim - use specific name to avoid matching multiple devices
            await client.post(
                "/api/v1/devices/claim",
                headers=auth_headers,
                json={"session_id": "session1", "name": "iPhone 16 Pro"},
            )

            # Second claim - same specific name
            resp = await client.post(
                "/api/v1/devices/claim",
                headers=auth_headers,
                json={"session_id": "session2", "name": "iPhone 16 Pro"},
            )
            assert resp.status_code == 409
            assert "already claimed" in resp.json()["detail"].lower()

    async def test_claim_not_found_returns_404(self, app, auth_headers, mock_device_pool):
        """Claiming nonexistent device returns 404."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/devices/claim",
                headers=auth_headers,
                json={"session_id": "test", "udid": "NONEXISTENT-UDID"},
            )
            assert resp.status_code == 404

    async def test_release_wrong_session_returns_500(self, app, auth_headers, mock_device_pool):
        """Releasing with wrong session ID returns error."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Claim with session1
            resp = await client.post(
                "/api/v1/devices/claim",
                headers=auth_headers,
                json={"session_id": "session1", "name": "iPhone"},
            )
            udid = resp.json()["device"]["udid"]

            # Try to release with session2
            resp = await client.post(
                "/api/v1/devices/release",
                headers=auth_headers,
                json={"udid": udid, "session_id": "session2"},
            )
            assert resp.status_code == 500
            assert "claimed by session" in resp.json()["detail"].lower()

    async def test_cleanup_endpoint(self, app, auth_headers, mock_device_pool):
        """Cleanup endpoint works."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/devices/cleanup",
                headers=auth_headers,
            )
            assert resp.status_code == 200
            data = resp.json()
            assert "devices_released" in data
            assert "count" in data
            assert isinstance(data["devices_released"], list)
            assert data["count"] == len(data["devices_released"])

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

    async def test_release_without_session_id(self, app, auth_headers, mock_device_pool):
        """Releasing without session ID works (no validation)."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Claim a device
            resp = await client.post(
                "/api/v1/devices/claim",
                headers=auth_headers,
                json={"session_id": "test", "name": "iPhone"},
            )
            udid = resp.json()["device"]["udid"]

            # Release without session_id (should work, skips validation)
            resp = await client.post(
                "/api/v1/devices/release",
                headers=auth_headers,
                json={"udid": udid},
            )
            assert resp.status_code == 200


class TestDevicePoolAuth:
    """Test authentication on device pool endpoints."""

    async def test_list_pool_requires_auth(self, app):
        """List pool endpoint requires authentication."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/devices/pool")
        assert resp.status_code == 401

    async def test_claim_requires_auth(self, app):
        """Claim endpoint requires authentication."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/devices/claim",
                json={"session_id": "test", "name": "iPhone"},
            )
        assert resp.status_code == 401

    async def test_release_requires_auth(self, app):
        """Release endpoint requires authentication."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/devices/release",
                json={"udid": "test-udid"},
            )
        assert resp.status_code == 401
