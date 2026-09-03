"""DeviceControllerUI — mixin for UI inspection and interaction."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from pathlib import Path

from server.device.probing import frame_key
from server.device.screenshots import annotate_screenshot
from server.device.ui_elements import (
    find_children_of,
    find_element,
    generate_screen_summary,
    get_center,
    get_tap_point,
    parse_elements,
)
from server.device.web_probing import WebSweepResult
from server.models import DeviceError, UIElement, WaitCondition


def _build_screen_context(elements: list[UIElement]) -> dict:
    """Build a lightweight screen context dict from an existing elements list.

    Used to enrich error responses so callers know what screen the app is on
    when an action fails. Uses max_elements=10 for compact output.
    """
    try:
        summary = generate_screen_summary(elements, max_elements=10)
        return {
            "screen_title": summary.get("screen_title", ""),
            "summary": summary.get("summary", ""),
            "element_count": summary.get("element_count", 0),
            "interactive_elements": summary.get("interactive_elements", []),
        }
    except Exception:
        return {}  # best-effort — don't mask the original error


_SCREENSHOT_DIR = Path("/tmp/quern/screenshots")


async def _capture_screenshot(controller, udid: str, label: str) -> str | None:
    """Best-effort screenshot capture. Returns file path or None."""
    try:
        _SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%S")
        filename = f"{ts}_{label}.png"
        filepath = _SCREENSHOT_DIR / filename
        image_bytes, _ = await controller.screenshot(udid=udid, scale=0.5)
        filepath.write_bytes(image_bytes)
        return str(filepath)
    except Exception:
        return None


def _effective_filter_label(
    label: str | None,
    label_contains: str | None,
    label_prefix: str | None,
) -> str | None:
    """Return the label value to use for get_ui_elements filter_label.

    get_ui_elements uses filter_label for exact match optimization during
    parsing. For contains/prefix, we skip that optimization (return None)
    since the filter is applied post-parse by find_element.
    """
    return label

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
    - self._is_android(udid) -> bool
    - self._ensure_android_screen_on(udid) -> None
    """

    # Bottom safe area inset for devices with home indicator (Face ID / Dynamic Island)
    _HOME_INDICATOR_INSET = 34  # points

    # Top safe inset (status bar / navigation bar). The iOS accessibility tree
    # includes elements that have scrolled *under* the top chrome — they carry
    # real, in-bounds frames but aren't visible or tappable. scroll_to_element
    # treats a target whose top edge is above this inset as not-yet-in-view.
    _TOP_SAFE_INSET = 50  # points

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

        Android devices use U2Backend; iOS physical devices use WdaBackend;
        iOS simulators use SimBridgeBackend (preferred) or IdbBackend (fallback).
        """
        if self._is_android(udid):
            return self.u2
        if self._is_physical(udid):
            return self.wda_client
        if self._sim_bridge_ok:
            return self.sim_bridge
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
        label_contains: str | None,
        label_prefix: str | None,
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

        filter_lbl = _effective_filter_label(label, label_contains, label_prefix)

        for attempt in range(self._MAX_SCROLL_ATTEMPTS):
            # Get element position before scroll
            elements_before, _ = await self.get_ui_elements(
                resolved, use_cache=False,
                filter_label=filter_lbl, filter_identifier=identifier,
                filter_type=element_type,
            )

            matches_before = find_element(
                elements_before, label=label,
                label_contains=label_contains, label_prefix=label_prefix,
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
            await self._ui_backend(resolved).swipe(
                resolved, mid_x, swipe_start_y, mid_x, swipe_end_y, 0.3,
            )
            self._invalidate_ui_cache(resolved)

            # Re-fetch and check
            elements_after, _ = await self.get_ui_elements(
                resolved, use_cache=False,
                filter_label=filter_lbl, filter_identifier=identifier,
                filter_type=element_type,
            )

            matches_after = find_element(
                elements_after, label=label,
                label_contains=label_contains, label_prefix=label_prefix,
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

    # The progress check runs exactly once, around the first blind swipe.
    # Repeating it was measured breaking long scrolls: each check costs a tree
    # read (~1.8s), and inserting that between swipes kills the fling momentum
    # that successive swipes chain together. With checks on every iteration a
    # row 60 down went from reliably found in 17s to intermittently not found
    # at all in 90s. Checking once, before the sweep gets going, costs two reads
    # and leaves the remaining swipes back to back.
    _BLIND_PROGRESS_CHECKS = 1

    async def _ios_scroll_to_element(
        self,
        resolved: str,
        label: str | None,
        identifier: str | None,
        max_swipes: int,
        target_known_absent: bool = False,
    ) -> UIElement | None:
        """Scroll an iOS scroll container until the target element is on-screen.

        The controller-level analog of Android's U2Backend.scroll_into_view
        swipe loop (#50/#54): a bounded, coordinate-swipe loop that re-checks the
        target by selector after each swipe. iOS accessibility snapshots are
        side-effect-free (unlike Android's dump_hierarchy, see #49), so matching
        against the tree is safe. Two scroller behaviours are handled:

        - Laid-out scrollers (UIScrollView / SwiftUI ScrollView) keep off-screen
          subviews in the tree with frames outside the viewport. When the target
          is located but off-screen we know its direction, so we swipe straight
          toward it and stop once its frame stops moving (end of travel).
        - Lazy/recycling scrollers (UITableView / UICollectionView / SwiftUI
          List) drop off-screen rows from the tree entirely — the target simply
          isn't matched — so we fall back to a blind sweep, down then up.

        Returns the on-screen UIElement, or None if it never became visible
        within the swipe budget.
        """
        dims = await self._get_screen_dimensions(resolved)
        screen_height = dims["height"] if dims else None
        screen_width = dims["width"] if dims else None
        if not screen_height or not screen_width:
            # Physical devices aren't in the dimensions table. Recover the
            # viewport from the Application element via a *targeted* query
            # (filter_type) — on physical this routes to a WDA predicate query
            # for the root element, avoiding the full /source read that can time
            # out and restart WDA on dense screens.
            app_els, _ = await self.get_ui_elements(
                resolved, use_cache=False, filter_type="Application",
            )
            app_frame = next((e.frame for e in app_els if e.frame), None)
            if app_frame:
                screen_height = screen_height or app_frame["height"]
                screen_width = screen_width or app_frame["width"]
        screen_height = screen_height or 852
        screen_width = screen_width or 393

        top_safe = self._TOP_SAFE_INSET
        bottom_safe = screen_height - self._HOME_INDICATOR_INSET
        mid_x = screen_width / 2
        # Swipe endpoints: near the bottom (y_far) and near the top (y_near).
        # Swiping y_far -> y_near reveals content below; y_near -> y_far reveals
        # content above (mirrors the Android sweep geometry).
        y_far = screen_height * 0.72
        y_near = screen_height * 0.30

        async def _fetch() -> UIElement | None:
            els, _ = await self.get_ui_elements(
                resolved, use_cache=False,
                filter_label=label, filter_identifier=identifier,
            )
            matches = find_element(els, label=label, identifier=identifier)
            return matches[0] if matches else None

        async def _signature() -> tuple | None:
            """Fingerprint the screen by hit-testing a few points.

            A full tree read costs ~1.8s; a hit-test is ~85ms, so sampling
            three points either side of one swipe costs about a tenth of a
            single tree read. The first version of this check used describe_all
            and cost more than the 30 swipes it was replacing — measured at no
            net improvement.

            Three points rather than one because a single sample can land on
            fixed chrome: a nav bar or a pinned header never moves, whether or
            not the content beneath it scrolls.
            """
            backend = self._ui_backend(resolved)
            out: list[tuple[str, int]] = []
            for fraction in (0.35, 0.55, 0.75):
                try:
                    hit = await backend.describe_point(
                        resolved, mid_x, screen_height * fraction,
                    )
                except Exception:
                    return None  # never let the progress check break the scroll
                if not hit:
                    out.append(("", -1))
                    continue
                frame = hit.get("frame") or {}
                out.append((
                    hit.get("AXUniqueId") or hit.get("identifier")
                    or hit.get("AXLabel") or hit.get("label") or "",
                    round(frame.get("y", -1)),
                ))
            return tuple(out)

        def _visible(el: UIElement) -> bool:
            # In view = the top edge clears the top chrome (not scrolled under
            # the nav/status bar) AND the tap point clears the home indicator.
            # The iOS a11y tree lists occluded rows with in-bounds frames, so a
            # top-edge bound is needed to reject them (a bare center check gives
            # false positives for rows tucked under the nav bar).
            if el.frame is None:
                return False
            _, cy = get_tap_point(el)
            return el.frame["y"] >= top_safe and cy <= bottom_safe

        async def _swipe(y1: float, y2: float) -> None:
            await self._ui_backend(resolved).swipe(resolved, mid_x, y1, mid_x, y2, 0.3)
            self._invalidate_ui_cache(resolved)

        # tap_element has just run the identical filtered query and found
        # nothing, so repeating it here spends a full describe_all (~1.8s)
        # re-learning what the caller already knows. scroll_to_element calls in
        # cold with no prior read, so it still needs this check.
        el = None if target_known_absent else await _fetch()
        if el is not None and _visible(el):
            return el

        last_cy: float | None = None
        stalls = 0
        blind_steps = 0
        # Progress detection for the blind branch. Sampling costs a tree read,
        # so it is bounded: a screen that cannot scroll reveals itself in the
        # first couple of swipes, and after that the cost would be pure waste on
        # a container that is scrolling perfectly well.
        # Set on the first blind iteration and compared on the next, so a
        # static screen costs two swipes rather than the full budget. Seeding it
        # from the caller's tree was tried and dropped: tap_element's read is
        # filtered to the missing target, so the list is always empty there.
        blind_signature: tuple | None = None
        progress_checked = False
        blind_down = max_swipes          # sweep down for the first budget...
        blind_total = max_swipes * 3     # ...then up for twice as long

        for _ in range(max_swipes * 3):
            if el is not None and el.frame is not None:
                # Located but off-screen: swipe straight toward it. Direction
                # mirrors _visible's two out-of-view cases.
                _, cy = get_tap_point(el)
                if el.frame["y"] < top_safe:
                    y1, y2 = y_near, y_far   # clipped above → reveal upper content
                else:
                    y1, y2 = y_far, y_near   # below safe area → reveal lower content
                # End-of-travel guard: frame stopped moving across swipes.
                if last_cy is not None and abs(cy - last_cy) < 2:
                    stalls += 1
                    if stalls >= 2:
                        logger.info(
                            "ios scroll-to-element: target off-screen but container "
                            "won't scroll further (cy=%.0f) — aborting", cy,
                        )
                        return None
                else:
                    stalls = 0
                last_cy = cy
            else:
                # Not located (recycled/lazy row): blind sweep, down then up.
                if blind_steps >= blind_total:
                    return None
                y1, y2 = (y_far, y_near) if blind_steps < blind_down else (y_near, y_far)
                blind_steps += 1
                last_cy = None
                stalls = 0
                if blind_steps == 1 and not progress_checked:
                    # Fingerprint BEFORE the first swipe, so one swipe is enough
                    # to answer "does anything here move". Capturing it after
                    # the first swipe instead costs a second swipe to reach the
                    # same conclusion, and swipes are the visible part.
                    blind_signature = await _signature()

            await _swipe(y1, y2)

            if el is None and not progress_checked and blind_signature is not None:
                progress_checked = True
                signature = await _signature()
                if signature is not None:
                    # An unchanged screen after a *downward* swipe is not enough
                    # to call it static: a list already scrolled to the bottom
                    # cannot move further down while content above it is still
                    # reachable. Probe the other direction before concluding,
                    # otherwise targets above the viewport report not_found.
                    if signature == blind_signature:
                        await _swipe(y_near, y_far)
                        reverse = await _signature()
                        if reverse is not None and reverse != blind_signature:
                            blind_signature = reverse  # it moves; carry on
                            el = await _fetch()
                            if el is not None and _visible(el):
                                return el
                            continue
                        signature = blind_signature  # neither direction moved
                    if signature == blind_signature:
                        # A swipe over scrollable content always moves geometry.
                        # Identical positions mean nothing scrolled, so the
                        # remaining budget would repeat this exact no-op.
                        logger.info(
                            "ios scroll-to-element: screen unchanged after a swipe "
                            "— nothing scrollable here, aborting after %d", blind_steps,
                        )
                        return None
                    blind_signature = signature

            el = await _fetch()
            if el is not None and _visible(el):
                # Settle and re-confirm: a swipe can leave the container
                # rubber-banding, so the immediate frame may be an over-scroll
                # bounce that snaps back out of view. Re-fetching once settled
                # both rejects that and returns accurate resting coordinates.
                await asyncio.sleep(0.3)
                el = await _fetch()
                if el is not None and _visible(el):
                    return el

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
            logger.debug(
                "[FAST PATH] Unknown screen dimensions for device, "
                "falling back to describe-all"
            )
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
                logger.debug(
                    f"[FAST PATH] Element at ({x}, {y}) is "
                    f"'{found_identifier}', not '{identifier}'"
                )
                return (True, None)  # Fast path succeeded, wrong element

        except Exception as e:
            logger.debug(f"[FAST PATH] describe-point failed: {e}, falling back")
            return (False, None)

    async def _wda_direct_query(
        self,
        udid: str,
        label: str | None = None,
        label_contains: str | None = None,
        label_prefix: str | None = None,
        identifier: str | None = None,
        element_type: str | None = None,
    ) -> tuple[list[UIElement], float]:
        """Query WDA directly for specific elements without fetching the full tree.

        Translates label/identifier/type into WDA locator strategies:
        - identifier only → 'accessibility id' (fastest, exact match)
        - label only → 'predicate string' with label ==[c] (case-insensitive)
        - label_contains → 'predicate string' with label CONTAINS[c]
        - label_prefix → 'predicate string' with label BEGINSWITH[c]
        - combined filters → 'predicate string' with AND clauses

        Returns (parsed UIElement objects, elapsed seconds). Empty list on no match or error.
        """
        def _escape(val: str) -> str:
            """Escape single quotes for NSPredicate string literals."""
            return val.replace("'", "\\'")

        xcui_type = f"XCUIElementType{element_type}" if element_type else None

        any_label = label or label_contains or label_prefix

        # Choose the most efficient WDA locator strategy
        if identifier and not any_label and not xcui_type:
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
            elif label_contains:
                clauses.append(f"label CONTAINS[c] '{_escape(label_contains)}'")
            elif label_prefix:
                clauses.append(f"label BEGINSWITH[c] '{_escape(label_prefix)}'")
            if xcui_type:
                clauses.append(f"type == '{xcui_type}'")

            if not clauses:
                return [], 0.0

            using = "predicate string"
            value = " AND ".join(clauses)

        logger.info("[WDA DIRECT] %s=%s on %s", using, value, udid[:8])
        start = time.perf_counter()
        raw = await self.wda_client.find_elements_by_query(udid, using, value)
        elapsed = time.perf_counter() - start
        elements = parse_elements(raw)

        # If accessibility id returned nothing, retry with predicate string —
        # but only if the first query was fast (<2s). On dense screens, WDA
        # queries are uniformly slow so retrying just wastes time.
        if (not elements and using == "accessibility id"
                and identifier and elapsed < 2.0):
            pred_value = f"name == '{_escape(identifier)}'"
            logger.info(
                "[WDA DIRECT] retrying with predicate string=%s on %s",
                pred_value, udid[:8],
            )
            raw = await self.wda_client.find_elements_by_query(udid, "predicate string", pred_value)
            elements = parse_elements(raw)
        elif not elements and elapsed >= 2.0:
            logger.info(
                "[WDA DIRECT] skipping predicate retry "
                "(first query took %.1fs) on %s", elapsed, udid[:8],
            )

        return elements, elapsed

    # How long web elements stay usable without being re-read. Generous, because
    # correctness does not rest on it: a tap against a stale overlay is caught by
    # the probe in tap_element, so the TTL only bounds how long a page that
    # changed underneath us keeps offering elements that will then be refused.
    _WEB_OVERLAY_TTL = 60.0

    def _merge_web_overlay(
        self,
        udid: str,
        elements: list[UIElement],
        *,
        filter_label: str | None = None,
        filter_identifier: str | None = None,
        filter_type: str | None = None,
    ) -> list[UIElement]:
        """Append web elements to a native element list."""
        entry = self._web_overlay.get(udid)
        if entry is None:
            return elements
        web, stored_at = entry
        if time.time() - stored_at > self._WEB_OVERLAY_TTL:
            self._web_overlay.pop(udid, None)
            return elements

        if filter_label or filter_identifier or filter_type:
            web = find_element(web, label=filter_label, identifier=filter_identifier,
                               element_type=filter_type)
        if not web:
            return elements

        # A web element whose frame the native tree already reports is the same
        # thing seen twice, and a duplicate would make tap_element ambiguous.
        seen = {frame_key(e.frame) for e in elements if e.frame}
        merged = list(elements)
        for element in web:
            key = frame_key(element.frame)
            if key is None or key in seen:
                continue
            seen.add(key)
            merged.append(element)
        return merged

    def _store_web_overlay(self, udid: str, web_elements: list[dict]) -> None:
        """Keep web content addressable by later UI reads."""
        parsed: list[UIElement] = []
        for element in web_elements:
            attrs = {
                key: str(value)
                for key, value in (
                    ("source", element.get("source")),
                    ("dom_id", element.get("dom_id")),
                    ("page_id", element.get("page_id")),
                    ("href", element.get("href")),
                    ("tag", element.get("tag")),
                )
                if value is not None
            }
            parsed.append(UIElement(
                type=element.get("type") or "Other",
                label=element.get("AXLabel") or "",
                # A DOM id is not an accessibility identifier and must not
                # masquerade as one: it never appears in the native tree, so
                # letting it match `identifier=` would make a web-only lookup
                # look like an ordinary one.
                identifier=None,
                frame=element.get("frame"),
                enabled=True,
                extra_attrs=attrs or None,
            ))
        if parsed:
            self._web_overlay[udid] = (parsed, time.time())
        else:
            self._web_overlay.pop(udid, None)

    @staticmethod
    def _encloses(outer: dict | None, inner: dict | None) -> bool:
        """Whether `outer` strictly contains `inner`.

        Strictly: two frames of the same size say nothing about nesting, and
        accepting them would let an element swapped in at the same coordinates
        pass as the one that used to be there -- which is the staleness this
        check exists to catch.
        """
        if not outer or not inner:
            return False
        outer_area = outer.get("width", 0) * outer.get("height", 0)
        inner_area = inner.get("width", 0) * inner.get("height", 0)
        if outer_area <= inner_area:
            return False
        return (
            outer.get("x", 0) <= inner.get("x", 0)
            and outer.get("y", 0) <= inner.get("y", 0)
            and outer.get("x", 0) + outer.get("width", 0)
            >= inner.get("x", 0) + inner.get("width", 0)
            and outer.get("y", 0) + outer.get("height", 0)
            >= inner.get("y", 0) + inner.get("height", 0)
        )

    async def _web_element_still_there(self, udid: str, element: UIElement) -> bool:
        """One probe, before tapping something the tree cannot see.

        The overlay records where a page's elements were when it was read. If the
        page has scrolled, navigated or been covered since, those coordinates now
        belong to something else -- and a tap would land on it silently. Paying
        ~93ms to be told otherwise is the difference between a wrong action and
        an honest error.
        """
        from server.device.web_content import _texts_correspond, normalise
        from server.device.web_probing import hit_contains

        frame = element.frame or {}
        x = frame.get("x", 0) + frame.get("width", 0) / 2
        y = frame.get("y", 0) + frame.get("height", 0) / 2
        try:
            hit = await self._ui_backend(udid).describe_point(udid, x, y)
        except Exception:
            logger.debug("web element verification probe failed", exc_info=True)
            return False
        if not hit or not hit_contains(hit.get("frame"), x, y):
            return False
        label = normalise(hit.get("AXLabel"))
        # An unlabelled control (an icon-only button) cannot be confirmed by
        # text; landing inside the expected frame is all the evidence there is.
        if not element.label:
            return True
        if _texts_correspond(label, normalise(element.label)):
            return True
        # Accessibility flattens nesting the DOM keeps: an emoji <span> inside a
        # link is reported as the link, and a form control is named by its
        # <label>. When the element answering encloses the one asked for, a tap
        # here still activates what the caller meant, so the differing name is
        # not evidence of staleness.
        return self._encloses(hit.get("frame"), frame)

    async def get_ui_elements(
        self,
        udid: str | None = None,
        use_cache: bool = True,
        filter_label: str | None = None,
        filter_identifier: str | None = None,
        filter_type: str | None = None,
        snapshot_depth: int | None = None,
        source_timeout: float | None = None,
        mode: str | None = None,
    ) -> tuple[list[UIElement], str]:
        """Native UI elements, plus any web content read by get_web_content.

        The merge happens here rather than in the cache so every consumer --
        tap_element, screen summaries, landmark matching, annotated screenshots
        -- sees web elements, while the cached tree stays purely native.
        """
        elements, resolved = await self._native_ui_elements(
            udid, use_cache, filter_label, filter_identifier, filter_type,
            snapshot_depth, source_timeout, mode,
        )
        return self._merge_web_overlay(
            resolved, elements,
            filter_label=filter_label, filter_identifier=filter_identifier,
            filter_type=filter_type,
        ), resolved

    async def _native_ui_elements(
        self,
        udid: str | None = None,
        use_cache: bool = True,
        filter_label: str | None = None,
        filter_identifier: str | None = None,
        filter_type: str | None = None,
        snapshot_depth: int | None = None,
        source_timeout: float | None = None,
        mode: str | None = None,
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
            elements, query_elapsed = await self._wda_direct_query(
                resolved, label=filter_label,
                identifier=filter_identifier, element_type=filter_type,
            )
            if elements:
                return elements, resolved
            # Direct query found nothing. If the query was slow (>3s), the screen
            # is too dense for any WDA query to work — skip /source to avoid a
            # 30s+ cascade of timeouts and WDA restarts.
            if query_elapsed > 3.0:
                logger.warning(
                    "[WDA DIRECT] query took %.1fs with 0 results "
                    "on %s — skipping /source fallback",
                    query_elapsed, resolved[:8],
                )
                return [], resolved
            logger.info(
                "[WDA DIRECT FALLBACK] No results for id=%s "
                "label=%s, trying /source",
                filter_identifier, filter_label,
            )

        # Cache miss or bypassed - fetch from idb
        self._cache_misses += 1

        # Wake Android screen before fetching UI tree (reliable via ADB)
        if self._is_android(resolved):
            await self._ensure_android_screen_on(resolved)

        backend = self._ui_backend(resolved)
        if mode == "flat" and hasattr(backend, "describe_all_flat"):
            raw = await backend.describe_all_flat(
                resolved, snapshot_depth=snapshot_depth,
                source_timeout=source_timeout,
            )
            use_cache = False  # flat mode returns different elements
        else:
            raw = await backend.describe_all(
                resolved, snapshot_depth=snapshot_depth,
                source_timeout=source_timeout,
            )

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

            # Stamped when the read COMPLETED, not when it started.
            #
            # `now` is taken at function entry, and describe_all takes ~1.8s on
            # a simulator — so entries stamped with it were born 1.8s old and
            # could never satisfy the 300ms TTL. The cache was effectively dead
            # on this path: a not-found did two full reads back to back, the
            # second of them for screen context, both ~1.8s.
            self._ui_cache[resolved] = (elements, time.time())

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
        nested = await self._ui_backend(resolved).describe_all_nested(
            resolved, snapshot_depth=snapshot_depth,
        )
        child_dicts = find_children_of(
            nested, parent_identifier=children_of,
            parent_label=children_of,
        )
        elements = parse_elements(child_dicts)
        return elements, resolved

    async def get_element(
        self,
        label: str | None = None,
        label_contains: str | None = None,
        label_prefix: str | None = None,
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
        any_label = label or label_contains or label_prefix
        if not any_label and not identifier and not element_type:
            raise DeviceError(
                "At least one of label, identifier, or element_type is required",
                tool="idb",
            )

        elements, resolved = await self.get_ui_elements(udid)
        matches = find_element(
            elements, label=label, label_contains=label_contains,
            label_prefix=label_prefix, identifier=identifier,
            element_type=element_type,
        )

        if len(matches) == 0:
            search_desc = (
                f"label='{label}'" if label
                else f"label_contains='{label_contains}'" if label_contains
                else f"label_prefix='{label_prefix}'" if label_prefix
                else f"identifier='{identifier}'"
            )
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
        label_contains: str | None = None,
        label_prefix: str | None = None,
        identifier: str | None = None,
        element_type: str | None = None,
        value: str | None = None,
        timeout: float = 10,
        interval: float = 0.5,
        udid: str | None = None,
        mode: str | None = None,
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
        any_label = label or label_contains or label_prefix
        if not any_label and not identifier and not element_type:
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
                    # Best-effort screen context + screenshot for fast-path timeout
                    try:
                        ctx_elements, _ = await self.get_ui_elements(
                            resolved, mode=mode,
                        )
                        screen_context = _build_screen_context(ctx_elements)
                    except Exception:
                        screen_context = {}
                    screenshot = await _capture_screenshot(
                        self, resolved, "wait_timeout",
                    )
                    if screenshot:
                        screen_context["screenshot"] = screenshot
                    return (
                        {
                            "matched": False,
                            "elapsed_seconds": round(elapsed, 2),
                            "polls": polls,
                            "last_state": last_element.model_dump() if last_element else None,
                            "screen_context": screen_context,
                        },
                        resolved,
                    )

                # Wait before next poll
                await asyncio.sleep(interval)
                continue

            # Fast path not applicable or failed - use traditional describe-all
            # Fetch UI elements with filtering for performance
            filter_label = _effective_filter_label(label, label_contains, label_prefix)
            elements, _ = await self.get_ui_elements(
                resolved,
                filter_label=filter_label,
                filter_identifier=identifier,
                filter_type=element_type,
                mode=mode,
            )

            matches = find_element(
                elements,
                label=label,
                label_contains=label_contains,
                label_prefix=label_prefix,
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
                # Fetch unfiltered elements for screen context (the polling
                # loop uses filtered fetches that may return empty)
                try:
                    ctx_elements, _ = await self.get_ui_elements(
                        resolved, mode=mode,
                    )
                    screen_context = _build_screen_context(ctx_elements)
                except Exception:
                    screen_context = {}
                screenshot = await _capture_screenshot(
                    self, resolved, "wait_timeout",
                )
                if screenshot:
                    screen_context["screenshot"] = screenshot
                return {
                    "matched": False,
                    "elapsed_seconds": round(elapsed, 2),
                    "polls": polls,
                    "last_state": last_element.model_dump() if last_element else None,
                    "screen_context": screen_context,
                }, resolved

            # Sleep before next poll
            await asyncio.sleep(interval)

    async def get_screen_summary(
        self,
        max_elements: int = 20,
        udid: str | None = None,
        snapshot_depth: int | None = None,
        strategy: str | None = None,
        source_timeout: float | None = None,
        mode: str | None = None,
    ) -> tuple[dict, list, str]:
        """Generate an LLM-optimized screen summary.

        Returns (summary_dict, elements, resolved_udid).

        Args:
            max_elements: Maximum interactive elements to include (0 = unlimited)
            udid: Device UDID (auto-resolves if omitted)
            snapshot_depth: WDA accessibility tree depth (1-50, physical devices only)
            strategy: 'skeleton' to skip /source timeout on complex screens (physical only)
            source_timeout: Override WDA /source timeout in seconds (physical devices only)
            mode: 'flat' to use flat idb output (for custom companion). Default uses nested.
        """
        resolved = await self.resolve_udid(udid)
        if strategy == "skeleton" and self._is_physical(resolved):
            raw = await self.wda_client.build_screen_skeleton(resolved)
            elements = parse_elements(raw)
        else:
            elements, resolved = await self.get_ui_elements(
                udid, snapshot_depth=snapshot_depth,
                source_timeout=source_timeout,
                mode=mode,
            )
        return generate_screen_summary(elements, max_elements=max_elements), elements, resolved

    async def tap(self, x: float, y: float, udid: str | None = None) -> str:
        """Tap at coordinates. Returns the resolved udid."""
        resolved = await self.resolve_udid(udid)
        await self._ui_backend(resolved).tap(resolved, x, y)
        self._invalidate_ui_cache(resolved)  # UI changed
        return resolved

    async def tap_element(
        self,
        label: str | None = None,
        label_contains: str | None = None,
        label_prefix: str | None = None,
        identifier: str | None = None,
        element_type: str | None = None,
        udid: str | None = None,
        skip_stability_check: bool = False,
        source_timeout: float | None = None,
        value: str | None = None,
        scroll_to_find: bool = True,
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
        """
        any_label = label or label_contains or label_prefix
        if not any_label and not identifier:
            raise DeviceError(
                "Either label/label_contains/label_prefix or identifier "
                "is required for tap-element",
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
                    logger.warning(
                        f"[FAST PATH TAP] Unknown anchor '{anchor}' "
                        f"for {identifier}, falling back"
                    )
                    # Fall through to traditional path

                if anchor in ("bottom-left", "top-right"):
                    logger.info(
                        f"[FAST PATH TAP] Tapping {identifier} at "
                        f"calculated coordinates ({x}, {y}) "
                        f"[anchor={anchor}]"
                    )

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

        # Android fast path: tap by native selector, skipping the full UI-tree
        # dump. dump_hierarchy() scrolls CoordinatorLayout/RecyclerView content
        # out of view on some screens (e.g. cache-detail), so resolving an
        # identifier via the tree can scroll the target away before the tap. A
        # native selector click (UiObject.click) finds+taps the one element with
        # no such side effect, and is faster. Restricted to unambiguous cases
        # (exact identifier/label, no type/value/substring filters); anything
        # else falls through to the dump-based path below.
        resolved_fast = await self.resolve_udid(udid)
        if (
            self._is_android(resolved_fast)
            and value is None
            and not label_contains
            and not label_prefix
            and not element_type
            and (identifier or label)
        ):
            backend = self._ui_backend(resolved_fast)
            tapped = await backend.tap_by_selector(
                resolved_fast, identifier=identifier, label=label,
            )
            # Not in the current view — auto-scroll to it and retry. Uses the
            # selector-based swipe loop (no dump_hierarchy), so it inherits the
            # no-dump-induced-scroll property of the fast path.
            if tapped is None and scroll_to_find:
                found = await backend.scroll_into_view(
                    resolved_fast, identifier=identifier, label=label,
                )
                if found is not None:
                    # Let the scroll settle before clicking — a tap landing
                    # while the list is still coming to rest gets absorbed as a
                    # scroll-stop instead of a click (observed intermittently).
                    await asyncio.sleep(0.4)
                    tapped = await backend.tap_by_selector(
                        resolved_fast, identifier=identifier, label=label,
                    )
            if tapped is not None:
                self._invalidate_ui_cache(resolved_fast)
                return {"status": "ok", "tapped": tapped}
            # Not found even after scrolling — fall through to the dump-based path.

        # Traditional path: fetch full UI tree
        filter_label = _effective_filter_label(label, label_contains, label_prefix)
        cache_hits_before = self._cache_hits
        elements, resolved = await self.get_ui_elements(
            udid,
            filter_label=filter_label,
            filter_identifier=identifier,
            filter_type=element_type,
            source_timeout=source_timeout,
        )
        served_from_cache = self._cache_hits > cache_hits_before

        # Use shared search helper
        matches = find_element(
            elements, label=label, label_contains=label_contains,
            label_prefix=label_prefix, identifier=identifier,
            element_type=element_type,
        )

        # iOS off-screen retry: on a tree-path miss, scroll the target into view
        # and retry. Android selector-misses are handled by the fast path above,
        # so this is scoped to iOS. Only exact label/identifier can be scrolled
        # to (scroll_to_element's contract); contains/prefix/type-only fall
        # through to not_found.
        if (
            len(matches) == 0
            and scroll_to_find
            and not self._is_android(resolved)
            and (label or identifier)
        ):
            # The miss above is only authoritative if that read reached the
            # device. With the cache live it can be served from an entry up to
            # the TTL old, and an element that appeared in that window would be
            # skipped entirely if the scroll loop also declined to look.
            scrolled = await self._ios_scroll_to_element(
                resolved, label=label, identifier=identifier, max_swipes=10,
                target_known_absent=not served_from_cache,
            )
            if scrolled is not None:
                matches = [scrolled]

        if len(matches) == 0:
            search_desc = (
                f"label='{label}'" if label
                else f"label_contains='{label_contains}'" if label_contains
                else f"label_prefix='{label_prefix}'" if label_prefix
                else f"identifier='{identifier}'"
            )
            if element_type:
                search_desc += f", type='{element_type}'"
            # Fetch full (unfiltered) elements for screen context if we used filters
            if filter_label or identifier or element_type:
                try:
                    all_elements, _ = await self.get_ui_elements(resolved)
                except Exception:
                    all_elements = elements
            else:
                all_elements = elements
            screen_context = _build_screen_context(all_elements)
            screenshot = await _capture_screenshot(
                self, resolved, "tap_not_found",
            )
            if screenshot:
                screen_context["screenshot"] = screenshot
            return {
                "status": "not_found",
                "detail": f"No element found matching {search_desc}",
                "screen_context": screen_context,
            }

        if len(matches) == 1:
            el = matches[0]

            # A web element comes from the overlay, not the tree, so it gets its
            # own path: confirm it is still where it was read, then tap. The
            # stability re-fetch below cannot help here -- re-reading the tree
            # returns the same overlay, because the tree never saw the page.
            if (el.extra_attrs or {}).get("source") in ("web-inspector", "web-probe"):
                if not await self._web_element_still_there(resolved, el):
                    self._web_overlay.pop(resolved, None)
                    return {
                        "status": "not_found",
                        "reason": "stale_web_content",
                        "message": (
                            f"'{el.label}' was read from web content that has since "
                            "moved, been covered or navigated away. The stale "
                            "elements have been dropped; call get_web_content again "
                            "to re-read the page."
                        ),
                    }
                cx, cy = get_tap_point(el)
                await self._ui_backend(resolved).tap(resolved, cx, cy)
                self._invalidate_ui_cache(resolved)
                return {
                    "status": "ok",
                    "tapped": {
                        "type": el.type, "label": el.label,
                        "x": cx, "y": cy,
                        "source": (el.extra_attrs or {}).get("source"),
                    },
                }

            # Value check for switches/toggles: skip tap if already in desired state
            if value is not None:
                current_value = el.value or ""
                if current_value == value:
                    return {
                        "status": "already_set",
                        "value": current_value,
                        "element": {
                            "label": el.label,
                            "type": el.type,
                            "identifier": el.identifier,
                        },
                    }

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
                    resolved, label, label_contains, label_prefix,
                    identifier, element_type,
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
                    filter_label=filter_label,
                    filter_identifier=identifier,
                    filter_type=element_type,
                )

                matches_check = find_element(
                    elements_check, label=label,
                    label_contains=label_contains,
                    label_prefix=label_prefix,
                    identifier=identifier,
                    element_type=element_type,
                )

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
                            filter_label=filter_label,
                            filter_identifier=identifier,
                            filter_type=element_type,
                        )

                        matches_final = find_element(
                            elements_final, label=label,
                            label_contains=label_contains,
                            label_prefix=label_prefix,
                            identifier=identifier,
                            element_type=element_type,
                        )
                        if matches_final:
                            cx, cy = get_tap_point(matches_final[0])

            await self._ui_backend(resolved).tap(resolved, cx, cy)
            self._invalidate_ui_cache(resolved)  # UI changed

            # Future enhancement: Post-tap verification
            # if verify_disappears:
            #     await asyncio.sleep(0.2)
            #     elements_verify, _ = await self.get_ui_elements(resolved)
            #     matches_verify = find_element(
            #         elements_verify, label=label,
            #         identifier=identifier, element_type=element_type,
            #     )
            #     if matches_verify:
            #         logger.warning("Element still present after tap, may have failed")
            #         # Could retry here if retry_attempts > 1

            result = {
                "status": "ok",
                "tapped": {
                    "label": el.label,
                    "type": el.type,
                    "identifier": el.identifier,
                    "x": cx,
                    "y": cy,
                },
            }
            if value is not None:
                result["previous_value"] = el.value or ""
                result["requested_value"] = value
            return result

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

    async def _connected_web_inspector(self):
        """The shared Web Inspector connection, opening one if needed."""
        from server.device.webinspector import SimulatorWebInspector

        async with self._web_inspector_lock:
            if self._web_inspector is None:
                inspector = SimulatorWebInspector()
                await inspector.connect()
                self._web_inspector = inspector
            return self._web_inspector

    async def _close_web_inspector(self) -> None:
        async with self._web_inspector_lock:
            inspector, self._web_inspector = self._web_inspector, None
        if inspector is not None:
            try:
                await inspector.close()
            except Exception:
                logger.debug("web inspector close failed", exc_info=True)

    async def _sweep_web_content(
        self, udid: str, native: list[dict],
    ) -> WebSweepResult:
        """Find on-screen content by hit-testing, aimed with a screenshot.

        The route of last resort, and the only one for a web view that no
        process publishes for inspection. Full scale rather than the default
        half: the aiming is Vision text recognition, and halving the pixels
        costs it the small text.
        """
        from server.device.web_probing import sweep_web_content

        async def capture() -> bytes | None:
            try:
                image, _ = await self.screenshot(udid=udid, format="png", scale=1.0)
            except Exception:
                logger.debug("web sweep: screenshot failed", exc_info=True)
                return None
            return image

        return await sweep_web_content(
            udid, self._ui_backend(udid).describe_point, capture, native,
        )

    async def web_page_urls(self, udid: str) -> list[str]:
        """URLs of every inspectable page on this device.

        Costs one Web Inspector round trip and no probes, which is what makes a
        URL usable as a landmark. Silent on failure: a screen identified by
        native landmarks must not stop being identifiable because the Inspector
        is unreachable.
        """
        from server.device.web_content import _is_app
        from server.device.webinspector import simulator_udid_for_application

        if self._is_android(udid) or self._is_physical(udid):
            return []
        require_match = await self._booted_simulator_count() > 1
        try:
            # The same lock the content path takes. The connection is shared and
            # the protocol interleaves replies, so a listing running alongside a
            # read would consume its messages.
            async with self._web_inspector_op_lock:
                inspector = await self._connected_web_inspector()
                urls: list[str] = []
                for application in await inspector.connected_applications():
                    app_id = application.get("application_id")
                    # WebKit's own helper processes are not the app under test.
                    if not app_id or not _is_app(application):
                        continue
                    # The socket carries no UDID, so with several simulators
                    # booted a page could belong to another one. Attributing it
                    # is the same rule get_web_content applies before returning
                    # anyone's content as this device's.
                    owner = await simulator_udid_for_application(app_id)
                    if owner != udid and (owner is not None or require_match):
                        continue
                    for page in await inspector.pages(app_id):
                        url = page.get("url")
                        if url:
                            urls.append(str(url))
            return urls
        except Exception:
            logger.debug("could not list web pages for identification", exc_info=True)
            return []

    async def _booted_simulator_count(self) -> int:
        try:
            devices = await self.simctl.list_devices()
        except Exception:
            return 0
        return sum(1 for d in devices if str(getattr(d, "state", "")).lower().endswith("booted"))

    async def get_web_content(
        self,
        udid: str | None = None,
        bundle_id: str | None = None,
        hints: list | None = None,
    ) -> dict:
        """Read web content the accessibility tree cannot see.

        A `WKWebView` is absent from the tree walk entirely on iOS simulators,
        so a screen built around one looks empty apart from its native chrome.
        The Web Inspector can read the DOM, but reports geometry in page space;
        this pairs the two, returning elements with real screen frames.

        Simulator-only. Android's accessibility tree already descends into
        `WebView`, and physical iOS devices are reached over a different
        transport, so on both the ordinary UI tree is the right tool.
        """
        from server.device.web_content import collect_web_content, from_probe
        from server.device.webinspector import (
            WebInspectorError,
            simulator_udid_for_application,
        )

        resolved = await self.resolve_udid(udid)
        if self._is_android(resolved):
            raise DeviceError(
                "get_web_content is for iOS simulators. Android's accessibility "
                "tree already includes WebView contents, so use get_ui_tree.",
                tool="web-content",
            )
        if self._is_physical(resolved):
            raise DeviceError(
                "get_web_content is for iOS simulators; the Web Inspector service "
                "used here is the simulator's. Use get_ui_tree on a physical device.",
                tool="web-content",
            )

        # Deliberately the unmerged read. get_ui_elements would fold in the
        # previous overlay, and those elements feed page-title ranking,
        # app_frame and the candidate origins -- so a stale read would help
        # decide where the next one thinks the page is.
        elements, resolved = await self._native_ui_elements(resolved)
        native = [
            {"type": e.type, "AXLabel": e.label, "frame": e.frame}
            for e in elements if e.frame
        ]

        # More than one booted simulator means the connection cannot be steered
        # by UDID, so attribution stops being advisory and becomes required.
        require_match = await self._booted_simulator_count() > 1

        async def attempt() -> dict:
            inspector = await self._connected_web_inspector()
            return await collect_web_content(
                resolved, bundle_id, self._ui_backend(resolved).describe_point,
                inspector, native,
                attribute_udid=simulator_udid_for_application,
                require_device_match=require_match,
                hints=hints,
            )

        # One transaction at a time: the connection is shared, and a retry
        # closes it. Concurrent callers would read each other's replies.
        async with self._web_inspector_op_lock:
            try:
                result = await attempt()
                # A reused connection can outlive the app it was reporting on --
                # the simulator reboots, the app relaunches -- and the symptom is
                # an empty application list rather than an error. Retry once on a
                # fresh connection before believing it.
                if not result.get("pages") and not result.get("device_mismatch"):
                    await self._close_web_inspector()
                    result = await attempt()
            except WebInspectorError as exc:
                await self._close_web_inspector()
                raise DeviceError(
                    f"could not reach the simulator's Web Inspector: {exc}",
                    tool="web-content",
                ) from exc

        if result.get("device_mismatch"):
            raise DeviceError(result["reason"], tool="web-content")

        result["route"] = "inspector" if result.get("elements") else None

        # Nothing came back through the Inspector. That is not necessarily a
        # failure: an ASWebAuthenticationSession is presented with no connected
        # application at all, so the OAuth screen every app has is exactly the
        # one the Inspector cannot describe. Hit-testing still reaches it, so
        # sweep for the content directly. The elements are already in screen
        # coordinates -- there is no page space to anchor.
        if not result.get("elements"):
            swept = await self._sweep_web_content(resolved, native)
            result["probes"] += swept.probes
            result["sweep_ms"] = round(swept.elapsed_ms, 1)
            if swept.elements:
                result["elements"] = from_probe(swept.elements)
                result["route"] = "probe"
                result["reason"] = None
                result["truncated"] = swept.truncated
            elif swept.reason:
                result["reason"] = (
                    f"{result.get('reason') or 'the Web Inspector returned nothing'}; "
                    f"hit-testing found nothing either ({swept.reason})"
                )

        result["udid"] = resolved
        # Make the elements addressable by label through the ordinary UI path.
        self._store_web_overlay(resolved, result.get("elements") or [])
        return result

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

    async def scroll_to_element(
        self,
        label: str | None = None,
        identifier: str | None = None,
        udid: str | None = None,
        max_swipes: int = 10,
    ) -> dict:
        """Scroll until an element is in view, without interacting with it.

        A bounded, coordinate swipe loop that re-checks the target by selector
        after each swipe (no full-tree dump-induced scroll — see #49). Works
        across each platform's mix of scroll containers where native
        scroll-into-view is unreliable (Android: View RecyclerView, Compose
        LazyColumn, Compose-in-ScrollView — see #50). Backends:
        - Android: U2Backend.scroll_into_view (`.exists` re-check).
        - iOS (physical WDA + simulator): _ios_scroll_to_element, which also
          handles laid-out scrollers by swiping directly toward an off-screen
          but located target.

        Returns {"status": "ok", "element": {...}} once visible, or
        {"status": "not_found", ...} if it never appeared.
        """
        if not label and not identifier:
            raise DeviceError(
                "Either label or identifier is required for scroll-to-element",
                tool="u2",
            )

        resolved = await self.resolve_udid(udid)
        target = f"identifier='{identifier}'" if identifier else f"label='{label}'"

        if self._is_android(resolved):
            found = await self._ui_backend(resolved).scroll_into_view(
                resolved, identifier=identifier, label=label, max_swipes=max_swipes,
            )
            self._invalidate_ui_cache(resolved)  # scrolling changes the viewport
            if found is None:
                return {
                    "status": "not_found",
                    "detail": f"Element not found after scrolling ({target})",
                }
            return {"status": "ok", "element": found}

        # iOS (physical WDA + simulator)
        el = await self._ios_scroll_to_element(
            resolved, label=label, identifier=identifier, max_swipes=max_swipes,
        )
        self._invalidate_ui_cache(resolved)  # scrolling changes the viewport
        if el is None:
            return {
                "status": "not_found",
                "detail": f"Element not found after scrolling ({target})",
            }
        cx, cy = get_tap_point(el)
        return {
            "status": "ok",
            "element": {
                "label": el.label,
                "identifier": el.identifier,
                "type": el.type,
                "x": cx,
                "y": cy,
            },
        }

    async def type_text(self, text: str, udid: str | None = None) -> str:
        """Type text into focused field. Returns the resolved udid."""
        resolved = await self.resolve_udid(udid)
        await self._ui_backend(resolved).type_text(resolved, text)
        self._invalidate_ui_cache(resolved)  # UI changed (text field value updated)
        return resolved

    _TEXT_FIELD_TYPES = ("TextField", "SecureTextField", "TextArea", "SearchField")

    async def clear_text(
        self,
        udid: str | None = None,
        label: str | None = None,
        identifier: str | None = None,
    ) -> str:
        """Clear a text field: triple-tap to select, then Backspace.

        Name the field with `label` or `identifier` on any screen holding more
        than one. Without a selector this picks the first field that has a
        value, which is not the same thing as the focused field: on a sign-in
        form, clearing before typing the password finds the *email* field and
        empties that instead. It cannot tell which field has focus, because the
        accessibility tree does not report it.
        """
        resolved = await self.resolve_udid(udid)

        elements, _ = await self.get_ui_elements(udid=resolved)
        text_fields = [
            e for e in elements
            if e.type in self._TEXT_FIELD_TYPES and e.frame
        ]

        target = None
        if label or identifier:
            matches = find_element(text_fields, label=label, identifier=identifier)
            if not matches:
                raise DeviceError(
                    f"No text field matching {label or identifier!r} to clear",
                    tool="wda" if self._is_physical(resolved) else "idb",
                )
            target = matches[0]
        else:
            for tf in text_fields:
                if tf.value:
                    target = tf
                    break
            if target is None and text_fields:
                target = text_fields[0]

        if target is None or target.frame is None:
            tool = "wda" if self._is_physical(resolved) else "idb"
            raise DeviceError(
                "No text field found to clear", tool=tool,
            )

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
        grid: int | None = None,
    ) -> tuple[bytes, str]:
        """Capture an annotated screenshot with accessibility overlays.

        Returns (image_bytes, media_type).
        """
        resolved = await self.resolve_udid(udid)
        if self._is_android(resolved):
            raw_png = await self.adb.screenshot(resolved)
        elif self._is_physical(resolved):
            raw_png = await self.pmd3.screenshot(resolved)
        else:
            raw_png = await self.simctl.screenshot(resolved)
        elements, _ = await self.get_ui_elements(resolved)
        return annotate_screenshot(raw_png, elements, scale=scale, quality=quality, grid=grid)
