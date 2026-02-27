"""API routes for app state checkpoints and plist inspection."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request

from server.device.app_state import (
    delete_state,
    list_states,
    resolve_container,
    restore_state,
    save_state,
)
from server.device.plist import read_plist, remove_plist_key, set_plist_value
from server.models import (
    DeleteAppPlistKeyRequest,
    DeviceError,
    ReadAppPlistRequest,
    RestoreAppStateRequest,
    SaveAppStateRequest,
    SetAppPlistValueRequest,
)

router = APIRouter(prefix="/api/v1/device/app/state", tags=["app-state"])
logger = logging.getLogger("quern-debug-server.api")


def _get_controller(request: Request):
    controller = request.app.state.device_controller
    if controller is None:
        raise HTTPException(status_code=503, detail="Device controller not initialized")
    return controller


def _handle_device_error(e: DeviceError) -> HTTPException:
    msg = str(e)
    if "not found" in msg.lower() and "checkpoint" in msg.lower():
        return HTTPException(status_code=404, detail=msg)
    if "not found" in msg.lower() and "container" in msg.lower():
        return HTTPException(status_code=404, detail=msg)
    return HTTPException(status_code=500, detail=f"[{e.tool}] {msg}")


# ---------------------------------------------------------------------------
# Checkpoint endpoints
# ---------------------------------------------------------------------------


@router.post("/save")
async def save_app_state(request: Request, body: SaveAppStateRequest):
    """Save a named checkpoint of the app's state (data container + app groups).

    Simulator only. Terminates the app before copying.
    """
    controller = _get_controller(request)
    try:
        udid = await controller.resolve_udid(body.udid)
        controller._require_simulator(udid, "save_app_state")
        meta = await save_state(
            udid=udid,
            bundle_id=body.bundle_id,
            label=body.label,
            description=body.description or "",
        )
        return {"status": "saved", "udid": udid, "meta": meta}
    except DeviceError as e:
        raise _handle_device_error(e)


@router.post("/restore")
async def restore_app_state(request: Request, body: RestoreAppStateRequest):
    """Restore a named checkpoint. Terminates the app and re-resolves live container paths."""
    controller = _get_controller(request)
    try:
        udid = await controller.resolve_udid(body.udid)
        controller._require_simulator(udid, "restore_app_state")
        meta = await restore_state(
            udid=udid,
            bundle_id=body.bundle_id,
            label=body.label,
        )
        return {"status": "restored", "udid": udid, "meta": meta}
    except DeviceError as e:
        raise _handle_device_error(e)


@router.get("/list")
async def list_app_states(
    request: Request,
    bundle_id: str = Query(..., description="App bundle identifier"),
):
    """List all saved checkpoints for a bundle ID."""
    states = list_states(bundle_id)
    return {"bundle_id": bundle_id, "states": states, "total": len(states)}


@router.delete("/{label}")
async def delete_app_state(
    request: Request,
    label: str,
    bundle_id: str = Query(..., description="App bundle identifier"),
):
    """Delete a named checkpoint."""
    try:
        delete_state(bundle_id, label)
        return {"status": "deleted", "bundle_id": bundle_id, "label": label}
    except DeviceError as e:
        raise _handle_device_error(e)


# ---------------------------------------------------------------------------
# Plist endpoints
# ---------------------------------------------------------------------------


@router.get("/plist")
async def read_app_plist(
    request: Request,
    bundle_id: str = Query(...),
    container: str = Query(..., description='"data" or a group ID like "group.com.example"'),
    plist_path: str = Query(..., description="Relative path to the plist within the container"),
    key: str | None = Query(default=None, description="Plist key to read (omit for entire plist)"),
    udid: str | None = Query(default=None),
):
    """Read a plist value (or entire plist) from an app container."""
    controller = _get_controller(request)
    try:
        udid_resolved = await controller.resolve_udid(udid)
        controller._require_simulator(udid_resolved, "read_app_plist")
        container_path = await resolve_container(udid_resolved, bundle_id, container)
        full_path = container_path / plist_path
        if not full_path.exists():
            raise HTTPException(status_code=404, detail=f"Plist not found: {plist_path}")
        data = await read_plist(full_path)
        if key is not None:
            if key not in data:
                raise HTTPException(status_code=404, detail=f"Key {key!r} not found in plist")
            return {"key": key, "value": data[key], "plist_path": plist_path, "container": container}
        return {"data": data, "plist_path": plist_path, "container": container}
    except HTTPException:
        raise
    except DeviceError as e:
        raise _handle_device_error(e)


@router.post("/plist")
async def set_app_plist_value(request: Request, body: SetAppPlistValueRequest):
    """Set a plist key in an app container."""
    controller = _get_controller(request)
    try:
        udid = await controller.resolve_udid(body.udid)
        controller._require_simulator(udid, "set_app_plist_value")
        container_path = await resolve_container(udid, body.bundle_id, body.container)
        full_path = container_path / body.plist_path
        if not full_path.exists():
            raise HTTPException(status_code=404, detail=f"Plist not found: {body.plist_path}")
        await set_plist_value(full_path, body.key, body.value)
        return {
            "status": "ok",
            "key": body.key,
            "value": body.value,
            "plist_path": body.plist_path,
            "container": body.container,
        }
    except HTTPException:
        raise
    except DeviceError as e:
        raise _handle_device_error(e)


@router.delete("/plist/key")
async def delete_app_plist_key(request: Request, body: DeleteAppPlistKeyRequest):
    """Remove a key from a plist in an app container."""
    controller = _get_controller(request)
    try:
        udid = await controller.resolve_udid(body.udid)
        controller._require_simulator(udid, "delete_app_plist_key")
        container_path = await resolve_container(udid, body.bundle_id, body.container)
        full_path = container_path / body.plist_path
        if not full_path.exists():
            raise HTTPException(status_code=404, detail=f"Plist not found: {body.plist_path}")
        await remove_plist_key(full_path, body.key)
        return {
            "status": "ok",
            "key": body.key,
            "plist_path": body.plist_path,
            "container": body.container,
        }
    except HTTPException:
        raise
    except DeviceError as e:
        raise _handle_device_error(e)
