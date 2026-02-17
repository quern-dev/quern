"""API routes for device management and screenshots."""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from server.models import (
    BootDeviceRequest,
    ClearTextRequest,
    DeviceError,
    GrantPermissionRequest,
    InstallAppRequest,
    LaunchAppRequest,
    PressButtonRequest,
    SetLocationRequest,
    ShutdownDeviceRequest,
    StartSimLogRequest,
    StopSimLogRequest,
    SwipeRequest,
    TapElementRequest,
    TapRequest,
    TerminateAppRequest,
    TypeTextRequest,
    WaitForElementRequest,
)

router = APIRouter(prefix="/api/v1/device", tags=["device"])
logger = logging.getLogger("quern-debug-server.api")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_controller(request: Request):
    """Get the DeviceController from app state."""
    controller = request.app.state.device_controller
    if controller is None:
        raise HTTPException(status_code=503, detail="Device controller not initialized")
    return controller


def _handle_device_error(e: DeviceError) -> HTTPException:
    """Map a DeviceError to an appropriate HTTPException."""
    msg = str(e)
    if "No booted simulator" in msg or "Multiple simulators booted" in msg:
        return HTTPException(status_code=400, detail=msg)
    if "not found" in msg.lower() and e.tool == "idb" and "element" not in msg.lower():
        return HTTPException(status_code=503, detail=msg)
    if "No element found" in msg:
        return HTTPException(status_code=404, detail=msg)
    if "not available" in msg.lower():
        return HTTPException(status_code=503, detail=msg)
    return HTTPException(status_code=500, detail=f"[{e.tool}] {msg}")


# ---------------------------------------------------------------------------
# Device management
# ---------------------------------------------------------------------------


@router.get("/list")
async def list_devices(request: Request):
    """List all simulators and tool availability."""
    controller = _get_controller(request)
    try:
        devices = await controller.list_devices()
        tools = await controller.check_tools()
        return {
            "devices": [d.model_dump() for d in devices],
            "tools": tools,
            "active_udid": controller._active_udid,
        }
    except DeviceError as e:
        raise _handle_device_error(e)


@router.post("/boot")
async def boot_device(request: Request, body: BootDeviceRequest):
    """Boot a simulator by udid or name."""
    controller = _get_controller(request)
    try:
        udid = await controller.boot(udid=body.udid, name=body.name)
        return {"status": "booted", "udid": udid}
    except DeviceError as e:
        raise _handle_device_error(e)


@router.post("/shutdown")
async def shutdown_device(request: Request, body: ShutdownDeviceRequest):
    """Shutdown a simulator."""
    controller = _get_controller(request)
    try:
        await controller.shutdown(udid=body.udid)
        return {"status": "shutdown", "udid": body.udid}
    except DeviceError as e:
        raise _handle_device_error(e)


# ---------------------------------------------------------------------------
# App management
# ---------------------------------------------------------------------------


@router.post("/app/install")
async def install_app(request: Request, body: InstallAppRequest):
    """Install an app on a simulator."""
    controller = _get_controller(request)
    try:
        udid = await controller.install_app(app_path=body.app_path, udid=body.udid)
        return {"status": "installed", "udid": udid, "app_path": body.app_path}
    except DeviceError as e:
        raise _handle_device_error(e)


@router.post("/app/launch")
async def launch_app(request: Request, body: LaunchAppRequest):
    """Launch an app on a simulator."""
    controller = _get_controller(request)
    try:
        udid = await controller.launch_app(bundle_id=body.bundle_id, udid=body.udid)
        return {"status": "launched", "udid": udid, "bundle_id": body.bundle_id}
    except DeviceError as e:
        raise _handle_device_error(e)


@router.post("/app/terminate")
async def terminate_app(request: Request, body: TerminateAppRequest):
    """Terminate an app on a simulator."""
    controller = _get_controller(request)
    try:
        udid = await controller.terminate_app(bundle_id=body.bundle_id, udid=body.udid)
        return {"status": "terminated", "udid": udid, "bundle_id": body.bundle_id}
    except DeviceError as e:
        raise _handle_device_error(e)


@router.get("/app/list")
async def list_apps(request: Request, udid: str | None = Query(default=None)):
    """List installed apps on a simulator."""
    controller = _get_controller(request)
    try:
        apps, resolved_udid = await controller.list_apps(udid=udid)
        return {
            "apps": [a.model_dump() for a in apps],
            "udid": resolved_udid,
        }
    except DeviceError as e:
        raise _handle_device_error(e)


# ---------------------------------------------------------------------------
# Screenshots
# ---------------------------------------------------------------------------


@router.get("/screenshot")
async def take_screenshot(
    request: Request,
    udid: str | None = Query(default=None),
    format: str = Query(default="png", pattern="^(png|jpeg)$"),
    scale: float = Query(default=0.5, ge=0.1, le=1.0),
    quality: int = Query(default=85, ge=1, le=100),
):
    """Capture a screenshot from a simulator."""
    controller = _get_controller(request)
    try:
        image_bytes, media_type = await controller.screenshot(
            udid=udid, format=format, scale=scale, quality=quality,
        )
        return Response(content=image_bytes, media_type=media_type)
    except DeviceError as e:
        raise _handle_device_error(e)


# ---------------------------------------------------------------------------
# UI inspection & interaction (Phase 3b)
# ---------------------------------------------------------------------------


@router.get("/ui")
async def get_ui_elements(
    request: Request,
    udid: str | None = Query(default=None),
    children_of: str | None = Query(default=None, description="Only return children of the element with this identifier or label"),
):
    """Get all UI accessibility elements from the current screen.

    Optionally scope to children of a specific element using the `children_of` parameter.
    """
    start = time.perf_counter()
    logger.info(f"[PERF] API /ui START (children_of={children_of})")

    controller = _get_controller(request)
    try:
        if children_of:
            elements, resolved_udid = await controller.get_ui_elements_children_of(
                children_of=children_of, udid=udid,
            )
        else:
            elements, resolved_udid = await controller.get_ui_elements(udid=udid)

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
):
    """Get an LLM-optimized screen description with smart truncation.

    Query params:
    - max_elements: Maximum interactive elements to include (0 = unlimited, default 20)
    - udid: Device UDID (auto-resolves if omitted)

    Returns summary with truncated, total_interactive_elements fields.
    """
    controller = _get_controller(request)
    try:
        summary, resolved_udid = await controller.get_screen_summary(
            max_elements=max_elements,
            udid=udid,
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


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@router.post("/location")
async def set_location(request: Request, body: SetLocationRequest):
    """Set the simulated GPS location."""
    controller = _get_controller(request)
    try:
        udid = await controller.set_location(
            latitude=body.latitude, longitude=body.longitude, udid=body.udid,
        )
        return {
            "status": "ok",
            "udid": udid,
            "latitude": body.latitude,
            "longitude": body.longitude,
        }
    except DeviceError as e:
        raise _handle_device_error(e)


@router.post("/permission")
async def grant_permission(request: Request, body: GrantPermissionRequest):
    """Grant an app permission."""
    controller = _get_controller(request)
    try:
        udid = await controller.grant_permission(
            bundle_id=body.bundle_id, permission=body.permission, udid=body.udid,
        )
        return {
            "status": "ok",
            "udid": udid,
            "bundle_id": body.bundle_id,
            "permission": body.permission,
        }
    except DeviceError as e:
        raise _handle_device_error(e)


# ---------------------------------------------------------------------------
# Annotated screenshots
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Simulator logging
# ---------------------------------------------------------------------------


@router.post("/logging/start")
async def start_simulator_logging(request: Request, body: StartSimLogRequest):
    """Start capturing logs from a simulator app via unified logging."""
    from server.sources.simulator_log import SimulatorLogAdapter

    controller = _get_controller(request)

    # Resolve UDID
    try:
        udid = await controller.resolve_udid(body.udid)
    except DeviceError as e:
        raise _handle_device_error(e)

    # Check if already running for this UDID
    sim_adapters: dict = request.app.state.sim_log_adapters
    if udid in sim_adapters and sim_adapters[udid].is_running:
        return {"status": "already_running", "udid": udid, "adapter_id": sim_adapters[udid].adapter_id}

    # Get the deduplicator as the entry callback (same pipeline as other adapters)
    dedup = request.app.state.deduplicator

    adapter = SimulatorLogAdapter(
        udid=udid,
        on_entry=dedup.process,
        process_filter=body.process,
        subsystem_filter=body.subsystem,
        level=body.level,
    )

    await adapter.start()

    if adapter._error:
        raise HTTPException(status_code=500, detail=adapter._error)

    # Register in both dicts so it appears in list_log_sources
    sim_adapters[udid] = adapter
    request.app.state.source_adapters[adapter.adapter_id] = adapter

    return {"status": "started", "udid": udid, "adapter_id": adapter.adapter_id}


@router.post("/logging/stop")
async def stop_simulator_logging(request: Request, body: StopSimLogRequest):
    """Stop capturing logs from a simulator."""
    controller = _get_controller(request)

    # Resolve UDID
    try:
        udid = await controller.resolve_udid(body.udid)
    except DeviceError as e:
        raise _handle_device_error(e)

    sim_adapters: dict = request.app.state.sim_log_adapters
    adapter = sim_adapters.get(udid)
    if not adapter:
        raise HTTPException(status_code=404, detail=f"No simulator logging active for UDID {udid}")

    await adapter.stop()

    # Remove from both dicts
    del sim_adapters[udid]
    request.app.state.source_adapters.pop(adapter.adapter_id, None)

    return {"status": "stopped", "udid": udid}


@router.get("/screenshot/annotated")
async def screenshot_annotated(
    request: Request,
    udid: str | None = Query(default=None),
    scale: float = Query(default=0.5, ge=0.1, le=1.0),
    quality: int = Query(default=85, ge=1, le=100),
):
    """Capture an annotated screenshot with accessibility overlays."""
    controller = _get_controller(request)
    try:
        image_bytes, media_type = await controller.screenshot_annotated(
            udid=udid, scale=scale, quality=quality,
        )
        return Response(content=image_bytes, media_type=media_type)
    except DeviceError as e:
        raise _handle_device_error(e)
