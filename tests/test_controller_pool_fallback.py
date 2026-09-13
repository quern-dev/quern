"""Tests for DeviceController.resolve_udid() pool fallback behavior."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from server.device.controller import DeviceController
from server.device.pool import DevicePool
from server.models import DeviceInfo, DeviceState, DeviceType


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
    ctrl.adb = AsyncMock()
    ctrl.adb.list_devices = AsyncMock(return_value=[])

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
        ctrl.adb = AsyncMock()
        ctrl.adb.list_devices = AsyncMock(return_value=[])
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


@pytest.mark.asyncio
async def test_devicectl_cannot_relabel_a_simulator_as_a_device():
    """The controller's type cache decides how UI reads are routed.

    simctl populates it first, devicectl second, and devicectl's loop used to
    stamp `DeviceType.DEVICE` for every entry regardless of what the backend
    said. Any UDID in both lists therefore ended up typed `device` -- which is
    every simulator, once Xcode 26 started registering them as CoreDevices.
    From there `resolve_device` wrote `{"type": "device"}`, UI reads routed to
    WDA instead of sim-bridge, and `get_screen_summary` returned HTTP 500
    against a healthy booted simulator.

    devicectl now filters simulators out, so in practice this cannot happen.
    This covers the second half: if anything ever does return one, the cache
    must believe the backend rather than the loop it arrived in.
    """
    from server.device.controller import DeviceController
    from server.models import DeviceInfo, DeviceState, DeviceType

    udid = "F5AF3736-C05F-493F-AA52-CA883B13B18C"
    sim = DeviceInfo(
        udid=udid, name="iPhone 16 Pro", state=DeviceState.BOOTED,
        device_type=DeviceType.SIMULATOR, os_version="iOS 26.0",
    )
    # The same device, as devicectl would hand it over if it ever did.
    from_devicectl = DeviceInfo(
        udid=udid, name="iPhone 16 Pro", state=DeviceState.BOOTED,
        device_type=DeviceType.SIMULATOR, os_version="iOS 26.0",
    )

    ctrl = DeviceController()
    ctrl.simctl = AsyncMock()
    ctrl.simctl.list_devices = AsyncMock(return_value=[sim])
    ctrl.devicectl = AsyncMock()
    ctrl.devicectl.list_devices = AsyncMock(return_value=[from_devicectl])
    ctrl.usbmux = AsyncMock()
    ctrl.usbmux.list_devices = AsyncMock(return_value=[])
    ctrl.adb = AsyncMock()
    ctrl.adb.list_devices = AsyncMock(return_value=[])

    await ctrl.list_devices()

    assert ctrl._device_type_cache[udid] == DeviceType.SIMULATOR, (
        "devicectl's pass relabelled a simulator as a physical device"
    )
