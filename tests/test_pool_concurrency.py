"""Concurrency and race condition tests for device pool."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from server.device.pool import DevicePool
from server.models import DeviceClaimStatus, DeviceError, DeviceInfo, DeviceState, DeviceType


@pytest.fixture
def pool_3_devices(tmp_path):
    """Pool with 3 booted, unclaimed devices."""
    from server.device.controller import DeviceController

    ctrl = DeviceController()
    ctrl.simctl = AsyncMock()
    ctrl.simctl.boot = AsyncMock()
    ctrl.simctl.list_devices = AsyncMock(
        return_value=[
            DeviceInfo(
                udid=f"DEV-{i}",
                name="iPhone 16 Pro",
                state=DeviceState.BOOTED,
                device_type=DeviceType.SIMULATOR,
                os_version="iOS 18.2",
                runtime="com.apple.CoreSimulator.SimRuntime.iOS-18-2",
                is_available=True,
            )
            for i in range(3)
        ]
    )
    ctrl.list_devices = ctrl.simctl.list_devices
    pool = DevicePool(ctrl)
    pool._pool_file = tmp_path / "device-pool.json"
    return pool


class TestConcurrentClaims:
    """Verify no double-booking under concurrent access."""

    async def test_concurrent_claims_no_double_booking(self, pool_3_devices):
        """Three concurrent claims on 3 devices should each get a unique device."""
        pool = pool_3_devices
        await pool.refresh_from_simctl()

        results = await asyncio.gather(
            pool.claim_device(session_id="s1", name="iPhone 16 Pro"),
            pool.claim_device(session_id="s2", name="iPhone 16 Pro"),
            pool.claim_device(session_id="s3", name="iPhone 16 Pro"),
        )

        udids = [r.udid for r in results]
        assert len(set(udids)) == 3, f"Expected 3 unique UDIDs, got {udids}"

    async def test_fourth_concurrent_claim_fails(self, pool_3_devices):
        """Four concurrent claims on 3 devices — one must fail."""
        pool = pool_3_devices
        await pool.refresh_from_simctl()

        results = await asyncio.gather(
            pool.claim_device(session_id="s1", name="iPhone 16 Pro"),
            pool.claim_device(session_id="s2", name="iPhone 16 Pro"),
            pool.claim_device(session_id="s3", name="iPhone 16 Pro"),
            pool.claim_device(session_id="s4", name="iPhone 16 Pro"),
            return_exceptions=True,
        )

        successes = [r for r in results if not isinstance(r, Exception)]
        failures = [r for r in results if isinstance(r, DeviceError)]
        assert len(successes) == 3
        assert len(failures) == 1
        assert "claimed" in str(failures[0]).lower()

    async def test_concurrent_resolve_no_double_booking(self, pool_3_devices):
        """Concurrent resolve_device calls with session_id should not double-book."""
        pool = pool_3_devices
        await pool.refresh_from_simctl()

        results = await asyncio.gather(
            pool.resolve_device(name="iPhone 16 Pro", session_id="s1"),
            pool.resolve_device(name="iPhone 16 Pro", session_id="s2"),
            pool.resolve_device(name="iPhone 16 Pro", session_id="s3"),
        )

        assert len(set(results)) == 3

    async def test_claim_then_release_then_reclaim(self, pool_3_devices):
        """Release makes a device immediately available for re-claiming."""
        pool = pool_3_devices
        await pool.refresh_from_simctl()

        device = await pool.claim_device(session_id="s1", name="iPhone 16 Pro")
        await pool.release_device(udid=device.udid, session_id="s1")

        device2 = await pool.claim_device(session_id="s2", udid=device.udid)
        assert device2.udid == device.udid
        assert device2.claimed_by == "s2"


class TestCleanupRecovery:
    """Test session expiry and orphan cleanup."""

    async def test_expired_claim_auto_released(self, pool_3_devices):
        """Claims older than CLAIM_TIMEOUT should be released by cleanup."""
        pool = pool_3_devices
        await pool.refresh_from_simctl()

        device = await pool.claim_device(session_id="s1", name="iPhone 16 Pro")

        # Manually backdate the claim
        with pool._lock_pool_file():
            state = pool._read_state()
            entry = state.devices[device.udid]
            entry.claimed_at = entry.claimed_at - timedelta(minutes=60)
            pool._write_state(state)

        released = await pool.cleanup_stale_claims()
        assert device.udid in released

        state_after = pool._read_state()
        assert state_after.devices[device.udid].claim_status == DeviceClaimStatus.AVAILABLE

    async def test_multiple_sessions_share_pool(self, pool_3_devices):
        """Two sessions can each claim different devices from the same pool."""
        pool = pool_3_devices
        await pool.refresh_from_simctl()

        d1 = await pool.claim_device(session_id="session-A", name="iPhone 16 Pro")
        d2 = await pool.claim_device(session_id="session-B", name="iPhone 16 Pro")

        assert d1.udid != d2.udid
        assert d1.claimed_by == "session-A"
        assert d2.claimed_by == "session-B"

        # Release one, verify the other is still claimed
        await pool.release_device(udid=d1.udid, session_id="session-A")
        state = pool._read_state()
        assert state.devices[d1.udid].claim_status == DeviceClaimStatus.AVAILABLE
        assert state.devices[d2.udid].claim_status == DeviceClaimStatus.CLAIMED
