"""DeviceControllerUI — mixin for UI inspection and interaction (idb-dependent)."""

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
    - self.simctl: SimctlBackend
    - self._ui_cache: dict[str, tuple[list[UIElement], float]]
    - self._cache_ttl: float
    - self._cache_hits: int
    - self._cache_misses: int
    - self._device_info_cache: dict[str, DeviceInfo]
    - self.resolve_udid(udid) -> str
    - self._invalidate_ui_cache(udid) -> None
    """

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
            element = await self.idb.describe_point(udid, x, y)
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

    async def get_ui_elements(
        self,
        udid: str | None = None,
        use_cache: bool = True,
        filter_label: str | None = None,
        filter_identifier: str | None = None,
        filter_type: str | None = None,
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
        start = time.perf_counter()
        has_filters = filter_label or filter_identifier or filter_type
        logger.info(f"[PERF] get_ui_elements START (cache={use_cache}, filters={has_filters})")

        resolved = await self.resolve_udid(udid)
        now = time.time()

        # Check cache (unless explicitly bypassed)
        # Strategy: Use cached full tree, then filter in memory (fast)
        if use_cache and resolved in self._ui_cache:
            cached_elements, cached_time = self._ui_cache[resolved]
            age = now - cached_time
            if age < self._cache_ttl:
                self._cache_hits += 1
                end = time.perf_counter()
                logger.info(f"[PERF] get_ui_elements CACHE HIT: {(end-start)*1000:.1f}ms, age={age*1000:.1f}ms, elements={len(cached_elements)}")

                # If filters provided, apply them to cached elements (in-memory filtering is fast)
                if has_filters:
                    filtered = find_element(cached_elements, label=filter_label,
                                          identifier=filter_identifier, element_type=filter_type)
                    logger.info(f"[PERF] get_ui_elements: filtered from {len(cached_elements)} to {len(filtered)} elements")
                    return filtered, resolved

                return cached_elements, resolved
            else:
                logger.info(f"[PERF CACHE] EXPIRED for {resolved[:8]}: age={age*1000:.1f}ms > ttl={self._cache_ttl*1000:.1f}ms")

        # Cache miss or bypassed - fetch from idb
        self._cache_misses += 1
        t1 = time.perf_counter()
        logger.info(f"[PERF] get_ui_elements: calling idb.describe_all (+{(t1-start)*1000:.1f}ms)")

        raw = await self.idb.describe_all(resolved)

        t2 = time.perf_counter()
        logger.info(f"[PERF] get_ui_elements: idb returned {len(raw)} raw elements (+{(t2-t1)*1000:.1f}ms)")

        # Parse strategy:
        # - If filters AND will cache: parse full tree (for cache), then filter in memory
        # - If filters but won't cache (bypass): parse with filters to save time
        # - If no filters: parse full tree
        t3 = time.perf_counter()
        logger.info(f"[PERF] get_ui_elements: starting parse (+{(t3-t2)*1000:.1f}ms)")

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

        t4 = time.perf_counter()
        end = time.perf_counter()
        logger.info(f"[PERF] get_ui_elements: parsed {len(elements)} elements (+{(t4-t3)*1000:.1f}ms)")
        logger.info(f"[PERF] get_ui_elements COMPLETE: total={( end-start)*1000:.1f}ms")

        return elements, resolved

    async def get_ui_elements_children_of(
        self,
        children_of: str,
        udid: str | None = None,
    ) -> tuple[list[UIElement], str]:
        """Get UI elements scoped to children of a specific parent.

        Uses the nested idb tree to find the parent by identifier or label,
        then returns its flattened descendants as parsed UIElements.
        """
        resolved = await self.resolve_udid(udid)
        nested = await self.idb.describe_all_nested(resolved)
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
        perf_start = time.perf_counter()
        polls = 0
        last_element: UIElement | None = None

        logger.info(f"[PERF] wait_for_element START (condition={condition}, timeout={timeout}s)")

        while True:
            polls += 1
            poll_start = time.perf_counter()
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
                    perf_end = time.perf_counter()
                    logger.info(f"[PERF] wait_for_element MATCHED (fast path): polls={polls}, total={(perf_end-perf_start)*1000:.1f}ms")
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
                    perf_end = time.perf_counter()
                    logger.info(f"[PERF] wait_for_element TIMEOUT (fast path): polls={polls}, total={(perf_end-perf_start)*1000:.1f}ms")
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

            poll_fetch = time.perf_counter()
            logger.debug(f"[PERF] wait_for_element poll #{polls}: fetch took {(poll_fetch-poll_start)*1000:.1f}ms")

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
                perf_end = time.perf_counter()
                logger.info(f"[PERF] wait_for_element MATCHED: polls={polls}, total={( perf_end-perf_start)*1000:.1f}ms")
                return {
                    "matched": True,
                    "elapsed_seconds": round(elapsed, 2),
                    "polls": polls,
                    "element": current_element.model_dump() if current_element else None,
                }, resolved

            # Check timeout
            if elapsed >= timeout:
                perf_end = time.perf_counter()
                logger.info(f"[PERF] wait_for_element TIMEOUT: polls={polls}, total={( perf_end-perf_start)*1000:.1f}ms")
                return {
                    "matched": False,
                    "elapsed_seconds": round(elapsed, 2),
                    "polls": polls,
                    "last_state": last_element.model_dump() if last_element else None,
                }, resolved

            poll_end = time.perf_counter()
            logger.debug(f"[PERF] wait_for_element poll #{polls} complete: {(poll_end-poll_start)*1000:.1f}ms")

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
        start = time.perf_counter()
        logger.info(f"[PERF] tap_element START (label={label}, id={identifier}, skip_stability={skip_stability_check})")

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
                    await self.idb.tap(resolved, x, y)

                    end = time.perf_counter()
                    logger.info(f"[PERF] tap_element COMPLETE (fast path): total={(end-start)*1000:.1f}ms")

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
        t1 = time.perf_counter()
        logger.info(f"[PERF] tap_element: fetching UI elements (+{(t1-start)*1000:.1f}ms)")

        elements, resolved = await self.get_ui_elements(
            udid,
            filter_label=label,
            filter_identifier=identifier,
            filter_type=element_type,
        )

        t2 = time.perf_counter()
        logger.info(f"[PERF] tap_element: got {len(elements)} elements (+{(t2-t1)*1000:.1f}ms)")

        # Use shared search helper
        matches = find_element(elements, label=label, identifier=identifier, element_type=element_type)

        t3 = time.perf_counter()
        logger.info(f"[PERF] tap_element: found {len(matches)} matches (+{(t3-t2)*1000:.1f}ms)")

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

            # Stability check: ensure element has stopped moving/animating
            # Skip for static elements (tab bars, nav bars) to avoid expensive tree fetches
            if not skip_stability_check:
                t4 = time.perf_counter()
                logger.info(f"[PERF] tap_element: starting stability check (+{(t4-t3)*1000:.1f}ms)")

                # Get initial position
                initial_frame = el.frame
                await asyncio.sleep(0.1)

                t5 = time.perf_counter()
                logger.info(f"[PERF] tap_element: stability check fetch #1 (+{(t5-t4)*1000:.1f}ms)")

                # Re-fetch UI and find element again (bypass cache to detect changes!)
                # Use filtering for performance
                elements_check, _ = await self.get_ui_elements(
                    resolved,
                    use_cache=False,
                    filter_label=label,
                    filter_identifier=identifier,
                    filter_type=element_type,
                )

                t6 = time.perf_counter()
                logger.info(f"[PERF] tap_element: stability check fetch #1 complete (+{(t6-t5)*1000:.1f}ms)")

                matches_check = find_element(elements_check, label=label, identifier=identifier, element_type=element_type)

                if matches_check:
                    # Check if position changed (element is animating)
                    current_frame = matches_check[0].frame
                    if current_frame != initial_frame:
                        logger.debug(
                            "Element position changed (animating), waiting for stability: %s -> %s",
                            initial_frame, current_frame
                        )
                        t7 = time.perf_counter()
                        logger.info(f"[PERF] tap_element: position changed, waiting 300ms (+{(t7-t6)*1000:.1f}ms)")

                        # Wait longer for animation to complete
                        await asyncio.sleep(0.3)

                        t8 = time.perf_counter()
                        logger.info(f"[PERF] tap_element: stability check fetch #2 (+{(t8-t7)*1000:.1f}ms)")

                        # Re-fetch one more time to get final position (bypass cache again)
                        # Use filtering for performance
                        elements_final, _ = await self.get_ui_elements(
                            resolved,
                            use_cache=False,
                            filter_label=label,
                            filter_identifier=identifier,
                            filter_type=element_type,
                        )

                        t9 = time.perf_counter()
                        logger.info(f"[PERF] tap_element: stability check fetch #2 complete (+{(t9-t8)*1000:.1f}ms)")

                        matches_final = find_element(elements_final, label=label, identifier=identifier, element_type=element_type)
                        if matches_final:
                            cx, cy = get_tap_point(matches_final[0])

                t_after_stability = time.perf_counter()
                logger.info(f"[PERF] tap_element: stability check complete (+{(t_after_stability-t4)*1000:.1f}ms)")

            t_before_tap = time.perf_counter()
            logger.info(f"[PERF] tap_element: executing tap at ({cx},{cy}) (+{(t_before_tap-t3)*1000:.1f}ms)")

            await self.idb.tap(resolved, cx, cy)
            self._invalidate_ui_cache(resolved)  # UI changed

            end = time.perf_counter()
            logger.info(f"[PERF] tap_element COMPLETE: total={( end-start)*1000:.1f}ms")

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

        await self.idb.select_all_and_delete(resolved, x=cx, y=cy)
        self._invalidate_ui_cache(resolved)
        return resolved

    async def press_button(self, button: str, udid: str | None = None) -> str:
        """Press a hardware button. Returns the resolved udid."""
        resolved = await self.resolve_udid(udid)
        await self.idb.press_button(resolved, button)
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
