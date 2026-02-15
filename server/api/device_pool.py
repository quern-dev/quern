"""API routes for device pool management."""

import time

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from server.models import DeviceError, EnsureDevicesRequest, ResolveDeviceRequest

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
    if "already claimed" in msg.lower() or "are claimed" in msg.lower():
        return HTTPException(status_code=409, detail=msg)
    if "timed out" in msg.lower():
        return HTTPException(status_code=408, detail=msg)
    if "did not boot" in msg.lower():
        return HTTPException(status_code=503, detail=msg)
    return HTTPException(status_code=500, detail=f"[{e.tool}] {msg}")


# Request models
class ClaimDeviceRequest(BaseModel):
    session_id: str
    udid: str | None = None
    name: str | None = None
    os_version: str | None = None


class ReleaseDeviceRequest(BaseModel):
    udid: str
    session_id: str | None = None


# Routes
@router.get("/pool")
async def list_device_pool(
    request: Request,
    state: str | None = Query(default=None, pattern="^(booted|shutdown)$"),
    claimed: str | None = Query(default=None, pattern="^(claimed|available)$"),
):
    """List all devices in the pool with optional filters.

    Query params:
    - state: Filter by boot state (booted, shutdown)
    - claimed: Filter by claim status (claimed, available)
    """
    pool = _get_pool(request)
    try:
        devices = await pool.list_devices(state_filter=state, claimed_filter=claimed)
        return {
            "devices": [d.model_dump() for d in devices],
            "total": len(devices),
        }
    except DeviceError as e:
        raise _handle_pool_error(e)


@router.post("/claim")
async def claim_device(request: Request, body: ClaimDeviceRequest):
    """Claim a device for exclusive use by a session.

    Returns 200 with device info on success.
    Returns 409 if device already claimed.
    Returns 404 if device not found.
    """
    pool = _get_pool(request)
    try:
        device = await pool.claim_device(
            session_id=body.session_id,
            udid=body.udid,
            name=body.name,
        )
        return {
            "status": "claimed",
            "device": device.model_dump(),
        }
    except DeviceError as e:
        raise _handle_pool_error(e)


@router.post("/release")
async def release_device(request: Request, body: ReleaseDeviceRequest):
    """Release a claimed device back to the pool."""
    pool = _get_pool(request)
    try:
        await pool.release_device(udid=body.udid, session_id=body.session_id)
        return {"status": "released", "udid": body.udid}
    except DeviceError as e:
        raise _handle_pool_error(e)


@router.post("/cleanup")
async def cleanup_stale_claims(request: Request):
    """Manually trigger cleanup of stale device claims."""
    pool = _get_pool(request)
    try:
        released = await pool.cleanup_stale_claims()
        return {
            "status": "cleaned",
            "devices_released": released,
            "count": len(released),
        }
    except DeviceError as e:
        raise _handle_pool_error(e)


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
    """Smart device resolution with criteria matching, auto-boot, and wait.

    Returns 200 with device info on success.
    Returns 404 if no device matching criteria found.
    Returns 408 if timed out waiting for available device.
    Returns 409 if device claimed and wait_if_busy=false.
    Returns 503 if boot failed.
    """
    pool = _get_pool(request)
    start = time.time()
    try:
        udid = await pool.resolve_device(
            udid=body.udid,
            name=body.name,
            os_version=body.os_version,
            auto_boot=body.auto_boot,
            wait_if_busy=body.wait_if_busy,
            wait_timeout=body.wait_timeout,
            session_id=body.session_id,
        )
        waited = round(time.time() - start, 2)
        device = await pool.get_device_state(udid)
        result = {
            "udid": udid,
            "name": device.name if device else "",
            "state": device.state.value if device else "unknown",
            "os_version": device.os_version if device else "",
            "waited_seconds": waited,
        }
        if body.session_id and device:
            result["claimed_by"] = device.claimed_by
        return result
    except DeviceError as e:
        raise _handle_pool_error(e)


@router.post("/ensure")
async def ensure_devices(request: Request, body: EnsureDevicesRequest):
    """Ensure N devices matching criteria are booted and available.

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
            auto_boot=body.auto_boot,
            session_id=body.session_id,
        )
        devices = []
        for udid in udids:
            device = await pool.get_device_state(udid)
            devices.append({
                "udid": udid,
                "name": device.name if device else "",
                "state": device.state.value if device else "unknown",
            })
        result = {
            "devices": devices,
            "total_available": len(udids),
        }
        if body.session_id:
            result["session_id"] = body.session_id
        return result
    except DeviceError as e:
        raise _handle_pool_error(e)
