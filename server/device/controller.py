"""DeviceController — orchestrates device backends and tracks active device."""

from __future__ import annotations

import logging

from server.device.idb import IdbBackend
from server.device.screenshots import annotate_screenshot, process_screenshot
from server.device.simctl import SimctlBackend
from server.device.ui_elements import (
    find_by_identifier,
    find_by_label,
    find_by_type,
    generate_screen_summary,
    get_center,
    parse_elements,
)
from server.models import AppInfo, DeviceError, DeviceInfo, DeviceState, UIElement

logger = logging.getLogger("quern-debug-server.device")


class DeviceController:
    """High-level device management: resolves active device, delegates to backends."""

    def __init__(self) -> None:
        self.simctl = SimctlBackend()
        self.idb = IdbBackend()
        self._active_udid: str | None = None

    async def check_tools(self) -> dict[str, bool]:
        """Check availability of CLI tools."""
        return {
            "simctl": await self.simctl.is_available(),
            "idb": await self.idb.is_available(),
        }

    async def resolve_udid(self, udid: str | None = None) -> str:
        """Resolve which device to target.

        Resolution order:
        1. Explicit udid parameter → use it, update active
        2. Stored active_udid → use it
        3. Auto-detect: exactly 1 booted simulator → use it, update active
        4. 0 booted → error
        5. 2+ booted → error
        """
        if udid:
            self._active_udid = udid
            return udid

        if self._active_udid:
            return self._active_udid

        devices = await self.simctl.list_devices()
        booted = [d for d in devices if d.state == DeviceState.BOOTED]

        if len(booted) == 0:
            raise DeviceError("No booted simulator found", tool="simctl")
        if len(booted) > 1:
            names = ", ".join(f"{d.name} ({d.udid[:8]})" for d in booted)
            raise DeviceError(
                f"Multiple simulators booted ({names}), specify udid",
                tool="simctl",
            )

        self._active_udid = booted[0].udid
        return self._active_udid

    async def list_devices(self) -> list[DeviceInfo]:
        """List all simulators."""
        return await self.simctl.list_devices()

    async def boot(self, udid: str | None = None, name: str | None = None) -> str:
        """Boot a simulator by udid or name. Returns the udid that was booted."""
        if udid:
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
        await self.simctl.shutdown(udid)
        if self._active_udid == udid:
            self._active_udid = None

    async def install_app(self, app_path: str, udid: str | None = None) -> str:
        """Install an app. Returns the resolved udid."""
        resolved = await self.resolve_udid(udid)
        await self.simctl.install_app(resolved, app_path)
        return resolved

    async def launch_app(self, bundle_id: str, udid: str | None = None) -> str:
        """Launch an app. Returns the resolved udid."""
        resolved = await self.resolve_udid(udid)
        await self.simctl.launch_app(resolved, bundle_id)
        return resolved

    async def terminate_app(self, bundle_id: str, udid: str | None = None) -> str:
        """Terminate an app. Returns the resolved udid."""
        resolved = await self.resolve_udid(udid)
        await self.simctl.terminate_app(resolved, bundle_id)
        return resolved

    async def list_apps(self, udid: str | None = None) -> tuple[list[AppInfo], str]:
        """List installed apps. Returns (apps, resolved_udid)."""
        resolved = await self.resolve_udid(udid)
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
        raw_png = await self.simctl.screenshot(resolved)
        return process_screenshot(raw_png, format=format, scale=scale, quality=quality)

    # -------------------------------------------------------------------
    # UI inspection & interaction (Phase 3b — requires idb)
    # -------------------------------------------------------------------

    async def get_ui_elements(self, udid: str | None = None) -> tuple[list[UIElement], str]:
        """Get all UI accessibility elements. Returns (elements, resolved_udid).

        Requires idb to be installed.
        """
        resolved = await self.resolve_udid(udid)
        raw = await self.idb.describe_all(resolved)
        return parse_elements(raw), resolved

    async def get_screen_summary(self, udid: str | None = None) -> tuple[dict, str]:
        """Generate an LLM-optimized screen summary. Returns (summary_dict, resolved_udid)."""
        elements, resolved = await self.get_ui_elements(udid)
        return generate_screen_summary(elements), resolved

    async def tap(self, x: float, y: float, udid: str | None = None) -> str:
        """Tap at coordinates. Returns the resolved udid."""
        resolved = await self.resolve_udid(udid)
        await self.idb.tap(resolved, x, y)
        return resolved

    async def tap_element(
        self,
        label: str | None = None,
        identifier: str | None = None,
        element_type: str | None = None,
        udid: str | None = None,
    ) -> dict:
        """Find an element by label/identifier and tap its center.

        Returns:
            {"status": "ok", "tapped": {...}} for single match
            {"status": "ambiguous", "matches": [...], "message": "..."} for multiple
        Raises:
            DeviceError for 0 matches
        """
        if not label and not identifier:
            raise DeviceError(
                "Either label or identifier is required for tap-element",
                tool="idb",
            )

        elements, resolved = await self.get_ui_elements(udid)

        # Filter by label or identifier
        if label:
            matches = find_by_label(elements, label)
        else:
            matches = find_by_identifier(elements, identifier)  # type: ignore[arg-type]

        # Optional type filter to narrow results
        if element_type and matches:
            matches = find_by_type(matches, element_type)

        if len(matches) == 0:
            search_desc = f"label='{label}'" if label else f"identifier='{identifier}'"
            if element_type:
                search_desc += f", type='{element_type}'"
            raise DeviceError(
                f"No element found matching {search_desc}",
                tool="idb",
            )

        if len(matches) == 1:
            el = matches[0]
            cx, cy = get_center(el)
            await self.idb.tap(resolved, cx, cy)
            return {
                "status": "ok",
                "tapped": {
                    "label": el.label,
                    "type": el.type,
                    "identifier": el.identifier,
                    "x": cx,
                    "y": cy,
                },
            }

        # Multiple matches — return ambiguous
        match_list = []
        for el in matches:
            entry: dict = {
                "label": el.label,
                "type": el.type,
                "identifier": el.identifier,
            }
            if el.frame:
                cx, cy = get_center(el)
                entry["center_x"] = cx
                entry["center_y"] = cy
            match_list.append(entry)

        return {
            "status": "ambiguous",
            "matches": match_list,
            "message": (
                f"Found {len(matches)} matches, "
                "specify element_type or identifier to narrow"
            ),
        }

    async def swipe(
        self,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
        duration: float = 0.5,
        udid: str | None = None,
    ) -> str:
        """Swipe gesture. Returns the resolved udid."""
        resolved = await self.resolve_udid(udid)
        await self.idb.swipe(resolved, start_x, start_y, end_x, end_y, duration)
        return resolved

    async def type_text(self, text: str, udid: str | None = None) -> str:
        """Type text into focused field. Returns the resolved udid."""
        resolved = await self.resolve_udid(udid)
        await self.idb.type_text(resolved, text)
        return resolved

    async def press_button(self, button: str, udid: str | None = None) -> str:
        """Press a hardware button. Returns the resolved udid."""
        resolved = await self.resolve_udid(udid)
        await self.idb.press_button(resolved, button)
        return resolved

    async def set_location(
        self, latitude: float, longitude: float, udid: str | None = None,
    ) -> str:
        """Set simulated GPS location. Returns the resolved udid."""
        resolved = await self.resolve_udid(udid)
        await self.simctl.set_location(resolved, latitude, longitude)
        return resolved

    async def grant_permission(
        self, bundle_id: str, permission: str, udid: str | None = None,
    ) -> str:
        """Grant an app permission. Returns the resolved udid."""
        resolved = await self.resolve_udid(udid)
        await self.simctl.grant_permission(resolved, bundle_id, permission)
        return resolved

    async def screenshot_annotated(
        self,
        udid: str | None = None,
        scale: float = 0.5,
        quality: int = 85,
    ) -> tuple[bytes, str]:
        """Capture an annotated screenshot with accessibility overlays.

        Returns (image_bytes, media_type).
        """
        resolved = await self.resolve_udid(udid)
        raw_png = await self.simctl.screenshot(resolved)
        elements, _ = await self.get_ui_elements(resolved)
        return annotate_screenshot(raw_png, elements, scale=scale, quality=quality)
