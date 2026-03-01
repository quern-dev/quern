"""Unit tests for device pool management."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from server.device.pool import DevicePool
from server.models import (
    DeviceInfo,
    DeviceState,
    DeviceType,
)


@pytest.fixture
def mock_controller():
    """Mock DeviceController with sample devices."""
    from server.device.controller import DeviceController

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
    return ctrl


@pytest.fixture
def pool(tmp_path, mock_controller):
    """DevicePool with temp state file."""
    pool = DevicePool(mock_controller)
    pool._pool_file = tmp_path / "device-pool.json"
    return pool


class TestListDevices:
    """Test device listing with filters."""

    async def test_list_all(self, pool):
        """List all devices returns all entries."""
        devices = await pool.list_devices()
        assert len(devices) == 2

    async def test_filter_by_state(self, pool):
        """Filter by boot state works correctly."""
        await pool.refresh_from_simctl()
        booted = await pool.list_devices(state_filter="booted")
        assert len(booted) == 1
        assert booted[0].state == DeviceState.BOOTED
        assert booted[0].name == "iPhone 16 Pro"


class TestListDevicesDeviceType:
    """Test device type filtering on list_devices."""

    @pytest.fixture
    def mixed_pool(self, tmp_path):
        """Pool with mixed simulators and physical devices."""
        from server.device.controller import DeviceController

        ctrl = DeviceController()
        ctrl.list_devices = AsyncMock(
            return_value=[
                DeviceInfo(
                    udid="SIM-1111",
                    name="iPhone 16 Pro",
                    state=DeviceState.BOOTED,
                    device_type=DeviceType.SIMULATOR,
                    os_version="iOS 18.2",
                    runtime="com.apple.CoreSimulator.SimRuntime.iOS-18-2",
                ),
                DeviceInfo(
                    udid="DEV-2222",
                    name="John's iPhone",
                    state=DeviceState.BOOTED,
                    device_type=DeviceType.DEVICE,
                    os_version="iOS 18.2",
                    connection_type="usb",
                ),
            ]
        )
        pool = DevicePool(ctrl)
        pool._pool_file = tmp_path / "device-pool.json"
        return pool

    async def test_filter_simulators(self, mixed_pool):
        """list_devices with device_type=SIMULATOR returns only simulators."""
        devices = await mixed_pool.list_devices(device_type=DeviceType.SIMULATOR)
        assert len(devices) == 1
        assert devices[0].device_type == DeviceType.SIMULATOR

    async def test_filter_physical(self, mixed_pool):
        """list_devices with device_type=DEVICE returns only physical devices."""
        devices = await mixed_pool.list_devices(device_type=DeviceType.DEVICE)
        assert len(devices) == 1
        assert devices[0].device_type == DeviceType.DEVICE

    async def test_no_filter_returns_all(self, mixed_pool):
        """list_devices with no device_type returns all devices."""
        devices = await mixed_pool.list_devices()
        assert len(devices) == 2


class TestRefreshCaching:
    """Test refresh caching behavior."""

    async def test_refresh_cache_prevents_redundant_simctl_calls(self, pool, mock_controller):
        """Verify 2-second cache TTL on refresh_from_simctl()."""
        await pool.refresh_from_simctl()
        first_call_count = mock_controller.list_devices.call_count

        # Immediate second refresh should use cache
        await pool.refresh_from_simctl()
        assert mock_controller.list_devices.call_count == first_call_count

        # After 2.1 seconds, should refresh again
        pool._last_refresh_at = datetime.now(timezone.utc) - timedelta(seconds=2.1)
        await pool.refresh_from_simctl()
        assert mock_controller.list_devices.call_count == first_call_count + 1
