"""API routes for device pool management."""

import time

from fastapi import APIRouter, HTTPException, Request
from server.models import DeviceError, DeviceType, EnsureDevicesRequest, ResolveDeviceRequest

router = APIRouter(prefix="/api/v1/devices", tags=["device-pool"])


def _get_pool(request: Request):
    """Get the DevicePool from app state."""
    pool = request.app.state.device_pool
    if pool is None:
        raise HTTPException(status_code=503, detail="Device pool not initialized")
    return pool


def _handle_pool_error(e: DeviceError) -> HTTPException:
    """Map a DeviceError to an appropriate HTTPException."""
    msg = str(e)
    if "not found" in msg.lower() or "no device matching" in msg.lower():
        return HTTPException(status_code=404, detail=msg)
    if "did not boot" in msg.lower():
        return HTTPException(status_code=503, detail=msg)
    return HTTPException(status_code=500, detail=f"[{e.tool}] {msg}")


def _parse_device_type(value: str | None) -> DeviceType | None:
    """Convert a string device_type to DeviceType enum, or None."""
    if value is None:
        return None
    try:
        return DeviceType(value)
    except ValueError:
        return None


# Routes
@router.post("/refresh")
async def refresh_pool(request: Request):
    """Refresh pool state from simctl (discover new devices)."""
    pool = _get_pool(request)
    try:
        await pool.refresh_from_simctl()
        devices = await pool.list_devices()
        return {
            "status": "refreshed",
            "device_count": len(devices),
        }
    except DeviceError as e:
        raise _handle_pool_error(e)


@router.post("/resolve")
async def resolve_device(request: Request, body: ResolveDeviceRequest):
    """Smart device resolution with criteria matching and auto-boot.

    Returns 200 with device info on success. The resolved device becomes
    the active device for subsequent tool calls.
    Returns 404 if no device matching criteria found.
    Returns 503 if boot failed.
    """
    pool = _get_pool(request)
    start = time.time()
    try:
        udid = await pool.resolve_device(
            udid=body.udid,
            name=body.name,
            os_version=body.os_version,
            device_type=_parse_device_type(body.device_type),
            device_family=body.device_family,
            auto_boot=body.auto_boot,
        )
        waited = round(time.time() - start, 2)
        device = await pool.get_device_state(udid)
        return {
            "udid": udid,
            "name": device.name if device else "",
            "state": device.state.value if device else "unknown",
            "os_version": device.os_version if device else "",
            "waited_seconds": waited,
            "active": True,
        }
    except DeviceError as e:
        raise _handle_pool_error(e)


@router.post("/ensure")
async def ensure_devices(request: Request, body: EnsureDevicesRequest):
    """Ensure N devices matching criteria are booted and available.

    The first device becomes the active device for subsequent tool calls.
    Returns 200 with device list on success.
    Returns 404 if not enough matching devices exist.
    Returns 503 if boot failed for one or more devices.
    """
    pool = _get_pool(request)
    try:
        udids = await pool.ensure_devices(
            count=body.count,
            name=body.name,
            os_version=body.os_version,
            device_type=_parse_device_type(body.device_type),
            device_family=body.device_family,
            auto_boot=body.auto_boot,
        )
        devices = []
        for udid in udids:
            device = await pool.get_device_state(udid)
            devices.append({
                "udid": udid,
                "name": device.name if device else "",
                "state": device.state.value if device else "unknown",
            })
        return {
            "devices": devices,
            "total_available": len(udids),
            "active_udid": udids[0] if udids else None,
        }
    except DeviceError as e:
        raise _handle_pool_error(e)
