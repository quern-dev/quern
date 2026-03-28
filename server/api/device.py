"""API routes for device management and screenshots."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from starlette.responses import StreamingResponse

from server.models import (
    BootDeviceRequest,
    DeviceError,
    DeviceType,
    GrantPermissionRequest,
    InstallAppRequest,
    LaunchAppRequest,
    OpenUrlRequest,
    PreviewStartRequest,
    PreviewStopRequest,
    SetDisplayDensityRequest,
    SetFontScaleRequest,
    SetLocaleRequest,
    SetLocationRequest,
    ShutdownDeviceRequest,
    StartDeviceLogRequest,
    StartSimLogRequest,
    StopDeviceLogRequest,
    StopSimLogRequest,
    TerminateAppRequest,
    UninstallAppRequest,
    WdaElementNotFoundError,
    WdaElementNotInteractableError,
    WdaInvalidSessionError,
    WdaKeyboardNotPresentError,
)

router = APIRouter(prefix="/api/v1/device", tags=["device"])
logger = logging.getLogger("quern-debug-server.api")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _capture_screen_context(controller, udid: str) -> dict:
    """Best-effort screen context capture for action responses."""
    try:
        summary, _ = await controller.get_screen_summary(
            max_elements=10, udid=udid,
        )
        return {
            "screen_title": summary.get("screen_title", ""),
            "summary": summary.get("summary", ""),
            "element_count": summary.get("element_count", 0),
            "interactive_elements": summary.get("interactive_elements", []),
        }
    except Exception:
        return {}


async def _capture_action_screenshot(controller, udid: str, label: str) -> str | None:
    """Best-effort screenshot capture for action before/after pairs."""
    try:
        from datetime import UTC, datetime
        from pathlib import Path

        screenshot_dir = Path("/tmp/quern/screenshots")
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%S")
        filename = f"{ts}_{label}.png"
        filepath = screenshot_dir / filename
        image_bytes, _ = await controller.screenshot(udid=udid, scale=0.5)
        filepath.write_bytes(image_bytes)
        return str(filepath)
    except Exception:
        return None


def _get_controller(request: Request):
    """Get the DeviceController from app state."""
    controller = request.app.state.device_controller
    if controller is None:
        raise HTTPException(status_code=503, detail="Device controller not initialized")
    return controller


def _handle_device_error(e: DeviceError) -> HTTPException:
    """Map a DeviceError to an appropriate HTTPException."""
    msg = str(e)
    # Type-based routing for WDA errors (more reliable than string matching)
    if isinstance(e, WdaElementNotFoundError):
        return HTTPException(status_code=404, detail=msg)
    if isinstance(e, WdaInvalidSessionError):
        return HTTPException(status_code=503, detail=msg)
    if isinstance(e, (WdaKeyboardNotPresentError, WdaElementNotInteractableError)):
        return HTTPException(status_code=400, detail=msg)
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
    device_type: str | None = Query(
        default=None,
        pattern="^(simulator|device|android_emulator|android_device)$",
    ),
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
        udid = await controller.boot(udid=body.udid, name=body.name, headless=body.headless)
    except DeviceError as e:
        raise _handle_device_error(e)

    # Auto-install proxy cert if the cert file exists
    cert_auto_installed: bool | None = None
    if _cert_manager.get_cert_path().exists():
        try:
            cert_auto_installed = await _cert_manager.install_cert(controller, udid)
        except Exception as exc:
            logger.warning("Auto cert install failed for %s: %s", udid, exc)

    # Auto-start proxy if cert is installed but proxy isn't running
    proxy_auto_started = False
    has_cert = cert_auto_installed is True
    if not has_cert:
        # Check if cert was previously installed
        from server.proxy.cert_state import read_cert_state_for_device
        state = read_cert_state_for_device(udid)
        has_cert = state.get("cert_installed", False) if state else False

    if has_cert:
        proxy_adapter = request.app.state.proxy_adapter
        if proxy_adapter is not None and not proxy_adapter.is_running:
            try:
                await proxy_adapter.start()
                try:
                    from server.lifecycle.state import update_state
                    update_state(proxy_status="running")
                except Exception:
                    pass
                proxy_auto_started = True
                logger.info("Auto-started proxy for device %s (cert installed)", udid[:12])
            except Exception as exc:
                logger.warning("Failed to auto-start proxy for %s: %s", udid[:12], exc)

    return {
        "status": "booted",
        "udid": udid,
        "cert_auto_installed": cert_auto_installed,
        "proxy_auto_started": proxy_auto_started,
    }


@router.post("/shutdown")
async def shutdown_device(request: Request, body: ShutdownDeviceRequest):
    """Shutdown a simulator."""
    controller = _get_controller(request)
    try:
        await controller.shutdown(udid=body.udid)
        return {"status": "shutdown", "udid": body.udid}
    except DeviceError as e:
        raise _handle_device_error(e)


@router.post("/erase")
async def erase_device(request: Request, body: ShutdownDeviceRequest):
    """Erase a simulator, resetting it to factory state. Simulator only."""
    controller = _get_controller(request)
    try:
        await controller.erase(udid=body.udid)
        return {"status": "erased", "udid": body.udid}
    except DeviceError as e:
        raise _handle_device_error(e)


@router.post("/active")
async def set_active_device(request: Request, body: ShutdownDeviceRequest):
    """Set the active device by UDID."""
    controller = _get_controller(request)
    controller._active_udid = body.udid
    return {"active_udid": body.udid}


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
        resolved = await controller.resolve_udid(body.udid)
        if body.capture_screenshots:
            before = await _capture_action_screenshot(controller, resolved, "launch_before")
        udid = await controller.launch_app(
            bundle_id=body.bundle_id, udid=body.udid, env=body.env,
        )
        result: dict = {"status": "launched", "udid": udid, "bundle_id": body.bundle_id}
        if body.capture_screenshots:
            await asyncio.sleep(body.settle_delay)
            after = await _capture_action_screenshot(controller, udid, "launch_after")
            result["screenshots"] = {"before": before, "after": after}
        if body.include_screen_context:
            if not body.capture_screenshots:
                await asyncio.sleep(body.settle_delay)
            result["screen_context"] = await _capture_screen_context(controller, udid)
        return result
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


@router.get("/video")
async def video_stream(
    request: Request,
    udid: str | None = Query(default=None),
    fps: float = Query(default=2, ge=0.5, le=10),
    scale: float = Query(default=0.5, ge=0.1, le=1.0),
    quality: int = Query(default=70, ge=1, le=100),
):
    """Stream live MJPEG video from a device.

    Returns a multipart/x-mixed-replace response with JPEG frames.
    Works in any browser via <img src="...">, in VLC, or any MJPEG client.

    FPS is limited by screenshot capture speed (~1-3 FPS depending on device).
    The fps parameter sets the minimum interval between frames.
    """
    controller = _get_controller(request)
    interval = 1.0 / fps

    async def generate():
        boundary = b"--frame\r\n"
        while True:
            t0 = asyncio.get_event_loop().time()
            try:
                image_bytes, _ = await controller.screenshot(
                    udid=udid, format="jpeg", scale=scale, quality=quality,
                )
                yield (
                    boundary
                    + b"Content-Type: image/jpeg\r\n"
                    + f"Content-Length: {len(image_bytes)}\r\n".encode()
                    + b"\r\n"
                    + image_bytes
                    + b"\r\n"
                )
            except Exception:
                pass
            elapsed = asyncio.get_event_loop().time() - t0
            remaining = interval - elapsed
            if remaining > 0:
                await asyncio.sleep(remaining)

    return StreamingResponse(
        generate(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


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


@router.post("/open-url")
async def open_url(request: Request, body: OpenUrlRequest):
    """Open a URL on a simulator or emulator."""
    controller = _get_controller(request)
    try:
        resolved = await controller.resolve_udid(body.udid)
        if body.capture_screenshots:
            before = await _capture_action_screenshot(controller, resolved, "open_url_before")
        udid = await controller.open_url(url=body.url, udid=body.udid)
        result: dict = {"status": "ok", "udid": udid, "url": body.url}
        if body.capture_screenshots:
            await asyncio.sleep(body.settle_delay)
            after = await _capture_action_screenshot(controller, udid, "open_url_after")
            result["screenshots"] = {"before": before, "after": after}
        if body.include_screen_context:
            if not body.capture_screenshots:
                await asyncio.sleep(body.settle_delay)
            result["screen_context"] = await _capture_screen_context(controller, udid)
        return result
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


@router.post("/locale")
async def set_locale(request: Request, body: SetLocaleRequest):
    """Set the system locale (Android only)."""
    controller = _get_controller(request)
    try:
        udid = await controller.set_locale(
            lang=body.lang, country=body.country, udid=body.udid,
        )
        locale_tag = f"{body.lang}-{body.country}" if body.country else body.lang
        return {"status": "ok", "udid": udid, "locale": locale_tag}
    except DeviceError as e:
        raise _handle_device_error(e)


@router.post("/font-scale")
async def set_font_scale(request: Request, body: SetFontScaleRequest):
    """Set the font scale (Android only). 1.0 = default."""
    controller = _get_controller(request)
    try:
        udid = await controller.set_font_scale(scale=body.scale, udid=body.udid)
        return {"status": "ok", "udid": udid, "scale": body.scale}
    except DeviceError as e:
        raise _handle_device_error(e)


@router.post("/display-density")
async def set_display_density(request: Request, body: SetDisplayDensityRequest):
    """Set display density override (Android only). Omit dpi to reset."""
    controller = _get_controller(request)
    try:
        udid = await controller.set_display_density(dpi=body.dpi, udid=body.udid)
        return {
            "status": "ok",
            "udid": udid,
            "dpi": body.dpi,
            "reset": body.dpi is None,
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
        return {
            "status": "already_running", "udid": udid,
            "adapter_id": sim_adapters[udid].adapter_id,
        }

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

    # Apply ingestion filter preset if requested
    preset_applied = None
    if body.preset:
        from server.processing.ingestion_filter import PRESETS, build_config

        if body.preset not in PRESETS:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown preset: {body.preset!r}. Available: {sorted(PRESETS)}",
            )
        ingestion_filter = request.app.state.ingestion_filter
        config = build_config(preset=body.preset)
        from server.models import LogSource
        ingestion_filter.update_filter(config, source=LogSource.SIMULATOR)
        buffer = request.app.state.ring_buffer
        await buffer.purge(lambda e: ingestion_filter.should_admit(e))
        preset_applied = body.preset

    # Auto-start plist watchers from persistent config
    plist_watchers_started = []
    plist_watchers_already_running = []
    try:
        from server.config import get_plist_watch_config
        from server.sources.plist_watcher import PlistWatcherAdapter

        pw_config = get_plist_watch_config()
        watchers: dict = request.app.state.plist_watchers

        for bundle_id, bundle_cfg in pw_config.items():
            for watch in bundle_cfg.get("watches", []):
                container = watch["container"]
                plist_path = watch["plist_path"]
                watch_key = f"{udid}:{container}:{plist_path}"
                watch_label = f"{container}:{plist_path}"

                if watch_key in watchers and watchers[watch_key].is_running:
                    plist_watchers_already_running.append(watch_label)
                    continue

                pw_adapter = PlistWatcherAdapter(
                    udid=udid,
                    bundle_id=bundle_id,
                    container=container,
                    plist_path=plist_path,
                    ignore_prefixes=watch.get("ignore_prefixes", []),
                    on_entry=dedup.process,
                )
                await pw_adapter.start()
                if not pw_adapter._error:
                    watchers[watch_key] = pw_adapter
                    request.app.state.source_adapters[pw_adapter.adapter_id] = pw_adapter
                    plist_watchers_started.append(watch_label)
                else:
                    logger.warning(
                        "Auto-start plist watch failed for %s: %s",
                        watch_label, pw_adapter._error,
                    )
    except Exception as e:
        logger.warning("Failed to auto-start plist watchers: %s", e)

    result = {
        "status": "started", "udid": udid,
        "adapter_id": adapter.adapter_id,
        "preset_applied": preset_applied,
    }
    if plist_watchers_started:
        result["plist_watchers_started"] = plist_watchers_started
    if plist_watchers_already_running:
        result["plist_watchers_already_running"] = plist_watchers_already_running
    return result


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

    # Auto-stop any plist watchers for this UDID
    plist_watchers_stopped = []
    watchers: dict = request.app.state.plist_watchers
    to_remove = [k for k in watchers if k.startswith(f"{udid}:")]
    for key in to_remove:
        pw = watchers.pop(key)
        await pw.stop()
        request.app.state.source_adapters.pop(pw.adapter_id, None)
        plist_watchers_stopped.append(key.split(":", 1)[1])

    result = {"status": "stopped", "udid": udid}
    if plist_watchers_stopped:
        result["plist_watchers_stopped"] = plist_watchers_stopped
    return result


# ---------------------------------------------------------------------------
# Physical device logging
# ---------------------------------------------------------------------------


@router.post("/logging/device/start")
async def start_device_logging(request: Request, body: StartDeviceLogRequest):
    """Start capturing logs from a physical device.

    For iOS devices: uses pymobiledevice3 syslog. Captures os_log and Logger
    output with source="device".
    For Android devices: uses adb logcat. Captures logcat output with
    source="logcat".

    Use process filter to limit noise. Use preset to apply an ingestion
    filter at start time.
    """
    from server.sources.device_log import PhysicalDeviceLogAdapter
    from server.sources.logcat import LogcatAdapter

    controller = _get_controller(request)

    # Resolve UDID
    try:
        udid = await controller.resolve_udid(body.udid)
    except DeviceError as e:
        raise _handle_device_error(e)

    # Verify it's a physical or Android device (not a simulator)
    is_android = controller._is_android(udid)
    if not controller._is_physical(udid) and not is_android:
        raise HTTPException(
            status_code=400,
            detail=f"Device {udid} is a simulator. Use start_simulator_logging instead.",
        )

    # Check if already running for this UDID
    dev_adapters: dict = request.app.state.device_log_adapters
    if udid in dev_adapters and dev_adapters[udid].is_running:
        return {
            "status": "already_running", "udid": udid,
            "adapter_id": dev_adapters[udid].adapter_id,
        }

    # Get the deduplicator as the entry callback (same pipeline as other adapters)
    dedup = request.app.state.deduplicator

    if is_android:
        adapter = LogcatAdapter(
            serial=udid,
            on_entry=dedup.process,
            process_filter=body.process,
        )
    else:
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

    # Apply ingestion filter preset if requested
    preset_applied = None
    if body.preset:
        from server.processing.ingestion_filter import PRESETS, build_config

        if body.preset not in PRESETS:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown preset: {body.preset!r}. Available: {sorted(PRESETS)}",
            )
        ingestion_filter = request.app.state.ingestion_filter
        config = build_config(preset=body.preset)
        from server.models import LogSource
        ingestion_filter.update_filter(config, source=LogSource.DEVICE)
        buffer = request.app.state.ring_buffer
        await buffer.purge(lambda e: ingestion_filter.should_admit(e))
        preset_applied = body.preset

    return {
        "status": "started", "udid": udid,
        "adapter_id": adapter.adapter_id,
        "preset_applied": preset_applied,
    }


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


def _get_scrcpy_preview(request: Request):
    """Get the ScrcpyPreview from app state."""
    sp = getattr(request.app.state, "scrcpy_preview", None)
    if sp is None:
        raise HTTPException(status_code=503, detail="Scrcpy preview not initialized")
    return sp


@router.post("/preview/start")
async def preview_start(request: Request, body: PreviewStartRequest):
    """Start a live preview window for a device.

    - iOS physical devices: CoreMediaIO screen capture (USB only, not simulators)
    - Android devices (emulator or physical): scrcpy (requires `brew install scrcpy`)

    If a UDID is provided, adds that single device. If omitted, adds all
    available USB iOS devices. Multiple devices can be previewed independently.
    """
    controller = _get_controller(request)

    if body.udid:
        # Resolve UDID and validate
        try:
            udid = await controller.resolve_udid(body.udid)
        except DeviceError as e:
            raise _handle_device_error(e)

        # Android devices → scrcpy
        if controller._is_android(udid):
            sp = _get_scrcpy_preview(request)

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
                device_name = udid

            try:
                session = await sp.add(udid, device_name)
                return {
                    "status": "added",
                    "name": session.name,
                    "serial": session.serial,
                    "platform": "android",
                }
            except RuntimeError as e:
                raise HTTPException(status_code=500, detail=str(e))

        # iOS physical → CoreMediaIO
        if not controller._is_physical(udid):
            raise HTTPException(
                status_code=400,
                detail=f"Device {udid} is a simulator. Live preview only works with "
                       f"physical devices connected via USB.",
            )

        pm = _get_preview_manager(request)

        # Get device name for the CoreMediaIO match
        device_name = None
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
                "platform": "ios",
            }
        except RuntimeError as e:
            raise HTTPException(status_code=500, detail=str(e))

    else:
        # No UDID — add all available iOS USB devices (existing behavior)
        pm = _get_preview_manager(request)
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
                added.append({
                    "name": preview.name,
                    "position": preview.position,
                    "status": "added",
                })
            except RuntimeError as e:
                errors.append({"name": dev.name, "error": str(e)})

        return {"devices": added, "errors": errors}


@router.post("/preview/stop")
async def preview_stop(request: Request, body: PreviewStopRequest):
    """Stop live preview.

    If a UDID is provided, stops only that device's preview (routes to the
    correct manager based on device type). If omitted, stops all previews.
    """
    if body.udid:
        controller = _get_controller(request)
        try:
            udid = await controller.resolve_udid(body.udid)
        except DeviceError as e:
            raise _handle_device_error(e)

        # Android → scrcpy
        if controller._is_android(udid):
            sp = _get_scrcpy_preview(request)
            await sp.remove(udid)
            return {"status": "removed", "serial": udid}

        # iOS → CoreMediaIO
        pm = _get_preview_manager(request)
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

    # No UDID — stop all
    pm = _get_preview_manager(request)
    sp = _get_scrcpy_preview(request)
    await sp.stop()
    return await pm.stop()


@router.get("/preview/status")
async def preview_status(request: Request):
    """Get the current preview state including per-device breakdown."""
    pm = _get_preview_manager(request)
    sp = _get_scrcpy_preview(request)
    ios_status = pm.status()
    android_status = sp.status()
    return {
        "ios": ios_status,
        "android": android_status,
    }


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


# ---------------------------------------------------------------------------
# Screenshot timeline
# ---------------------------------------------------------------------------


@router.post("/screenshot/timeline/start")
async def start_timeline(
    request: Request,
    body: dict | None = None,
):
    """Start a screenshot timeline that auto-captures around every UI action."""
    from server.screenshot_timeline import ScreenshotTimeline

    if getattr(request.app.state, "active_timeline", None) is not None:
        raise HTTPException(status_code=409, detail="A timeline is already active")

    body = body or {}
    udid = body.get("udid")
    session_id = body.get("session_id")

    # Resolve active device if no udid provided
    if not udid:
        controller = _get_controller(request)
        try:
            udid = await controller.resolve_udid(None)
        except DeviceError:
            pass

    timeline = ScreenshotTimeline(udid=udid, session_id=session_id)
    request.app.state.active_timeline = timeline
    return {
        "session_id": timeline.session_id,
        "started_at": timeline.started_at.isoformat(),
        "output_dir": str(timeline.output_dir),
        "udid": udid,
    }


@router.post("/screenshot/timeline/stop")
async def stop_timeline(request: Request):
    """Stop the active screenshot timeline and return its manifest."""
    timeline = getattr(request.app.state, "active_timeline", None)
    if timeline is None:
        raise HTTPException(status_code=404, detail="No active timeline")
    request.app.state.active_timeline = None
    return timeline.get_manifest()


@router.get("/screenshot/timeline")
async def get_timeline(request: Request):
    """Get the manifest of the active screenshot timeline."""
    timeline = getattr(request.app.state, "active_timeline", None)
    if timeline is None:
        raise HTTPException(status_code=404, detail="No active timeline")
    return timeline.get_manifest()
