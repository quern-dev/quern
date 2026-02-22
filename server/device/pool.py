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

from server.config import get_default_device_family
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
        device_type: DeviceType | None = None,  # "simulator", "device", None=all
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

        if device_type:
            devices = [d for d in devices if d.device_type == device_type]

        return devices

    async def claim_device(
        self,
        session_id: str,
        udid: str | None = None,
        name: str | None = None,
        device_family: str | None = None,
        device_type: DeviceType | None = None,
    ) -> DevicePoolEntry:
        """Claim a device for exclusive use by a session.

        Args:
            session_id: Session claiming the device
            udid: Specific device to claim (takes precedence)
            name: Device name pattern to match
            device_family: Device family filter (e.g. "iPhone", "iPad")

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
                all_devices = list(state.devices.values())
                effective_family = self._infer_device_family(name, device_family)
                all_matches = self._find_candidates(
                    all_devices, name=name, device_family=effective_family,
                    device_type=device_type,
                )
                if not all_matches:
                    raise DeviceError(
                        f"No device found matching name='{name}'", tool="pool"
                    )

                # Check if any are available
                available_matches = [
                    d for d in all_matches if d.claim_status == DeviceClaimStatus.AVAILABLE
                ]
                if not available_matches:
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
                    entry.device_family = device_info.device_family
                else:
                    # New device discovered
                    entry = DevicePoolEntry(
                        udid=device_info.udid,
                        name=device_info.name,
                        state=device_info.state,
                        device_type=device_info.device_type,
                        os_version=device_info.os_version,
                        runtime=device_info.runtime,
                        device_family=device_info.device_family,
                        claim_status=DeviceClaimStatus.AVAILABLE,
                        claimed_by=None,
                        claimed_at=None,
                        last_used=now,
                        is_available=device_info.is_available,
                    )
                    state.devices[device_info.udid] = entry

            # Remove devices that no longer exist in simctl
            live_udids = {d.udid for d in simctl_devices}
            stale_udids = [uid for uid in state.devices if uid not in live_udids]
            for uid in stale_udids:
                removed = state.devices.pop(uid)
                logger.info(
                    "Pruned deleted device from pool: %s (%s, %s)",
                    uid[:8], removed.name, removed.os_version,
                )

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
        os_version: str | None = None,
        device_type: DeviceType | None = None,
        device_family: str | None = None,
    ) -> bool:
        """Check if a device matches the given criteria (excluding name).

        Name filtering is handled separately by _filter_by_name() because
        exact-match preference requires the full candidate list.

        All criteria are AND'd. No criteria = match all available devices.
        Rejects is_available=False devices (corrupted/unavailable simulators).
        """
        if not device.is_available:
            return False
        if os_version and not self._os_version_matches(device.os_version, os_version):
            return False
        if device_type and device.device_type != device_type:
            return False
        if device_family and self._effective_device_family(device).lower() != device_family.lower():
            return False
        return True

    @staticmethod
    def _filter_by_name(
        devices: list[DevicePoolEntry],
        name: str | None,
    ) -> list[DevicePoolEntry]:
        """Filter devices by name, preferring exact matches over substring.

        If any device name matches exactly (case-insensitive), return only
        exact matches. Otherwise fall back to substring matching.
        """
        if not name:
            return devices
        name_lower = name.lower()
        exact = [d for d in devices if d.name.lower() == name_lower]
        if exact:
            return exact
        return [d for d in devices if name_lower in d.name.lower()]

    @staticmethod
    def _effective_device_family(device: DevicePoolEntry) -> str:
        """Get the effective device family, inferring from name if not set.

        Handles backwards compat with pool entries that have device_family=''.
        """
        if device.device_family:
            return device.device_family
        name_lower = device.name.lower()
        if "ipad" in name_lower:
            return "iPad"
        if "iphone" in name_lower:
            return "iPhone"
        if "apple watch" in name_lower:
            return "Apple Watch"
        if "apple tv" in name_lower:
            return "Apple TV"
        return ""

    @staticmethod
    def _infer_device_family(
        name: str | None,
        device_family: str | None,
    ) -> str | None:
        """Determine effective device_family filter for resolution.

        Priority:
        1. Explicit device_family from caller → use it
        2. Name contains family hint (e.g. "iPad") → infer that family
        3. Fall back to config default (usually "iPhone")
        """
        if device_family is not None:
            return device_family
        if name:
            name_lower = name.lower()
            if "ipad" in name_lower:
                return "iPad"
            if "apple watch" in name_lower:
                return "Apple Watch"
            if "apple tv" in name_lower:
                return "Apple TV"
            if "iphone" in name_lower:
                return "iPhone"
        return get_default_device_family()

    def _find_candidates(
        self,
        devices: list[DevicePoolEntry],
        name: str | None = None,
        os_version: str | None = None,
        device_type: DeviceType | None = None,
        device_family: str | None = None,
    ) -> list[DevicePoolEntry]:
        """Find candidates matching all criteria including name and device_family."""
        matched = [
            d for d in devices
            if self._match_criteria(d, os_version=os_version, device_type=device_type, device_family=device_family)
        ]
        return self._filter_by_name(matched, name)

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
        name: str | None = None,
        os_version: str | None = None,
        device_type: DeviceType | None = None,
        device_family: str | None = None,
        session_id: str | None = None,
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
            candidates = self._find_candidates(
                list(state.devices.values()),
                name=name, os_version=os_version,
                device_type=device_type, device_family=device_family,
            )
            for device in candidates:
                is_available = (
                    device.claim_status == DeviceClaimStatus.AVAILABLE
                    or (session_id and device.claimed_by == session_id)
                )
                if is_available and device.state == DeviceState.BOOTED:
                    return device
        return None

    def _build_resolution_error(
        self,
        all_devices: list[DevicePoolEntry],
        name: str | None = None,
        os_version: str | None = None,
        device_type: DeviceType | None = None,
        device_family: str | None = None,
    ) -> DeviceError:
        """Build a diagnostic error explaining why resolution failed."""
        name_matched = self._filter_by_name(
            [d for d in all_devices if d.is_available], name,
        )
        os_matched = [d for d in all_devices if d.is_available and (not os_version or self._os_version_matches(d.os_version, os_version))]
        both_matched = self._find_candidates(
            all_devices, name=name, os_version=os_version,
            device_type=device_type, device_family=device_family,
        )

        criteria_parts = []
        if name:
            criteria_parts.append(f"name='{name}'")
        if os_version:
            criteria_parts.append(f"os_version='{os_version}'")
        if device_family:
            criteria_parts.append(f"device_family='{device_family}'")
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
                available_names = sorted(set(d.name for d in all_devices if d.is_available))
                parts.append(f"Available device names: {', '.join(available_names)}")
            elif os_version and not os_matched:
                available_versions = sorted(set(d.os_version for d in all_devices if d.is_available))
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
        device_family: str | None = None,
        auto_boot: bool = False,
        wait_if_busy: bool = False,
        wait_timeout: float = 30.0,
        session_id: str | None = None,
    ) -> str:
        """Resolve a device matching criteria, optionally boot and/or claim it.

        Returns the UDID of the resolved device.

        Resolution priority:
        1. Explicit UDID → use directly
        2. Booted + available (unclaimed or owned by session) → use best match
        3. Shutdown + available + auto_boot → boot best match
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
                    if device.claim_status == DeviceClaimStatus.CLAIMED and device.claimed_by != session_id:
                        raise DeviceError(
                            f"Device {udid} ({device.name}) is already claimed by session {device.claimed_by}",
                            tool="pool",
                        )
                    if device.claim_status != DeviceClaimStatus.CLAIMED:
                        device.claim_status = DeviceClaimStatus.CLAIMED
                        device.claimed_by = session_id
                        device.claimed_at = datetime.now(timezone.utc)
                    device.last_used = datetime.now(timezone.utc)
                    state.updated_at = datetime.now(timezone.utc)
                    self._write_state(state)
                return udid

        effective_family = self._infer_device_family(name, device_family)

        state = self._read_state()
        all_devices = list(state.devices.values())
        candidates = self._find_candidates(
            all_devices, name=name, os_version=os_version,
            device_type=device_type, device_family=effective_family,
        )

        if not candidates:
            raise self._build_resolution_error(
                all_devices, name=name, os_version=os_version,
                device_type=device_type, device_family=effective_family,
            )

        candidates.sort(key=self._rank_candidate)

        def _is_available(d: DevicePoolEntry) -> bool:
            return (
                d.claim_status == DeviceClaimStatus.AVAILABLE
                or (session_id is not None and d.claimed_by == session_id)
            )

        # Priority 2: booted + available (unclaimed or owned by session)
        booted_available = [
            d for d in candidates
            if d.state == DeviceState.BOOTED and _is_available(d)
        ]
        if booted_available:
            chosen = booted_available[0]
            if session_id and chosen.claimed_by != session_id:
                with self._lock_pool_file():
                    state = self._read_state()
                    device = state.devices[chosen.udid]
                    if device.claim_status == DeviceClaimStatus.CLAIMED and device.claimed_by != session_id:
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

        # Priority 3: shutdown + available + auto_boot
        shutdown_available = [
            d for d in candidates
            if d.state == DeviceState.SHUTDOWN and _is_available(d)
        ]
        if shutdown_available and auto_boot:
            chosen = shutdown_available[0]
            await self._boot_and_wait(chosen.udid)
            if session_id and chosen.claimed_by != session_id:
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
            found = await self._wait_for_available(
                name=name, os_version=os_version, device_type=device_type,
                device_family=effective_family, session_id=session_id,
                timeout=wait_timeout,
            )
            if found:
                if session_id and found.claimed_by != session_id:
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
            claimed_candidates = [
                d for d in self._find_candidates(
                    all_devices, name=name, os_version=os_version,
                    device_type=device_type, device_family=effective_family,
                )
                if d.claim_status == DeviceClaimStatus.CLAIMED
            ]
            criteria_parts = []
            if name:
                criteria_parts.append(f"name={name!r}")
            if os_version:
                criteria_parts.append(f"os_version={os_version!r}")
            if effective_family:
                criteria_parts.append(f"device_family={effective_family!r}")
            criteria_str = ", ".join(criteria_parts) or "any"
            claimed_sessions = ", ".join(d.claimed_by or "unknown" for d in claimed_candidates)
            raise DeviceError(
                f"Timed out after {wait_timeout}s waiting for a device matching {criteria_str} "
                f"to become available. {len(claimed_candidates)} matching devices are claimed by: {claimed_sessions}",
                tool="pool",
            )

        # Priority 5: nothing viable
        raise self._build_resolution_error(
            all_devices, name=name, os_version=os_version,
            device_type=device_type, device_family=effective_family,
        )

    async def ensure_devices(
        self,
        count: int,
        name: str | None = None,
        os_version: str | None = None,
        device_type: DeviceType | None = None,
        device_family: str | None = None,
        auto_boot: bool = True,
        session_id: str | None = None,
    ) -> list[str]:
        """Ensure N devices matching criteria are booted and available.

        Returns list of UDIDs for the ready devices.
        When session_id is provided, devices already claimed by the session
        count as available and won't be re-claimed.
        Rolls back claims on partial failure.
        """
        await self.refresh_from_simctl()

        effective_family = self._infer_device_family(name, device_family)

        state = self._read_state()
        all_devices = list(state.devices.values())
        matching = self._find_candidates(
            all_devices, name=name, os_version=os_version,
            device_type=device_type, device_family=effective_family,
        )

        def _is_available(d: DevicePoolEntry) -> bool:
            return (
                d.claim_status == DeviceClaimStatus.AVAILABLE
                or (session_id is not None and d.claimed_by == session_id)
            )

        booted_available = sorted(
            [d for d in matching if d.state == DeviceState.BOOTED and _is_available(d)],
            key=self._rank_candidate,
        )
        shutdown_available = sorted(
            [d for d in matching if d.state == DeviceState.SHUTDOWN and _is_available(d)],
            key=self._rank_candidate,
        )

        total_available = len(booted_available) + (len(shutdown_available) if auto_boot else 0)
        if total_available < count:
            criteria_parts = []
            if name:
                criteria_parts.append(f"name='{name}'")
            if os_version:
                criteria_parts.append(f"os_version='{os_version}'")
            if effective_family:
                criteria_parts.append(f"device_family='{effective_family}'")
            criteria_str = ", ".join(criteria_parts) if criteria_parts else "any"
            claimed_count = len([d for d in matching if d.claim_status == DeviceClaimStatus.CLAIMED and not _is_available(d)])
            raise DeviceError(
                f"Need {count} devices matching {criteria_str} but only {total_available} available "
                f"({len(booted_available)} booted, {len(shutdown_available)} shutdown, {claimed_count} claimed).",
                tool="pool",
            )

        # Select devices: booted first (ranked), then shutdown to boot (ranked)
        selected_udids: list[str] = []
        newly_claimed_udids: list[str] = []

        try:
            # Take booted first
            for d in booted_available:
                if len(selected_udids) >= count:
                    break
                selected_udids.append(d.udid)

            # Boot more if needed
            for d in shutdown_available:
                if len(selected_udids) >= count:
                    break
                await self._boot_and_wait(d.udid)
                selected_udids.append(d.udid)

            # Claim all if session_id provided (skip devices already owned)
            if session_id:
                with self._lock_pool_file():
                    state = self._read_state()
                    for udid in selected_udids:
                        device = state.devices[udid]
                        if device.claimed_by == session_id:
                            # Already owned by this session — just update last_used
                            device.last_used = datetime.now(timezone.utc)
                        else:
                            device.claim_status = DeviceClaimStatus.CLAIMED
                            device.claimed_by = session_id
                            device.claimed_at = datetime.now(timezone.utc)
                            device.last_used = datetime.now(timezone.utc)
                            newly_claimed_udids.append(udid)
                    state.updated_at = datetime.now(timezone.utc)
                    self._write_state(state)

            return selected_udids

        except Exception:
            # Rollback: release only newly claimed devices (not session-owned ones)
            if newly_claimed_udids:
                for udid in newly_claimed_udids:
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
