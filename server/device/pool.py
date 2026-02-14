"""Device pool management for multi-device support."""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from server.device.controller import DeviceController
from server.models import DeviceClaimStatus, DeviceError, DevicePoolEntry, DevicePoolState, DeviceType

logger = logging.getLogger("quern-debug-server.device-pool")

POOL_FILE = Path.home() / ".quern" / "device-pool.json"
CLAIM_TIMEOUT = timedelta(minutes=30)  # Auto-release after 30 min
REFRESH_CACHE_TTL = timedelta(seconds=2)  # Avoid redundant simctl calls


class DevicePool:
    """Manages the pool of available devices and their claim states.

    TODO (Phase 4b-gamma): Unify with DeviceController.resolve_udid() - currently
    parallel systems. Pool should become the single source of truth for device
    resolution and availability.
    """

    def __init__(self, controller: DeviceController):
        self.controller = controller
        self._pool_file = POOL_FILE
        self._pool_file.parent.mkdir(parents=True, exist_ok=True)
        self._last_refresh_at: datetime | None = None

    # ----------------------------------------------------------------
    # Public API
    # ----------------------------------------------------------------

    async def list_devices(
        self,
        state_filter: str | None = None,  # "booted", "shutdown", None=all
        claimed_filter: str | None = None,  # "claimed", "available", None=all
    ) -> list[DevicePoolEntry]:
        """List all devices in the pool with optional filters."""
        await self.refresh_from_simctl()

        state = self._read_state()
        devices = list(state.devices.values())

        # Apply filters
        if state_filter:
            devices = [d for d in devices if d.state.value == state_filter]

        if claimed_filter == "claimed":
            devices = [d for d in devices if d.claim_status == DeviceClaimStatus.CLAIMED]
        elif claimed_filter == "available":
            devices = [d for d in devices if d.claim_status == DeviceClaimStatus.AVAILABLE]

        return devices

    async def claim_device(
        self,
        session_id: str,
        udid: str | None = None,
        name: str | None = None,
    ) -> DevicePoolEntry:
        """Claim a device for exclusive use by a session.

        Args:
            session_id: Session claiming the device
            udid: Specific device to claim (takes precedence)
            name: Device name pattern to match

        Returns:
            The claimed device entry

        Raises:
            DeviceError: If no matching device found or already claimed
        """
        await self.refresh_from_simctl()

        with self._lock_pool_file():
            state = self._read_state()

            # Find matching device
            if udid:
                device = state.devices.get(udid)
                if not device:
                    raise DeviceError(f"Device {udid} not found in pool", tool="pool")
            else:
                # Find first matching device by name (regardless of claim status)
                all_matches = [
                    d
                    for d in state.devices.values()
                    if (name is None or name.lower() in d.name.lower())
                ]
                if not all_matches:
                    raise DeviceError(
                        f"No device found matching name='{name}'", tool="pool"
                    )

                # Check if any are available
                available_matches = [
                    d for d in all_matches if d.claim_status == DeviceClaimStatus.AVAILABLE
                ]
                if not available_matches:
                    # All matches are claimed - raise appropriate error
                    claimed_device = all_matches[0]
                    raise DeviceError(
                        f"Device {claimed_device.udid} ({claimed_device.name}) is already claimed by session {claimed_device.claimed_by}",
                        tool="pool",
                    )
                device = available_matches[0]

            # Final check if already claimed (for UDID path)
            if device.claim_status == DeviceClaimStatus.CLAIMED:
                raise DeviceError(
                    f"Device {device.udid} ({device.name}) is already claimed by session {device.claimed_by}",
                    tool="pool",
                )

            # Claim it
            device.claim_status = DeviceClaimStatus.CLAIMED
            device.claimed_by = session_id
            device.claimed_at = datetime.utcnow()
            device.last_used = datetime.utcnow()

            state.devices[device.udid] = device
            state.updated_at = datetime.utcnow()
            self._write_state(state)

            logger.info(
                "Device claimed: %s (%s) by session %s", device.udid, device.name, session_id
            )

            return device

    async def release_device(
        self,
        udid: str,
        session_id: str | None = None,
    ) -> None:
        """Release a claimed device back to the pool.

        Args:
            udid: Device to release
            session_id: Session releasing (validated if provided)

        Raises:
            DeviceError: If device not found or not claimed by session
        """
        with self._lock_pool_file():
            state = self._read_state()

            device = state.devices.get(udid)
            if not device:
                raise DeviceError(f"Device {udid} not found in pool", tool="pool")

            if device.claim_status != DeviceClaimStatus.CLAIMED:
                logger.warning("Device %s was not claimed, ignoring release", udid)
                return

            # Validate session owns this device
            if session_id and device.claimed_by != session_id:
                raise DeviceError(
                    f"Device {udid} is claimed by session {device.claimed_by}, "
                    f"cannot release by session {session_id}",
                    tool="pool",
                )

            # Release it
            device.claim_status = DeviceClaimStatus.AVAILABLE
            device.claimed_by = None
            device.claimed_at = None
            device.last_used = datetime.utcnow()

            state.devices[device.udid] = device
            state.updated_at = datetime.utcnow()
            self._write_state(state)

            logger.info("Device released: %s (%s)", device.udid, device.name)

    async def get_device_state(self, udid: str) -> DevicePoolEntry | None:
        """Get the current state of a specific device."""
        state = self._read_state()
        return state.devices.get(udid)

    async def cleanup_stale_claims(self) -> list[str]:
        """Release devices with expired claims. Returns list of released UDIDs."""
        with self._lock_pool_file():
            state = self._read_state()
            now = datetime.utcnow()
            released = []

            for udid, device in state.devices.items():
                if device.claim_status == DeviceClaimStatus.CLAIMED and device.claimed_at:
                    age = now - device.claimed_at
                    if age > CLAIM_TIMEOUT:
                        age_minutes = int(age.total_seconds() / 60)
                        logger.warning(
                            "Released device %s (%s) - claimed by %s %d minutes ago (expired)",
                            device.udid,
                            device.name,
                            device.claimed_by,
                            age_minutes,
                        )
                        device.claim_status = DeviceClaimStatus.AVAILABLE
                        device.claimed_by = None
                        device.claimed_at = None
                        device.last_used = now
                        released.append(udid)

            if released:
                state.updated_at = now
                self._write_state(state)

            return released

    async def refresh_from_simctl(self) -> None:
        """Refresh pool state from simctl (discover new devices, update boot states).

        Cached for 2 seconds to avoid redundant simctl calls during rapid operations
        (e.g., parallel test execution claiming multiple devices).
        """
        # Check cache
        now = datetime.utcnow()
        if self._last_refresh_at and (now - self._last_refresh_at) < REFRESH_CACHE_TTL:
            return

        # Get current devices from simctl (~200-500ms, expensive)
        simctl_devices = await self.controller.list_devices()

        with self._lock_pool_file():
            state = self._read_state()
            now = datetime.utcnow()

            # Update or add devices
            for device_info in simctl_devices:
                if device_info.udid in state.devices:
                    # Update existing entry (preserve claim info)
                    entry = state.devices[device_info.udid]
                    entry.name = device_info.name
                    entry.state = device_info.state
                    entry.os_version = device_info.os_version
                    entry.runtime = device_info.runtime
                    entry.is_available = device_info.is_available
                else:
                    # New device discovered
                    entry = DevicePoolEntry(
                        udid=device_info.udid,
                        name=device_info.name,
                        state=device_info.state,
                        device_type=device_info.device_type,
                        os_version=device_info.os_version,
                        runtime=device_info.runtime,
                        claim_status=DeviceClaimStatus.AVAILABLE,
                        claimed_by=None,
                        claimed_at=None,
                        last_used=now,
                        is_available=device_info.is_available,
                    )
                    state.devices[device_info.udid] = entry

            state.updated_at = now
            self._write_state(state)
            self._last_refresh_at = now

    # ----------------------------------------------------------------
    # Internal helpers
    # ----------------------------------------------------------------

    def _read_state(self) -> DevicePoolState:
        """Read pool state from disk."""
        if not self._pool_file.exists():
            return DevicePoolState(updated_at=datetime.utcnow(), devices={})

        try:
            data = json.loads(self._pool_file.read_text())
            return DevicePoolState.model_validate(data)
        except Exception as e:
            logger.error("Failed to parse pool state file: %s", e)
            return DevicePoolState(updated_at=datetime.utcnow(), devices={})

    def _write_state(self, state: DevicePoolState) -> None:
        """Write pool state to disk."""
        self._pool_file.write_text(state.model_dump_json(indent=2, exclude_none=True))

    @contextmanager
    def _lock_pool_file(self):
        """Context manager for exclusive file locking."""
        lock_file = self._pool_file.parent / "device-pool.lock"
        lock_file.touch(exist_ok=True)

        with open(lock_file, "r") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
