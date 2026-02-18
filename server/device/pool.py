"""Device pool management for multi-device support."""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import re
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from server.device.controller import DeviceController
from server.models import DeviceClaimStatus, DeviceError, DevicePoolEntry, DevicePoolState, DeviceState, DeviceType

logger = logging.getLogger("quern-debug-server.device-pool")

POOL_FILE = Path.home() / ".quern" / "device-pool.json"
CLAIM_TIMEOUT = timedelta(minutes=30)  # Auto-release after 30 min
REFRESH_CACHE_TTL = timedelta(seconds=2)  # Avoid redundant simctl calls


class DevicePool:
    """Manages the pool of available devices and their claim states.

    Includes smart device resolution (Phase 4b-gamma): criteria matching,
    auto-boot, wait-for-available, and bulk provisioning. Integrates with
    DeviceController.resolve_udid() as the preferred resolution path.
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
            device.claimed_at = datetime.now(timezone.utc)
            device.last_used = datetime.now(timezone.utc)

            state.devices[device.udid] = device
            state.updated_at = datetime.now(timezone.utc)
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
            device.last_used = datetime.now(timezone.utc)

            state.devices[device.udid] = device
            state.updated_at = datetime.now(timezone.utc)
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
            now = datetime.now(timezone.utc)
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
        now = datetime.now(timezone.utc)
        if self._last_refresh_at and (now - self._last_refresh_at) < REFRESH_CACHE_TTL:
            return

        # Get current devices from simctl (~200-500ms, expensive)
        simctl_devices = await self.controller.list_devices()

        with self._lock_pool_file():
            state = self._read_state()
            now = datetime.now(timezone.utc)

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
    # Resolution protocol (Phase 4b-gamma)
    # ----------------------------------------------------------------

    @staticmethod
    def _os_version_matches(device_os: str, requested: str) -> bool:
        """Check if device OS version matches the requested version prefix.

        Accepts both bare versions ("18.2") and prefixed ("iOS 18.2").

        Examples:
            _os_version_matches("iOS 18.2", "18") → True
            _os_version_matches("iOS 18.2", "18.2") → True
            _os_version_matches("iOS 18.2", "iOS 18.2") → True
            _os_version_matches("iOS 18.2", "18.6") → False
            _os_version_matches("iOS 17.5", "18") → False
        """
        match = re.search(r"[\d.]+", device_os)
        if not match:
            return False
        device_version = match.group()
        # Strip platform prefix from requested (e.g. "iOS 18.2" → "18.2")
        req_match = re.search(r"[\d.]+", requested)
        if not req_match:
            return False
        req_version = req_match.group()
        return device_version == req_version or device_version.startswith(req_version + ".")

    def _match_criteria(
        self,
        device: DevicePoolEntry,
        name: str | None = None,
        os_version: str | None = None,
        device_type: DeviceType | None = None,
    ) -> bool:
        """Check if a device matches the given criteria.

        All criteria are AND'd. No criteria = match all available devices.
        Rejects is_available=False devices (corrupted/unavailable simulators).
        """
        if not device.is_available:
            return False
        if name and name.lower() not in device.name.lower():
            return False
        if os_version and not self._os_version_matches(device.os_version, os_version):
            return False
        if device_type and device.device_type != device_type:
            return False
        return True

    def _rank_candidate(self, device: DevicePoolEntry) -> tuple:
        """Return sort key for candidate ranking (lower = better).

        Priority:
        1. Booted before shutdown (avoid boot cost)
        2. Unclaimed before claimed (available now)
        3. Most recently used (warm simulator state, caches)
        4. Name alphabetical (deterministic tie-breaking)
        """
        return (
            0 if device.state == DeviceState.BOOTED else 1,
            0 if device.claim_status == DeviceClaimStatus.AVAILABLE else 1,
            -device.last_used.timestamp(),
            device.name,
        )

    async def _boot_and_wait(
        self,
        udid: str,
        timeout: float = 30.0,
        poll_interval: float = 0.5,
    ) -> None:
        """Boot a device and wait for it to reach 'booted' state.

        Raises DeviceError if boot fails or times out.
        """
        await self.controller.simctl.boot(udid)

        start = time.time()
        while time.time() - start < timeout:
            await asyncio.sleep(poll_interval)
            devices = await self.controller.simctl.list_devices()
            for d in devices:
                if d.udid == udid and d.state == DeviceState.BOOTED:
                    await self.refresh_from_simctl()
                    logger.info("Device %s booted in %.1fs", udid[:8], time.time() - start)
                    return

        raise DeviceError(
            f"Device {udid} did not boot within {timeout}s",
            tool="simctl",
        )

    async def _wait_for_available(
        self,
        criteria: dict,
        timeout: float = 30.0,
        poll_interval: float = 1.0,
    ) -> DevicePoolEntry | None:
        """Wait for a matching device to become available.

        Calls refresh_from_simctl() each iteration to detect both pool file
        changes (releases) and simctl state changes (external boots).
        Returns the first matching device or None on timeout.
        """
        start = time.time()
        while True:
            remaining = timeout - (time.time() - start)
            if remaining <= 0:
                return None
            await asyncio.sleep(min(poll_interval, remaining))

            await self.refresh_from_simctl()
            state = self._read_state()
            for device in state.devices.values():
                if (
                    device.claim_status == DeviceClaimStatus.AVAILABLE
                    and device.state == DeviceState.BOOTED
                    and self._match_criteria(device, **criteria)
                ):
                    return device
        return None

    def _build_resolution_error(
        self,
        criteria: dict,
        all_devices: list[DevicePoolEntry],
    ) -> DeviceError:
        """Build a diagnostic error explaining why resolution failed."""
        name = criteria.get("name")
        os_version = criteria.get("os_version")

        name_matched = [d for d in all_devices if not name or name.lower() in d.name.lower()]
        os_matched = [d for d in all_devices if not os_version or self._os_version_matches(d.os_version, os_version)]
        both_matched = [d for d in all_devices if self._match_criteria(d, **criteria)]

        criteria_parts = []
        if name:
            criteria_parts.append(f"name='{name}'")
        if os_version:
            criteria_parts.append(f"os_version='{os_version}'")
        criteria_str = ", ".join(criteria_parts) if criteria_parts else "no criteria"

        if not both_matched:
            parts = [f"No device matching {criteria_str}."]

            if name and os_version:
                name_only = [d for d in name_matched if d not in os_matched]
                os_only = [d for d in os_matched if d not in name_matched]
                if name_only:
                    versions = set(d.os_version for d in name_only)
                    parts.append(f"{len(name_only)} matched name but were {', '.join(sorted(versions))}")
                if os_only:
                    names = set(d.name for d in os_only)
                    parts.append(f"{len(os_only)} matched OS but were {', '.join(sorted(names))}")
                if not name_only and not os_only:
                    parts.append("No devices matched either criterion.")
            elif name and not name_matched:
                available_names = sorted(set(d.name for d in all_devices))
                parts.append(f"Available device names: {', '.join(available_names)}")
            elif os_version and not os_matched:
                available_versions = sorted(set(d.os_version for d in all_devices))
                parts.append(f"Available OS versions: {', '.join(available_versions)}")

            parts.append(f"Pool has {len(all_devices)} total devices.")
            return DeviceError(" ".join(parts), tool="pool")

        # Devices matched criteria but none are usable
        booted_unclaimed = [d for d in both_matched if d.state == DeviceState.BOOTED and d.claim_status == DeviceClaimStatus.AVAILABLE]
        claimed = [d for d in both_matched if d.claim_status == DeviceClaimStatus.CLAIMED]
        shutdown = [d for d in both_matched if d.state == DeviceState.SHUTDOWN and d.claim_status == DeviceClaimStatus.AVAILABLE]

        if claimed and not booted_unclaimed and not shutdown:
            claimed_info = ", ".join(
                f"{d.claimed_by} ({d.name}, {d.udid[:8]})" for d in claimed
            )
            return DeviceError(
                f"All {len(claimed)} devices matching {criteria_str} are claimed. "
                f"Use wait_if_busy=true to wait, or release a device first. "
                f"Claimed by: {claimed_info}",
                tool="pool",
            )

        if shutdown and not booted_unclaimed:
            shutdown_info = ", ".join(f"{d.name} ({d.udid[:8]})" for d in shutdown)
            return DeviceError(
                f"Found {len(shutdown)} matching devices but all are shutdown: {shutdown_info}. "
                f"Use auto_boot=true to boot one, or boot manually with boot_device.",
                tool="pool",
            )

        return DeviceError(f"No usable device matching {criteria_str}.", tool="pool")

    async def resolve_device(
        self,
        udid: str | None = None,
        name: str | None = None,
        os_version: str | None = None,
        device_type: DeviceType | None = None,
        auto_boot: bool = False,
        wait_if_busy: bool = False,
        wait_timeout: float = 30.0,
        session_id: str | None = None,
    ) -> str:
        """Resolve a device matching criteria, optionally boot and/or claim it.

        Returns the UDID of the resolved device.

        Resolution priority:
        1. Explicit UDID → use directly
        2. Booted + unclaimed + matching → use best match
        3. Shutdown + unclaimed + auto_boot → boot best match
        4. All claimed + wait_if_busy → wait for release
        5. No matches → diagnostic error
        """
        await self.refresh_from_simctl()

        # Priority 1: explicit UDID
        if udid:
            with self._lock_pool_file():
                state = self._read_state()
                if udid not in state.devices:
                    raise DeviceError(f"Device {udid} not found in pool", tool="pool")
                device = state.devices[udid]
                if session_id:
                    if device.claim_status == DeviceClaimStatus.CLAIMED:
                        raise DeviceError(
                            f"Device {udid} ({device.name}) is already claimed by session {device.claimed_by}",
                            tool="pool",
                        )
                    device.claim_status = DeviceClaimStatus.CLAIMED
                    device.claimed_by = session_id
                    device.claimed_at = datetime.now(timezone.utc)
                    device.last_used = datetime.now(timezone.utc)
                    state.updated_at = datetime.now(timezone.utc)
                    self._write_state(state)
                return udid

        criteria = {}
        if name:
            criteria["name"] = name
        if os_version:
            criteria["os_version"] = os_version
        if device_type:
            criteria["device_type"] = device_type

        state = self._read_state()
        all_devices = list(state.devices.values())
        candidates = [d for d in all_devices if self._match_criteria(d, **criteria)]

        if not candidates:
            raise self._build_resolution_error(criteria, all_devices)

        candidates.sort(key=self._rank_candidate)

        # Priority 2: booted + unclaimed
        booted_unclaimed = [
            d for d in candidates
            if d.state == DeviceState.BOOTED and d.claim_status == DeviceClaimStatus.AVAILABLE
        ]
        if booted_unclaimed:
            chosen = booted_unclaimed[0]
            if session_id:
                with self._lock_pool_file():
                    state = self._read_state()
                    device = state.devices[chosen.udid]
                    if device.claim_status == DeviceClaimStatus.CLAIMED:
                        # Race: someone else claimed it. Try next.
                        # Simplification: re-run resolve without this device
                        raise DeviceError(
                            f"Device {chosen.udid} was claimed by another session during resolution",
                            tool="pool",
                        )
                    device.claim_status = DeviceClaimStatus.CLAIMED
                    device.claimed_by = session_id
                    device.claimed_at = datetime.now(timezone.utc)
                    device.last_used = datetime.now(timezone.utc)
                    state.updated_at = datetime.now(timezone.utc)
                    self._write_state(state)
            return chosen.udid

        # Priority 3: shutdown + unclaimed + auto_boot
        shutdown_unclaimed = [
            d for d in candidates
            if d.state == DeviceState.SHUTDOWN and d.claim_status == DeviceClaimStatus.AVAILABLE
        ]
        if shutdown_unclaimed and auto_boot:
            chosen = shutdown_unclaimed[0]
            # Boot OUTSIDE lock to avoid blocking
            await self._boot_and_wait(chosen.udid)
            if session_id:
                with self._lock_pool_file():
                    state = self._read_state()
                    device = state.devices[chosen.udid]
                    device.claim_status = DeviceClaimStatus.CLAIMED
                    device.claimed_by = session_id
                    device.claimed_at = datetime.now(timezone.utc)
                    device.last_used = datetime.now(timezone.utc)
                    state.updated_at = datetime.now(timezone.utc)
                    self._write_state(state)
            return chosen.udid

        # Priority 4: wait for release
        if wait_if_busy:
            found = await self._wait_for_available(criteria, timeout=wait_timeout)
            if found:
                if session_id:
                    with self._lock_pool_file():
                        state = self._read_state()
                        device = state.devices[found.udid]
                        if device.claim_status == DeviceClaimStatus.AVAILABLE:
                            device.claim_status = DeviceClaimStatus.CLAIMED
                            device.claimed_by = session_id
                            device.claimed_at = datetime.now(timezone.utc)
                            device.last_used = datetime.now(timezone.utc)
                            state.updated_at = datetime.now(timezone.utc)
                            self._write_state(state)
                return found.udid

            # Timed out — build diagnostic error about claimed devices
            state = self._read_state()
            all_devices = list(state.devices.values())
            claimed = [
                d for d in all_devices
                if self._match_criteria(d, **criteria) and d.claim_status == DeviceClaimStatus.CLAIMED
            ]
            claimed_sessions = ", ".join(d.claimed_by or "unknown" for d in claimed)
            raise DeviceError(
                f"Timed out after {wait_timeout}s waiting for a device matching {', '.join(f'{k}={v!r}' for k, v in criteria.items()) or 'any'} "
                f"to become available. {len(claimed)} matching devices are claimed by: {claimed_sessions}",
                tool="pool",
            )

        # Priority 5: nothing viable
        raise self._build_resolution_error(criteria, all_devices)

    async def ensure_devices(
        self,
        count: int,
        name: str | None = None,
        os_version: str | None = None,
        device_type: DeviceType | None = None,
        auto_boot: bool = True,
        session_id: str | None = None,
    ) -> list[str]:
        """Ensure N devices matching criteria are booted and available.

        Returns list of UDIDs for the ready devices.
        Rolls back claims on partial failure.
        """
        await self.refresh_from_simctl()

        criteria = {}
        if name:
            criteria["name"] = name
        if os_version:
            criteria["os_version"] = os_version
        if device_type:
            criteria["device_type"] = device_type

        state = self._read_state()
        all_devices = list(state.devices.values())
        matching = [d for d in all_devices if self._match_criteria(d, **criteria)]

        booted_unclaimed = [
            d for d in matching
            if d.state == DeviceState.BOOTED and d.claim_status == DeviceClaimStatus.AVAILABLE
        ]
        shutdown_unclaimed = [
            d for d in matching
            if d.state == DeviceState.SHUTDOWN and d.claim_status == DeviceClaimStatus.AVAILABLE
        ]

        total_available = len(booted_unclaimed) + (len(shutdown_unclaimed) if auto_boot else 0)
        if total_available < count:
            criteria_str = ", ".join(f"{k}='{v}'" for k, v in criteria.items()) if criteria else "any"
            claimed_count = len([d for d in matching if d.claim_status == DeviceClaimStatus.CLAIMED])
            raise DeviceError(
                f"Need {count} devices matching {criteria_str} but only {total_available} available "
                f"({len(booted_unclaimed)} booted, {len(shutdown_unclaimed)} shutdown, {claimed_count} claimed).",
                tool="pool",
            )

        # Select devices: booted first, then shutdown to boot
        selected_udids: list[str] = []
        claimed_udids: list[str] = []

        try:
            # Take booted first
            for d in booted_unclaimed:
                if len(selected_udids) >= count:
                    break
                selected_udids.append(d.udid)

            # Boot more if needed
            for d in shutdown_unclaimed:
                if len(selected_udids) >= count:
                    break
                await self._boot_and_wait(d.udid)
                selected_udids.append(d.udid)

            # Claim all if session_id provided
            if session_id:
                with self._lock_pool_file():
                    state = self._read_state()
                    for udid in selected_udids:
                        device = state.devices[udid]
                        device.claim_status = DeviceClaimStatus.CLAIMED
                        device.claimed_by = session_id
                        device.claimed_at = datetime.now(timezone.utc)
                        device.last_used = datetime.now(timezone.utc)
                        claimed_udids.append(udid)
                    state.updated_at = datetime.now(timezone.utc)
                    self._write_state(state)

            return selected_udids

        except Exception:
            # Rollback: release any claimed devices
            if claimed_udids:
                for udid in claimed_udids:
                    try:
                        await self.release_device(udid, session_id=session_id)
                    except Exception:
                        pass
            raise

    # ----------------------------------------------------------------
    # Internal helpers
    # ----------------------------------------------------------------

    def _read_state(self) -> DevicePoolState:
        """Read pool state from disk."""
        if not self._pool_file.exists():
            return DevicePoolState(updated_at=datetime.now(timezone.utc), devices={})

        try:
            data = json.loads(self._pool_file.read_text())
            return DevicePoolState.model_validate(data)
        except Exception as e:
            logger.error("Failed to parse pool state file: %s", e)
            return DevicePoolState(updated_at=datetime.now(timezone.utc), devices={})

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
