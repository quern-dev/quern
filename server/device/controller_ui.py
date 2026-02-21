"""DeviceControllerUI — mixin for UI inspection and interaction."""

from __future__ import annotations

import asyncio
import logging
import time

from server.device.screenshots import annotate_screenshot
from server.device.ui_elements import (
    find_children_of,
    find_element,
    generate_screen_summary,
    get_center,
    get_tap_point,
    parse_elements,
)
from server.models import DeviceError, UIElement, WaitCondition

logger = logging.getLogger("quern-debug-server.device")


class DeviceControllerUI:
    """Mixin providing UI inspection and interaction methods.

    Expects the consuming class to provide:
    - self.idb: IdbBackend
    - self.wda_client: WdaBackend
    - self.simctl: SimctlBackend
    - self._ui_cache: dict[str, tuple[list[UIElement], float]]
    - self._cache_ttl: float
    - self._cache_hits: int
    - self._cache_misses: int
    - self._device_info_cache: dict[str, DeviceInfo]
    - self._device_type_cache: dict[str, DeviceType]
    - self.resolve_udid(udid) -> str
    - self._invalidate_ui_cache(udid) -> None
    - self._is_physical(udid) -> bool
    """

    # Bottom safe area inset for devices with home indicator (Face ID / Dynamic Island)
    _HOME_INDICATOR_INSET = 34  # points

    # Maximum scroll-into-view attempts before giving up
    _MAX_SCROLL_ATTEMPTS = 3

    # Known screen dimensions by device model (portrait orientation)
    _SCREEN_DIMENSIONS = {
        "iPhone 16": {"width": 402, "height": 844},
        "iPhone 16 Plus": {"width": 440, "height": 926},
        "iPhone 16 Pro": {"width": 402, "height": 852},
        "iPhone 16 Pro Max": {"width": 440, "height": 926},
        "iPhone 15": {"width": 402, "height": 844},
        "iPhone 15 Plus": {"width": 440, "height": 926},
        "iPhone 15 Pro": {"width": 402, "height": 852},
        "iPhone 15 Pro Max": {"width": 440, "height": 926},
    }

    # Known positions for static UI elements
    # Format: (x_offset, y_offset, anchor)
    # anchor: "bottom-left" = tab bar, "top-right" = nav bar button
    _STATIC_ELEMENT_POSITIONS = {
        "_Profile button in tab bar": (40, 40, "bottom-left"),
        "_Map button in tab bar": (120, 40, "bottom-left"),
        "_Activities button in tab bar": (200, 40, "bottom-left"),
        "_Trackables button in tab bar": (280, 40, "bottom-left"),
        "_Settings button": (28, 78, "top-right"),  # 28px from right edge, 78px from top
    }

    def _ui_backend(self, udid: str):
        """Return the appropriate UI automation backend for a device.

        Physical devices use WdaBackend; simulators use IdbBackend.
        """
        if self._is_physical(udid):
            return self.wda_client
        return self.idb

    async def _get_screen_dimensions(self, udid: str) -> dict | None:
        """Get screen dimensions for a device. Returns {"width": int, "height": int} or None."""
        # Check cache first
        if udid in self._device_info_cache:
            device_info = self._device_info_cache[udid]
            return self._SCREEN_DIMENSIONS.get(device_info.name)

        # Fetch device info
        try:
            devices = await self.simctl.list_devices()
            for device in devices:
                if device.udid == udid:
                    self._device_info_cache[udid] = device
                    return self._SCREEN_DIMENSIONS.get(device.name)
        except Exception:
            pass

        return None

    def _is_obscured_by_home_indicator(
        self,
        element: UIElement,
        screen_height: float,
    ) -> bool:
        """Check if an element's tap point falls in the home indicator zone."""
        if element.frame is None:
            return False
        _, cy = get_tap_point(element)
        safe_bottom = screen_height - self._HOME_INDICATOR_INSET
        return cy > safe_bottom

    def _get_screen_height_from_elements(self, elements: list) -> float | None:
        """Extract screen height from the Application element in the UI tree."""
        for el in elements:
            if el.type == "Application" and el.frame:
                return el.frame["height"]
        return None

    def _get_screen_width_from_elements(self, elements: list) -> float | None:
        """Extract screen width from the Application element in the UI tree."""
        for el in elements:
            if el.type == "Application" and el.frame:
                return el.frame["width"]
        return None

    async def _scroll_element_into_view(
        self,
        resolved: str,
        label: str | None,
        identifier: str | None,
        element_type: str | None,
        screen_height: float,
        screen_width: float = 393,
    ) -> UIElement | None:
        """Scroll an obscured element into the tappable area.

        Performs a small upward swipe, re-fetches the element, and checks
        whether it moved. If it moved into the safe zone, returns the updated
        element. If it didn't move (fixed-position element), returns None
        to signal the caller that scrolling won't help.
        """
        safe_bottom = screen_height - self._HOME_INDICATOR_INSET
        scroll_amount = 100  # points to scroll per attempt
        mid_x = screen_width / 2

        for attempt in range(self._MAX_SCROLL_ATTEMPTS):
            # Get element position before scroll
            elements_before, _ = await self.get_ui_elements(
                resolved, use_cache=False,
                filter_label=label, filter_identifier=identifier,
                filter_type=element_type,
            )

            matches_before = find_element(
                elements_before, label=label,
                identifier=identifier, element_type=element_type,
            )
            if not matches_before:
                return None

            before_frame = matches_before[0].frame
            _, before_cy = get_tap_point(matches_before[0])

            # Already in safe zone?
            if before_cy <= safe_bottom:
                return matches_before[0]

            # Swipe up (start lower, end higher)
            swipe_start_y = screen_height * 0.7
            swipe_end_y = swipe_start_y - scroll_amount
            await self._ui_backend(resolved).swipe(resolved, mid_x, swipe_start_y, mid_x, swipe_end_y, 0.3)
            self._invalidate_ui_cache(resolved)

            # Re-fetch and check
            elements_after, _ = await self.get_ui_elements(
                resolved, use_cache=False,
                filter_label=label, filter_identifier=identifier,
                filter_type=element_type,
            )

            matches_after = find_element(
                elements_after, label=label,
                identifier=identifier, element_type=element_type,
            )
            if not matches_after:
                return None

            after_frame = matches_after[0].frame

            # Did the element move? If not, it's fixed — scrolling won't help
            if after_frame == before_frame:
                logger.info("scroll-into-view: element did not move (fixed-position), aborting")
                return None

            _, after_cy = get_tap_point(matches_after[0])
            if after_cy <= safe_bottom:
                logger.info(
                    "scroll-into-view: success after %d attempt(s) (y: %.1f -> %.1f)",
                    attempt + 1, before_cy, after_cy,
                )
                return matches_after[0]

            logger.info(
                "scroll-into-view attempt %d: element moved (y: %.1f -> %.1f) but still obscured",
                attempt + 1, before_cy, after_cy,
            )

        logger.warning(
            "scroll-into-view: failed after %d attempts", self._MAX_SCROLL_ATTEMPTS,
        )
        return None

    async def _try_fast_path_element_check(
        self,
        udid: str,
        identifier: str | None,
        condition: WaitCondition
    ) -> tuple[bool, dict | None]:
        """Try to check element using describe-point instead of describe-all.

        Returns (success: bool, element: dict | None)
        - (True, element) if fast path succeeded and element matches condition
        - (False, None) if fast path not applicable or failed
        """
        # Only support 'exists' condition for now
        if condition != WaitCondition.EXISTS:
            return (False, None)

        # Only works for identifiers, not labels
        if not identifier:
            return (False, None)

        # Check if this is a known static element
        if identifier not in self._STATIC_ELEMENT_POSITIONS:
            return (False, None)

        # Get screen dimensions
        dimensions = await self._get_screen_dimensions(udid)
        if not dimensions:
            logger.debug(f"[FAST PATH] Unknown screen dimensions for device, falling back to describe-all")
            return (False, None)

        # Calculate coordinates based on anchor
        x_offset, y_offset, anchor = self._STATIC_ELEMENT_POSITIONS[identifier]

        if anchor == "bottom-left":
            x = x_offset
            y = dimensions["height"] - y_offset
        elif anchor == "top-right":
            x = dimensions["width"] - x_offset
            y = y_offset
        else:
            logger.warning(f"[FAST PATH] Unknown anchor '{anchor}' for {identifier}")
            return (False, None)

        logger.info(f"[FAST PATH] Probing {identifier} at ({x}, {y}) instead of describe-all")

        # Probe the point
        try:
            element = await self._ui_backend(udid).describe_point(udid, x, y)
            if not element:
                logger.debug(f"[FAST PATH] No element at ({x}, {y})")
                return (True, None)  # Fast path succeeded, but element not found

            # Check if identifier matches
            found_identifier = element.get("AXUniqueId") or element.get("identifier")
            if found_identifier == identifier:
                logger.info(f"[FAST PATH] ✓ Found {identifier} at ({x}, {y})")
                return (True, element)
            else:
                logger.debug(f"[FAST PATH] Element at ({x}, {y}) is '{found_identifier}', not '{identifier}'")
                return (True, None)  # Fast path succeeded, wrong element

        except Exception as e:
            logger.debug(f"[FAST PATH] describe-point failed: {e}, falling back")
            return (False, None)

    async def _wda_direct_query(
        self,
        udid: str,
        label: str | None = None,
        identifier: str | None = None,
        element_type: str | None = None,
    ) -> list[UIElement]:
        """Query WDA directly for specific elements without fetching the full tree.

        Translates label/identifier/type into WDA locator strategies:
        - identifier only → 'accessibility id' (fastest, exact match)
        - label only → 'predicate string' with label ==[c] (case-insensitive)
        - combined filters → 'predicate string' with AND clauses

        Returns parsed UIElement objects. Empty list on no match or error.
        """
        def _escape(val: str) -> str:
            """Escape single quotes for NSPredicate string literals."""
            return val.replace("'", "\\'")

        xcui_type = f"XCUIElementType{element_type}" if element_type else None

        # Choose the most efficient WDA locator strategy
        if identifier and not label and not xcui_type:
            # Fastest: direct accessibility id lookup
            using = "accessibility id"
            value = identifier
        else:
            # Build NSPredicate string
            clauses: list[str] = []
            if identifier:
                clauses.append(f"name == '{_escape(identifier)}'")
            if label:
                clauses.append(f"label ==[c] '{_escape(label)}'")
            if xcui_type:
                clauses.append(f"type == '{xcui_type}'")

            if not clauses:
                return []

            using = "predicate string"
            value = " AND ".join(clauses)

        logger.info("[WDA DIRECT] %s=%s on %s", using, value, udid[:8])
        raw = await self.wda_client.find_elements_by_query(udid, using, value)
        return parse_elements(raw)

    async def get_ui_elements(
        self,
        udid: str | None = None,
        use_cache: bool = True,
        filter_label: str | None = None,
        filter_identifier: str | None = None,
        filter_type: str | None = None,
        snapshot_depth: int | None = None,
    ) -> tuple[list[UIElement], str]:
        """Get UI accessibility elements with TTL-based caching and optional filtering.

        Returns (elements, resolved_udid).
        Requires idb to be installed.

        Args:
            udid: Device UDID (auto-resolves if None)
            use_cache: If False, bypass cache and force fresh fetch (default True)
            filter_label: Only parse elements with this label (performance optimization)
            filter_identifier: Only parse elements with this identifier (performance optimization)
            filter_type: Only parse elements with this type (performance optimization)

        Performance note: Filters are applied during parsing, not after. On screens with
        hundreds of elements (e.g., map with 400+ pins), this can be 100x faster than
        parsing everything and filtering afterwards.

        Cache TTL: 300ms (configurable via self._cache_ttl)
        Cache is invalidated on mutation operations (tap, swipe, type, launch).
        Filtered results are NOT cached (only full tree is cached).
        """
        has_filters = filter_label or filter_identifier or filter_type

        resolved = await self.resolve_udid(udid)
        now = time.time()

        # Bypass cache when snapshot_depth is provided (different depth = different tree shape)
        if snapshot_depth is not None:
            use_cache = False

        # Check cache (unless explicitly bypassed)
        # Strategy: Use cached full tree, then filter in memory (fast)
        if use_cache and resolved in self._ui_cache:
            cached_elements, cached_time = self._ui_cache[resolved]
            age = now - cached_time
            if age < self._cache_ttl:
                self._cache_hits += 1

                # If filters provided, apply them to cached elements (in-memory filtering is fast)
                if has_filters:
                    filtered = find_element(cached_elements, label=filter_label,
                                          identifier=filter_identifier, element_type=filter_type)
                    return filtered, resolved

                return cached_elements, resolved

        # WDA direct query: physical device + filters + cache miss → query directly
        if has_filters and self._is_physical(resolved):
            elements = await self._wda_direct_query(resolved, filter_label, filter_identifier, filter_type)
            if elements:
                return elements, resolved
            # Direct query found nothing — fall through to /source snapshot.
            # WDA 'accessibility id' doesn't always match identifiers that
            # appear in the full /source tree on physical devices.
            logger.info("[WDA DIRECT FALLBACK] No results for id=%s label=%s, trying /source",
                        filter_identifier, filter_label)

        # Cache miss or bypassed - fetch from idb
        self._cache_misses += 1

        raw = await self._ui_backend(resolved).describe_all(resolved, snapshot_depth=snapshot_depth)

        # Parse strategy:
        # - If filters AND will cache: parse full tree (for cache), then filter in memory
        # - If filters but won't cache (bypass): parse with filters to save time
        # - If no filters: parse full tree

        if has_filters and not use_cache:
            # Bypassing cache - use filtered parsing for speed
            elements = parse_elements(raw, filter_label, filter_identifier, filter_type)
        else:
            # Parse full tree for caching
            elements = parse_elements(raw)

            # Cache the full tree
            self._ui_cache[resolved] = (elements, now)

            # Apply filters in memory if needed
            if has_filters:
                elements = find_element(elements, label=filter_label,
                                      identifier=filter_identifier, element_type=filter_type)

        return elements, resolved

    async def get_ui_elements_children_of(
        self,
        children_of: str,
        udid: str | None = None,
        snapshot_depth: int | None = None,
    ) -> tuple[list[UIElement], str]:
        """Get UI elements scoped to children of a specific parent.

        Uses the nested idb tree to find the parent by identifier or label,
        then returns its flattened descendants as parsed UIElements.
        """
        resolved = await self.resolve_udid(udid)
        nested = await self._ui_backend(resolved).describe_all_nested(resolved, snapshot_depth=snapshot_depth)
        child_dicts = find_children_of(nested, parent_identifier=children_of, parent_label=children_of)
        elements = parse_elements(child_dicts)
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
        if not label and not identifier and not element_type:
            raise DeviceError(
                "At least one of label, identifier, or element_type is required",
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
        if not label and not identifier and not element_type:
            raise DeviceError(
                "At least one of label, identifier, or element_type is required",
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

            # Try fast path first (describe-point for known static elements)
            fast_path_success, fast_path_element = await self._try_fast_path_element_check(
                resolved, identifier, condition
            )

            if fast_path_success:
                # Fast path worked - use the result
                # Convert raw dict to UIElement if we got one
                if fast_path_element:
                    parsed = parse_elements([fast_path_element])
                    current_element = parsed[0] if parsed else None
                else:
                    current_element = None

                last_element = current_element

                # Check condition
                if checker(current_element):
                    return (
                        {
                            "matched": True,
                            "elapsed_seconds": round(elapsed, 2),
                            "polls": polls,
                            "element": current_element.model_dump() if current_element else None,
                        },
                        resolved,
                    )

                # Condition not met yet, but fast path worked - check timeout
                if elapsed >= timeout:
                    return (
                        {
                            "matched": False,
                            "elapsed_seconds": round(elapsed, 2),
                            "polls": polls,
                            "last_state": last_element.model_dump() if last_element else None,
                        },
                        resolved,
                    )

                # Wait before next poll
                await asyncio.sleep(interval)
                continue

            # Fast path not applicable or failed - use traditional describe-all
            # Fetch UI elements with filtering for performance
            elements, _ = await self.get_ui_elements(
                resolved,
                filter_label=label,
                filter_identifier=identifier,
                filter_type=element_type,
            )

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
        snapshot_depth: int | None = None,
        strategy: str | None = None,
    ) -> tuple[dict, str]:
        """Generate an LLM-optimized screen summary. Returns (summary_dict, resolved_udid).

        Args:
            max_elements: Maximum interactive elements to include (0 = unlimited)
            udid: Device UDID (auto-resolves if omitted)
            snapshot_depth: WDA accessibility tree depth (1-50, physical devices only)
            strategy: 'skeleton' to skip /source timeout on complex screens (physical only)
        """
        resolved = await self.resolve_udid(udid)
        if strategy == "skeleton" and self._is_physical(resolved):
            raw = await self.wda_client.build_screen_skeleton(resolved)
            elements = parse_elements(raw)
        else:
            elements, resolved = await self.get_ui_elements(udid, snapshot_depth=snapshot_depth)
        return generate_screen_summary(elements, max_elements=max_elements), resolved

    async def tap(self, x: float, y: float, udid: str | None = None) -> str:
        """Tap at coordinates. Returns the resolved udid."""
        resolved = await self.resolve_udid(udid)
        await self._ui_backend(resolved).tap(resolved, x, y)
        self._invalidate_ui_cache(resolved)  # UI changed
        return resolved

    async def tap_element(
        self,
        label: str | None = None,
        identifier: str | None = None,
        element_type: str | None = None,
        udid: str | None = None,
        skip_stability_check: bool = False,
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

        # Fast path: for known static elements, tap directly at known coordinates
        if identifier and identifier in self._STATIC_ELEMENT_POSITIONS:
            resolved = await self.resolve_udid(udid)
            dimensions = await self._get_screen_dimensions(resolved)

            if dimensions:
                x_offset, y_offset, anchor = self._STATIC_ELEMENT_POSITIONS[identifier]

                if anchor == "bottom-left":
                    x = x_offset
                    y = dimensions["height"] - y_offset
                elif anchor == "top-right":
                    x = dimensions["width"] - x_offset
                    y = y_offset
                else:
                    logger.warning(f"[FAST PATH TAP] Unknown anchor '{anchor}' for {identifier}, falling back")
                    # Fall through to traditional path

                if anchor in ("bottom-left", "top-right"):
                    logger.info(f"[FAST PATH TAP] Tapping {identifier} at calculated coordinates ({x}, {y}) [anchor={anchor}]")

                    # Tap directly without fetching UI tree
                    await self._ui_backend(resolved).tap(resolved, x, y)

                    return {
                        "status": "ok",
                        "tapped": {
                            "identifier": identifier,
                            "type": "Button",
                            "x": x,
                            "y": y,
                        },
                    }

        # Traditional path: fetch full UI tree
        elements, resolved = await self.get_ui_elements(
            udid,
            filter_label=label,
            filter_identifier=identifier,
            filter_type=element_type,
        )

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
            cx, cy = get_tap_point(el)

            # Home indicator obstruction check: if the element's tap point
            # falls in the bottom safe area (home indicator zone), try to
            # scroll it into view before tapping.
            screen_height = self._get_screen_height_from_elements(elements)
            screen_width = self._get_screen_width_from_elements(elements)
            if not screen_height or not screen_width:
                # Filtered element list may not include the Application element;
                # fall back to the device dimensions lookup table.
                dims = await self._get_screen_dimensions(resolved)
                if dims:
                    screen_height = screen_height or dims["height"]
                    screen_width = screen_width or dims["width"]
            screen_width = screen_width or 393
            if screen_height and self._is_obscured_by_home_indicator(el, screen_height):
                logger.info(
                    "tap_element: element '%s' obscured by home indicator — scrolling into view",
                    el.label or el.identifier,
                )
                scrolled_el = await self._scroll_element_into_view(
                    resolved, label, identifier, element_type,
                    screen_height, screen_width,
                )
                if scrolled_el is not None:
                    el = scrolled_el
                    cx, cy = get_tap_point(el)
                else:
                    logger.warning(
                        "tap_element: scroll-into-view failed for '%s' (fixed-position). "
                        "Tapping at original coordinates.",
                        el.label or el.identifier,
                    )

            # Stability check: ensure element has stopped moving/animating
            # Skip for static elements (tab bars, nav bars) to avoid expensive tree fetches
            if not skip_stability_check:
                # Get initial position
                initial_frame = el.frame
                await asyncio.sleep(0.1)

                # Re-fetch UI and find element again (bypass cache to detect changes!)
                # Use filtering for performance
                elements_check, _ = await self.get_ui_elements(
                    resolved,
                    use_cache=False,
                    filter_label=label,
                    filter_identifier=identifier,
                    filter_type=element_type,
                )

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
                        # Use filtering for performance
                        elements_final, _ = await self.get_ui_elements(
                            resolved,
                            use_cache=False,
                            filter_label=label,
                            filter_identifier=identifier,
                            filter_type=element_type,
                        )

                        matches_final = find_element(elements_final, label=label, identifier=identifier, element_type=element_type)
                        if matches_final:
                            cx, cy = get_tap_point(matches_final[0])

            await self._ui_backend(resolved).tap(resolved, cx, cy)
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
        await self._ui_backend(resolved).swipe(resolved, start_x, start_y, end_x, end_y, duration)
        self._invalidate_ui_cache(resolved)  # UI changed
        return resolved

    async def type_text(self, text: str, udid: str | None = None) -> str:
        """Type text into focused field. Returns the resolved udid."""
        resolved = await self.resolve_udid(udid)
        await self._ui_backend(resolved).type_text(resolved, text)
        self._invalidate_ui_cache(resolved)  # UI changed (text field value updated)
        return resolved

    async def clear_text(self, udid: str | None = None) -> str:
        """Clear text in the currently focused text field.

        Finds a text field with content, triple-taps to select all, then
        presses Backspace. Returns the resolved udid.
        """
        resolved = await self.resolve_udid(udid)

        # Find the focused text field by looking for TextField/SecureTextField with a value
        elements, _ = await self.get_ui_elements(udid=resolved)
        text_fields = [
            e for e in elements
            if e.type in ("TextField", "SecureTextField", "TextArea", "SearchField")
            and e.frame
        ]

        # Prefer fields with a value (text to clear), fall back to first text field
        target = None
        for tf in text_fields:
            if tf.value:
                target = tf
                break
        if target is None and text_fields:
            target = text_fields[0]

        if target is None or target.frame is None:
            raise DeviceError("No text field found to clear", tool="idb")

        cx = target.frame["x"] + target.frame["width"] / 2
        cy = target.frame["y"] + target.frame["height"] / 2

        await self._ui_backend(resolved).select_all_and_delete(
            resolved, x=cx, y=cy, element_type=target.type,
        )
        self._invalidate_ui_cache(resolved)
        return resolved

    async def press_button(self, button: str, udid: str | None = None) -> str:
        """Press a hardware button. Returns the resolved udid."""
        resolved = await self.resolve_udid(udid)
        await self._ui_backend(resolved).press_button(resolved, button)
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
