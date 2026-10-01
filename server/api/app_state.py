"""API routes for app state checkpoints and plist inspection."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request

from server.api.actions import logged_action
from server.device.app_state import (
    contained_path,
    delete_state,
    get_checkpoint_plist_path,
    list_states,
    resolve_container,
    restore_state,
    save_state,
    sync_preferences,
)
from server.device.plist import diff_plists, read_plist, remove_plist_key, set_plist_values
from server.models import (
    AppStateNotFoundError,
    ClearPlistWatchConfigRequest,
    ConfigurePlistWatchRequest,
    DeleteAppPlistKeyRequest,
    DeviceError,
    InvalidAppStatePathError,
    RestoreAppStateRequest,
    SaveAppStateRequest,
    SetAppPlistValueRequest,
    SetAppPlistValuesRequest,
    StartPlistWatchRequest,
    StopPlistWatchRequest,
)

router = APIRouter(prefix="/api/v1/device/app/state", tags=["app-state"])
logger = logging.getLogger(__name__)


def _get_controller(request: Request):
    controller = request.app.state.device_controller
    if controller is None:
        raise HTTPException(status_code=503, detail="Device controller not initialized")
    return controller


def _handle_device_error(e: DeviceError) -> HTTPException:
    msg = str(e)
    # By type, not by text. This used to match "not found" plus "container" in
    # the message, and every simulator container *path* contains `Containers`
    # -- so any failure that quoted its path and said "not found" anywhere
    # became a 404 for a container that existed.
    if isinstance(e, AppStateNotFoundError):
        return HTTPException(status_code=404, detail=msg)
    if isinstance(e, InvalidAppStatePathError):
        return HTTPException(status_code=400, detail=msg)
    if "only supported on simulators" in msg:
        # 400, not 500. Asking for an iOS-simulator operation on an Android
        # device is a bad request, not a server fault, and `server/api/
        # device.py` has classified it that way all along -- this module
        # carries its own copy of this function and never gained the rule, so
        # the same refusal was a 400 from one endpoint and a 500 from another
        # depending only on which file the route lived in. #263 predicted
        # exactly this; a live call against a real phone confirmed it.
        return HTTPException(status_code=400, detail=msg)
    return HTTPException(status_code=500, detail=f"[{e.tool}] {msg}")


def _with_sync(result: dict, *syncs: dict) -> dict:
    """Attach the cfprefsd outcome, promoting a failure to a top-level warning.

    The warning is the part that changes what the result means -- the app may
    not see the change -- so it goes where a caller reading the body will see
    it, not only in the server log.
    """
    failed = next((x for x in syncs if not x.get("synced")), None)
    result["preferences"] = failed or (syncs[-1] if syncs else {"synced": True})
    if failed:
        result["warning"] = failed["warning"]
    return result


def _warn_if_unsynced(result: dict, meta: dict) -> dict:
    prefs = meta.get("preferences") or {}
    if prefs.get("synced") is False:
        result["warning"] = prefs["warning"]
    return result


async def _live_plist(udid: str, bundle_id: str, container: str, plist_path: str):
    container_path = await resolve_container(udid, bundle_id, container)
    full_path = contained_path(container_path, plist_path)
    if not full_path.exists():
        raise HTTPException(status_code=404, detail=f"Plist not found: {plist_path}")
    return full_path


# ---------------------------------------------------------------------------
# Checkpoint endpoints
# ---------------------------------------------------------------------------


@router.post("/save")
@logged_action("save_app_state", category="device.action")
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
        return _warn_if_unsynced({"status": "saved", "udid": udid, "meta": meta}, meta)
    except DeviceError as e:
        raise _handle_device_error(e)


@router.post("/restore")
@logged_action("restore_app_state", category="device.action")
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
        return _warn_if_unsynced({"status": "restored", "udid": udid, "meta": meta}, meta)
    except DeviceError as e:
        raise _handle_device_error(e)


@router.get("/list")
@logged_action("list_app_states", category="device.read")
async def list_app_states(
    request: Request,
    bundle_id: str = Query(..., description="App bundle identifier"),
):
    """List all saved checkpoints for a bundle ID."""
    try:
        states = list_states(bundle_id)
    except DeviceError as e:
        raise _handle_device_error(e)
    return {"bundle_id": bundle_id, "states": states, "total": len(states)}


@router.delete("/{label}")
@logged_action("delete_app_state", category="device.action")
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
@logged_action("read_app_plist", category="device.read")
async def read_app_plist(
    request: Request,
    bundle_id: str = Query(...),
    container: str = Query(..., description='"data" or a group ID like "group.com.example"'),
    plist_path: str = Query(..., description="Relative path to the plist within the container"),
    key: str | None = Query(default=None, description="Plist key to read (omit for entire plist)"),
    udid: str | None = Query(default=None),
):
    """Read a plist value (or entire plist) from an app container.

    cfprefsd is flushed first, so the file reflects what the app last wrote
    rather than what the daemon had got round to saving.
    """
    controller = _get_controller(request)
    try:
        udid_resolved = await controller.resolve_udid(udid)
        controller._require_simulator(udid_resolved, "read_app_plist")
        sync = await sync_preferences(udid_resolved)
        full_path = await _live_plist(udid_resolved, bundle_id, container, plist_path)
        data = await read_plist(full_path)
        if key is not None:
            if key not in data:
                raise HTTPException(status_code=404, detail=f"Key {key!r} not found in plist")
            return _with_sync({
                "key": key, "value": data[key],
                "plist_path": plist_path, "container": container,
            }, sync)
        return _with_sync({"data": data, "plist_path": plist_path, "container": container}, sync)
    except HTTPException:
        raise
    except DeviceError as e:
        raise _handle_device_error(e)


async def _edit_live_plist(udid: str, body, edit) -> tuple[dict, dict]:
    """Resolve, flush cfprefsd, edit, then restart it so the edit is what is served.

    Both restarts are needed, and both are reported. The first writes out anything the app changed
    that cfprefsd is still holding -- otherwise that flush could land after
    the edit and overwrite it. The second drops the cache, which would
    otherwise keep serving the old values to the next launch.
    """
    # Sync first: an app's first write can sit in cfprefsd for seconds before
    # the file exists at all, and checking for it first answered 404 for a
    # plist the app had already written.
    before = await sync_preferences(udid)
    full_path = await _live_plist(udid, body.bundle_id, body.container, body.plist_path)
    await edit(full_path)
    after = await sync_preferences(udid)
    return before, after


@router.post("/plist")
@logged_action("set_app_plist_value", category="device.action")
async def set_app_plist_value(request: Request, body: SetAppPlistValueRequest):
    """Set a top-level plist key in an app container. The key is taken
    literally: `com.example.flag` is one key, not a path."""
    controller = _get_controller(request)
    try:
        udid = await controller.resolve_udid(body.udid)
        controller._require_simulator(udid, "set_app_plist_value")
        before, after = await _edit_live_plist(
            udid, body,
            lambda path: set_plist_values(path, {body.key: body.value}),
        )
        return _with_sync({
            "status": "ok",
            "key": body.key,
            "value": body.value,
            "plist_path": body.plist_path,
            "container": body.container,
        }, before, after)
    except HTTPException:
        raise
    except DeviceError as e:
        raise _handle_device_error(e)


@router.post("/plist/batch")
@logged_action("set_app_plist_values", category="device.action")
async def set_app_plist_values(request: Request, body: SetAppPlistValuesRequest):
    """Set multiple plist keys in one write: all of them, or none.

    This used to report HTTP 200 with `status: "partial"` and `keys_set: 0`
    when every key had failed. A failure is now an error response, and the
    file is left as it was.
    """
    controller = _get_controller(request)
    try:
        udid = await controller.resolve_udid(body.udid)
        controller._require_simulator(udid, "set_app_plist_values")
        before, after = await _edit_live_plist(
            udid, body,
            lambda path: set_plist_values(path, body.values),
        )
        return _with_sync({
            "status": "ok",
            "keys_set": len(body.values),
            "plist_path": body.plist_path,
            "container": body.container,
        }, before, after)
    except HTTPException:
        raise
    except DeviceError as e:
        raise _handle_device_error(e)


@router.get("/plist/diff")
@logged_action("diff_app_plist", category="device.read")
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

        # The checkpoint side first: it is a pure lookup, and a bad label or
        # path should be refused before anything touches the device.
        checkpoint_path = get_checkpoint_plist_path(
            bundle_id, checkpoint_label, container, plist_path,
        )
        checkpoint_data = await read_plist(checkpoint_path)

        sync = await sync_preferences(udid_resolved)
        try:
            live_path = await _live_plist(udid_resolved, bundle_id, container, plist_path)
        except HTTPException as e:
            raise HTTPException(
                status_code=404, detail=f"Live plist not found: {plist_path}",
            ) from e
        live_data = await read_plist(live_path)

        diff = diff_plists(checkpoint_data, live_data)
        return _with_sync({
            "checkpoint_label": checkpoint_label,
            "plist_path": plist_path,
            "container": container,
            **diff,
        }, sync)
    except HTTPException:
        raise
    except DeviceError as e:
        raise _handle_device_error(e)


@router.delete("/plist/key")
@logged_action("delete_app_plist_key", category="device.action")
async def delete_app_plist_key(request: Request, body: DeleteAppPlistKeyRequest):
    """Remove a top-level key, taken literally, from a plist in an app container.

    404 when the key is not there, rather than a removal that did nothing.
    """
    controller = _get_controller(request)
    try:
        udid = await controller.resolve_udid(body.udid)
        controller._require_simulator(udid, "delete_app_plist_key")
        before, after = await _edit_live_plist(
            udid, body,
            lambda path: remove_plist_key(path, body.key),
        )
        return _with_sync({
            "status": "ok",
            "key": body.key,
            "plist_path": body.plist_path,
            "container": body.container,
        }, before, after)
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
@logged_action("start_plist_watch", category="logs")
async def start_plist_watch(request: Request, body: StartPlistWatchRequest):
    """Start polling a plist file and emitting changes as log entries."""
    from server.sources.plist_watcher import PlistWatcherAdapter

    controller = _get_controller(request)
    try:
        udid = await controller.resolve_udid(body.udid)
        controller._require_simulator(udid, "start_plist_watch")
        # Checked here so a path outside the container, or a plist that is not
        # there, is a 400 or 404 rather than the adapter's generic 500.
        await sync_preferences(udid)
        await _live_plist(udid, body.bundle_id, body.container, body.plist_path)
    except HTTPException:
        raise
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
@logged_action("stop_plist_watch", category="logs")
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
@logged_action("configure_plist_watch", category="logs")
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
@logged_action("clear_plist_watch_config_endpoint", category="logs")
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
