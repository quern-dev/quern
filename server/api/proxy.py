"""API routes for proxy status, control, system proxy, and flow queries."""

from __future__ import annotations

import logging
import subprocess
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query, Request

from server.lifecycle.state import update_state, detect_current_ssid, detect_host_ip_for_subnet

_proxy_logger = logging.getLogger(__name__)

from server.models import (
    DeviceCertState,
    WifiProxyNetworkConfig,
    FlowQueryParams,
    FlowQueryResponse,
    FlowRecord,
    FlowSummaryResponse,
    ProxyStatusResponse,
    SystemProxyInfo,
    SystemProxyRestoreInfo,
    WaitForFlowRequest,
    WaitForFlowResponse,
)
from server.processing.summarizer import WINDOW_DURATIONS, parse_cursor
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


from server.lifecycle.state import detect_local_ip as _detect_local_ip  # noqa: E402


def _get_proxy_status(request: Request) -> ProxyStatusResponse:
    """Build a ProxyStatusResponse from current app state."""
    from server.lifecycle.state import read_state

    adapter = request.app.state.proxy_adapter
    flow_store = request.app.state.flow_store

    # Read cert setup from persistent cert-state.json and system proxy from state.json
    cert_setup = None
    system_proxy_info: SystemProxyInfo | None = None
    local_ip = _detect_local_ip()
    try:
        from server.proxy.cert_state import read_cert_state, strip_noncanonical_fields
        device_certs = read_cert_state()
        if device_certs:
            current_ssid = detect_current_ssid()
            cert_setup = {}
            for udid, cert_data in device_certs.items():
                configs: dict = cert_data.get("wifi_proxy_configs") or {}

                wifi_proxy_stale = True
                active_network = None
                for ssid, cfg in configs.items():
                    device_client_ip = cfg.get("client_ip")
                    stored_host = cfg.get("proxy_host")
                    if device_client_ip:
                        mac_ip = detect_host_ip_for_subnet(device_client_ip)
                        if mac_ip and mac_ip == stored_host:
                            wifi_proxy_stale = False
                            active_network = ssid
                            break
                    elif ssid == current_ssid and stored_host == local_ip:
                        wifi_proxy_stale = False
                        active_network = ssid
                        break

                # No configs at all means device hasn't been configured — not stale
                if not configs:
                    wifi_proxy_stale = False

                try:
                    entry = DeviceCertState(
                        **{k: v for k, v in cert_data.items() if k not in ("wifi_proxy_configs",)},
                        wifi_proxy_configs={
                            ssid: WifiProxyNetworkConfig(**cfg)
                            for ssid, cfg in configs.items()
                        } if configs else None,
                        wifi_proxy_stale=wifi_proxy_stale,
                        active_wifi_network=active_network,
                    )
                except Exception:
                    _proxy_logger.warning(
                        "cert-state entry for %r has invalid stored fields; "
                        "stripping non-canonical data and rebuilding", udid
                    )
                    strip_noncanonical_fields(udid)
                    canonical = {
                        k: v for k, v in cert_data.items()
                        if k in ("name", "cert_installed", "fingerprint",
                                 "installed_at", "verified_at")
                    }
                    entry = DeviceCertState(
                        **canonical,
                        wifi_proxy_configs={
                            ssid: WifiProxyNetworkConfig(**cfg)
                            for ssid, cfg in configs.items()
                        } if configs else None,
                        wifi_proxy_stale=wifi_proxy_stale,
                        active_wifi_network=active_network,
                    )

                cert_setup[udid] = entry
    except Exception as e:
        _proxy_logger.debug(f"Failed to load cert state: {e}")

    try:
        state = read_state()
        if state:
            if state.get("system_proxy_configured"):
                system_proxy_info = SystemProxyInfo(
                    configured=True,
                    interface=state.get("system_proxy_interface"),
                    original_state="unknown",  # Don't need to reconstruct this
                )
    except Exception as e:
        _proxy_logger.debug(f"Failed to load state: {e}")

    local_capture = getattr(request.app.state, "local_capture_processes", [])

    if adapter is None:
        return ProxyStatusResponse(
            status="stopped",
            local_capture=local_capture,
            local_ip=local_ip,
            cert_setup=cert_setup,
            system_proxy=system_proxy_info,
        )

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
            local_capture=local_capture,
            local_ip=local_ip,
            cert_setup=cert_setup,
            system_proxy=system_proxy_info,
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
            local_capture=local_capture,
            local_ip=local_ip,
            cert_setup=cert_setup,
            system_proxy=system_proxy_info,
        )

    return ProxyStatusResponse(
        status="stopped",
        port=adapter.listen_port,
        listen_host=adapter.listen_host,
        flows_captured=flow_store.size if flow_store else 0,
        local_capture=local_capture,
        local_ip=local_ip,
        cert_setup=cert_setup,
        system_proxy=system_proxy_info,
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

    # Auto-configure system proxy only if explicitly enabled
    want_system_proxy = False
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
    simulator_udid: str | None = None,
    client_ip: str | None = None,
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
        simulator_udid=simulator_udid,
        client_ip=client_ip,
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
    simulator_udid: str | None = None,
    client_ip: str | None = None,
) -> FlowSummaryResponse:
    """Get an LLM-optimized summary of recent HTTP traffic."""
    flow_store = request.app.state.flow_store
    if flow_store is None:
        return generate_flow_summary([], window=window, host=host, simulator_udid=simulator_udid, client_ip=client_ip)

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

    return generate_flow_summary(flows, window=window, host=host, simulator_udid=simulator_udid, client_ip=client_ip)


@router.post("/flows/wait", response_model=WaitForFlowResponse)
async def wait_for_flow(request: Request, body: WaitForFlowRequest) -> WaitForFlowResponse:
    """Block until a flow matching the filters appears, or timeout."""
    import asyncio
    import time

    flow_store = request.app.state.flow_store

    # Default since to now - 5s to catch flows that completed just before the call
    effective_since = body.since or (datetime.now(timezone.utc) - timedelta(seconds=5))

    start = time.monotonic()
    polls = 0

    while True:
        polls += 1

        if flow_store is not None:
            params = FlowQueryParams(
                host=body.host,
                path_contains=body.path_contains,
                method=body.method,
                status_min=body.status_min,
                status_max=body.status_max,
                has_error=body.has_error,
                simulator_udid=body.simulator_udid,
                client_ip=body.client_ip,
                since=effective_since,
                limit=1,
            )
            flows, _ = await flow_store.query(params)
            if flows:
                return WaitForFlowResponse(
                    matched=True,
                    flow=flows[0],
                    elapsed_seconds=round(time.monotonic() - start, 3),
                    polls=polls,
                )

        elapsed = time.monotonic() - start
        if elapsed >= body.timeout:
            return WaitForFlowResponse(
                matched=False,
                elapsed_seconds=round(elapsed, 3),
                polls=polls,
            )

        await asyncio.sleep(body.interval)


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
# Local capture
# ---------------------------------------------------------------------------


@router.post("/local-capture", response_model=ProxyStatusResponse)
async def set_local_capture(request: Request, body: dict) -> ProxyStatusResponse:
    """Set the local capture process list. Restarts the proxy to apply.

    Body: {"processes": ["Metatext", "MobileSafari"]}
    Empty list disables local capture.
    """
    processes = body.get("processes")
    if processes is None:
        raise HTTPException(status_code=400, detail="Missing 'processes' field")
    if not isinstance(processes, list):
        raise HTTPException(status_code=400, detail="'processes' must be a list of strings")
    processes = [str(p) for p in processes if p]

    adapter = request.app.state.proxy_adapter
    if adapter is None:
        raise HTTPException(status_code=503, detail="Proxy adapter not configured")

    # Update app state
    request.app.state.local_capture_processes = processes

    # Persist to config
    from server.config import set_local_capture_processes
    set_local_capture_processes(processes)

    # Restart proxy if running to apply new mode
    was_running = adapter.is_running
    if was_running:
        await adapter.stop()
        adapter.reconfigure(local_capture_processes=processes)
        await adapter.start()
    else:
        adapter.reconfigure(local_capture_processes=processes)

    # Update state file
    try:
        update_state(local_capture=processes)
    except Exception:
        _proxy_logger.debug("Could not update state file", exc_info=True)

    return _get_proxy_status(request)
