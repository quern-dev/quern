"""Device pool management for multi-device support."""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from server.config import get_default_device_family
from server.device.controller import DeviceController
from server.models import DeviceError, DevicePoolEntry, DevicePoolState, DeviceState, DeviceType

logger = logging.getLogger("quern-debug-server.device-pool")

POOL_FILE = Path.home() / ".quern" / "device-pool.json"
REFRESH_CACHE_TTL_SECONDS = 2  # Avoid redundant simctl calls


class DevicePool:
    """Manages the pool of available devices.

    Includes smart device resolution: criteria matching, auto-boot,
    and bulk provisioning. Integrates with DeviceController.resolve_udid()
    as the preferred resolution path.
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
        device_type: DeviceType | None = None,  # "simulator", "device", None=all
    ) -> list[DevicePoolEntry]:
        """List all devices in the pool with optional filters."""
        await self.refresh_from_simctl()

        state = self._read_state()
        devices = list(state.devices.values())

        # Apply filters
        if state_filter:
            devices = [d for d in devices if d.state.value == state_filter]

        if device_type:
            devices = [d for d in devices if d.device_type == device_type]

        return devices

    async def get_device_state(self, udid: str) -> DevicePoolEntry | None:
        """Get the current state of a specific device."""
        state = self._read_state()
        return state.devices.get(udid)

    async def refresh_from_simctl(self, *, force: bool = False) -> None:
        """Refresh pool state from simctl (discover new devices, update boot states).

        Cached for 2 seconds to avoid redundant simctl calls during rapid operations.
        Use force=True to bypass the cache (e.g. after booting a device).
        """
        # Check cache
        now = datetime.now(timezone.utc)
        if not force and self._last_refresh_at and (now - self._last_refresh_at).total_seconds() < REFRESH_CACHE_TTL_SECONDS:
            return

        # Get current devices from simctl (~200-500ms, expensive)
        simctl_devices = await self.controller.list_devices()

        with self._lock_pool_file():
            state = self._read_state()
            now = datetime.now(timezone.utc)

            # Update or add devices
            for device_info in simctl_devices:
                if device_info.udid in state.devices:
                    # Update existing entry
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
    # Resolution protocol
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
        2. Most recently used (warm simulator state, caches)
        3. Name alphabetical (deterministic tie-breaking)
        """
        return (
            0 if device.state == DeviceState.BOOTED else 1,
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
                    await self.refresh_from_simctl(force=True)
                    logger.info("Device %s booted in %.1fs", udid[:8], time.time() - start)
                    return

        raise DeviceError(
            f"Device {udid} did not boot within {timeout}s",
            tool="simctl",
        )

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
        shutdown = [d for d in both_matched if d.state == DeviceState.SHUTDOWN]

        if shutdown:
            shutdown_info = ", ".join(f"{d.name} ({d.udid[:8]})" for d in shutdown)
            return DeviceError(
                f"Found {len(shutdown)} matching devices but all are shutdown: {shutdown_info}. "
                f"Set auto_boot=true to boot one, or boot manually with boot_device.",
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
        auto_boot: bool = True,
    ) -> str:
        """Resolve a device matching criteria, optionally booting it.

        Returns the UDID of the resolved device and sets it as the active device.

        Resolution priority:
        1. No criteria + active device already set → return active
        2. Explicit UDID → use directly
        3. Booted match → use best ranked
        4. Shutdown + auto_boot → boot best match
        5. No matches → diagnostic error
        """
        # Short-circuit: no criteria and active device already set
        has_criteria = any([udid, name, os_version, device_family])
        if not has_criteria and self.controller._active_udid:
            return self.controller._active_udid

        await self.refresh_from_simctl()

        # Priority 1: explicit UDID
        if udid:
            state = self._read_state()
            if udid not in state.devices:
                raise DeviceError(f"Device {udid} not found in pool", tool="pool")
            self.controller._active_udid = udid
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

        # Priority 2: booted match
        booted = [d for d in candidates if d.state == DeviceState.BOOTED]
        if booted:
            chosen = booted[0]
            self.controller._active_udid = chosen.udid
            return chosen.udid

        # Priority 3: shutdown + auto_boot
        shutdown = [d for d in candidates if d.state == DeviceState.SHUTDOWN]
        if shutdown and auto_boot:
            chosen = shutdown[0]
            await self._boot_and_wait(chosen.udid)
            self.controller._active_udid = chosen.udid
            return chosen.udid

        # Priority 4: nothing viable
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
    ) -> list[str]:
        """Ensure N devices matching criteria are booted and available.

        Returns list of UDIDs for the ready devices.
        Sets the first device as the active device.
        """
        await self.refresh_from_simctl()

        effective_family = self._infer_device_family(name, device_family)

        state = self._read_state()
        all_devices = list(state.devices.values())
        matching = self._find_candidates(
            all_devices, name=name, os_version=os_version,
            device_type=device_type, device_family=effective_family,
        )

        booted_available = sorted(
            [d for d in matching if d.state == DeviceState.BOOTED],
            key=self._rank_candidate,
        )
        shutdown_available = sorted(
            [d for d in matching if d.state == DeviceState.SHUTDOWN],
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
            raise DeviceError(
                f"Need {count} devices matching {criteria_str} but only {total_available} available "
                f"({len(booted_available)} booted, {len(shutdown_available)} shutdown).",
                tool="pool",
            )

        # Select devices: booted first (ranked), then shutdown to boot (ranked)
        selected_udids: list[str] = []

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

        # Set first device as active
        if selected_udids:
            self.controller._active_udid = selected_udids[0]

        return selected_udids

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
