"""DeviceController — orchestrates device backends and tracks active device."""

from __future__ import annotations

import logging
import time

from server.device.controller_ui import DeviceControllerUI
from server.device.devicectl import DevicectlBackend
from server.device.pmd3 import Pmd3Backend
from server.device.screenshots import process_screenshot
from server.device.simctl import SimctlBackend
from server.device.idb import IdbBackend
from server.device.usbmux import UsbmuxBackend
from server.models import AppInfo, DeviceError, DeviceInfo, DeviceState, DeviceType, UIElement

logger = logging.getLogger("quern-debug-server.device")


class DeviceController(DeviceControllerUI):
    """High-level device management: resolves active device, delegates to backends."""

    def __init__(self) -> None:
        self.simctl = SimctlBackend()
        self.idb = IdbBackend()
        self.devicectl = DevicectlBackend()
        self.pmd3 = Pmd3Backend()
        self.usbmux = UsbmuxBackend()
        self._active_udid: str | None = None
        self._pool = None  # Set by main.py after pool is created; None = no pool
        # UI tree cache: {udid: (elements, timestamp)}
        self._ui_cache: dict[str, tuple[list[UIElement], float]] = {}
        self._cache_ttl: float = 0.3  # 300ms cache TTL
        self._cache_hits: int = 0
        self._cache_misses: int = 0
        # Device info cache for screen dimensions
        self._device_info_cache: dict[str, DeviceInfo] = {}
        # Device type cache: udid -> DeviceType (populated by list_devices)
        self._device_type_cache: dict[str, DeviceType] = {}

    async def check_tools(self) -> dict[str, bool]:
        """Check availability of CLI tools."""
        from server.device.tunneld import is_tunneld_running

        return {
            "simctl": await self.simctl.is_available(),
            "idb": await self.idb.is_available(),
            "devicectl": await self.devicectl.is_available(),
            "pymobiledevice3": await self.pmd3.is_available(),
            "tunneld": await is_tunneld_running(),
        }

    def _device_type(self, udid: str) -> DeviceType:
        """Look up device type from cache. Defaults to simulator if unknown."""
        return self._device_type_cache.get(udid, DeviceType.SIMULATOR)

    def _is_physical(self, udid: str) -> bool:
        return self._device_type(udid) == DeviceType.DEVICE

    def _require_simulator(self, udid: str, operation: str) -> None:
        """Raise DeviceError if the device is physical (operation not supported)."""
        if self._is_physical(udid):
            raise DeviceError(
                f"{operation} is only supported on simulators",
                tool="simctl",
            )

    async def resolve_udid(self, udid: str | None = None) -> str:
        """Resolve which device to target.

        If a DevicePool is attached, attempts pool-based resolution for
        claim-aware, multi-device-friendly behavior. If pool resolution
        fails for any reason, silently falls back to the original logic.

        Resolution order:
        1. Explicit udid parameter → use it, update active
        2. Stored active_udid → use it
        3. Pool resolution (if pool attached) → best available booted device
        4. Fallback: simple auto-detect (original logic, unchanged)
        """
        if udid:
            self._active_udid = udid
            return udid

        if self._active_udid:
            return self._active_udid

        # Step 3: try pool-based resolution (silent upgrade)
        if self._pool is not None:
            try:
                resolved = await self._pool.resolve_device()
                self._active_udid = resolved
                return resolved
            except Exception as e:
                logger.debug("Pool resolution failed, falling back: %s", e)

        # Step 4: fallback — auto-detect from all backends
        devices = await self.list_devices()
        booted = [d for d in devices if d.state == DeviceState.BOOTED]

        if len(booted) == 0:
            raise DeviceError("No booted device found", tool="simctl")
        if len(booted) > 1:
            names = ", ".join(f"{d.name} ({d.udid[:8]})" for d in booted)
            raise DeviceError(
                f"Multiple devices booted ({names}), specify udid",
                tool="simctl",
            )

        self._active_udid = booted[0].udid
        return self._active_udid

    def _invalidate_ui_cache(self, udid: str | None = None) -> None:
        """Invalidate UI tree cache for a device (or all devices if udid=None)."""
        if udid:
            self._ui_cache.pop(udid, None)
            logger.debug(f"UI cache invalidated for device {udid[:8]}")
        else:
            self._ui_cache.clear()
            logger.debug("UI cache cleared for all devices")

    def get_cache_stats(self) -> dict:
        """Return cache statistics for observability."""
        total = self._cache_hits + self._cache_misses
        hit_rate = (self._cache_hits / total * 100) if total > 0 else 0

        # Add per-device cache age info
        cache_ages = {}
        now = time.time()
        for udid, (elements, timestamp) in self._ui_cache.items():
            age_ms = (now - timestamp) * 1000
            cache_ages[udid[:8]] = f"{age_ms:.1f}ms"

        return {
            "hits": self._cache_hits,
            "misses": self._cache_misses,
            "hit_rate_percent": round(hit_rate, 1),
            "cached_devices": len(self._ui_cache),
            "ttl_ms": int(self._cache_ttl * 1000),
            "cache_ages": cache_ages,
        }

    async def list_devices(self) -> list[DeviceInfo]:
        """List all devices (simulators + physical + pre-iOS 17 USB)."""
        sim_devices = await self.simctl.list_devices()
        physical_devices = await self.devicectl.list_devices()
        usbmux_devices = await self.usbmux.list_devices()

        # Populate device type cache
        for d in sim_devices:
            self._device_type_cache[d.udid] = DeviceType.SIMULATOR
        for d in physical_devices:
            self._device_type_cache[d.udid] = DeviceType.DEVICE
        for d in usbmux_devices:
            self._device_type_cache[d.udid] = DeviceType.DEVICE

        return sim_devices + physical_devices + usbmux_devices

    async def boot(self, udid: str | None = None, name: str | None = None) -> str:
        """Boot a simulator by udid or name. Returns the udid that was booted."""
        if udid:
            self._require_simulator(udid, "Boot")
            await self.simctl.boot(udid)
            self._active_udid = udid
            return udid

        if name:
            devices = await self.simctl.list_devices()
            matches = [d for d in devices if d.name == name]
            if not matches:
                raise DeviceError(f"No simulator found with name '{name}'", tool="simctl")
            target = matches[0]
            await self.simctl.boot(target.udid)
            self._active_udid = target.udid
            return target.udid

        raise DeviceError("Either udid or name is required to boot", tool="simctl")

    async def shutdown(self, udid: str) -> None:
        """Shutdown a simulator."""
        self._require_simulator(udid, "Shutdown")
        await self.simctl.shutdown(udid)
        if self._active_udid == udid:
            self._active_udid = None

    async def install_app(self, app_path: str, udid: str | None = None) -> str:
        """Install an app. Returns the resolved udid."""
        resolved = await self.resolve_udid(udid)
        if self._is_physical(resolved):
            await self.devicectl.install_app(resolved, app_path)
        else:
            await self.simctl.install_app(resolved, app_path)
        return resolved

    async def launch_app(self, bundle_id: str, udid: str | None = None) -> str:
        """Launch an app. Returns the resolved udid."""
        resolved = await self.resolve_udid(udid)
        if self._is_physical(resolved):
            await self.devicectl.launch_app(resolved, bundle_id)
        else:
            await self.simctl.launch_app(resolved, bundle_id)
        self._invalidate_ui_cache(resolved)  # UI changed
        return resolved

    async def terminate_app(self, bundle_id: str, udid: str | None = None) -> str:
        """Terminate an app. Returns the resolved udid."""
        resolved = await self.resolve_udid(udid)
        if self._is_physical(resolved):
            await self.devicectl.terminate_app(resolved, bundle_id)
        else:
            await self.simctl.terminate_app(resolved, bundle_id)
        return resolved

    async def uninstall_app(self, bundle_id: str, udid: str | None = None) -> str:
        """Uninstall an app. Returns the resolved udid."""
        resolved = await self.resolve_udid(udid)
        if self._is_physical(resolved):
            await self.devicectl.uninstall_app(resolved, bundle_id)
        else:
            await self.simctl.uninstall_app(resolved, bundle_id)
        return resolved

    async def list_apps(self, udid: str | None = None) -> tuple[list[AppInfo], str]:
        """List installed apps. Returns (apps, resolved_udid)."""
        resolved = await self.resolve_udid(udid)
        if self._is_physical(resolved):
            apps = await self.devicectl.list_apps(resolved)
        else:
            apps = await self.simctl.list_apps(resolved)
        return apps, resolved

    async def screenshot(
        self,
        udid: str | None = None,
        format: str = "png",
        scale: float = 0.5,
        quality: int = 85,
    ) -> tuple[bytes, str]:
        """Capture and process a screenshot. Returns (image_bytes, media_type)."""
        resolved = await self.resolve_udid(udid)
        if self._is_physical(resolved):
            raw_png = await self.pmd3.screenshot(resolved)
        else:
            raw_png = await self.simctl.screenshot(resolved)
        return process_screenshot(raw_png, format=format, scale=scale, quality=quality)

    async def set_location(
        self, latitude: float, longitude: float, udid: str | None = None,
    ) -> str:
        """Set simulated GPS location. Returns the resolved udid."""
        resolved = await self.resolve_udid(udid)
        self._require_simulator(resolved, "Set location")
        await self.simctl.set_location(resolved, latitude, longitude)
        return resolved

    async def grant_permission(
        self, bundle_id: str, permission: str, udid: str | None = None,
    ) -> str:
        """Grant an app permission. Returns the resolved udid."""
        resolved = await self.resolve_udid(udid)
        self._require_simulator(resolved, "Grant permission")
        await self.simctl.grant_permission(resolved, bundle_id, permission)
        return resolved
