"""API routes for device pool management."""

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from server.models import DeviceError

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
    if "not found" in msg.lower():
        return HTTPException(status_code=404, detail=msg)
    if "already claimed" in msg.lower():
        return HTTPException(status_code=409, detail=msg)
    return HTTPException(status_code=500, detail=f"[{e.tool}] {msg}")


# Request models
class ClaimDeviceRequest(BaseModel):
    session_id: str
    udid: str | None = None
    name: str | None = None


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
