"""API routes for network proxy flow inspection and control."""

from __future__ import annotations

import logging
import socket
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from server.lifecycle.state import update_state

_proxy_logger = logging.getLogger(__name__)

from server.models import (
    CertInstallRequest,
    CertStatusResponse,
    CertVerifyRequest,
    CertVerifyResponse,
    DeviceCertInstallStatus,
    DeviceCertState,
    FlowQueryParams,
    FlowQueryResponse,
    FlowRecord,
    FlowSummaryResponse,
    HeldFlow,
    InterceptSetRequest,
    InterceptStatusResponse,
    MockListResponse,
    MockRuleInfo,
    MockResponseSpec,
    ProxyStatusResponse,
    ReleaseFlowRequest,
    ReplayRequest,
    ReplayResponse,
    SetMockRequest,
    SystemProxyInfo,
    SystemProxyRestoreInfo,
)
from server.processing.summarizer import WINDOW_DURATIONS, parse_cursor
from server.proxy import cert_manager
from server.proxy.summary import generate_flow_summary
from server.proxy.system_proxy import (
    SystemProxySnapshot,
    configure_system_proxy,
    detect_active_interface,
    detect_and_configure,
    get_default_route_device,
    bsd_device_to_service_name,
    restore_system_proxy,
    snapshot_system_proxy,
)

router = APIRouter(prefix="/api/v1/proxy", tags=["proxy"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_proxy_status(request: Request) -> ProxyStatusResponse:
    """Build a ProxyStatusResponse from current app state."""
    from server.lifecycle.state import read_state

    adapter = request.app.state.proxy_adapter
    flow_store = request.app.state.flow_store

    # Get cert setup info from state.json
    cert_setup = None
    try:
        state = read_state()
        if state and state.get("device_certs"):
            cert_setup = {
                udid: DeviceCertState(**cert_data)
                for udid, cert_data in state["device_certs"].items()
            }
    except Exception as e:
        _proxy_logger.debug(f"Failed to load cert_setup: {e}")

    if adapter is None:
        return ProxyStatusResponse(status="stopped", cert_setup=cert_setup)

    if adapter._error:
        return ProxyStatusResponse(
            status="error",
            port=adapter.listen_port,
            listen_host=adapter.listen_host,
            error=adapter._error,
            flows_captured=flow_store.size if flow_store else 0,
            active_intercept=adapter._intercept_pattern,
            held_flows_count=len(adapter._held_flows),
            mock_rules_count=len(adapter._mock_rules),
            cert_setup=cert_setup,
        )

    if adapter.is_running:
        return ProxyStatusResponse(
            status="running",
            port=adapter.listen_port,
            listen_host=adapter.listen_host,
            started_at=adapter.started_at,
            flows_captured=flow_store.size if flow_store else 0,
            active_intercept=adapter._intercept_pattern,
            held_flows_count=len(adapter._held_flows),
            mock_rules_count=len(adapter._mock_rules),
            cert_setup=cert_setup,
        )

    return ProxyStatusResponse(
        status="stopped",
        port=adapter.listen_port,
        listen_host=adapter.listen_host,
        flows_captured=flow_store.size if flow_store else 0,
        cert_setup=cert_setup,
    )


def _require_running_proxy(request: Request):
    """Return the proxy adapter, raising 503 if not running."""
    adapter = request.app.state.proxy_adapter
    if adapter is None or not adapter.is_running:
        raise HTTPException(status_code=503, detail="Proxy is not running")
    return adapter


# ---------------------------------------------------------------------------
# Status & control
# ---------------------------------------------------------------------------


@router.get("/status", response_model=ProxyStatusResponse)
async def proxy_status(request: Request) -> ProxyStatusResponse:
    """Get current proxy status and configuration."""
    return _get_proxy_status(request)


@router.post("/start", response_model=ProxyStatusResponse)
async def start_proxy(request: Request, body: dict | None = None) -> ProxyStatusResponse:
    """Start the mitmproxy network capture."""
    import asyncio

    adapter = request.app.state.proxy_adapter
    if adapter is None:
        raise HTTPException(status_code=503, detail="Proxy adapter not configured")

    if adapter.is_running:
        raise HTTPException(status_code=409, detail="Proxy is already running")

    # Apply optional port/host reconfiguration
    if body:
        port = body.get("port")
        listen_host = body.get("listen_host")
        adapter.reconfigure(listen_port=port, listen_host=listen_host)

    await adapter.start()
    try:
        update_state(proxy_status="running")
    except Exception:
        _proxy_logger.debug("Could not update state file (test mode?)", exc_info=True)

    # Auto-configure system proxy unless explicitly disabled
    want_system_proxy = True
    if body and body.get("system_proxy") is not None:
        want_system_proxy = body["system_proxy"]

    system_proxy_info: SystemProxyInfo | None = None
    if want_system_proxy:
        try:
            snap = await asyncio.to_thread(detect_and_configure, adapter.listen_port)
            if snap:
                system_proxy_info = SystemProxyInfo(
                    configured=True,
                    interface=snap.interface,
                    original_state="enabled" if snap.http_proxy_enabled else "disabled",
                )
                try:
                    update_state(
                        system_proxy_configured=True,
                        system_proxy_interface=snap.interface,
                        system_proxy_snapshot=snap.to_dict(),
                    )
                except Exception:
                    _proxy_logger.debug("Could not update state file (test mode?)", exc_info=True)
        except Exception:
            _proxy_logger.warning("Failed to configure system proxy", exc_info=True)

    resp = _get_proxy_status(request)
    resp.system_proxy = system_proxy_info
    return resp


@router.post("/stop")
async def stop_proxy(request: Request) -> dict:
    """Stop the mitmproxy network capture and restore system proxy if configured."""
    import asyncio

    from server.lifecycle.state import read_state

    adapter = request.app.state.proxy_adapter
    if adapter is None:
        raise HTTPException(status_code=503, detail="Proxy adapter not configured")

    if not adapter.is_running:
        raise HTTPException(status_code=409, detail="Proxy is not running")

    await adapter.stop()
    try:
        update_state(proxy_status="stopped")
    except Exception:
        _proxy_logger.debug("Could not update state file (test mode?)", exc_info=True)

    # Restore system proxy if we configured it
    restore_info: SystemProxyRestoreInfo | None = None
    try:
        state = read_state()
        if state and state.get("system_proxy_configured"):
            snapshot_data = state.get("system_proxy_snapshot")
            if snapshot_data:
                snap = SystemProxySnapshot.from_dict(snapshot_data)
                await asyncio.to_thread(restore_system_proxy, snap)
                restore_info = SystemProxyRestoreInfo(
                    restored=True,
                    interface=snap.interface,
                    restored_to="enabled" if snap.http_proxy_enabled else "disabled",
                )
            try:
                update_state(
                    system_proxy_configured=False,
                    system_proxy_interface=None,
                    system_proxy_snapshot=None,
                )
            except Exception:
                _proxy_logger.debug("Could not update state file (test mode?)", exc_info=True)
    except Exception:
        _proxy_logger.warning("Failed to restore system proxy", exc_info=True)

    resp = _get_proxy_status(request).model_dump()
    resp["system_proxy_restore"] = restore_info.model_dump() if restore_info else None
    return resp


# ---------------------------------------------------------------------------
# System proxy configuration
# ---------------------------------------------------------------------------


@router.post("/configure-system", response_model=SystemProxyInfo)
async def configure_system(request: Request, body: dict | None = None) -> SystemProxyInfo:
    """Manually configure macOS system proxy to route through mitmproxy."""
    import asyncio

    from server.lifecycle.state import read_state

    adapter = request.app.state.proxy_adapter
    if adapter is None or not adapter.is_running:
        raise HTTPException(status_code=503, detail="Proxy is not running")

    state = read_state()
    if state and state.get("system_proxy_configured"):
        raise HTTPException(status_code=409, detail="System proxy already configured by Quern")

    interface_override = body.get("interface") if body else None

    try:
        snap = await asyncio.to_thread(
            detect_and_configure, adapter.listen_port, interface_override,
        )
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"networksetup failed: {e.stderr}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not snap:
        raise HTTPException(
            status_code=500,
            detail="Could not detect active network interface. "
            "Pass 'interface' in the request body.",
        )

    try:
        update_state(
            system_proxy_configured=True,
            system_proxy_interface=snap.interface,
            system_proxy_snapshot=snap.to_dict(),
        )
    except Exception:
        _proxy_logger.debug("Could not update state file (test mode?)", exc_info=True)

    return SystemProxyInfo(
        configured=True,
        interface=snap.interface,
        original_state="enabled" if snap.http_proxy_enabled else "disabled",
    )


@router.post("/unconfigure-system", response_model=SystemProxyRestoreInfo)
async def unconfigure_system(request: Request) -> SystemProxyRestoreInfo:
    """Restore macOS system proxy to its pre-Quern state."""
    import asyncio

    from server.lifecycle.state import read_state

    state = read_state()
    if not state or not state.get("system_proxy_configured"):
        raise HTTPException(status_code=409, detail="System proxy is not configured by Quern")

    snapshot_data = state.get("system_proxy_snapshot")
    if not snapshot_data:
        raise HTTPException(
            status_code=500,
            detail="No snapshot found — cannot restore. Manually disable system proxy.",
        )

    snap = SystemProxySnapshot.from_dict(snapshot_data)
    try:
        await asyncio.to_thread(restore_system_proxy, snap)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to restore: {e}")

    try:
        update_state(
            system_proxy_configured=False,
            system_proxy_interface=None,
            system_proxy_snapshot=None,
        )
    except Exception:
        _proxy_logger.debug("Could not update state file (test mode?)", exc_info=True)

    return SystemProxyRestoreInfo(
        restored=True,
        interface=snap.interface,
        restored_to="enabled" if snap.http_proxy_enabled else "disabled",
    )


# ---------------------------------------------------------------------------
# Flows — IMPORTANT: /flows/summary MUST come before /flows/{flow_id}
# to avoid FastAPI treating "summary" as a flow_id path parameter.
# ---------------------------------------------------------------------------


@router.get("/flows", response_model=FlowQueryResponse)
async def query_flows(
    request: Request,
    host: str | None = None,
    path_contains: str | None = None,
    method: str | None = None,
    status_min: int | None = None,
    status_max: int | None = None,
    has_error: bool | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    device_id: str = "default",
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> FlowQueryResponse:
    """Query captured HTTP flows with filters and pagination."""
    flow_store = request.app.state.flow_store
    if flow_store is None:
        return FlowQueryResponse(flows=[], total=0, has_more=False)

    params = FlowQueryParams(
        host=host,
        path_contains=path_contains,
        method=method,
        status_min=status_min,
        status_max=status_max,
        has_error=has_error,
        since=since,
        until=until,
        device_id=device_id,
        limit=limit,
        offset=offset,
    )

    flows, total = await flow_store.query(params)
    return FlowQueryResponse(
        flows=flows,
        total=total,
        has_more=(offset + limit) < total,
    )


@router.get("/flows/summary", response_model=FlowSummaryResponse)
async def flow_summary(
    request: Request,
    window: str = Query(default="5m", pattern=r"^(30s|1m|5m|15m|1h)$"),
    host: str | None = None,
    since_cursor: str | None = None,
) -> FlowSummaryResponse:
    """Get an LLM-optimized summary of recent HTTP traffic."""
    flow_store = request.app.state.flow_store
    if flow_store is None:
        return generate_flow_summary([], window=window, host=host)

    now = datetime.now(timezone.utc)

    # Determine time boundary from cursor or window
    if since_cursor:
        since_ts = parse_cursor(since_cursor)
        if since_ts is None:
            raise HTTPException(status_code=400, detail="Invalid cursor")
        flows = await flow_store.get_since(since_ts)
    else:
        duration = WINDOW_DURATIONS.get(window, timedelta(minutes=5))
        since_ts = now - duration
        flows = await flow_store.get_since(since_ts)

    return generate_flow_summary(flows, window=window, host=host)


@router.get("/flows/{flow_id}", response_model=FlowRecord)
async def get_flow(request: Request, flow_id: str) -> FlowRecord:
    """Get full details for a single captured flow."""
    flow_store = request.app.state.flow_store
    if flow_store is None:
        raise HTTPException(status_code=404, detail="Flow store not available")

    flow = await flow_store.get(flow_id)
    if flow is None:
        raise HTTPException(status_code=404, detail=f"Flow {flow_id} not found")
    return flow


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------


@router.post("/filter")
async def set_proxy_filter(request: Request, body: dict) -> dict[str, str]:
    """Set a host filter on the proxy addon."""
    proxy_adapter = request.app.state.proxy_adapter
    if proxy_adapter is None or not proxy_adapter.is_running:
        raise HTTPException(status_code=503, detail="Proxy is not running")

    host = body.get("host")
    if host:
        await proxy_adapter.send_command({"action": "set_filter", "host": host})
        return {"status": "accepted", "filter": host}
    else:
        await proxy_adapter.send_command({"action": "clear_filter"})
        return {"status": "accepted", "filter": "none"}


# ---------------------------------------------------------------------------
# Intercept
# ---------------------------------------------------------------------------


@router.post("/intercept")
async def set_intercept(request: Request, body: InterceptSetRequest) -> dict:
    """Set an intercept pattern. Matching requests will be held."""
    adapter = _require_running_proxy(request)
    await adapter.set_intercept(body.pattern)
    return {
        "status": "accepted",
        "pattern": body.pattern,
        "note": "Matching requests will be held. Use GET /proxy/intercept/held to see them.",
    }


@router.delete("/intercept")
async def clear_intercept(request: Request) -> dict:
    """Clear the intercept pattern and release all held flows."""
    adapter = _require_running_proxy(request)
    count = len(adapter._held_flows)
    await adapter.clear_intercept()
    return {
        "status": "accepted",
        "note": f"Intercept cleared. {count} held flow(s) released.",
    }


@router.get("/intercept/held", response_model=InterceptStatusResponse)
async def list_held_flows(
    request: Request,
    timeout: float = Query(default=0, ge=0, le=60),
) -> InterceptStatusResponse:
    """List currently held flows.

    Supports long-polling: if timeout > 0 and no flows are currently held,
    the server blocks until a flow is intercepted or timeout expires.
    """
    adapter = request.app.state.proxy_adapter

    if adapter is None:
        return InterceptStatusResponse()

    # Long-poll: wait for a held flow if none exist and timeout > 0
    if timeout > 0 and not adapter._held_flows:
        await adapter.wait_for_held(timeout)

    held = adapter.get_held_flows()
    return InterceptStatusResponse(
        pattern=adapter._intercept_pattern,
        held_flows=[
            HeldFlow(
                id=h["id"],
                held_at=h["held_at"],
                age_seconds=h["age_seconds"],
                request=h["request"],
            )
            for h in held
        ],
        total_held=len(held),
    )


@router.post("/intercept/release")
async def release_flow(request: Request, body: ReleaseFlowRequest) -> dict:
    """Release a single held flow, optionally with request modifications."""
    adapter = _require_running_proxy(request)

    if body.flow_id not in adapter._held_flows:
        raise HTTPException(status_code=404, detail=f"Flow {body.flow_id} not held")

    await adapter.release_flow(body.flow_id, modifications=body.modifications)
    # Remove from server-side tracking (addon will also emit a released event)
    adapter._held_flows.pop(body.flow_id, None)
    return {"status": "released", "flow_id": body.flow_id}


@router.post("/intercept/release-all")
async def release_all(request: Request) -> dict:
    """Release all currently held flows."""
    adapter = _require_running_proxy(request)
    count = len(adapter._held_flows)
    await adapter.release_all()
    return {"status": "released", "count": count}


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


@router.post("/replay/{flow_id}", response_model=ReplayResponse)
async def replay_flow(
    request: Request,
    flow_id: str,
    body: ReplayRequest | None = None,
) -> ReplayResponse:
    """Replay a captured flow through the proxy."""
    adapter = _require_running_proxy(request)
    flow_store = request.app.state.flow_store
    if flow_store is None:
        raise HTTPException(status_code=404, detail="Flow store not available")

    original = await flow_store.get(flow_id)
    if original is None:
        raise HTTPException(status_code=404, detail=f"Flow {flow_id} not found")

    # Reconstruct the request
    req = original.request
    headers = dict(req.headers)
    request_body = req.body

    if body:
        if body.modify_headers:
            headers.update(body.modify_headers)
        if body.modify_body is not None:
            request_body = body.modify_body

    # Route through the proxy so the replayed flow appears in captures
    proxy_url = f"http://127.0.0.1:{adapter.listen_port}"

    # Use mitmproxy CA cert for TLS if available
    cert_path = Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.pem"
    verify = str(cert_path) if cert_path.is_file() else False

    try:
        async with httpx.AsyncClient(
            proxy=proxy_url,
            verify=verify,
            timeout=30.0,
        ) as client:
            resp = await client.request(
                method=req.method,
                url=req.url,
                headers=headers,
                content=request_body.encode("utf-8") if request_body else None,
            )
        return ReplayResponse(
            status="success",
            original_flow_id=flow_id,
            status_code=resp.status_code,
        )
    except Exception as e:
        return ReplayResponse(
            status="error",
            original_flow_id=flow_id,
            error=str(e),
        )


# ---------------------------------------------------------------------------
# Mocks
# ---------------------------------------------------------------------------


@router.post("/mocks")
async def set_mock(request: Request, body: SetMockRequest) -> dict:
    """Add a mock response rule. Matching requests get a synthetic response."""
    adapter = _require_running_proxy(request)
    rule_id = await adapter.set_mock(
        pattern=body.pattern,
        response=body.response.model_dump(),
    )
    return {"status": "accepted", "rule_id": rule_id, "pattern": body.pattern}


@router.get("/mocks", response_model=MockListResponse)
async def list_mocks(request: Request) -> MockListResponse:
    """List all active mock rules."""
    adapter = request.app.state.proxy_adapter
    if adapter is None:
        return MockListResponse(rules=[], total=0)

    rules = [
        MockRuleInfo(
            rule_id=r["rule_id"],
            pattern=r["pattern"],
            response=MockResponseSpec(),  # Server-side only tracks rule_id + pattern
        )
        for r in adapter._mock_rules
    ]
    return MockListResponse(rules=rules, total=len(rules))


@router.delete("/mocks/{rule_id}")
async def delete_mock(request: Request, rule_id: str) -> dict:
    """Delete a specific mock rule."""
    adapter = _require_running_proxy(request)
    await adapter.clear_mock(rule_id=rule_id)
    return {"status": "deleted", "rule_id": rule_id}


@router.delete("/mocks")
async def delete_all_mocks(request: Request) -> dict:
    """Delete all mock rules."""
    adapter = _require_running_proxy(request)
    count = len(adapter._mock_rules)
    await adapter.clear_mock()
    return {"status": "deleted", "count": count}


# ---------------------------------------------------------------------------
# Certificate & setup guide
# ---------------------------------------------------------------------------


@router.get("/cert")
async def download_cert() -> FileResponse:
    """Download the mitmproxy CA certificate for device installation."""
    cert_path = Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.pem"
    if not cert_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="CA certificate not found. Run mitmproxy once to generate it.",
        )
    return FileResponse(
        path=str(cert_path),
        media_type="application/x-pem-file",
        filename="mitmproxy-ca-cert.pem",
    )


@router.get("/cert/status", response_model=CertStatusResponse)
async def cert_status(request: Request) -> CertStatusResponse:
    """Get certificate installation status for all devices.

    Returns cert file existence, fingerprint, and cached installation state
    for all known devices. Does not perform SQLite verification - use
    POST /cert/verify for ground truth.
    """
    from server.lifecycle.state import read_state

    cert_path = cert_manager.get_cert_path()
    cert_exists = cert_path.exists()

    fingerprint = None
    if cert_exists:
        try:
            fingerprint = cert_manager.get_cert_fingerprint(cert_path)
        except Exception as e:
            _proxy_logger.warning(f"Failed to get cert fingerprint: {e}")

    # Get cached device cert states from state.json
    state = read_state()
    device_certs_dict = state.get("device_certs", {}) if state else {}

    # Convert to DeviceCertState models
    devices = {
        udid: DeviceCertState(**cert_data)
        for udid, cert_data in device_certs_dict.items()
    }

    return CertStatusResponse(
        cert_exists=cert_exists,
        cert_path=str(cert_path),
        fingerprint=fingerprint,
        devices=devices,
    )


@router.post("/cert/verify", response_model=CertVerifyResponse)
async def verify_cert(request: Request, body: CertVerifyRequest) -> CertVerifyResponse:
    """Verify certificate installation via SQLite TrustStore query.

    Always performs ground-truth verification by querying the simulator's
    TrustStore database. Updates state.json cache with results.

    Args:
        body.udid: Specific device UDID. If None, verifies all booted devices.

    Returns:
        Detailed installation status per device with timestamps.
    """
    controller = request.app.state.device_controller
    if controller is None:
        raise HTTPException(status_code=503, detail="Device controller not initialized")

    # Determine which devices to verify
    if body.udid:
        udids = [body.udid]
    else:
        # Verify all booted devices
        try:
            all_devices = await controller.list_devices()
            from server.models import DeviceState

            udids = [d.udid for d in all_devices if d.state == DeviceState.BOOTED]
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to list devices: {e}")

    if not udids:
        raise HTTPException(status_code=400, detail="No booted devices found to verify")

    # Verify each device
    device_statuses = []
    for udid in udids:
        try:
            cert_state = await cert_manager.get_device_cert_state(
                controller, udid, verify=True
            )
            device_statuses.append(
                DeviceCertInstallStatus(
                    udid=udid,
                    name=cert_state.name,
                    cert_installed=cert_state.cert_installed,
                    fingerprint=cert_state.fingerprint,
                    verified_at=cert_state.verified_at or datetime.now(timezone.utc).isoformat(),
                )
            )
        except Exception as e:
            _proxy_logger.warning(f"Failed to verify cert for {udid}: {e}")
            # Continue with other devices rather than failing completely
            device_statuses.append(
                DeviceCertInstallStatus(
                    udid=udid,
                    name="Unknown Device",
                    cert_installed=False,
                    fingerprint=None,
                    verified_at=datetime.now(timezone.utc).isoformat(),
                )
            )

    # All verified successfully if all devices have cert installed
    all_verified = all(status.cert_installed for status in device_statuses)

    return CertVerifyResponse(
        verified=all_verified,
        devices=device_statuses,
    )


@router.post("/cert/install")
async def install_cert(request: Request, body: CertInstallRequest) -> dict:
    """Install mitmproxy CA certificate on simulator(s).

    Idempotent - skips devices that already have the cert installed
    unless force=True.

    Args:
        body.udid: Specific device UDID. If None, installs on all booted devices.
        body.force: Force reinstall even if already present.

    Returns:
        Installation results per device.
    """
    controller = request.app.state.device_controller
    if controller is None:
        raise HTTPException(status_code=503, detail="Device controller not initialized")

    # Determine which devices to install on
    if body.udid:
        udids = [body.udid]
    else:
        # Install on all booted devices
        try:
            all_devices = await controller.list_devices()
            from server.models import DeviceState

            udids = [d.udid for d in all_devices if d.state == DeviceState.BOOTED]
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to list devices: {e}")

    if not udids:
        raise HTTPException(status_code=400, detail="No booted devices found to install on")

    # Install on each device
    results = []
    for udid in udids:
        try:
            was_installed = await cert_manager.install_cert(
                controller, udid, force=body.force
            )
            results.append({
                "udid": udid,
                "status": "installed" if was_installed else "already_installed",
                "success": True,
            })
        except Exception as e:
            _proxy_logger.error(f"Failed to install cert on {udid}: {e}")
            results.append({
                "udid": udid,
                "status": "failed",
                "success": False,
                "error": str(e),
            })

    success_count = sum(1 for r in results if r["success"])
    return {
        "total": len(results),
        "succeeded": success_count,
        "failed": len(results) - success_count,
        "devices": results,
    }


@router.get("/setup-guide")
async def setup_guide(request: Request) -> dict:
    """Get device proxy setup instructions with auto-detected local IP and network interface."""
    from server.lifecycle.state import read_state

    adapter = request.app.state.proxy_adapter
    port = adapter.listen_port if adapter else 9101

    # Auto-detect local IP
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = "127.0.0.1"

    # Auto-detect active network interface name (macOS networksetup name)
    active_interface = _detect_active_interface()

    # Detect VPN and other potential issues
    warnings = _detect_proxy_warnings()

    # Get cert status for booted devices
    cert_status_by_device = {}
    try:
        controller = request.app.state.device_controller
        if controller:
            all_devices = await controller.list_devices()
            from server.models import DeviceState

            booted_devices = [d for d in all_devices if d.state == DeviceState.BOOTED]

            # Load cached cert states
            state = read_state()
            device_certs = state.get("device_certs", {}) if state else {}

            for device in booted_devices:
                cached = device_certs.get(device.udid, {})
                cert_installed = cached.get("cert_installed", False)
                cert_status_by_device[device.udid] = {
                    "name": device.name,
                    "cert_installed": cert_installed,
                    "status_icon": "✓" if cert_installed else "✗",
                }
    except Exception as e:
        _proxy_logger.debug(f"Failed to get cert status: {e}")

    # Build simulator proxy commands with detected or placeholder interface
    iface_display = active_interface or "<interface>"
    set_proxy_cmd = (
        f'networksetup -setwebproxy "{iface_display}" 127.0.0.1 {port} && '
        f'networksetup -setsecurewebproxy "{iface_display}" 127.0.0.1 {port}'
    )
    clear_proxy_cmd = (
        f'networksetup -setwebproxystate "{iface_display}" off && '
        f'networksetup -setsecurewebproxystate "{iface_display}" off'
    )

    simulator_steps = []
    if not active_interface:
        simulator_steps.append(
            "0. Detect your active network interface: "
            "route -n get default | grep interface, then "
            "networksetup -listallhardwareports | grep -B1 <device>"
        )
    simulator_steps.extend([
        "1. If you have multiple network interfaces active (e.g. Wi-Fi + Ethernet), "
        "disable the one you're NOT using to avoid routing ambiguity.",
        f"2. Set proxy on the Mac's active network interface (NOT in simulator settings): "
        f"{set_proxy_cmd}",
        "3. Install the CA certificate into the simulator keychain: "
        "xcrun simctl keychain booted add-root-cert ~/.mitmproxy/mitmproxy-ca-cert.pem",
        "4. Reboot the simulator — it reads the host's proxy settings at boot time, "
        "so the proxy must be set BEFORE the simulator starts.",
        f"5. When done, disable the proxy: {clear_proxy_cmd}",
    ])

    # Add cert status to simulator steps if available
    if cert_status_by_device:
        cert_status_text = "\n".join([
            f"   {status['status_icon']} {status['name']}: "
            f"{'Cert installed' if status['cert_installed'] else 'Cert NOT installed'}"
            for status in cert_status_by_device.values()
        ])
        simulator_steps.insert(0, f"Certificate Status:\n{cert_status_text}")

    return {
        "proxy_host": local_ip,
        "proxy_port": port,
        "active_interface": active_interface,
        "warnings": warnings,
        "cert_install_url": "http://mitm.it",
        "cert_status": cert_status_by_device,
        "steps": [
            {
                "target": "simulator",
                "note": "The iOS Simulator inherits the Mac's network proxy settings. "
                        "You must configure the proxy on macOS, not inside the simulator.",
                "instructions": simulator_steps,
            },
            {
                "target": "physical_device",
                "note": "Physical devices are configured directly in iOS Settings.",
                "instructions": [
                    "1. Go to Settings > Wi-Fi > (your network) > Configure Proxy > Manual",
                    f"2. Set Server: {local_ip}, Port: {port}",
                    "3. Open Safari and go to http://mitm.it to download the CA certificate",
                    "4. Install the profile in Settings > General > VPN & Device Management",
                    "5. Trust the certificate in Settings > General > About > "
                    "Certificate Trust Settings",
                ],
            },
        ],
    }


def _detect_active_interface() -> str | None:
    """Delegate to system_proxy module (kept for local references)."""
    return detect_active_interface()


def _detect_proxy_warnings() -> list[str]:
    """Detect VPNs and network conditions that may interfere with mitmproxy.

    Checks:
    1. scutil --nc list — finds NetworkExtension/built-in VPNs with "Connected" status
    2. Default route interface — if it goes through a utun device, a VPN is routing traffic
    """
    warnings: list[str] = []

    # 1. Check scutil --nc list for connected VPNs
    connected_vpns = _get_connected_vpns()
    for vpn_name in connected_vpns:
        warnings.append(
            f"VPN detected: '{vpn_name}' is connected. "
            "This may route traffic around the proxy, causing flows to not appear."
        )

    # 2. Check if the default route goes through a utun interface
    default_device = get_default_route_device()
    if default_device and default_device.startswith("utun"):
        if not connected_vpns:
            # Only add this if we didn't already flag via scutil (avoid redundancy)
            warnings.append(
                f"Default route uses tunnel interface ({default_device}), "
                "which suggests a VPN is active. Traffic may bypass the proxy."
            )
        warnings.append(
            "Consider disconnecting the VPN before starting the proxy, "
            "or configure split tunneling to exclude the target traffic."
        )

    return warnings


def _get_connected_vpns() -> list[str]:
    """Parse `scutil --nc list` for VPN configurations with Connected status."""
    try:
        result = subprocess.run(
            ["scutil", "--nc", "list"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return []

        vpns: list[str] = []
        for line in result.stdout.splitlines():
            # Lines look like:
            # * (Connected)      "My VPN" [com.apple.something]
            # * (Disconnected)   "Other VPN" [com.apple.something]
            if "(Connected)" not in line:
                continue
            # Extract the quoted name
            start = line.find('"')
            end = line.find('"', start + 1) if start != -1 else -1
            if start != -1 and end != -1:
                vpns.append(line[start + 1 : end])
        return vpns
    except Exception:
        return []


def _get_default_route_device() -> str | None:
    """Delegate to system_proxy module (kept for local references)."""
    return get_default_route_device()
