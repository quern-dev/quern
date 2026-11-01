"""API tests for resolution protocol endpoints."""

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
        assert data["active"] is True
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

    async def test_resolve_with_device_type(self, app, auth_headers, mock_device_pool):
        """Resolve with device_type='simulator' returns only simulators."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/devices/resolve",
                headers=auth_headers,
                json={"device_type": "simulator"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "udid" in data

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
        assert "active_udid" in data

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
                        udid="CCCC-3333",
                        name="iPhone 16 Pro",
                        state=DeviceState.BOOTED,
                        os_version="iOS 18.2",
                        runtime="...",
                        is_available=True,
                        device_family="iPhone",
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

    async def _ensure(self, app, auth_headers, **body):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post("/api/v1/devices/ensure", headers=auth_headers, json=body)

    async def test_ensure_more_than_exist_is_409(self, app, auth_headers, mock_device_pool):
        """Some match but too few: the caller's request, not a server fault.
        It was a 500 (#586 follow-up)."""
        resp = await self._ensure(app, auth_headers, count=10, name="iPhone 16 Pro")
        assert resp.status_code == 409
        assert "Need 10" in resp.json()["detail"]

    async def test_ensure_with_no_match_is_404(self, app, auth_headers, mock_device_pool):
        resp = await self._ensure(app, auth_headers, count=1, name="No Such Phone")
        assert resp.status_code == 404
        assert "only 0 available (0 booted, 0 shutdown)" in resp.json()["detail"]

    async def test_ensure_without_booting_says_why_shut_down_ones_did_not_count(
            self, app, auth_headers, mock_device_pool):
        # Three iPhone 16 Pros, one shut down: three wanted with auto_boot
        # off is two available, and the answer says so.
        resp = await self._ensure(app, auth_headers, count=3, name="iPhone 16 Pro",
                                  auto_boot=False)
        assert resp.status_code == 409
        assert "auto_boot is off" in resp.json()["detail"]

    @staticmethod
    def _real_listing(pool, simctl):
        """The controller's own list_devices over a fake simctl (the fixture
        swaps it for simctl's), every other backend empty."""
        ctrl = pool.controller
        del ctrl.list_devices
        ctrl.simctl.list_devices = simctl
        for backend in (ctrl.devicectl, ctrl.usbmux, ctrl.adb):
            backend.list_devices = AsyncMock(return_value=[])
        pool._last_refresh_at = None

    async def test_ensure_when_listing_failed_is_503_not_404(self, app, auth_headers,
                                                             mock_device_pool):
        """simctl ran and failed: the empty list is "could not tell", not
        "none exist" (review)."""
        from server.models import DeviceError

        self._real_listing(mock_device_pool, AsyncMock(
            side_effect=DeviceError("CoreSimulatorService connection invalid", tool="simctl")))
        resp = await self._ensure(app, auth_headers, count=1, name="No Such Phone")
        assert resp.status_code == 503
        assert "listing devices failed (simctl: CoreSimulatorService" in resp.json()["detail"]
        # An Android pick is not about simctl: still "none".
        resp = await self._ensure(app, auth_headers, count=1, name="No Such Phone",
                                  platform="android")
        assert resp.status_code == 404

    async def test_ensure_reads_the_failures_of_the_listing_it_used(self, app, auth_headers,
                                                                    mock_device_pool):
        """A listing elsewhere, inside the pool's cache window, must not
        change the answer drawn from the cached state (review)."""
        from server.models import DeviceError

        self._real_listing(mock_device_pool, AsyncMock(
            side_effect=DeviceError("CoreSimulatorService connection invalid", tool="simctl")))
        await mock_device_pool.refresh()
        mock_device_pool.controller.listing_failures = {}    # an unrelated listing since
        resp = await self._ensure(app, auth_headers, count=1, name="No Such Phone")
        assert resp.status_code == 503

    async def test_ensure_without_xcode_is_still_404(self, app, auth_headers, mock_device_pool):
        """A tool that is not installed is absent, not failed."""
        self._real_listing(mock_device_pool, AsyncMock(side_effect=FileNotFoundError("xcrun")))
        resp = await self._ensure(app, auth_headers, count=1, name="No Such Phone")
        assert resp.status_code == 404

    async def test_ensure_counts_a_booting_match(self, app, auth_headers, mock_device_pool):
        """The only match still booting: it exists, so 409, and it is named
        in the count (review)."""
        self._real_listing(mock_device_pool, AsyncMock(return_value=[
            DeviceInfo(udid="EEEE-5555", name="iPhone Booting", state=DeviceState.BOOTING,
                       device_type=DeviceType.SIMULATOR, os_version="iOS 18.2",
                       runtime="com.apple.CoreSimulator.SimRuntime.iOS-18-2",
                       device_family="iPhone"),
        ]))
        resp = await self._ensure(app, auth_headers, count=1, name="iPhone Booting")
        assert resp.status_code == 409
        assert "1 booting or otherwise busy" in resp.json()["detail"]

    async def test_ensure_with_device_type(self, app, auth_headers, mock_device_pool):
        """Ensure with device_type='simulator' returns only simulators."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/devices/ensure",
                headers=auth_headers,
                json={"count": 2, "device_type": "simulator"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["devices"]) == 2
