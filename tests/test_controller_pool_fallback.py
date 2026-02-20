"""Tests for DeviceController.resolve_udid() pool fallback behavior."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from server.device.controller import DeviceController
from server.device.pool import DevicePool
from server.models import DeviceError, DeviceInfo, DeviceState, DeviceType


@pytest.fixture
def controller_with_pool(tmp_path):
    """Controller with a mock pool attached."""
    ctrl = DeviceController()
    ctrl.simctl = AsyncMock()
    ctrl.simctl.list_devices = AsyncMock(
        return_value=[
            DeviceInfo(
                udid="SOLO",
                name="iPhone 16 Pro",
                state=DeviceState.BOOTED,
                device_type=DeviceType.SIMULATOR,
                os_version="iOS 18.2",
                runtime="...",
                is_available=True,
            ),
        ]
    )
    ctrl.devicectl = AsyncMock()
    ctrl.devicectl.list_devices = AsyncMock(return_value=[])
    ctrl.usbmux = AsyncMock()
    ctrl.usbmux.list_devices = AsyncMock(return_value=[])

    pool = DevicePool(ctrl)
    pool._pool_file = tmp_path / "device-pool.json"
    ctrl._pool = pool
    return ctrl, pool


class TestPoolFallback:

    async def test_pool_none_uses_old_logic(self):
        """When _pool is None, behave identically to pre-4b-gamma."""
        ctrl = DeviceController()
        ctrl._pool = None
        ctrl.simctl = AsyncMock()
        ctrl.simctl.list_devices = AsyncMock(
            return_value=[
                DeviceInfo(
                    udid="ONLY",
                    name="iPhone 16 Pro",
                    state=DeviceState.BOOTED,
                    device_type=DeviceType.SIMULATOR,
                    os_version="iOS 18.2",
                    runtime="...",
                    is_available=True,
                ),
            ]
        )
        ctrl.devicectl = AsyncMock()
        ctrl.devicectl.list_devices = AsyncMock(return_value=[])
        ctrl.usbmux = AsyncMock()
        ctrl.usbmux.list_devices = AsyncMock(return_value=[])
        udid = await ctrl.resolve_udid()
        assert udid == "ONLY"

    async def test_pool_exception_falls_back_silently(self, controller_with_pool):
        """When pool.resolve_device() raises, fall back without crashing."""
        ctrl, pool = controller_with_pool
        pool.resolve_device = AsyncMock(side_effect=Exception("pool is broken"))

        udid = await ctrl.resolve_udid()
        assert udid == "SOLO"  # Fell back to simple logic

    async def test_pool_success_skips_fallback(self, controller_with_pool):
        """When pool resolves successfully, don't call simctl.list_devices."""
        ctrl, pool = controller_with_pool
        pool.resolve_device = AsyncMock(return_value="POOL-DEVICE")

        udid = await ctrl.resolve_udid()
        assert udid == "POOL-DEVICE"
        ctrl.simctl.list_devices.assert_not_called()

    async def test_explicit_udid_bypasses_pool(self, controller_with_pool):
        """Explicit UDID never touches the pool."""
        ctrl, pool = controller_with_pool
        pool.resolve_device = AsyncMock()

        udid = await ctrl.resolve_udid(udid="EXPLICIT")
        assert udid == "EXPLICIT"
        pool.resolve_device.assert_not_called()

    async def test_active_udid_bypasses_pool(self, controller_with_pool):
        """Stored active UDID never touches the pool."""
        ctrl, pool = controller_with_pool
        ctrl._active_udid = "STORED"
        pool.resolve_device = AsyncMock()

        udid = await ctrl.resolve_udid()
        assert udid == "STORED"
        pool.resolve_device.assert_not_called()
