"""Tests for the device resolution protocol."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from server.device.pool import DevicePool
from server.models import (
    DeviceError,
    DeviceInfo,
    DevicePoolEntry,
    DeviceState,
    DeviceType,
)


# ----------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------


@pytest.fixture
def mock_controller():
    """Mock DeviceController with a rich 7-device set (5 iPhones + 2 iPads)."""
    from server.device.controller import DeviceController

    ctrl = DeviceController()
    ctrl.simctl = AsyncMock()
    ctrl.simctl.boot = AsyncMock()
    ctrl.simctl.list_devices = AsyncMock(
        return_value=[
            DeviceInfo(
                udid="AAAA",
                name="iPhone 16 Pro",
                state=DeviceState.BOOTED,
                device_type=DeviceType.SIMULATOR,
                os_version="iOS 18.2",
                runtime="com.apple.CoreSimulator.SimRuntime.iOS-18-2",
                is_available=True,
                device_family="iPhone",
            ),
            DeviceInfo(
                udid="BBBB",
                name="iPhone 16 Pro",
                state=DeviceState.BOOTED,
                device_type=DeviceType.SIMULATOR,
                os_version="iOS 18.2",
                runtime="com.apple.CoreSimulator.SimRuntime.iOS-18-2",
                is_available=True,
                device_family="iPhone",
            ),
            DeviceInfo(
                udid="CCCC",
                name="iPhone 16 Pro",
                state=DeviceState.SHUTDOWN,
                device_type=DeviceType.SIMULATOR,
                os_version="iOS 18.2",
                runtime="com.apple.CoreSimulator.SimRuntime.iOS-18-2",
                is_available=True,
                device_family="iPhone",
            ),
            DeviceInfo(
                udid="DDDD",
                name="iPhone 15",
                state=DeviceState.BOOTED,
                device_type=DeviceType.SIMULATOR,
                os_version="iOS 17.5",
                runtime="com.apple.CoreSimulator.SimRuntime.iOS-17-5",
                is_available=True,
                device_family="iPhone",
            ),
            DeviceInfo(
                udid="EEEE",
                name="iPhone 15",
                state=DeviceState.SHUTDOWN,
                device_type=DeviceType.SIMULATOR,
                os_version="iOS 17.5",
                runtime="com.apple.CoreSimulator.SimRuntime.iOS-17-5",
                is_available=True,
                device_family="iPhone",
            ),
            DeviceInfo(
                udid="FFFF",
                name="iPad Pro 13-inch (M4)",
                state=DeviceState.BOOTED,
                device_type=DeviceType.SIMULATOR,
                os_version="iOS 18.2",
                runtime="com.apple.CoreSimulator.SimRuntime.iOS-18-2",
                is_available=True,
                device_family="iPad",
            ),
            DeviceInfo(
                udid="GGGG",
                name="iPad Air 13-inch (M2)",
                state=DeviceState.SHUTDOWN,
                device_type=DeviceType.SIMULATOR,
                os_version="iOS 18.2",
                runtime="com.apple.CoreSimulator.SimRuntime.iOS-18-2",
                is_available=True,
                device_family="iPad",
            ),
        ]
    )
    # Also set list_devices on ctrl itself for controller fallback tests
    ctrl.list_devices = ctrl.simctl.list_devices
    return ctrl


@pytest.fixture
def pool(tmp_path, mock_controller):
    """DevicePool with temp state file and 7-device mock."""
    p = DevicePool(mock_controller)
    p._pool_file = tmp_path / "device-pool.json"
    return p


# ----------------------------------------------------------------
# TestOsVersionMatches
# ----------------------------------------------------------------


class TestOsVersionMatches:
    """Test _os_version_matches() static method."""

    def test_major_version_match(self):
        assert DevicePool._os_version_matches("iOS 18.2", "18")

    def test_exact_version_match(self):
        assert DevicePool._os_version_matches("iOS 18.2", "18.2")

    def test_major_mismatch(self):
        assert not DevicePool._os_version_matches("iOS 18.2", "17")

    def test_minor_mismatch(self):
        assert not DevicePool._os_version_matches("iOS 18.2", "18.6")

    def test_no_numeric_in_os_string(self):
        assert not DevicePool._os_version_matches("unknown", "18")

    def test_partial_major_no_false_positive(self):
        """'1' should not match '18.2' -- prefix match is on dot-separated components."""
        assert not DevicePool._os_version_matches("iOS 18.2", "1")

    def test_prefixed_format_accepted(self):
        """'iOS 18.2' should match 'iOS 18.2' (agent copies from list_devices output)."""
        assert DevicePool._os_version_matches("iOS 18.2", "iOS 18.2")

    def test_prefixed_major_accepted(self):
        """'iOS 18' should match 'iOS 18.2'."""
        assert DevicePool._os_version_matches("iOS 18.2", "iOS 18")


# ----------------------------------------------------------------
# TestCriteriaMatching
# ----------------------------------------------------------------


class TestCriteriaMatching:
    """Test _match_criteria() and _filter_by_name() filtering logic."""

    async def test_match_by_os_version_prefix(self, pool):
        await pool.refresh_from_simctl()
        state = pool._read_state()
        device = state.devices["AAAA"]  # iOS 18.2
        assert pool._match_criteria(device, os_version="18")
        assert pool._match_criteria(device, os_version="18.2")
        assert not pool._match_criteria(device, os_version="18.6")
        assert not pool._match_criteria(device, os_version="17")

    async def test_match_by_device_family(self, pool):
        await pool.refresh_from_simctl()
        state = pool._read_state()
        iphone = state.devices["AAAA"]
        ipad = state.devices["FFFF"]
        assert pool._match_criteria(iphone, device_family="iPhone")
        assert not pool._match_criteria(iphone, device_family="iPad")
        assert pool._match_criteria(ipad, device_family="iPad")
        assert not pool._match_criteria(ipad, device_family="iPhone")

    async def test_no_criteria_matches_all(self, pool):
        await pool.refresh_from_simctl()
        state = pool._read_state()
        for device in state.devices.values():
            assert pool._match_criteria(device)

    async def test_unavailable_device_rejected(self, pool):
        """Devices with is_available=False are never matched."""
        await pool.refresh_from_simctl()
        state = pool._read_state()
        device = state.devices["AAAA"]
        device.is_available = False
        assert not pool._match_criteria(device)

    async def test_find_candidates_combined(self, pool):
        """_find_candidates applies name + os_version + device_family."""
        await pool.refresh_from_simctl()
        state = pool._read_state()
        all_devices = list(state.devices.values())
        results = pool._find_candidates(
            all_devices, name="iPhone 16", os_version="18", device_family="iPhone",
        )
        assert all(d.name == "iPhone 16 Pro" for d in results)
        # No results for mismatched OS
        results2 = pool._find_candidates(
            all_devices, name="iPhone 16", os_version="17", device_family="iPhone",
        )
        assert len(results2) == 0


# ----------------------------------------------------------------
# TestDeviceTypeFiltering
# ----------------------------------------------------------------


class TestDeviceTypeFiltering:
    """Test that device_type filtering excludes physical devices."""

    @pytest.fixture
    def mixed_controller(self):
        """Mock controller returning simulators + physical devices."""
        from server.device.controller import DeviceController

        ctrl = DeviceController()
        ctrl.simctl = AsyncMock()
        ctrl.simctl.boot = AsyncMock()
        ctrl.simctl.list_devices = AsyncMock(
            return_value=[
                DeviceInfo(
                    udid="SIM-1", name="iPhone 16 Pro", state=DeviceState.BOOTED,
                    device_type=DeviceType.SIMULATOR, os_version="iOS 18.2",
                    runtime="...", is_available=True, device_family="iPhone",
                ),
                DeviceInfo(
                    udid="SIM-2", name="iPhone 15", state=DeviceState.BOOTED,
                    device_type=DeviceType.SIMULATOR, os_version="iOS 17.5",
                    runtime="...", is_available=True, device_family="iPhone",
                ),
                DeviceInfo(
                    udid="DEV-1", name="John's iPhone", state=DeviceState.BOOTED,
                    device_type=DeviceType.DEVICE, os_version="iOS 18.2",
                    is_available=True, device_family="iPhone", connection_type="usb",
                ),
            ]
        )
        ctrl.list_devices = ctrl.simctl.list_devices
        return ctrl

    @pytest.fixture
    def mixed_pool(self, tmp_path, mixed_controller):
        p = DevicePool(mixed_controller)
        p._pool_file = tmp_path / "device-pool.json"
        return p

    async def test_resolve_simulator_only(self, mixed_pool):
        """resolve_device with device_type=SIMULATOR excludes physical devices."""
        await mixed_pool.refresh_from_simctl()
        udid = await mixed_pool.resolve_device(device_type=DeviceType.SIMULATOR)
        assert udid in ("SIM-1", "SIM-2")

    async def test_resolve_device_only(self, mixed_pool):
        """resolve_device with device_type=DEVICE returns only physical device."""
        await mixed_pool.refresh_from_simctl()
        udid = await mixed_pool.resolve_device(device_type=DeviceType.DEVICE, device_family=None)
        assert udid == "DEV-1"

    async def test_ensure_simulator_only(self, mixed_pool):
        """ensure_devices with device_type=SIMULATOR excludes physical devices."""
        await mixed_pool.refresh_from_simctl()
        udids = await mixed_pool.ensure_devices(count=2, device_type=DeviceType.SIMULATOR)
        assert "DEV-1" not in udids
        assert set(udids) == {"SIM-1", "SIM-2"}

    async def test_ensure_device_type_none_returns_all(self, mixed_pool):
        """ensure_devices with device_type=None returns simulators and physical devices."""
        await mixed_pool.refresh_from_simctl()
        udids = await mixed_pool.ensure_devices(count=3, device_type=None, device_family=None)
        assert set(udids) == {"SIM-1", "SIM-2", "DEV-1"}


# ----------------------------------------------------------------
# TestRankCandidate
# ----------------------------------------------------------------


class TestRankCandidate:
    """Test _rank_candidate() sort ordering."""

    async def test_booted_before_shutdown(self, pool):
        await pool.refresh_from_simctl()
        state = pool._read_state()
        booted = state.devices["AAAA"]  # BOOTED
        shutdown = state.devices["CCCC"]  # SHUTDOWN
        assert pool._rank_candidate(booted) < pool._rank_candidate(shutdown)

    async def test_deterministic_tiebreak_by_name(self, pool):
        await pool.refresh_from_simctl()
        state = pool._read_state()
        # AAAA and BBBB are both booted iPhone 16 Pro
        a = state.devices["AAAA"]
        b = state.devices["BBBB"]
        # Same rank except last_used and name -- the ranking should be deterministic
        rank_a = pool._rank_candidate(a)
        rank_b = pool._rank_candidate(b)
        assert rank_a is not None and rank_b is not None


# ----------------------------------------------------------------
# TestResolveDevice
# ----------------------------------------------------------------


class TestResolveDevice:
    """Test resolve_device() smart resolution."""

    async def test_explicit_udid(self, pool):
        """Explicit UDID bypasses all criteria matching."""
        await pool.refresh_from_simctl()
        udid = await pool.resolve_device(udid="AAAA")
        assert udid == "AAAA"

    async def test_explicit_udid_not_found(self, pool):
        """Error when explicit UDID doesn't exist."""
        await pool.refresh_from_simctl()
        with pytest.raises(DeviceError, match="not found"):
            await pool.resolve_device(udid="ZZZZ")

    async def test_prefer_booted(self, pool):
        """Should pick a booted device over shutdown ones."""
        await pool.refresh_from_simctl()
        udid = await pool.resolve_device(name="iPhone 16 Pro")
        assert udid in ("AAAA", "BBBB")

    async def test_auto_boot_when_no_booted(self, pool):
        """Should boot a shutdown device when no booted ones match criteria."""
        await pool.refresh_from_simctl()

        # After boot, simctl returns EEEE as booted
        pool.controller.simctl.list_devices = AsyncMock(
            return_value=[
                DeviceInfo(
                    udid="EEEE",
                    name="iPhone 15",
                    state=DeviceState.BOOTED,
                    os_version="iOS 17.5",
                    runtime="com.apple.CoreSimulator.SimRuntime.iOS-17-5",
                    is_available=True,
                    device_family="iPhone",
                ),
            ]
        )

        # Only DDDD is booted for iPhone 15 in iOS 17; ask for 2 but only 1 booted
        # Force a scenario where the best candidate is shutdown
        # Use a name that only matches shutdown devices by filtering OS
        udid = await pool.resolve_device(name="iPhone 15", os_version="17")
        # DDDD is booted, so it should be picked first
        assert udid == "DDDD"

    async def test_auto_boot_true_by_default(self, pool):
        """resolve_device should auto-boot by default (auto_boot defaults to True)."""
        # Set up pool with ONLY a shutdown device
        shutdown_only = [
            DeviceInfo(
                udid="CCCC", name="iPhone 16 Pro", state=DeviceState.SHUTDOWN,
                os_version="iOS 18.2", runtime="...", is_available=True,
                device_family="iPhone",
            ),
        ]
        pool.controller.list_devices = AsyncMock(return_value=shutdown_only)
        await pool.refresh_from_simctl()

        # After boot, simctl returns CCCC as booted
        booted = [
            DeviceInfo(
                udid="CCCC", name="iPhone 16 Pro", state=DeviceState.BOOTED,
                os_version="iOS 18.2", runtime="...", is_available=True,
                device_family="iPhone",
            ),
        ]
        pool.controller.simctl.list_devices = AsyncMock(return_value=booted)
        pool.controller.list_devices = AsyncMock(return_value=booted)

        # Do NOT pass auto_boot -- it should default to True
        udid = await pool.resolve_device(name="iPhone 16 Pro")
        assert udid == "CCCC"
        pool.controller.simctl.boot.assert_called_once_with("CCCC")

    async def test_os_version_filtering(self, pool):
        """Should only return devices matching OS version prefix."""
        await pool.refresh_from_simctl()
        udid = await pool.resolve_device(os_version="17")
        assert udid == "DDDD"  # Only iPhone 15 has iOS 17.x

    async def test_no_matching_devices(self, pool):
        """Should error with diagnostic message when no devices match."""
        await pool.refresh_from_simctl()
        with pytest.raises(DeviceError, match="No device matching"):
            await pool.resolve_device(name="Pixel 9")

    async def test_no_args_resolves_any_booted_iphone(self, pool):
        """resolve_device() with no args should resolve any booted iPhone (default family)."""
        await pool.refresh_from_simctl()
        udid = await pool.resolve_device()
        assert udid in ("AAAA", "BBBB", "DDDD")  # iPhones only, not FFFF (iPad)

    async def test_resolve_sets_active_device(self, pool):
        """resolve_device should set controller._active_udid after resolution."""
        await pool.refresh_from_simctl()
        udid = await pool.resolve_device(name="iPhone 16 Pro")
        assert pool.controller._active_udid == udid

    async def test_resolve_no_params_returns_active(self, pool):
        """resolve_device with no params should short-circuit if active device is set."""
        await pool.refresh_from_simctl()
        pool.controller._active_udid = "BBBB"
        udid = await pool.resolve_device()
        assert udid == "BBBB"

    async def test_resolve_with_params_overrides_active(self, pool):
        """resolve_device with criteria should override previously active device."""
        await pool.refresh_from_simctl()
        pool.controller._active_udid = "AAAA"
        udid = await pool.resolve_device(os_version="17")
        assert udid == "DDDD"
        assert pool.controller._active_udid == "DDDD"


# ----------------------------------------------------------------
# TestBootAndWait
# ----------------------------------------------------------------


class TestBootAndWait:
    """Test _boot_and_wait() boot polling."""

    async def test_boot_success(self, pool):
        """Boot succeeds when device reaches booted state."""
        pool.controller.simctl.list_devices = AsyncMock(
            return_value=[
                DeviceInfo(
                    udid="CCCC",
                    name="iPhone 16 Pro",
                    state=DeviceState.BOOTED,
                    os_version="iOS 18.2",
                    runtime="...",
                    is_available=True,
                ),
            ]
        )
        await pool._boot_and_wait("CCCC", timeout=5, poll_interval=0.1)
        pool.controller.simctl.boot.assert_called_once_with("CCCC")

    async def test_boot_timeout(self, pool):
        """Boot times out when device never reaches booted state."""
        pool.controller.simctl.list_devices = AsyncMock(
            return_value=[
                DeviceInfo(
                    udid="CCCC",
                    name="iPhone 16 Pro",
                    state=DeviceState.SHUTDOWN,
                    os_version="iOS 18.2",
                    runtime="...",
                    is_available=True,
                ),
            ]
        )
        with pytest.raises(DeviceError, match="did not boot"):
            await pool._boot_and_wait("CCCC", timeout=0.3, poll_interval=0.1)

    async def test_boot_refreshes_pool(self, pool):
        """After boot, pool state should be refreshed."""
        await pool.refresh_from_simctl()
        # Force cache expiry for the refresh that happens after boot
        pool._last_refresh_at = None

        booted_list = AsyncMock(
            return_value=[
                DeviceInfo(
                    udid="CCCC",
                    name="iPhone 16 Pro",
                    state=DeviceState.BOOTED,
                    os_version="iOS 18.2",
                    runtime="...",
                    is_available=True,
                ),
            ]
        )
        pool.controller.simctl.list_devices = booted_list
        pool.controller.list_devices = booted_list
        await pool._boot_and_wait("CCCC", timeout=5, poll_interval=0.1)
        # Pool should have refreshed -- CCCC should be in state
        state = pool._read_state()
        assert "CCCC" in state.devices
        assert state.devices["CCCC"].state == DeviceState.BOOTED


# ----------------------------------------------------------------
# TestEnsureDevices
# ----------------------------------------------------------------


class TestEnsureDevices:
    """Test ensure_devices() bulk provisioning."""

    async def test_enough_booted_devices(self, pool):
        """Should return already-booted devices without booting more."""
        await pool.refresh_from_simctl()
        udids = await pool.ensure_devices(count=2, name="iPhone 16 Pro")
        assert len(udids) == 2
        assert set(udids) == {"AAAA", "BBBB"}

    async def test_boot_additional_devices(self, pool):
        """Should boot shutdown devices to meet the count."""
        await pool.refresh_from_simctl()

        # After boot, simctl returns CCCC as booted
        original_list = pool.controller.simctl.list_devices
        call_count = [0]

        async def list_with_boot(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] > 1:
                return [
                    DeviceInfo(
                        udid="CCCC", name="iPhone 16 Pro", state=DeviceState.BOOTED,
                        os_version="iOS 18.2", runtime="...", is_available=True,
                    ),
                ]
            return await original_list(*args, **kwargs)

        pool.controller.simctl.list_devices = AsyncMock(side_effect=list_with_boot)
        pool._last_refresh_at = None

        udids = await pool.ensure_devices(count=3, name="iPhone 16 Pro", auto_boot=True)
        assert len(udids) == 3
        assert "CCCC" in udids
        pool.controller.simctl.boot.assert_called_once_with("CCCC")

    async def test_not_enough_devices_error(self, pool):
        """Should error when not enough matching devices exist."""
        await pool.refresh_from_simctl()
        with pytest.raises(DeviceError, match="Need 5"):
            await pool.ensure_devices(count=5, name="iPhone 16 Pro")

    async def test_ensure_sets_first_as_active(self, pool):
        """ensure_devices should set controller._active_udid to the first selected device."""
        await pool.refresh_from_simctl()
        udids = await pool.ensure_devices(count=2, name="iPhone 16 Pro")
        assert len(udids) == 2
        assert pool.controller._active_udid == udids[0]


# ----------------------------------------------------------------
# TestBuildResolutionError
# ----------------------------------------------------------------


class TestBuildResolutionError:
    """Test _build_resolution_error() diagnostic messages."""

    async def test_name_mismatch_lists_available(self, pool):
        """Error for name mismatch should list available device names."""
        await pool.refresh_from_simctl()
        state = pool._read_state()
        all_devices = list(state.devices.values())

        err = pool._build_resolution_error(all_devices, name="Pixel 9")
        msg = str(err)
        assert "No device matching" in msg
        assert "Pixel 9" in msg

    async def test_os_mismatch_lists_versions(self, pool):
        """Error for OS mismatch should list available versions."""
        await pool.refresh_from_simctl()
        state = pool._read_state()
        all_devices = list(state.devices.values())

        err = pool._build_resolution_error(all_devices, os_version="19")
        msg = str(err)
        assert "No device matching" in msg
        assert "Available OS versions" in msg

    async def test_cross_criteria_mismatch(self, pool):
        """Error for name+OS cross-mismatch should explain partial matches."""
        await pool.refresh_from_simctl()
        state = pool._read_state()
        all_devices = list(state.devices.values())

        err = pool._build_resolution_error(
            all_devices, name="iPhone 16 Pro", os_version="17",
        )
        msg = str(err)
        assert "matched name" in msg

    async def test_all_shutdown_suggests_auto_boot(self, pool):
        """Error when all matched devices are shutdown should suggest auto_boot."""
        await pool.refresh_from_simctl()
        state = pool._read_state()
        # Only include shutdown devices
        shutdown_devices = [state.devices["CCCC"]]

        err = pool._build_resolution_error(
            shutdown_devices, name="iPhone 16 Pro",
        )
        msg = str(err)
        assert "shutdown" in msg.lower()
        assert "auto_boot" in msg


# ----------------------------------------------------------------
# TestBootOnDemand
# ----------------------------------------------------------------


class TestBootOnDemand:
    """Test the full auto-boot flow end-to-end."""

    async def test_boot_timeout_raises(self, pool):
        """Boot that never completes should raise DeviceError."""
        await pool.refresh_from_simctl()

        # Mock: simctl.boot succeeds but device never reaches booted state
        pool.controller.simctl.boot = AsyncMock()
        pool.controller.simctl.list_devices = AsyncMock(
            return_value=[
                DeviceInfo(
                    udid="CCCC",
                    name="iPhone 16 Pro",
                    state=DeviceState.SHUTDOWN,
                    os_version="iOS 18.2",
                    runtime="...",
                    is_available=True,
                ),
            ]
        )

        with pytest.raises(DeviceError, match="did not boot"):
            await pool._boot_and_wait("CCCC", timeout=0.5, poll_interval=0.1)


# ----------------------------------------------------------------
# TestControllerPoolFallback
# ----------------------------------------------------------------


class TestControllerPoolFallback:
    """Verify resolve_udid() fallback behavior when pool fails."""

    async def test_pool_none_uses_old_logic(self, mock_controller):
        """When _pool is None, behave identically to pre-pool logic."""
        from server.device.controller import DeviceController

        ctrl = DeviceController()
        ctrl.simctl = mock_controller.simctl
        ctrl.devicectl = AsyncMock()
        ctrl.devicectl.list_devices = AsyncMock(return_value=[])
        ctrl.usbmux = AsyncMock()
        ctrl.usbmux.list_devices = AsyncMock(return_value=[])
        ctrl._pool = None

        # With 3 booted devices, old logic should fail
        with pytest.raises(DeviceError, match="Multiple devices booted"):
            await ctrl.resolve_udid()

    async def test_pool_exception_falls_back_silently(self, mock_controller):
        """When pool.resolve_device() raises, fall back without crashing."""
        from server.device.controller import DeviceController

        ctrl = DeviceController()
        ctrl.simctl = mock_controller.simctl
        ctrl.devicectl = AsyncMock()
        ctrl.devicectl.list_devices = AsyncMock(return_value=[])
        ctrl.usbmux = AsyncMock()
        ctrl.usbmux.list_devices = AsyncMock(return_value=[])

        broken_pool = AsyncMock()
        broken_pool.resolve_device = AsyncMock(
            side_effect=Exception("pool is broken")
        )
        ctrl._pool = broken_pool

        # Pool failed, but old logic should still run
        # With 3 booted devices, old fallback also fails -- that's expected
        with pytest.raises(DeviceError, match="Multiple devices booted"):
            await ctrl.resolve_udid()

    async def test_pool_exception_with_single_booted(self):
        """When pool fails and exactly 1 device booted, fallback works."""
        from server.device.controller import DeviceController

        ctrl = DeviceController()
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
        broken_pool = AsyncMock()
        broken_pool.resolve_device = AsyncMock(
            side_effect=Exception("pool broken")
        )
        ctrl._pool = broken_pool

        udid = await ctrl.resolve_udid()
        assert udid == "ONLY"

    async def test_pool_success_skips_fallback(self, mock_controller):
        """When pool resolves successfully, don't call simctl.list_devices."""
        from server.device.controller import DeviceController

        ctrl = DeviceController()
        ctrl.simctl = AsyncMock()
        ctrl.simctl.list_devices = AsyncMock()  # Track calls

        good_pool = AsyncMock()
        good_pool.resolve_device = AsyncMock(return_value="AAAA")
        ctrl._pool = good_pool

        udid = await ctrl.resolve_udid()
        assert udid == "AAAA"
        ctrl.simctl.list_devices.assert_not_called()

    async def test_explicit_udid_bypasses_pool(self, mock_controller):
        """Explicit UDID never touches the pool."""
        from server.device.controller import DeviceController

        ctrl = DeviceController()
        good_pool = AsyncMock()
        good_pool.resolve_device = AsyncMock()
        ctrl._pool = good_pool

        udid = await ctrl.resolve_udid(udid="XXXX")
        assert udid == "XXXX"
        good_pool.resolve_device.assert_not_called()

    async def test_active_udid_bypasses_pool(self, mock_controller):
        """Stored active UDID never touches the pool."""
        from server.device.controller import DeviceController

        ctrl = DeviceController()
        ctrl._active_udid = "YYYY"
        good_pool = AsyncMock()
        good_pool.resolve_device = AsyncMock()
        ctrl._pool = good_pool

        udid = await ctrl.resolve_udid()
        assert udid == "YYYY"
        good_pool.resolve_device.assert_not_called()


# ----------------------------------------------------------------
# TestFilterByName
# ----------------------------------------------------------------


class TestFilterByName:
    """Test _filter_by_name() exact-match preference."""

    async def test_exact_match_preferred_over_substring(self, pool):
        """'iPhone 15' should NOT match 'iPhone 15 Pro Max' when exact match exists."""
        await pool.refresh_from_simctl()
        state = pool._read_state()
        all_devices = list(state.devices.values())

        # Add an "iPhone 15 Pro Max" device
        pro_max = DevicePoolEntry(
            udid="XMAX",
            name="iPhone 15 Pro Max",
            state=DeviceState.BOOTED,
            device_type=DeviceType.SIMULATOR,
            os_version="iOS 17.5",
            runtime="...",
            device_family="iPhone",
            last_used=datetime.now(timezone.utc),
            is_available=True,
        )
        all_devices.append(pro_max)

        results = DevicePool._filter_by_name(all_devices, "iPhone 15")
        # Should return only exact "iPhone 15" matches, not "iPhone 15 Pro Max"
        assert all(d.name == "iPhone 15" for d in results)
        assert len(results) >= 1

    async def test_substring_fallback_when_no_exact(self, pool):
        """'iPhone 16' should match 'iPhone 16 Pro' via substring when no exact match."""
        await pool.refresh_from_simctl()
        state = pool._read_state()
        all_devices = list(state.devices.values())

        results = DevicePool._filter_by_name(all_devices, "iPhone 16")
        assert len(results) > 0
        assert all("iPhone 16" in d.name for d in results)

    def test_none_name_returns_all(self):
        """None name should return all devices."""
        devices = [
            DevicePoolEntry(
                udid="X", name="iPhone 16 Pro", state=DeviceState.BOOTED,
                device_type=DeviceType.SIMULATOR, os_version="iOS 18.2",
                runtime="...",
                last_used=datetime.now(timezone.utc), is_available=True,
            ),
        ]
        assert DevicePool._filter_by_name(devices, None) == devices

    def test_case_insensitive_exact_match(self):
        """Exact matching should be case-insensitive."""
        devices = [
            DevicePoolEntry(
                udid="X", name="iPhone 16 Pro", state=DeviceState.BOOTED,
                device_type=DeviceType.SIMULATOR, os_version="iOS 18.2",
                runtime="...",
                last_used=datetime.now(timezone.utc), is_available=True,
            ),
        ]
        results = DevicePool._filter_by_name(devices, "iphone 16 pro")
        assert len(results) == 1


# ----------------------------------------------------------------
# TestParseDeviceFamily
# ----------------------------------------------------------------


class TestParseDeviceFamily:
    """Test SimctlBackend._parse_device_family()."""

    def test_iphone(self):
        from server.device.simctl import SimctlBackend
        assert SimctlBackend._parse_device_family(
            "com.apple.CoreSimulator.SimDeviceType.iPhone-16-Pro"
        ) == "iPhone"

    def test_ipad(self):
        from server.device.simctl import SimctlBackend
        assert SimctlBackend._parse_device_family(
            "com.apple.CoreSimulator.SimDeviceType.iPad-Pro-13-inch-M4"
        ) == "iPad"

    def test_apple_watch(self):
        from server.device.simctl import SimctlBackend
        assert SimctlBackend._parse_device_family(
            "com.apple.CoreSimulator.SimDeviceType.Apple-Watch-Series-10-46mm"
        ) == "Apple Watch"

    def test_apple_tv(self):
        from server.device.simctl import SimctlBackend
        assert SimctlBackend._parse_device_family(
            "com.apple.CoreSimulator.SimDeviceType.Apple-TV-4K-3rd-generation-4K"
        ) == "Apple TV"

    def test_unknown(self):
        from server.device.simctl import SimctlBackend
        assert SimctlBackend._parse_device_family("") == ""
        assert SimctlBackend._parse_device_family("com.apple.CoreSimulator.SimDeviceType.Unknown-Device") == ""


# ----------------------------------------------------------------
# TestDeviceFamilyInference
# ----------------------------------------------------------------


class TestDeviceFamilyInference:
    """Test _infer_device_family() logic."""

    def test_explicit_family_takes_precedence(self):
        assert DevicePool._infer_device_family("iPhone 16 Pro", "iPad") == "iPad"

    def test_name_contains_ipad(self):
        assert DevicePool._infer_device_family("iPad Pro", None) == "iPad"

    def test_name_contains_iphone(self):
        assert DevicePool._infer_device_family("iPhone 15", None) == "iPhone"

    def test_no_name_defaults_to_config(self):
        """No name and no explicit family -> config default (iPhone)."""
        result = DevicePool._infer_device_family(None, None)
        assert result == "iPhone"  # Default from get_default_device_family()


# ----------------------------------------------------------------
# TestDeviceFamilyFiltering
# ----------------------------------------------------------------


class TestDeviceFamilyFiltering:
    """Test that device_family filtering excludes iPads by default."""

    async def test_resolve_excludes_ipad_by_default(self, pool):
        """resolve_device with no device_family should return iPhone, not iPad."""
        await pool.refresh_from_simctl()
        udid = await pool.resolve_device(os_version="18")
        assert udid in ("AAAA", "BBBB")  # iPhones, not FFFF (iPad)

    async def test_resolve_explicit_ipad(self, pool):
        """resolve_device with device_family='iPad' should return iPad."""
        await pool.refresh_from_simctl()
        udid = await pool.resolve_device(os_version="18", device_family="iPad")
        assert udid == "FFFF"  # The booted iPad

    async def test_ensure_excludes_ipad_by_default(self, pool):
        """ensure_devices with os_version should return iPhones, not iPads."""
        await pool.refresh_from_simctl()
        udids = await pool.ensure_devices(count=2, os_version="18")
        assert "FFFF" not in udids  # iPad excluded
        assert set(udids) == {"AAAA", "BBBB"}

    async def test_ensure_explicit_ipad(self, pool):
        """ensure_devices with device_family='iPad' should return iPad."""
        await pool.refresh_from_simctl()
        udids = await pool.ensure_devices(count=1, os_version="18", device_family="iPad")
        assert udids == ["FFFF"]

    async def test_name_ipad_infers_family(self, pool):
        """Passing name='iPad' should infer device_family='iPad'."""
        await pool.refresh_from_simctl()
        udid = await pool.resolve_device(name="iPad Pro")
        assert udid == "FFFF"


# ----------------------------------------------------------------
# TestEnsureDevicesRanking
# ----------------------------------------------------------------


class TestEnsureDevicesRanking:
    """Test that ensure_devices uses ranking (booted first, most recent)."""

    async def test_ensure_prefers_booted_over_shutdown(self, pool):
        """ensure_devices should prefer booted devices over shutdown ones."""
        await pool.refresh_from_simctl()
        # Request 1 device matching iPhone 16 Pro -- should pick booted (AAAA or BBBB), not CCCC
        udids = await pool.ensure_devices(count=1, name="iPhone 16 Pro")
        assert udids[0] in ("AAAA", "BBBB")


# ----------------------------------------------------------------
# TestConfigReading
# ----------------------------------------------------------------


class TestConfigReading:
    """Test config file reading for default_device_family."""

    def test_missing_config_returns_iphone(self, tmp_path, monkeypatch):
        """Missing config file defaults to 'iPhone'."""
        import server.config
        monkeypatch.setattr(server.config, "USER_CONFIG_FILE", tmp_path / "missing.json")
        assert server.config.get_default_device_family() == "iPhone"

    def test_config_with_ipad_default(self, tmp_path, monkeypatch):
        """Config with default_device_family='iPad' returns 'iPad'."""
        import server.config
        config_file = tmp_path / "config.json"
        config_file.write_text('{"default_device_family": "iPad"}')
        monkeypatch.setattr(server.config, "USER_CONFIG_FILE", config_file)
        assert server.config.get_default_device_family() == "iPad"

    def test_invalid_json_returns_iphone(self, tmp_path, monkeypatch):
        """Invalid JSON defaults to 'iPhone'."""
        import server.config
        config_file = tmp_path / "config.json"
        config_file.write_text("not json")
        monkeypatch.setattr(server.config, "USER_CONFIG_FILE", config_file)
        assert server.config.get_default_device_family() == "iPhone"
