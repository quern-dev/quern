"""DeviceController — orchestrates device backends and tracks active device."""

from __future__ import annotations

import asyncio
import logging
import time

from server.device.idb import IdbBackend
from server.device.screenshots import annotate_screenshot, process_screenshot
from server.device.simctl import SimctlBackend
from server.device.ui_elements import (
    find_element,
    generate_screen_summary,
    get_center,
    parse_elements,
)
from server.models import AppInfo, DeviceError, DeviceInfo, DeviceState, UIElement, WaitCondition

logger = logging.getLogger("quern-debug-server.device")


class DeviceController:
    """High-level device management: resolves active device, delegates to backends."""

    def __init__(self) -> None:
        self.simctl = SimctlBackend()
        self.idb = IdbBackend()
        self._active_udid: str | None = None
        # UI tree cache: {udid: (elements, timestamp)}
        self._ui_cache: dict[str, tuple[list[UIElement], float]] = {}
        self._cache_ttl: float = 0.3  # 300ms cache TTL
        self._cache_hits: int = 0
        self._cache_misses: int = 0

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
        return {
            "hits": self._cache_hits,
            "misses": self._cache_misses,
            "hit_rate_percent": round(hit_rate, 1),
            "cached_devices": len(self._ui_cache),
            "ttl_ms": int(self._cache_ttl * 1000),
        }

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
        self._invalidate_ui_cache(resolved)  # UI changed
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

    async def get_ui_elements(self, udid: str | None = None, use_cache: bool = True) -> tuple[list[UIElement], str]:
        """Get all UI accessibility elements with TTL-based caching.

        Returns (elements, resolved_udid).
        Requires idb to be installed.

        Args:
            udid: Device UDID (auto-resolves if None)
            use_cache: If False, bypass cache and force fresh fetch (default True)

        Cache TTL: 300ms (configurable via self._cache_ttl)
        Cache is invalidated on mutation operations (tap, swipe, type, launch).
        """
        resolved = await self.resolve_udid(udid)
        now = time.time()

        # Check cache (unless explicitly bypassed)
        if use_cache and resolved in self._ui_cache:
            cached_elements, cached_time = self._ui_cache[resolved]
            age = now - cached_time
            if age < self._cache_ttl:
                self._cache_hits += 1
                logger.debug(f"UI cache hit for {resolved[:8]} (age: {int(age*1000)}ms)")
                return cached_elements, resolved

        # Cache miss or bypassed - fetch from idb
        self._cache_misses += 1
        logger.debug(f"UI cache {'bypass' if not use_cache else 'miss'} for {resolved[:8]} - fetching from idb")
        raw = await self.idb.describe_all(resolved)
        elements = parse_elements(raw)

        # Update cache (always, even if bypassed)
        self._ui_cache[resolved] = (elements, now)

        return elements, resolved

    async def get_element(
        self,
        label: str | None = None,
        identifier: str | None = None,
        element_type: str | None = None,
        udid: str | None = None,
    ) -> tuple[dict, str]:
        """Get a single element's state without fetching the entire UI tree.

        Returns (element_dict, resolved_udid).
        Element dict includes match_count if multiple matches found.

        Raises:
            DeviceError if no matches or validation fails.
        """
        if not label and not identifier:
            raise DeviceError(
                "Either label or identifier is required",
                tool="idb",
            )

        elements, resolved = await self.get_ui_elements(udid)
        matches = find_element(elements, label=label, identifier=identifier, element_type=element_type)

        if len(matches) == 0:
            search_desc = f"label='{label}'" if label else f"identifier='{identifier}'"
            if element_type:
                search_desc += f", type='{element_type}'"
            raise DeviceError(
                f"No element found matching {search_desc}",
                tool="idb",
            )

        # Return first match with match_count if ambiguous
        el = matches[0]
        result = el.model_dump()
        if len(matches) > 1:
            result["match_count"] = len(matches)

        return result, resolved

    async def wait_for_element(
        self,
        condition: WaitCondition,
        label: str | None = None,
        identifier: str | None = None,
        element_type: str | None = None,
        value: str | None = None,
        timeout: float = 10,
        interval: float = 0.5,
        udid: str | None = None,
    ) -> tuple[dict, str]:
        """Wait for an element to satisfy a condition (server-side polling).

        Returns (result_dict, resolved_udid).
        Result dict contains:
        - matched: bool - whether condition was satisfied
        - elapsed_seconds: float - time spent polling
        - polls: int - number of polls performed
        - element: dict | None - element state if matched
        - last_state: dict | None - last seen state if timeout

        Raises:
            DeviceError if validation fails or timeout > 60s.
        """
        if not label and not identifier:
            raise DeviceError(
                "Either label or identifier is required",
                tool="idb",
            )

        if timeout > 60:
            raise DeviceError(
                "Timeout cannot exceed 60 seconds",
                tool="idb",
            )

        if condition in (WaitCondition.VALUE_EQUALS, WaitCondition.VALUE_CONTAINS):
            if value is None:
                raise DeviceError(
                    f"Condition '{condition}' requires a value parameter",
                    tool="idb",
                )

        resolved = await self.resolve_udid(udid)

        # Define condition checker functions
        def check_exists(el: UIElement | None) -> bool:
            return el is not None

        def check_not_exists(el: UIElement | None) -> bool:
            return el is None

        def check_visible(el: UIElement | None) -> bool:
            # Treat visible as "exists and has a frame"
            return el is not None and el.frame is not None

        def check_enabled(el: UIElement | None) -> bool:
            return el is not None and el.enabled

        def check_disabled(el: UIElement | None) -> bool:
            return el is not None and not el.enabled

        def check_value_equals(el: UIElement | None) -> bool:
            return el is not None and el.value == value

        def check_value_contains(el: UIElement | None) -> bool:
            return (
                el is not None
                and el.value is not None
                and value is not None
                and value in el.value
            )

        # Map condition to checker
        checkers = {
            WaitCondition.EXISTS: check_exists,
            WaitCondition.NOT_EXISTS: check_not_exists,
            WaitCondition.VISIBLE: check_visible,
            WaitCondition.ENABLED: check_enabled,
            WaitCondition.DISABLED: check_disabled,
            WaitCondition.VALUE_EQUALS: check_value_equals,
            WaitCondition.VALUE_CONTAINS: check_value_contains,
        }

        checker = checkers.get(condition)
        if not checker:
            raise DeviceError(
                f"Unknown condition: {condition}",
                tool="idb",
            )

        # Polling loop
        start_time = time.time()
        polls = 0
        last_element: UIElement | None = None

        while True:
            polls += 1
            elapsed = time.time() - start_time

            # Fetch UI elements
            elements, _ = await self.get_ui_elements(resolved)
            matches = find_element(
                elements,
                label=label,
                identifier=identifier,
                element_type=element_type,
            )

            # Get first match (or None if no matches)
            current_element = matches[0] if matches else None
            last_element = current_element

            # Check condition
            if checker(current_element):
                return {
                    "matched": True,
                    "elapsed_seconds": round(elapsed, 2),
                    "polls": polls,
                    "element": current_element.model_dump() if current_element else None,
                }, resolved

            # Check timeout
            if elapsed >= timeout:
                return {
                    "matched": False,
                    "elapsed_seconds": round(elapsed, 2),
                    "polls": polls,
                    "last_state": last_element.model_dump() if last_element else None,
                }, resolved

            # Sleep before next poll
            await asyncio.sleep(interval)

    async def get_screen_summary(
        self,
        max_elements: int = 20,
        udid: str | None = None,
    ) -> tuple[dict, str]:
        """Generate an LLM-optimized screen summary. Returns (summary_dict, resolved_udid).

        Args:
            max_elements: Maximum interactive elements to include (0 = unlimited)
            udid: Device UDID (auto-resolves if omitted)
        """
        elements, resolved = await self.get_ui_elements(udid)
        return generate_screen_summary(elements, max_elements=max_elements), resolved

    async def tap(self, x: float, y: float, udid: str | None = None) -> str:
        """Tap at coordinates. Returns the resolved udid."""
        resolved = await self.resolve_udid(udid)
        await self.idb.tap(resolved, x, y)
        self._invalidate_ui_cache(resolved)  # UI changed
        return resolved

    async def tap_element(
        self,
        label: str | None = None,
        identifier: str | None = None,
        element_type: str | None = None,
        udid: str | None = None,
        # Future enhancements to consider:
        # stability_check_ms: int = 100,  # Configurable stability check interval
        # verify_disappears: bool = False,  # Verify element disappears after tap
        # retry_attempts: int = 1,  # Number of retry attempts if tap fails
    ) -> dict:
        """Find an element by label/identifier and tap its center.

        Uses adaptive timing with stability checking to handle animations:
        - Checks element position after 100ms
        - If position changed (animating), waits 300ms more
        - Taps final position for accuracy

        Returns:
            {"status": "ok", "tapped": {...}} for single match
            {"status": "ambiguous", "matches": [...], "message": "..."} for multiple
        Raises:
            DeviceError for 0 matches

        Future enhancement ideas:
        1. Post-tap verification - Verify expected outcome (e.g., element disappears)
        2. Retry logic - If tap doesn't work, retry with fresh coordinates
        3. Configurable stability timing - Allow tuning the stability check interval
        """
        if not label and not identifier:
            raise DeviceError(
                "Either label or identifier is required for tap-element",
                tool="idb",
            )

        elements, resolved = await self.get_ui_elements(udid)

        # Use shared search helper
        matches = find_element(elements, label=label, identifier=identifier, element_type=element_type)

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

            # Stability check: ensure element has stopped moving/animating
            # Get initial position
            initial_frame = el.frame
            await asyncio.sleep(0.1)

            # Re-fetch UI and find element again (bypass cache to detect changes!)
            elements_check, _ = await self.get_ui_elements(resolved, use_cache=False)
            matches_check = find_element(elements_check, label=label, identifier=identifier, element_type=element_type)

            if matches_check:
                # Check if position changed (element is animating)
                current_frame = matches_check[0].frame
                if current_frame != initial_frame:
                    logger.debug(
                        "Element position changed (animating), waiting for stability: %s -> %s",
                        initial_frame, current_frame
                    )
                    # Wait longer for animation to complete
                    await asyncio.sleep(0.3)
                    # Re-fetch one more time to get final position (bypass cache again)
                    elements_final, _ = await self.get_ui_elements(resolved, use_cache=False)
                    matches_final = find_element(elements_final, label=label, identifier=identifier, element_type=element_type)
                    if matches_final:
                        cx, cy = get_center(matches_final[0])

            await self.idb.tap(resolved, cx, cy)
            self._invalidate_ui_cache(resolved)  # UI changed

            # Future enhancement: Post-tap verification
            # if verify_disappears:
            #     await asyncio.sleep(0.2)
            #     elements_verify, _ = await self.get_ui_elements(resolved)
            #     matches_verify = find_element(elements_verify, label=label, identifier=identifier, element_type=element_type)
            #     if matches_verify:
            #         logger.warning("Element still present after tap, may have failed")
            #         # Could retry here if retry_attempts > 1

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

        # Future enhancement: Retry logic implementation
        # If we add retry_attempts parameter, wrap the tap attempt in a loop:
        # for attempt in range(retry_attempts):
        #     try:
        #         # ... existing tap logic ...
        #         if verify_success():
        #             break
        #     except Exception as e:
        #         if attempt == retry_attempts - 1:
        #             raise
        #         logger.debug(f"Tap attempt {attempt + 1} failed, retrying: {e}")
        #         await asyncio.sleep(0.3)

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
        self._invalidate_ui_cache(resolved)  # UI changed
        return resolved

    async def type_text(self, text: str, udid: str | None = None) -> str:
        """Type text into focused field. Returns the resolved udid."""
        resolved = await self.resolve_udid(udid)
        await self.idb.type_text(resolved, text)
        self._invalidate_ui_cache(resolved)  # UI changed (text field value updated)
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
