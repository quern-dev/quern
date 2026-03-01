"""API routes for device management and screenshots."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from server.models import (
    BootDeviceRequest,
    DeviceError,
    DeviceType,
    GrantPermissionRequest,
    InstallAppRequest,
    LaunchAppRequest,
    PreviewStartRequest,
    PreviewStopRequest,
    SetLocationRequest,
    ShutdownDeviceRequest,
    StartDeviceLogRequest,
    StartSimLogRequest,
    StopDeviceLogRequest,
    StopSimLogRequest,
    TerminateAppRequest,
    UninstallAppRequest,
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
    if "No booted device" in msg or "Multiple devices booted" in msg:
        return HTTPException(status_code=400, detail=msg)
    if "only supported on simulators" in msg:
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
async def list_devices(
    request: Request,
    state: str | None = Query(default=None, pattern="^(booted|shutdown)$"),
    device_type: str | None = Query(default=None, pattern="^(simulator|device)$"),
    name: str | None = Query(default=None),
    os_version: str | None = Query(default=None),
    device_family: str | None = Query(default=None),
    cert_installed: bool | None = Query(default=None),
    include_disconnected: bool = Query(default=False),
):
    """List all devices (simulators + physical) and tool availability.

    Query params:
    - state: Filter by boot state (booted, shutdown)
    - device_type: Filter by device type (simulator, device)
    - name: Filter by device name (case-insensitive, exact preferred, substring fallback)
    - os_version: Filter by OS version prefix (e.g. '18', '18.2', 'iOS 18.2')
    - device_family: Filter by device family ('iPhone', 'iPad', 'Apple Watch', 'Apple TV')
    - cert_installed: Filter by cert installation status (true/false)
    - include_disconnected: Include paired but unreachable physical devices
    """
    controller = _get_controller(request)
    try:
        devices = await controller.list_devices()
        tools = await controller.check_tools()

        # Apply server-side filters
        if not include_disconnected:
            devices = [d for d in devices if d.is_connected]
        if state:
            devices = [d for d in devices if d.state.value == state]
        if device_type:
            dt = DeviceType(device_type)
            devices = [d for d in devices if d.device_type == dt]

        # Name filter: exact match preferred, substring fallback
        if name:
            name_lower = name.lower()
            exact = [d for d in devices if d.name.lower() == name_lower]
            if exact:
                devices = exact
            else:
                devices = [d for d in devices if name_lower in d.name.lower()]

        # OS version filter: prefix match
        if os_version:
            import re
            def _os_matches(device_os: str, requested: str) -> bool:
                m = re.search(r"[\d.]+", device_os)
                if not m:
                    return False
                dev_ver = m.group()
                rm = re.search(r"[\d.]+", requested)
                if not rm:
                    return False
                req_ver = rm.group()
                return dev_ver == req_ver or dev_ver.startswith(req_ver + ".")
            devices = [d for d in devices if _os_matches(d.os_version, os_version)]

        # Device family filter: case-insensitive
        if device_family:
            family_lower = device_family.lower()
            devices = [d for d in devices if d.device_family.lower() == family_lower]

        device_dicts = [d.model_dump() for d in devices]

        # Enrich with cert_installed status if requested or always for convenience
        if cert_installed is not None:
            from server.proxy.cert_state import read_cert_state
            cert_states = read_cert_state()
            for dd in device_dicts:
                dd["cert_installed"] = cert_states.get(dd["udid"], {}).get("cert_installed", False)
            device_dicts = [
                dd for dd in device_dicts
                if dd["cert_installed"] == cert_installed
            ]
        return {
            "devices": device_dicts,
            "tools": tools,
            "active_udid": controller._active_udid,
        }
    except DeviceError as e:
        raise _handle_device_error(e)


@router.post("/boot")
async def boot_device(request: Request, body: BootDeviceRequest):
    """Boot a simulator by udid or name."""
    from server.proxy import cert_manager as _cert_manager

    controller = _get_controller(request)
    try:
        udid = await controller.boot(udid=body.udid, name=body.name)
    except DeviceError as e:
        raise _handle_device_error(e)

    # Auto-install proxy cert if the cert file exists
    cert_auto_installed: bool | None = None
    if _cert_manager.get_cert_path().exists():
        try:
            cert_auto_installed = await _cert_manager.install_cert(controller, udid)
        except Exception as exc:
            logger.warning("Auto cert install failed for %s: %s", udid, exc)

    return {"status": "booted", "udid": udid, "cert_auto_installed": cert_auto_installed}


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


@router.post("/app/uninstall")
async def uninstall_app(request: Request, body: UninstallAppRequest):
    """Uninstall an app from a simulator or physical device."""
    controller = _get_controller(request)
    try:
        udid = await controller.uninstall_app(bundle_id=body.bundle_id, udid=body.udid)
        return {"status": "uninstalled", "udid": udid, "bundle_id": body.bundle_id}
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


# ---------------------------------------------------------------------------
# Physical device logging
# ---------------------------------------------------------------------------


@router.post("/logging/device/start")
async def start_device_logging(request: Request, body: StartDeviceLogRequest):
    """Start capturing logs from a physical device via pymobiledevice3 syslog.

    Captures os_log, Logger, and NSLog output. Logs appear in tail_logs/query_logs
    with source="device". Use process filter to limit noise.

    NOTE: This does NOT capture print() output.
    """
    from server.sources.device_log import PhysicalDeviceLogAdapter

    controller = _get_controller(request)

    # Resolve UDID
    try:
        udid = await controller.resolve_udid(body.udid)
    except DeviceError as e:
        raise _handle_device_error(e)

    # Verify it's a physical device
    if not controller._is_physical(udid):
        raise HTTPException(
            status_code=400,
            detail=f"Device {udid} is a simulator. Use start_simulator_logging instead.",
        )

    # Check if already running for this UDID
    dev_adapters: dict = request.app.state.device_log_adapters
    if udid in dev_adapters and dev_adapters[udid].is_running:
        return {"status": "already_running", "udid": udid, "adapter_id": dev_adapters[udid].adapter_id}

    # Get the deduplicator as the entry callback (same pipeline as other adapters)
    dedup = request.app.state.deduplicator

    adapter = PhysicalDeviceLogAdapter(
        udid=udid,
        on_entry=dedup.process,
        process_filter=body.process,
        match_filter=body.match,
    )

    await adapter.start()

    if adapter._error:
        raise HTTPException(status_code=500, detail=adapter._error)

    # Register in both dicts so it appears in list_log_sources
    dev_adapters[udid] = adapter
    request.app.state.source_adapters[adapter.adapter_id] = adapter

    return {"status": "started", "udid": udid, "adapter_id": adapter.adapter_id}


@router.post("/logging/device/stop")
async def stop_device_logging(request: Request, body: StopDeviceLogRequest):
    """Stop capturing logs from a physical device."""
    controller = _get_controller(request)

    # Resolve UDID
    try:
        udid = await controller.resolve_udid(body.udid)
    except DeviceError as e:
        raise _handle_device_error(e)

    dev_adapters: dict = request.app.state.device_log_adapters
    adapter = dev_adapters.get(udid)
    if not adapter:
        raise HTTPException(status_code=404, detail=f"No device logging active for UDID {udid}")

    await adapter.stop()

    # Remove from both dicts
    del dev_adapters[udid]
    request.app.state.source_adapters.pop(adapter.adapter_id, None)

    return {"status": "stopped", "udid": udid}


# ---------------------------------------------------------------------------
# Live preview
# ---------------------------------------------------------------------------


def _get_preview_manager(request: Request):
    """Get the PreviewManager from app state."""
    pm = getattr(request.app.state, "preview_manager", None)
    if pm is None:
        raise HTTPException(status_code=503, detail="Preview manager not initialized")
    return pm


@router.post("/preview/start")
async def preview_start(request: Request, body: PreviewStartRequest):
    """Start a live preview window for USB-connected physical iOS devices.

    Opens a macOS window showing the device screen in real time via CoreMediaIO.
    Compiles the preview binary on first use (~5s). Device discovery takes ~3s
    on first launch (cached thereafter).

    If a UDID is provided, adds that single device. If omitted, adds all
    available USB devices. Multiple devices can be previewed independently.
    """
    controller = _get_controller(request)
    pm = _get_preview_manager(request)

    if body.udid:
        # Resolve UDID and validate it's a physical device
        try:
            udid = await controller.resolve_udid(body.udid)
        except DeviceError as e:
            raise _handle_device_error(e)

        if not controller._is_physical(udid):
            raise HTTPException(
                status_code=400,
                detail=f"Device {udid} is a simulator. Live preview only works with "
                       f"physical devices connected via USB.",
            )

        # Get device name for the CoreMediaIO match
        device_name: str | None = None
        try:
            devices = await controller.list_devices()
            for d in devices:
                if d.udid == udid:
                    device_name = d.name
                    break
        except DeviceError:
            pass

        if not device_name:
            raise HTTPException(
                status_code=404,
                detail=f"Could not resolve device name for UDID {udid}",
            )

        try:
            preview = await pm.add(device_name)
            return {
                "status": "added",
                "name": preview.name,
                "position": preview.position,
            }
        except RuntimeError as e:
            raise HTTPException(status_code=500, detail=str(e))

    else:
        # No UDID — add all available devices
        try:
            await pm._ensure_process()
        except RuntimeError as e:
            raise HTTPException(status_code=500, detail=str(e))

        added = []
        errors = []
        for dev in pm._available:
            if dev.name in pm._active:
                added.append({"name": dev.name, "status": "already_active"})
                continue
            try:
                preview = await pm.add(dev.name)
                added.append({"name": preview.name, "position": preview.position, "status": "added"})
            except RuntimeError as e:
                errors.append({"name": dev.name, "error": str(e)})

        return {"devices": added, "errors": errors}


@router.post("/preview/stop")
async def preview_stop(request: Request, body: PreviewStopRequest):
    """Stop live preview.

    If a UDID is provided, stops only that device's preview.
    If omitted, stops all previews and kills the process.
    """
    pm = _get_preview_manager(request)

    if body.udid:
        controller = _get_controller(request)
        try:
            udid = await controller.resolve_udid(body.udid)
        except DeviceError as e:
            raise _handle_device_error(e)

        # Resolve UDID → name
        device_name: str | None = None
        try:
            devices = await controller.list_devices()
            for d in devices:
                if d.udid == udid:
                    device_name = d.name
                    break
        except DeviceError:
            pass

        if not device_name:
            raise HTTPException(
                status_code=404,
                detail=f"Could not resolve device name for UDID {udid}",
            )

        await pm.remove(device_name)
        return {"status": "removed", "name": device_name}

    return await pm.stop()


@router.get("/preview/status")
async def preview_status(request: Request):
    """Get the current preview state including per-device breakdown."""
    pm = _get_preview_manager(request)
    return pm.status()


@router.get("/preview/devices")
async def preview_devices(request: Request):
    """List devices available for live preview via CoreMediaIO.

    Only physical USB-connected iOS devices appear. If the preview process
    is running, returns the cached device list instantly. Otherwise takes
    ~3s due to CoreMediaIO discovery.
    """
    pm = _get_preview_manager(request)
    try:
        return await pm.list_devices()
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


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
