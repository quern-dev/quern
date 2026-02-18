"""Unit tests for device pool management."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from server.device.pool import DevicePool
from server.models import (
    DeviceClaimStatus,
    DeviceError,
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
        assert all(d.claim_status == DeviceClaimStatus.AVAILABLE for d in devices)

    async def test_filter_by_state(self, pool):
        """Filter by boot state works correctly."""
        await pool.refresh_from_simctl()
        booted = await pool.list_devices(state_filter="booted")
        assert len(booted) == 1
        assert booted[0].state == DeviceState.BOOTED
        assert booted[0].name == "iPhone 16 Pro"

    async def test_filter_by_claimed(self, pool):
        """Filter by claim status works correctly."""
        await pool.refresh_from_simctl()
        await pool.claim_device(session_id="test", name="iPhone")

        claimed = await pool.list_devices(claimed_filter="claimed")
        assert len(claimed) == 1
        assert claimed[0].claim_status == DeviceClaimStatus.CLAIMED

        available = await pool.list_devices(claimed_filter="available")
        assert len(available) == 1
        assert available[0].claim_status == DeviceClaimStatus.AVAILABLE


class TestClaimDevice:
    """Test device claiming functionality."""

    async def test_claim_by_name(self, pool):
        """Claim device by name pattern works."""
        device = await pool.claim_device(session_id="test-session", name="iPhone 16")
        assert device.claim_status == DeviceClaimStatus.CLAIMED
        assert device.claimed_by == "test-session"
        assert device.claimed_at is not None
        assert device.udid == "AAAA-1111"

    async def test_claim_by_udid(self, pool):
        """Claim device by UDID works."""
        await pool.refresh_from_simctl()
        device = await pool.claim_device(session_id="test", udid="AAAA-1111")
        assert device.udid == "AAAA-1111"
        assert device.claimed_by == "test"

    async def test_claim_already_claimed_errors(self, pool):
        """Attempting to claim an already claimed device raises error."""
        await pool.refresh_from_simctl()
        await pool.claim_device(session_id="session1", name="iPhone 16")

        with pytest.raises(DeviceError, match="already claimed"):
            await pool.claim_device(session_id="session2", name="iPhone 16")

    async def test_claim_not_found_errors(self, pool):
        """Claiming nonexistent device raises error."""
        await pool.refresh_from_simctl()
        with pytest.raises(DeviceError, match="not found"):
            await pool.claim_device(session_id="test", udid="NONEXISTENT")

    async def test_claim_no_match_errors(self, pool):
        """Claiming with name that matches nothing raises error."""
        await pool.refresh_from_simctl()
        with pytest.raises(DeviceError, match="No device found"):
            await pool.claim_device(session_id="test", name="Android")


class TestReleaseDevice:
    """Test device release functionality."""

    async def test_release_claimed_device(self, pool):
        """Releasing a claimed device works."""
        await pool.refresh_from_simctl()
        device = await pool.claim_device(session_id="test", name="iPhone")

        await pool.release_device(udid=device.udid, session_id="test")

        state = await pool.get_device_state(device.udid)
        assert state.claim_status == DeviceClaimStatus.AVAILABLE
        assert state.claimed_by is None

    async def test_release_wrong_session_errors(self, pool):
        """Releasing device with wrong session ID raises error."""
        await pool.refresh_from_simctl()
        device = await pool.claim_device(session_id="session1", name="iPhone")

        with pytest.raises(DeviceError, match="claimed by session"):
            await pool.release_device(udid=device.udid, session_id="session2")

    async def test_release_unclaimed_is_noop(self, pool):
        """Releasing an unclaimed device is a no-op."""
        await pool.refresh_from_simctl()
        devices = await pool.list_devices()

        # Should not raise
        await pool.release_device(udid=devices[0].udid)

    async def test_release_not_found_errors(self, pool):
        """Releasing nonexistent device raises error."""
        with pytest.raises(DeviceError, match="not found"):
            await pool.release_device(udid="NONEXISTENT")


class TestStaleClaimCleanup:
    """Test stale claim cleanup functionality."""

    async def test_cleanup_stale_claims(self, pool):
        """Cleanup releases devices with expired claims."""
        await pool.refresh_from_simctl()
        device = await pool.claim_device(session_id="test", name="iPhone")

        # Manually backdate claim
        state = pool._read_state()
        state.devices[device.udid].claimed_at = datetime.now(timezone.utc) - timedelta(minutes=31)
        pool._write_state(state)

        released = await pool.cleanup_stale_claims()

        assert device.udid in released
        assert len(released) == 1

        # Verify it's available
        updated = await pool.get_device_state(device.udid)
        assert updated.claim_status == DeviceClaimStatus.AVAILABLE
        assert updated.claimed_by is None

    async def test_cleanup_no_stale_claims(self, pool):
        """Cleanup with no stale claims returns empty list."""
        await pool.refresh_from_simctl()
        await pool.claim_device(session_id="test", name="iPhone")

        released = await pool.cleanup_stale_claims()
        assert len(released) == 0


class TestConcurrentClaims:
    """Test concurrent claim handling."""

    async def test_concurrent_claims_no_double_booking(self, pool):
        """File locking prevents race conditions in concurrent claims."""
        await pool.refresh_from_simctl()

        # Fire two claims concurrently for the same device
        results = await asyncio.gather(
            pool.claim_device(session_id="session1", name="iPhone 16 Pro"),
            pool.claim_device(session_id="session2", name="iPhone 16 Pro"),
            return_exceptions=True,
        )

        # One should succeed, one should raise DeviceError
        from server.models import DevicePoolEntry

        successes = [r for r in results if isinstance(r, DevicePoolEntry)]
        errors = [r for r in results if isinstance(r, DeviceError)]

        assert len(successes) == 1, "Exactly one claim should succeed"
        assert len(errors) == 1, "Exactly one claim should fail"
        assert "already claimed" in str(errors[0]).lower()

    async def test_concurrent_release_and_claim(self, pool):
        """Concurrent release and claim operations are safe."""
        await pool.refresh_from_simctl()
        device = await pool.claim_device(session_id="session1", name="iPhone")

        # Try to release and claim concurrently
        results = await asyncio.gather(
            pool.release_device(udid=device.udid, session_id="session1"),
            pool.claim_device(session_id="session2", udid=device.udid),
            return_exceptions=True,
        )

        # One should succeed, the other might error or succeed depending on order
        # The key is that there should be no corruption or race condition
        errors = [r for r in results if isinstance(r, Exception)]
        # At most one should error
        assert len(errors) <= 1


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


class TestRefreshPreservesClaimInfo:
    """Test that refresh preserves claim information."""

    async def test_refresh_preserves_claimed_devices(self, pool):
        """Refreshing from simctl preserves claim information."""
        await pool.refresh_from_simctl()
        device = await pool.claim_device(session_id="test", name="iPhone")

        # Refresh again
        await pool.refresh_from_simctl()

        # Claim info should still be intact
        updated = await pool.get_device_state(device.udid)
        assert updated.claim_status == DeviceClaimStatus.CLAIMED
        assert updated.claimed_by == "test"
        assert updated.claimed_at is not None
