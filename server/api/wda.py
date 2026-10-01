"""API routes for WebDriverAgent setup on physical devices."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from server.api.actions import logged_action
from server.models import (
    DeviceError,
    DeviceType,
    SetupWdaRequest,
    StartDriverRequest,
    StopDriverRequest,
)

router = APIRouter(prefix="/api/v1/device/wda", tags=["wda"])
logger = logging.getLogger(__name__)


def _get_controller(request: Request):
    """Get the DeviceController from app state."""
    controller = request.app.state.device_controller
    if controller is None:
        raise HTTPException(status_code=503, detail="Device controller not initialized")
    return controller


@router.post("/setup")
@logged_action("setup_wda", category="device.lifecycle")
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

    if device.device_type == DeviceType.SIMULATOR:
        # No team, no provisioning, no install: just the simulator build (#336).
        from server.device.wda import build_wda_simulator

        try:
            built = await build_wda_simulator(force=body.force)
        except (RuntimeError, OSError) as e:
            raise HTTPException(status_code=500, detail=str(e))
        return {
            "status": "ok", "udid": body.udid, "simulator": True, "built": built,
            "message": "WDA is built for the iOS Simulator. start_driver on this "
                       "simulator to serve its UI through WDA -- XCUITest's view.",
        }

    if device.device_type != DeviceType.DEVICE:
        raise HTTPException(
            status_code=400,
            detail=f"Device {body.udid} is not an iOS device or simulator.",
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
            force=body.force,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    return result


async def _validate_ios_device(controller, udid: str):
    """Validate that a UDID refers to an iOS device or simulator. Returns the
    DeviceInfo. Simulators were refused here until #336."""
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

    if device.device_type not in (DeviceType.DEVICE, DeviceType.SIMULATOR):
        raise HTTPException(
            status_code=400,
            detail=f"Device {udid} is not an iOS device or simulator.",
        )

    return device


@router.post("/start")
@logged_action("start_wda_driver", category="device.lifecycle")
async def start_wda_driver(request: Request, body: StartDriverRequest):
    """Start WDA (xcodebuild test-without-building) on a device or simulator.

    On a simulator this is the opt-in to WDA (#336): once it answers, every UI
    read and action on that simulator goes through WDA -- XCUITest's view --
    until stop_driver. Registered only when it is ready, so a runner that never
    comes up leaves the simulator on sim-bridge rather than half-switched.
    """
    controller = _get_controller(request)
    device = await _validate_ios_device(controller, body.udid)

    if device.device_type == DeviceType.SIMULATOR:
        from server.device.wda import start_driver_simulator

        try:
            result = await start_driver_simulator(body.udid)
        except (RuntimeError, OSError) as e:
            raise HTTPException(status_code=500, detail=str(e))
        if result.get("ready"):
            controller.wda_client.register_simulator(body.udid, result["port"])
            controller._backend_switched(body.udid)
            result["backend"] = "wda"
            result["message"] = (
                "This simulator's UI reads and actions now go through WDA, so "
                "element types are XCUITest's (xcui_type on each element). "
                "stop_driver returns it to sim-bridge."
            )
        return result

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
@logged_action("stop_wda_driver", category="device.lifecycle")
async def stop_wda_driver(request: Request, body: StopDriverRequest):
    """Stop WDA on a device or simulator. A simulator returns to sim-bridge."""
    controller = _get_controller(request)
    device = await _validate_ios_device(controller, body.udid)

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
    finally:
        # Unregistered even if stopping raised: routing a simulator to a WDA
        # that may be gone is the worse failure, and sim-bridge heals the
        # bridge WDA poisoned on its next read (#337, #343).
        if device.device_type == DeviceType.SIMULATOR:
            controller.wda_client.unregister_simulator(body.udid)
            controller._backend_switched(body.udid)

    if device.device_type == DeviceType.SIMULATOR:
        # What the router now picks, not a literal: it is idb where sim-bridge
        # is unavailable, and a hardcoded name is the drift #186 removed.
        result["backend"] = controller._backend_name(body.udid)
    return result
