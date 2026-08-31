"""API routes for app state checkpoints and plist inspection."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request

from server.device.app_state import (
    delete_state,
    get_checkpoint_plist_path,
    list_states,
    resolve_container,
    restore_state,
    save_state,
)
from server.device.plist import diff_plists, read_plist, remove_plist_key, set_plist_value
from server.models import (
    ClearPlistWatchConfigRequest,
    ConfigurePlistWatchRequest,
    DeleteAppPlistKeyRequest,
    DeviceError,
    RestoreAppStateRequest,
    SaveAppStateRequest,
    SetAppPlistValueRequest,
    SetAppPlistValuesRequest,
    StartPlistWatchRequest,
    StopPlistWatchRequest,
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

    Simulator only. Terminates the app before copying. Set include_keychain to also
    capture the simulator keychain, which is required for the checkpoint to restore a
    logged-in session; that needs the device shut down.
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
            include_keychain=body.include_keychain,
        )
        return {"status": "saved", "udid": udid, "meta": meta}
    except DeviceError as e:
        raise _handle_device_error(e)


@router.post("/restore")
async def restore_app_state(request: Request, body: RestoreAppStateRequest):
    """Restore a named checkpoint. Terminates the app and re-resolves live container paths.

    If the checkpoint carries a keychain it is restored too (device must be shut down),
    unless include_keychain is explicitly false.
    """
    controller = _get_controller(request)
    try:
        udid = await controller.resolve_udid(body.udid)
        controller._require_simulator(udid, "restore_app_state")
        meta = await restore_state(
            udid=udid,
            bundle_id=body.bundle_id,
            label=body.label,
            include_keychain=body.include_keychain,
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
            return {
                "key": key, "value": data[key],
                "plist_path": plist_path, "container": container,
            }
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


@router.post("/plist/batch")
async def set_app_plist_values(request: Request, body: SetAppPlistValuesRequest):
    """Set multiple plist keys in one call."""
    controller = _get_controller(request)
    try:
        udid = await controller.resolve_udid(body.udid)
        controller._require_simulator(udid, "set_app_plist_values")
        container_path = await resolve_container(udid, body.bundle_id, body.container)
        full_path = container_path / body.plist_path
        if not full_path.exists():
            raise HTTPException(status_code=404, detail=f"Plist not found: {body.plist_path}")

        errors = []
        keys_set = 0
        for key, value in body.values.items():
            try:
                await set_plist_value(full_path, key, value)
                keys_set += 1
            except DeviceError as e:
                errors.append({"key": key, "error": str(e)})

        result = {
            "status": "ok" if not errors else "partial",
            "keys_set": keys_set,
            "plist_path": body.plist_path,
            "container": body.container,
        }
        if errors:
            result["errors"] = errors
        return result
    except HTTPException:
        raise
    except DeviceError as e:
        raise _handle_device_error(e)


@router.get("/plist/diff")
async def diff_app_plist(
    request: Request,
    bundle_id: str = Query(...),
    container: str = Query(..., description='"data" or a group ID'),
    plist_path: str = Query(..., description="Relative path to the plist"),
    checkpoint_label: str = Query(..., description="Saved checkpoint to compare against"),
    udid: str | None = Query(default=None),
):
    """Compare a live plist against a saved checkpoint.

    Returns added, removed, and changed keys.
    """
    controller = _get_controller(request)
    try:
        udid_resolved = await controller.resolve_udid(udid)
        controller._require_simulator(udid_resolved, "diff_app_plist")

        # Read live plist
        container_path = await resolve_container(udid_resolved, bundle_id, container)
        live_path = container_path / plist_path
        if not live_path.exists():
            raise HTTPException(status_code=404, detail=f"Live plist not found: {plist_path}")
        live_data = await read_plist(live_path)

        # Read checkpoint plist
        checkpoint_path = get_checkpoint_plist_path(
            bundle_id, checkpoint_label, container, plist_path,
        )
        checkpoint_data = await read_plist(checkpoint_path)

        diff = diff_plists(checkpoint_data, live_data)
        return {
            "checkpoint_label": checkpoint_label,
            "plist_path": plist_path,
            "container": container,
            **diff,
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


# ---------------------------------------------------------------------------
# Plist watch endpoints
# ---------------------------------------------------------------------------


def _watch_key(udid: str, container: str, plist_path: str) -> str:
    return f"{udid}:{container}:{plist_path}"


@router.post("/plist/watch/start")
async def start_plist_watch(request: Request, body: StartPlistWatchRequest):
    """Start polling a plist file and emitting changes as log entries."""
    from server.sources.plist_watcher import PlistWatcherAdapter

    controller = _get_controller(request)
    try:
        udid = await controller.resolve_udid(body.udid)
        controller._require_simulator(udid, "start_plist_watch")
    except DeviceError as e:
        raise _handle_device_error(e)

    watchers: dict = request.app.state.plist_watchers
    key = _watch_key(udid, body.container, body.plist_path)

    if key in watchers and watchers[key].is_running:
        return {
            "status": "already_running", "udid": udid,
            "adapter_id": watchers[key].adapter_id,
        }

    dedup = request.app.state.deduplicator

    adapter = PlistWatcherAdapter(
        udid=udid,
        bundle_id=body.bundle_id,
        container=body.container,
        plist_path=body.plist_path,
        poll_interval=body.poll_interval,
        ignore_prefixes=body.ignore_prefixes,
        on_entry=dedup.process,
    )

    await adapter.start()

    if adapter._error:
        raise HTTPException(status_code=500, detail=adapter._error)

    watchers[key] = adapter
    request.app.state.source_adapters[adapter.adapter_id] = adapter

    return {
        "status": "started", "udid": udid,
        "adapter_id": adapter.adapter_id,
        "container": body.container,
        "plist_path": body.plist_path,
        "poll_interval": body.poll_interval,
    }


@router.post("/plist/watch/stop")
async def stop_plist_watch(request: Request, body: StopPlistWatchRequest):
    """Stop polling a plist file."""
    controller = _get_controller(request)
    try:
        udid = await controller.resolve_udid(body.udid)
    except DeviceError as e:
        raise _handle_device_error(e)

    watchers: dict = request.app.state.plist_watchers
    key = _watch_key(udid, body.container, body.plist_path)

    adapter = watchers.get(key)
    if not adapter:
        raise HTTPException(
            status_code=404,
            detail=f"No plist watch active for {body.container}:{body.plist_path} on {udid}",
        )

    await adapter.stop()

    del watchers[key]
    request.app.state.source_adapters.pop(adapter.adapter_id, None)

    return {"status": "stopped", "udid": udid}


@router.get("/plist/watch/config")
async def get_plist_watch_config_endpoint():
    """Return the persistent plist watch configuration."""
    from server.config import get_plist_watch_config

    return {"plist_watch": get_plist_watch_config()}


@router.post("/plist/watch/configure")
async def configure_plist_watch(body: ConfigurePlistWatchRequest):
    """Save persistent plist watch config for a bundle_id.

    When start_simulator_logging runs, it checks this config and
    auto-starts plist watchers for all configured targets.
    """
    from server.config import set_plist_watch_config

    set_plist_watch_config(
        bundle_id=body.bundle_id,
        watches=[w.model_dump() for w in body.watches],
    )
    return {
        "status": "configured",
        "bundle_id": body.bundle_id,
        "watches": [w.model_dump() for w in body.watches],
    }


@router.delete("/plist/watch/configure")
async def clear_plist_watch_config_endpoint(body: ClearPlistWatchConfigRequest):
    """Remove persistent plist watch config for a bundle_id."""
    from server.config import clear_plist_watch_config

    existed = clear_plist_watch_config(body.bundle_id)
    if not existed:
        raise HTTPException(
            status_code=404,
            detail=f"No plist watch config for {body.bundle_id}",
        )
    return {"status": "cleared", "bundle_id": body.bundle_id}
