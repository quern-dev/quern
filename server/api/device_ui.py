"""API routes for device UI automation (idb-dependent)."""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, HTTPException, Query, Request

from server.models import (
    ClearTextRequest,
    DeviceError,
    PressButtonRequest,
    SwipeRequest,
    TapElementRequest,
    TapRequest,
    TypeTextRequest,
    WaitForElementRequest,
)

from server.api.device import _get_controller, _handle_device_error

router = APIRouter(prefix="/api/v1/device", tags=["device"])
logger = logging.getLogger("quern-debug-server.api")


# ---------------------------------------------------------------------------
# UI inspection & interaction
# ---------------------------------------------------------------------------


@router.get("/ui")
async def get_ui_elements(
    request: Request,
    udid: str | None = Query(default=None),
    children_of: str | None = Query(default=None, description="Only return children of the element with this identifier or label"),
    snapshot_depth: int | None = Query(default=None, ge=1, le=50, description="WDA accessibility tree depth (1-50, default 10). Only affects physical devices."),
    strategy: str | None = Query(default=None, description="Use 'skeleton' to skip /source timeout on complex screens. Physical devices only."),
):
    """Get all UI accessibility elements from the current screen.

    Optionally scope to children of a specific element using the `children_of` parameter.
    """
    start = time.perf_counter()
    logger.info(f"[PERF] API /ui START (children_of={children_of})")

    controller = _get_controller(request)
    try:
        if strategy == "skeleton":
            resolved_udid = await controller.resolve_udid(udid)
            if controller._is_physical(resolved_udid):
                raw = await controller.wda_client.build_screen_skeleton(resolved_udid)
                from server.device.ui_elements import parse_elements
                elements = parse_elements(raw)
            else:
                elements, resolved_udid = await controller.get_ui_elements(udid=udid, snapshot_depth=snapshot_depth)
        elif children_of:
            elements, resolved_udid = await controller.get_ui_elements_children_of(
                children_of=children_of, udid=udid, snapshot_depth=snapshot_depth,
            )
        else:
            elements, resolved_udid = await controller.get_ui_elements(udid=udid, snapshot_depth=snapshot_depth)

        end = time.perf_counter()
        logger.info(f"[PERF] API /ui SUCCESS: {(end-start)*1000:.1f}ms, elements={len(elements)}")
        return {
            "elements": [e.model_dump() for e in elements],
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
    identifier: str | None = Query(default=None),
    element_type: str | None = Query(default=None, alias="type"),
    udid: str | None = Query(default=None),
):
    """Get a single element's state without fetching the entire UI tree.

    Query params:
    - label: Element label (case-insensitive)
    - identifier: Element identifier (case-sensitive)
    - type: Element type to narrow results (optional)
    - udid: Device UDID (auto-resolves if omitted)

    Returns:
    - 200 with element dict (includes match_count if ambiguous)
    - 404 if no element found
    """
    controller = _get_controller(request)
    try:
        element, resolved_udid = await controller.get_element(
            label=label,
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
    logger.info(f"[PERF] API /ui/wait-for-element START: condition={body.condition}, timeout={body.timeout}s")

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
            identifier=body.identifier,
            element_type=body.element_type,
            value=body.value,
            timeout=body.timeout,
            interval=body.interval,
            udid=body.udid,
        )
        result["udid"] = resolved_udid

        end = time.perf_counter()
        logger.info(f"[PERF] API /ui/wait-for-element SUCCESS: {(end-start)*1000:.1f}ms, matched={result.get('matched')}")
        return result
    except DeviceError as e:
        end = time.perf_counter()
        logger.error(f"[PERF] API /ui/wait-for-element ERROR: {(end-start)*1000:.1f}ms, error={e}")
        raise _handle_device_error(e)


@router.get("/screen-summary")
async def get_screen_summary(
    request: Request,
    max_elements: int = Query(default=20, ge=0, le=500),
    udid: str | None = Query(default=None),
    snapshot_depth: int | None = Query(default=None, ge=1, le=50, description="WDA accessibility tree depth (1-50, default 10). Only affects physical devices."),
    strategy: str | None = Query(default=None, description="Use 'skeleton' to skip /source timeout on complex screens. Physical devices only."),
):
    """Get an LLM-optimized screen description with smart truncation.

    Query params:
    - max_elements: Maximum interactive elements to include (0 = unlimited, default 20)
    - udid: Device UDID (auto-resolves if omitted)
    - snapshot_depth: WDA accessibility tree depth (1-50, default 10). Only affects physical devices.
    - strategy: 'skeleton' to skip /source timeout on complex screens (physical devices only)

    Returns summary with truncated, total_interactive_elements fields.
    """
    controller = _get_controller(request)
    try:
        summary, resolved_udid = await controller.get_screen_summary(
            max_elements=max_elements,
            udid=udid,
            snapshot_depth=snapshot_depth,
            strategy=strategy,
        )
        summary["udid"] = resolved_udid
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
        result = await controller.tap_element(
            label=body.label,
            identifier=body.identifier,
            element_type=body.element_type,
            udid=body.udid,
            skip_stability_check=body.skip_stability_check,
        )

        end = time.perf_counter()
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


@router.post("/ui/type")
async def type_text(request: Request, body: TypeTextRequest):
    """Type text into the focused field."""
    controller = _get_controller(request)
    try:
        udid = await controller.type_text(text=body.text, udid=body.udid)
        return {"status": "ok", "udid": udid}
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
