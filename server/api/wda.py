"""API routes for WebDriverAgent setup on physical devices."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from server.models import DeviceError, DeviceType, SetupWdaRequest, StartDriverRequest, StopDriverRequest

router = APIRouter(prefix="/api/v1/device/wda", tags=["wda"])
logger = logging.getLogger("quern-debug-server.api")


def _get_controller(request: Request):
    """Get the DeviceController from app state."""
    controller = request.app.state.device_controller
    if controller is None:
        raise HTTPException(status_code=503, detail="Device controller not initialized")
    return controller


@router.post("/setup")
async def setup_wda(request: Request, body: SetupWdaRequest):
    """Set up WebDriverAgent on a physical device.

    Discovers signing identities, clones/builds/installs WDA.
    If multiple signing identities exist and no team_id is provided,
    returns the list for the user to choose from.
    """
    controller = _get_controller(request)

    # Find the device and validate it's physical
    try:
        devices = await controller.list_devices()
    except DeviceError as e:
        raise HTTPException(status_code=500, detail=str(e))

    device = None
    for d in devices:
        if d.udid == body.udid:
            device = d
            break

    if device is None:
        raise HTTPException(status_code=404, detail=f"Device {body.udid} not found")

    if device.device_type != DeviceType.DEVICE:
        raise HTTPException(
            status_code=400,
            detail=f"Device {body.udid} is a simulator. WDA setup is only for physical devices.",
        )

    if not device.os_version:
        raise HTTPException(
            status_code=400,
            detail=f"Device {body.udid} has no OS version info. Is it connected?",
        )

    from server.device.wda import setup_wda as _setup_wda

    try:
        result = await _setup_wda(
            udid=body.udid,
            os_version=device.os_version,
            team_id=body.team_id,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    return result


async def _validate_physical_device(controller, udid: str):
    """Validate that a UDID refers to a physical device. Returns the DeviceInfo."""
    try:
        devices = await controller.list_devices()
    except DeviceError as e:
        raise HTTPException(status_code=500, detail=str(e))

    device = None
    for d in devices:
        if d.udid == udid:
            device = d
            break

    if device is None:
        raise HTTPException(status_code=404, detail=f"Device {udid} not found")

    if device.device_type != DeviceType.DEVICE:
        raise HTTPException(
            status_code=400,
            detail=f"Device {udid} is a simulator. This endpoint is only for physical devices.",
        )

    return device


@router.post("/start")
async def start_wda_driver(request: Request, body: StartDriverRequest):
    """Start WDA driver (xcodebuild test-without-building) on a physical device."""
    controller = _get_controller(request)
    device = await _validate_physical_device(controller, body.udid)

    if not device.os_version:
        raise HTTPException(
            status_code=400,
            detail=f"Device {body.udid} has no OS version info. Is it connected?",
        )

    from server.device.wda import start_driver

    try:
        result = await start_driver(udid=body.udid, os_version=device.os_version)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    return result


@router.post("/stop")
async def stop_wda_driver(request: Request, body: StopDriverRequest):
    """Stop WDA driver on a physical device."""
    controller = _get_controller(request)
    await _validate_physical_device(controller, body.udid)

    # Delete session first
    try:
        await controller.wda_client.delete_session(body.udid)
    except Exception:
        pass

    from server.device.wda import stop_driver

    try:
        result = await stop_driver(udid=body.udid)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    return result
