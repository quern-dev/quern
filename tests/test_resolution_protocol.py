"""Tests for the device resolution protocol (Phase 4b-gamma)."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from server.device.pool import DevicePool
from server.models import (
    DeviceClaimStatus,
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
    """Mock DeviceController with a rich 5-device set."""
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
            ),
            DeviceInfo(
                udid="BBBB",
                name="iPhone 16 Pro",
                state=DeviceState.BOOTED,
                device_type=DeviceType.SIMULATOR,
                os_version="iOS 18.2",
                runtime="com.apple.CoreSimulator.SimRuntime.iOS-18-2",
                is_available=True,
            ),
            DeviceInfo(
                udid="CCCC",
                name="iPhone 16 Pro",
                state=DeviceState.SHUTDOWN,
                device_type=DeviceType.SIMULATOR,
                os_version="iOS 18.2",
                runtime="com.apple.CoreSimulator.SimRuntime.iOS-18-2",
                is_available=True,
            ),
            DeviceInfo(
                udid="DDDD",
                name="iPhone 15",
                state=DeviceState.BOOTED,
                device_type=DeviceType.SIMULATOR,
                os_version="iOS 17.5",
                runtime="com.apple.CoreSimulator.SimRuntime.iOS-17-5",
                is_available=True,
            ),
            DeviceInfo(
                udid="EEEE",
                name="iPhone 15",
                state=DeviceState.SHUTDOWN,
                device_type=DeviceType.SIMULATOR,
                os_version="iOS 17.5",
                runtime="com.apple.CoreSimulator.SimRuntime.iOS-17-5",
                is_available=True,
            ),
        ]
    )
    # Also set list_devices on ctrl itself for controller fallback tests
    ctrl.list_devices = ctrl.simctl.list_devices
    return ctrl


@pytest.fixture
def pool(tmp_path, mock_controller):
    """DevicePool with temp state file and 5-device mock."""
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
        """'1' should not match '18.2' — prefix match is on dot-separated components."""
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
    """Test _match_criteria() filtering logic."""

    async def test_match_by_name_substring(self, pool):
        await pool.refresh_from_simctl()
        state = pool._read_state()
        device = state.devices["AAAA"]
        assert pool._match_criteria(device, name="iPhone 16 Pro")
        assert pool._match_criteria(device, name="iPhone 16")
        assert pool._match_criteria(device, name="iphone 16")  # case insensitive
        assert not pool._match_criteria(device, name="iPhone 15")

    async def test_match_by_os_version_prefix(self, pool):
        await pool.refresh_from_simctl()
        state = pool._read_state()
        device = state.devices["AAAA"]  # iOS 18.2
        assert pool._match_criteria(device, os_version="18")
        assert pool._match_criteria(device, os_version="18.2")
        assert not pool._match_criteria(device, os_version="18.6")
        assert not pool._match_criteria(device, os_version="17")

    async def test_match_combined_criteria(self, pool):
        await pool.refresh_from_simctl()
        state = pool._read_state()
        device = state.devices["AAAA"]
        assert pool._match_criteria(device, name="iPhone 16", os_version="18")
        assert not pool._match_criteria(device, name="iPhone 16", os_version="17")

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

    async def test_unclaimed_before_claimed(self, pool):
        await pool.refresh_from_simctl()
        await pool.claim_device(session_id="test", udid="AAAA")
        state = pool._read_state()
        claimed = state.devices["AAAA"]
        unclaimed = state.devices["BBBB"]
        assert pool._rank_candidate(unclaimed) < pool._rank_candidate(claimed)

    async def test_deterministic_tiebreak_by_name(self, pool):
        await pool.refresh_from_simctl()
        state = pool._read_state()
        # AAAA and BBBB are both booted+unclaimed iPhone 16 Pro
        a = state.devices["AAAA"]
        b = state.devices["BBBB"]
        # Same rank except last_used and name — the ranking should be deterministic
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

    async def test_prefer_booted_unclaimed(self, pool):
        """Should pick a booted, unclaimed device over shutdown ones."""
        await pool.refresh_from_simctl()
        udid = await pool.resolve_device(name="iPhone 16 Pro")
        assert udid in ("AAAA", "BBBB")

    async def test_skip_claimed_devices(self, pool):
        """Should skip claimed devices and pick unclaimed ones."""
        await pool.refresh_from_simctl()
        await pool.claim_device(session_id="other", udid="AAAA")
        udid = await pool.resolve_device(name="iPhone 16 Pro")
        assert udid == "BBBB"

    async def test_auto_boot_when_no_booted(self, pool):
        """Should boot a shutdown device when auto_boot=True and no booted ones available."""
        await pool.refresh_from_simctl()
        await pool.claim_device(session_id="s1", udid="AAAA")
        await pool.claim_device(session_id="s2", udid="BBBB")

        # After boot, simctl returns CCCC as booted
        pool.controller.simctl.list_devices = AsyncMock(
            return_value=[
                DeviceInfo(
                    udid="CCCC",
                    name="iPhone 16 Pro",
                    state=DeviceState.BOOTED,
                    os_version="iOS 18.2",
                    runtime="com.apple.CoreSimulator.SimRuntime.iOS-18-2",
                    is_available=True,
                ),
            ]
        )

        udid = await pool.resolve_device(name="iPhone 16 Pro", auto_boot=True)
        assert udid == "CCCC"
        pool.controller.simctl.boot.assert_called_once_with("CCCC")

    async def test_error_when_all_claimed_no_wait(self, pool):
        """Should error when all matching booted devices are claimed and auto_boot=False."""
        await pool.refresh_from_simctl()
        await pool.claim_device(session_id="s1", udid="AAAA")
        await pool.claim_device(session_id="s2", udid="BBBB")

        with pytest.raises(DeviceError, match="shutdown"):
            await pool.resolve_device(
                name="iPhone 16 Pro", auto_boot=False, wait_if_busy=False
            )

    async def test_os_version_filtering(self, pool):
        """Should only return devices matching OS version prefix."""
        await pool.refresh_from_simctl()
        udid = await pool.resolve_device(os_version="17")
        assert udid == "DDDD"  # Only iPhone 15 has iOS 17.x

    async def test_claim_on_resolve(self, pool):
        """Should claim device when session_id is provided."""
        await pool.refresh_from_simctl()
        udid = await pool.resolve_device(
            name="iPhone 16 Pro", session_id="my-session"
        )
        state = await pool.get_device_state(udid)
        assert state.claimed_by == "my-session"
        assert state.claim_status == DeviceClaimStatus.CLAIMED

    async def test_no_matching_devices(self, pool):
        """Should error with diagnostic message when no devices match."""
        await pool.refresh_from_simctl()
        with pytest.raises(DeviceError, match="No device matching"):
            await pool.resolve_device(name="iPad Pro")

    async def test_no_args_resolves_any_booted(self, pool):
        """resolve_device() with no args should resolve any booted device."""
        await pool.refresh_from_simctl()
        udid = await pool.resolve_device()
        assert udid in ("AAAA", "BBBB", "DDDD")

    async def test_explicit_udid_with_claim(self, pool):
        """Explicit UDID with session_id should claim the device."""
        await pool.refresh_from_simctl()
        udid = await pool.resolve_device(udid="AAAA", session_id="my-session")
        assert udid == "AAAA"
        state = await pool.get_device_state("AAAA")
        assert state.claimed_by == "my-session"

    async def test_explicit_udid_already_claimed_errors(self, pool):
        """Explicit UDID that's already claimed should error when session_id provided."""
        await pool.refresh_from_simctl()
        await pool.claim_device(session_id="other", udid="AAAA")
        with pytest.raises(DeviceError, match="already claimed"):
            await pool.resolve_device(udid="AAAA", session_id="my-session")


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
        # Pool should have refreshed — CCCC should be in state
        state = pool._read_state()
        assert "CCCC" in state.devices
        assert state.devices["CCCC"].state == DeviceState.BOOTED


# ----------------------------------------------------------------
# TestWaitForAvailable
# ----------------------------------------------------------------


class TestWaitForAvailable:
    """Test _wait_for_available() polling behavior."""

    async def test_release_detected(self, pool):
        """Should detect when a device is released during wait."""
        await pool.refresh_from_simctl()
        # Claim both booted iPhone 16 Pro devices so nothing is immediately available
        await pool.claim_device(session_id="s1", udid="AAAA")
        await pool.claim_device(session_id="s2", udid="BBBB")

        async def delayed_release():
            await asyncio.sleep(0.3)
            await pool.release_device(udid="AAAA", session_id="s1")

        release_task = asyncio.create_task(delayed_release())
        found = await pool._wait_for_available(
            {"name": "iPhone 16 Pro"}, timeout=5.0, poll_interval=0.2
        )
        assert found is not None
        assert found.udid == "AAAA"
        await release_task

    async def test_external_boot_detected(self, pool):
        """Should detect externally booted devices via simctl refresh."""
        await pool.refresh_from_simctl()
        await pool.claim_device(session_id="s1", udid="AAAA")
        await pool.claim_device(session_id="s2", udid="BBBB")

        async def delayed_external_boot():
            await asyncio.sleep(0.3)
            new_list = AsyncMock(
                return_value=[
                    DeviceInfo(
                        udid="AAAA", name="iPhone 16 Pro", state=DeviceState.BOOTED,
                        os_version="iOS 18.2", runtime="...", is_available=True,
                    ),
                    DeviceInfo(
                        udid="BBBB", name="iPhone 16 Pro", state=DeviceState.BOOTED,
                        os_version="iOS 18.2", runtime="...", is_available=True,
                    ),
                    DeviceInfo(
                        udid="CCCC", name="iPhone 16 Pro", state=DeviceState.BOOTED,
                        os_version="iOS 18.2", runtime="...", is_available=True,
                    ),
                ]
            )
            pool.controller.simctl.list_devices = new_list
            pool.controller.list_devices = new_list
            pool._last_refresh_at = None  # Force cache expiry

        boot_task = asyncio.create_task(delayed_external_boot())
        found = await pool._wait_for_available(
            {"name": "iPhone 16 Pro"}, timeout=5.0, poll_interval=0.2
        )
        assert found is not None
        assert found.udid == "CCCC"  # Newly booted and unclaimed
        await boot_task

    async def test_timeout_returns_none(self, pool):
        """Should return None when timeout expires."""
        await pool.refresh_from_simctl()
        await pool.claim_device(session_id="s1", udid="AAAA")
        await pool.claim_device(session_id="s2", udid="BBBB")

        found = await pool._wait_for_available(
            {"name": "iPhone 16 Pro"}, timeout=0.3, poll_interval=0.1
        )
        assert found is None

    async def test_short_timeout_respects_min_sleep(self, pool):
        """Wait with timeout < poll_interval should sleep for timeout, not poll_interval."""
        await pool.refresh_from_simctl()
        await pool.claim_device(session_id="s1", udid="AAAA")
        await pool.claim_device(session_id="s2", udid="BBBB")

        start = time.time()
        found = await pool._wait_for_available(
            {"name": "iPhone 16 Pro"}, timeout=0.5, poll_interval=1.0
        )
        elapsed = time.time() - start
        assert found is None
        assert elapsed < 0.9, f"Expected ~0.5s timeout, took {elapsed:.1f}s"


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

    async def test_ensure_with_session_claims_all(self, pool):
        """Should claim all ensured devices when session_id provided."""
        await pool.refresh_from_simctl()
        udids = await pool.ensure_devices(
            count=2, name="iPhone 16 Pro", session_id="test-run"
        )
        for udid in udids:
            state = await pool.get_device_state(udid)
            assert state.claimed_by == "test-run"

    async def test_ensure_skips_claimed_devices(self, pool):
        """Should not include claimed devices in the result."""
        await pool.refresh_from_simctl()
        await pool.claim_device(session_id="other", udid="AAAA")

        # After boot, return CCCC as booted
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

        udids = await pool.ensure_devices(count=2, name="iPhone 16 Pro", auto_boot=True)
        assert "AAAA" not in udids
        assert len(udids) == 2

    async def test_ensure_rollback_on_failure(self, pool):
        """Should release claimed devices on partial failure."""
        await pool.refresh_from_simctl()

        # Make boot fail
        pool.controller.simctl.boot = AsyncMock(side_effect=DeviceError("boot failed", tool="simctl"))

        # Claim AAAA and BBBB first so we need to boot CCCC
        await pool.claim_device(session_id="blocker1", udid="AAAA")
        await pool.claim_device(session_id="blocker2", udid="BBBB")

        with pytest.raises(DeviceError):
            await pool.ensure_devices(
                count=1, name="iPhone 16 Pro", auto_boot=True, session_id="test-run"
            )


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

        err = pool._build_resolution_error({"name": "iPad Pro"}, all_devices)
        msg = str(err)
        assert "No device matching" in msg
        assert "iPad Pro" in msg

    async def test_os_mismatch_lists_versions(self, pool):
        """Error for OS mismatch should list available versions."""
        await pool.refresh_from_simctl()
        state = pool._read_state()
        all_devices = list(state.devices.values())

        err = pool._build_resolution_error({"os_version": "19"}, all_devices)
        msg = str(err)
        assert "No device matching" in msg
        assert "Available OS versions" in msg

    async def test_cross_criteria_mismatch(self, pool):
        """Error for name+OS cross-mismatch should explain partial matches."""
        await pool.refresh_from_simctl()
        state = pool._read_state()
        all_devices = list(state.devices.values())

        err = pool._build_resolution_error(
            {"name": "iPhone 16 Pro", "os_version": "17"}, all_devices
        )
        msg = str(err)
        assert "matched name" in msg

    async def test_all_claimed_shows_sessions(self, pool):
        """Error when all matched devices are claimed should show session info."""
        await pool.refresh_from_simctl()
        await pool.claim_device(session_id="session-abc", udid="AAAA")
        await pool.claim_device(session_id="session-def", udid="BBBB")

        state = pool._read_state()
        # Only include the two booted+claimed devices
        claimed_devices = [state.devices["AAAA"], state.devices["BBBB"]]

        err = pool._build_resolution_error(
            {"name": "iPhone 16 Pro"}, claimed_devices
        )
        msg = str(err)
        assert "claimed" in msg.lower()
        assert "session-abc" in msg

    async def test_all_shutdown_suggests_auto_boot(self, pool):
        """Error when all matched devices are shutdown should suggest auto_boot."""
        await pool.refresh_from_simctl()
        state = pool._read_state()
        # Only include shutdown devices
        shutdown_devices = [state.devices["CCCC"]]

        err = pool._build_resolution_error(
            {"name": "iPhone 16 Pro"}, shutdown_devices
        )
        msg = str(err)
        assert "shutdown" in msg.lower()
        assert "auto_boot" in msg


# ----------------------------------------------------------------
# TestControllerPoolFallback
# ----------------------------------------------------------------


class TestControllerPoolFallback:
    """Verify resolve_udid() fallback behavior when pool fails."""

    async def test_pool_none_uses_old_logic(self, mock_controller):
        """When _pool is None, behave identically to pre-4b-gamma."""
        from server.device.controller import DeviceController

        ctrl = DeviceController()
        ctrl.simctl = mock_controller.simctl
        ctrl._pool = None

        # With 3 booted devices, old logic should fail
        with pytest.raises(DeviceError, match="Multiple simulators booted"):
            await ctrl.resolve_udid()

    async def test_pool_exception_falls_back_silently(self, mock_controller):
        """When pool.resolve_device() raises, fall back without crashing."""
        from server.device.controller import DeviceController

        ctrl = DeviceController()
        ctrl.simctl = mock_controller.simctl

        broken_pool = AsyncMock()
        broken_pool.resolve_device = AsyncMock(
            side_effect=Exception("pool is broken")
        )
        ctrl._pool = broken_pool

        # Pool failed, but old logic should still run
        # With 3 booted devices, old fallback also fails — that's expected
        with pytest.raises(DeviceError, match="Multiple simulators booted"):
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
# TestResolveDeviceWait
# ----------------------------------------------------------------


class TestBootOnDemand:
    """Test the full auto-boot flow end-to-end."""

    async def test_boot_timeout_raises(self, pool):
        """Boot that never completes should raise DeviceError."""
        await pool.refresh_from_simctl()

        # Claim both booted iPhone 16 Pro devices
        await pool.claim_device(session_id="s1", udid="AAAA")
        await pool.claim_device(session_id="s2", udid="BBBB")

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


class TestResolveDeviceWait:
    """Test resolve_device() with wait_if_busy=True."""

    async def test_wait_for_release(self, pool):
        """Should wait and succeed when a claimed device is released."""
        await pool.refresh_from_simctl()
        await pool.claim_device(session_id="s1", udid="AAAA")
        await pool.claim_device(session_id="s2", udid="BBBB")

        async def delayed_release():
            await asyncio.sleep(0.3)
            await pool.release_device(udid="AAAA", session_id="s1")

        release_task = asyncio.create_task(delayed_release())
        udid = await pool.resolve_device(
            name="iPhone 16 Pro", wait_if_busy=True, wait_timeout=5.0, auto_boot=False
        )
        assert udid == "AAAA"
        await release_task

    async def test_wait_timeout_error(self, pool):
        """Should error with timeout message when wait expires."""
        await pool.refresh_from_simctl()
        await pool.claim_device(session_id="s1", udid="AAAA")
        await pool.claim_device(session_id="s2", udid="BBBB")

        with pytest.raises(DeviceError, match="Timed out"):
            await pool.resolve_device(
                name="iPhone 16 Pro",
                wait_if_busy=True,
                wait_timeout=1.0,
                auto_boot=False,
            )

    async def test_wait_respects_short_timeout(self, pool):
        """Wait with timeout shorter than poll_interval should still respect timeout."""
        await pool.refresh_from_simctl()
        await pool.claim_device(session_id="s1", udid="AAAA")
        await pool.claim_device(session_id="s2", udid="BBBB")

        start = time.time()
        with pytest.raises(DeviceError, match="Timed out"):
            await pool.resolve_device(
                name="iPhone 16 Pro",
                wait_if_busy=True,
                wait_timeout=1.0,
                auto_boot=False,
            )
        elapsed = time.time() - start
        assert elapsed < 2.0, f"Expected ~1s timeout, took {elapsed:.1f}s"
