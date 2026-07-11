"""API routes for device UI automation (idb-dependent)."""

from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, HTTPException, Query, Request

from server.api.device import (
    _capture_action_screenshot,
    _capture_screen_context,
    _get_controller,
    _handle_device_error,
)
from server.models import (
    ClearTextRequest,
    DeviceError,
    PressButtonRequest,
    ScrollToElementRequest,
    SwipeRequest,
    TapElementRequest,
    TapRequest,
    TypeTextRequest,
    WaitForElementRequest,
)

router = APIRouter(prefix="/api/v1/device", tags=["device"])
logger = logging.getLogger("quern-debug-server.api")


# ---------------------------------------------------------------------------
# UI inspection & interaction
# ---------------------------------------------------------------------------


@router.get("/ui")
async def get_ui_elements(
    request: Request,
    udid: str | None = Query(default=None),
    children_of: str | None = Query(
        default=None,
        description="Only return children of the element with this identifier or label",
    ),
    snapshot_depth: int | None = Query(
        default=None, ge=1, le=50,
        description=(
            "WDA accessibility tree depth (1-50, default 10). "
            "Only affects physical devices."
        ),
    ),
    strategy: str | None = Query(
        default=None,
        description=(
            "Use 'skeleton' to skip /source timeout on "
            "complex screens. Physical devices only."
        ),
    ),
    source_timeout: float | None = Query(
        default=None, ge=1, le=60,
        description="Override WDA /source timeout in seconds. Physical devices only.",
    ),
    mode: str | None = Query(
        default=None, pattern=r"^(flat)$",
        description="'flat' uses flat idb output with custom companion. Default uses nested.",
    ),
    include_raw: bool = Query(
        default=False,
        description=(
            "Include the raw source attributes (extra_attrs) from the underlying "
            "accessibility provider on each element. Useful for debugging the "
            "normalizer when you want to see what got collapsed (e.g., Android "
            "selected= or checkable= attributes that map into our value field). "
            "Stripped by default to keep payloads small. Currently only Android "
            "populates extra_attrs; iOS responses are unchanged either way."
        ),
    ),
):
    """Get all UI accessibility elements from the current screen.

    Optionally scope to children of a specific element using the `children_of` parameter.
    """
    start = time.perf_counter()
    logger.info(f"[PERF] API /ui START (children_of={children_of}, mode={mode})")

    controller = _get_controller(request)
    try:
        if strategy == "skeleton":
            resolved_udid = await controller.resolve_udid(udid)
            if controller._is_physical(resolved_udid):
                raw = await controller.wda_client.build_screen_skeleton(resolved_udid)
                from server.device.ui_elements import parse_elements
                elements = parse_elements(raw)
            else:
                elements, resolved_udid = await controller.get_ui_elements(
                    udid=udid, snapshot_depth=snapshot_depth,
                    source_timeout=source_timeout, mode=mode,
                )
        elif children_of:
            elements, resolved_udid = await controller.get_ui_elements_children_of(
                children_of=children_of, udid=udid, snapshot_depth=snapshot_depth,
            )
        else:
            elements, resolved_udid = await controller.get_ui_elements(
                udid=udid, snapshot_depth=snapshot_depth,
                source_timeout=source_timeout, mode=mode,
            )

        end = time.perf_counter()
        logger.info(f"[PERF] API /ui SUCCESS: {(end-start)*1000:.1f}ms, elements={len(elements)}")
        dump_kwargs = {} if include_raw else {"exclude": {"extra_attrs"}}
        return {
            "elements": [e.model_dump(**dump_kwargs) for e in elements],
            "element_count": len(elements),
            "udid": resolved_udid,
        }
    except DeviceError as e:
        end = time.perf_counter()
        logger.error(f"[PERF] API /ui ERROR: {(end-start)*1000:.1f}ms, error={e}")
        raise _handle_device_error(e)


@router.get("/ui/element")
async def get_element(
    request: Request,
    label: str | None = Query(default=None),
    label_contains: str | None = Query(default=None),
    label_prefix: str | None = Query(default=None),
    identifier: str | None = Query(default=None),
    element_type: str | None = Query(default=None, alias="type"),
    udid: str | None = Query(default=None),
):
    """Get a single element's state without fetching the entire UI tree.

    Query params:
    - label: Element label (case-insensitive exact match)
    - label_contains: Substring match on label (case-insensitive)
    - label_prefix: Prefix match on label (case-insensitive)
    - identifier: Element identifier (case-sensitive)
    - type: Element type to narrow results (optional)
    - udid: Device UDID (auto-resolves if omitted)

    Only one of label, label_contains, or label_prefix may be provided.

    Returns:
    - 200 with element dict (includes match_count if ambiguous)
    - 404 if no element found
    """
    label_params = [p for p in (label, label_contains, label_prefix) if p is not None]
    if len(label_params) > 1:
        raise HTTPException(
            status_code=400,
            detail="Only one of label, label_contains, or label_prefix may be provided",
        )

    controller = _get_controller(request)
    try:
        element, resolved_udid = await controller.get_element(
            label=label,
            label_contains=label_contains,
            label_prefix=label_prefix,
            identifier=identifier,
            element_type=element_type,
            udid=udid,
        )
        return {"element": element, "udid": resolved_udid}
    except DeviceError as e:
        raise _handle_device_error(e)


@router.post("/ui/wait-for-element")
async def wait_for_element(request: Request, body: WaitForElementRequest):
    """Wait for an element to satisfy a condition (server-side polling).

    Always returns 200 with matched field to distinguish success/timeout.
    Only non-200 responses are validation errors (400) or server errors (500/503).

    Request body:
    - label or identifier: Element search criteria (at least one required)
    - type: Optional element type to narrow results
    - condition: Condition to wait for (exists, enabled, value_equals, etc.)
    - value: Required for value_* conditions
    - timeout: Max wait time in seconds (default 10, max 60)
    - interval: Poll interval in seconds (default 0.5)
    - udid: Device UDID (auto-resolves if omitted)

    Response:
    - matched: bool - whether condition was satisfied
    - element: dict | None - element state if matched
    - last_state: dict | None - last seen state if timeout
    - elapsed_seconds: float - time spent polling
    - polls: int - number of polls performed
    """
    start = time.perf_counter()
    logger.info(
        f"[PERF] API /ui/wait-for-element START: "
        f"condition={body.condition}, timeout={body.timeout}s"
    )

    controller = _get_controller(request)

    # Validation
    if body.timeout > 60:
        raise HTTPException(status_code=400, detail="Timeout cannot exceed 60 seconds")

    if body.condition in ("value_equals", "value_contains") and body.value is None:
        raise HTTPException(
            status_code=400,
            detail=f"Condition '{body.condition}' requires a value parameter",
        )

    try:
        result, resolved_udid = await controller.wait_for_element(
            condition=body.condition,
            label=body.label,
            label_contains=body.label_contains,
            label_prefix=body.label_prefix,
            identifier=body.identifier,
            element_type=body.element_type,
            value=body.value,
            timeout=body.timeout,
            interval=body.interval,
            udid=body.udid,
            mode=body.mode,
        )
        result["udid"] = resolved_udid

        end = time.perf_counter()
        logger.info(
            f"[PERF] API /ui/wait-for-element SUCCESS: "
            f"{(end-start)*1000:.1f}ms, matched={result.get('matched')}"
        )
        return result
    except DeviceError as e:
        end = time.perf_counter()
        logger.error(
            f"[PERF] API /ui/wait-for-element ERROR: "
            f"{(end-start)*1000:.1f}ms, error={e}"
        )
        raise _handle_device_error(e)


@router.get("/screen-summary")
async def get_screen_summary(
    request: Request,
    max_elements: int = Query(default=20, ge=0, le=500),
    udid: str | None = Query(default=None),
    snapshot_depth: int | None = Query(
        default=None, ge=1, le=50,
        description=(
            "WDA accessibility tree depth (1-50, default 10). "
            "Only affects physical devices."
        ),
    ),
    strategy: str | None = Query(
        default=None,
        description=(
            "Use 'skeleton' to skip /source timeout on "
            "complex screens. Physical devices only."
        ),
    ),
    source_timeout: float | None = Query(
        default=None, ge=1, le=60,
        description=(
            "Override WDA /source timeout in seconds. "
            "Use for slow screens on older devices. Physical devices only."
        ),
    ),
    mode: str | None = Query(
        default=None, pattern=r"^(flat)$",
        description="'flat' uses flat idb output with custom companion. Default uses nested.",
    ),
    identify: bool = Query(
        default=False,
        description="Identify screen against loaded landmarks. Adds identified_as/confidence.",
    ),
):
    """Get an LLM-optimized screen description with smart truncation.

    Query params:
    - max_elements: Maximum interactive elements to include (0 = unlimited, default 20)
    - udid: Device UDID (auto-resolves if omitted)
    - snapshot_depth: WDA accessibility tree depth (1-50, default 10).
      Only affects physical devices.
    - strategy: 'skeleton' to skip /source timeout on complex screens (physical devices only)
    - source_timeout: Override WDA /source timeout in seconds (1-60). Physical devices only.
    - mode: 'flat' to use flat idb output with custom companion. Default uses nested.
    - identify: Match screen against loaded landmarks.

    Returns summary with truncated, total_interactive_elements fields.
    """
    controller = _get_controller(request)
    try:
        summary, elements, resolved_udid = await controller.get_screen_summary(
            max_elements=max_elements,
            udid=udid,
            snapshot_depth=snapshot_depth,
            strategy=strategy,
            source_timeout=source_timeout,
            mode=mode,
        )
        summary["udid"] = resolved_udid

        if identify:
            registry = request.app.state.landmark_registry
            result = registry.identify(elements)
            summary["identified_as"] = result["matched"]
            summary["confidence"] = result["confidence"]

        return summary
    except DeviceError as e:
        raise _handle_device_error(e)


@router.post("/ui/tap")
async def tap(request: Request, body: TapRequest):
    """Tap at specific coordinates."""
    start = time.perf_counter()
    logger.info(f"[PERF] API /ui/tap START: ({body.x}, {body.y})")

    controller = _get_controller(request)
    try:
        udid = await controller.tap(x=body.x, y=body.y, udid=body.udid)

        end = time.perf_counter()
        logger.info(f"[PERF] API /ui/tap SUCCESS: {(end-start)*1000:.1f}ms")
        return {"status": "ok", "udid": udid, "x": body.x, "y": body.y}
    except DeviceError as e:
        end = time.perf_counter()
        logger.error(f"[PERF] API /ui/tap ERROR: {(end-start)*1000:.1f}ms, error={e}")
        raise _handle_device_error(e)


@router.post("/ui/tap-element")
async def tap_element(request: Request, body: TapElementRequest):
    """Find an element by label/identifier and tap its center.

    Returns:
    - 200 with status "ok" and tapped element info for single match
    - 200 with status "ambiguous" and match list for multiple matches
    - 404 when no element matches
    """
    start = time.perf_counter()
    logger.info(f"[PERF] API /ui/tap-element START: label={body.label}, id={body.identifier}")

    controller = _get_controller(request)
    try:
        if body.capture_screenshots:
            resolved = await controller.resolve_udid(body.udid)
            before = await _capture_action_screenshot(controller, resolved, "tap_before")

        result = await controller.tap_element(
            label=body.label,
            label_contains=body.label_contains,
            label_prefix=body.label_prefix,
            identifier=body.identifier,
            element_type=body.element_type,
            udid=body.udid,
            skip_stability_check=body.skip_stability_check,
            source_timeout=body.source_timeout,
            value=body.value,
            scroll_to_find=body.scroll_to_find,
        )

        end = time.perf_counter()

        # Element not found — return 404 with screen context
        if result.get("status") == "not_found":
            logger.info(f"[PERF] API /ui/tap-element NOT_FOUND: {(end-start)*1000:.1f}ms")
            raise HTTPException(status_code=404, detail=result)

        if body.capture_screenshots:
            await asyncio.sleep(body.settle_delay)
            after = await _capture_action_screenshot(controller, body.udid, "tap_after")
            result["screenshots"] = {"before": before, "after": after}

        if body.include_screen_context and result.get("status") not in ("not_found", "ambiguous"):
            result["screen_context"] = await _capture_screen_context(controller, body.udid)

        logger.info(f"[PERF] API /ui/tap-element SUCCESS: {(end-start)*1000:.1f}ms")
        return result
    except DeviceError as e:
        end = time.perf_counter()
        logger.error(f"[PERF] API /ui/tap-element ERROR: {(end-start)*1000:.1f}ms, error={e}")
        raise _handle_device_error(e)


@router.post("/ui/swipe")
async def swipe(request: Request, body: SwipeRequest):
    """Perform a swipe gesture."""
    controller = _get_controller(request)
    try:
        udid = await controller.swipe(
            start_x=body.start_x,
            start_y=body.start_y,
            end_x=body.end_x,
            end_y=body.end_y,
            duration=body.duration,
            udid=body.udid,
        )
        return {"status": "ok", "udid": udid}
    except DeviceError as e:
        raise _handle_device_error(e)


@router.post("/ui/scroll-to-element")
async def scroll_to_element(request: Request, body: ScrollToElementRequest):
    """Scroll a scrollable container until the target element is in view.

    Android-only for now (native uiautomator2 scrollIntoView). Returns 404 when
    the element never appears after scrolling; 200 with status "not_supported"
    on non-Android devices.
    """
    controller = _get_controller(request)
    try:
        result = await controller.scroll_to_element(
            label=body.label,
            identifier=body.identifier,
            udid=body.udid,
            max_swipes=body.max_swipes,
        )
        if result.get("status") == "not_found":
            raise HTTPException(status_code=404, detail=result)
        return result
    except DeviceError as e:
        raise _handle_device_error(e)


@router.post("/ui/type")
async def type_text(request: Request, body: TypeTextRequest):
    """Type text into the focused field."""
    controller = _get_controller(request)
    try:
        if body.capture_screenshots:
            resolved = await controller.resolve_udid(body.udid)
            before = await _capture_action_screenshot(controller, resolved, "type_before")
        udid = await controller.type_text(text=body.text, udid=body.udid)
        result: dict = {"status": "ok", "udid": udid}
        if body.capture_screenshots:
            await asyncio.sleep(body.settle_delay)
            after = await _capture_action_screenshot(controller, udid, "type_after")
            result["screenshots"] = {"before": before, "after": after}
        if body.include_screen_context:
            result["screen_context"] = await _capture_screen_context(controller, udid)
        return result
    except DeviceError as e:
        raise _handle_device_error(e)


@router.post("/ui/clear")
async def clear_text(request: Request, body: ClearTextRequest):
    """Clear text in the currently focused text field (select-all + delete)."""
    controller = _get_controller(request)
    try:
        resolved = await controller.clear_text(udid=body.udid)
        return {"status": "ok", "udid": resolved}
    except DeviceError as e:
        raise _handle_device_error(e)


@router.post("/ui/press")
async def press_button(request: Request, body: PressButtonRequest):
    """Press a hardware button."""
    controller = _get_controller(request)
    try:
        udid = await controller.press_button(button=body.button, udid=body.udid)
        return {"status": "ok", "udid": udid}
    except DeviceError as e:
        raise _handle_device_error(e)
