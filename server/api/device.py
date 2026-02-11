"""API routes for device management and screenshots."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from server.models import (
    BootDeviceRequest,
    DeviceError,
    InstallAppRequest,
    LaunchAppRequest,
    ShutdownDeviceRequest,
    TapElementRequest,
    TerminateAppRequest,
)

router = APIRouter(prefix="/api/v1/device", tags=["device"])


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
):
    """Get all UI accessibility elements from the current screen."""
    controller = _get_controller(request)
    try:
        elements, resolved_udid = await controller.get_ui_elements(udid=udid)
        return {
            "elements": [e.model_dump() for e in elements],
            "element_count": len(elements),
            "udid": resolved_udid,
        }
    except DeviceError as e:
        raise _handle_device_error(e)


@router.get("/screen-summary")
async def get_screen_summary(
    request: Request,
    udid: str | None = Query(default=None),
):
    """Get an LLM-optimized screen description."""
    controller = _get_controller(request)
    try:
        summary, resolved_udid = await controller.get_screen_summary(udid=udid)
        summary["udid"] = resolved_udid
        return summary
    except DeviceError as e:
        raise _handle_device_error(e)


@router.post("/ui/tap-element")
async def tap_element(request: Request, body: TapElementRequest):
    """Find an element by label/identifier and tap its center.

    Returns:
    - 200 with status "ok" and tapped element info for single match
    - 200 with status "ambiguous" and match list for multiple matches
    - 404 when no element matches
    """
    controller = _get_controller(request)
    try:
        result = await controller.tap_element(
            label=body.label,
            identifier=body.identifier,
            element_type=body.element_type,
            udid=body.udid,
        )
        return result
    except DeviceError as e:
        raise _handle_device_error(e)
