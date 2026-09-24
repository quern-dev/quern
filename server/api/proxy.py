"""API routes for proxy status, control, system proxy, and flow queries."""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Query, Request
from sse_starlette.sse import EventSourceResponse

from server.api.actions import logged_action
from server.config import with_capture_minimum
from server.lifecycle.state import (
    detect_current_ssid,
    detect_host_ip_for_subnet,
    enumerate_local_interfaces,
    update_state,
)
from server.models import (
    CaptureStartRequest,
    CaptureStartResponse,
    CaptureStopRequest,
    CaptureStopResponse,
    ConfigureSystemProxyRequest,
    DeviceCertState,
    FlowEvent,
    FlowQueryParams,
    FlowQueryResponse,
    FlowRecord,
    FlowSummaryResponse,
    InterfaceInfo,
    LocalCaptureRequest,
    ProxyStatusResponse,
    StartProxyRequest,
    SystemProxyInfo,
    SystemProxyRestoreInfo,
    WaitForFlowRequest,
    WaitForFlowResponse,
    WifiProxyNetworkConfig,
)
from server.processing.summarizer import WINDOW_DURATIONS, parse_cursor
from server.proxy.summary import generate_flow_summary
from server.proxy.system_proxy import (
    SystemProxySnapshot,
    detect_and_configure,
    restore_system_proxy,
)

_proxy_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/proxy", tags=["proxy"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


from server.lifecycle.state import detect_local_ip as _detect_local_ip  # noqa: E402


async def _get_proxy_status(
    request: Request, *, include_offline: bool = False,
) -> ProxyStatusResponse:
    """Build a ProxyStatusResponse from current app state.

    cert_setup is filtered by default to only include devices currently
    visible to ``list_devices()`` — simulators that have been deleted and
    physical devices that aren't connected (USB or wifi) drop out of the
    routine response. The persisted cert-state.json file is unchanged;
    pass ``include_offline=True`` to see the full historical record.
    """
    from server.lifecycle.state import read_state

    adapter = request.app.state.proxy_adapter
    flow_store = request.app.state.flow_store

    # Resolve the set of currently-visible UDIDs once. If list_devices()
    # fails (simctl unavailable, etc.), fall back to showing everything —
    # filtering should never hide data when device discovery is broken.
    known_udids: set[str] | None = None
    if not include_offline:
        controller = getattr(request.app.state, "device_controller", None)
        if controller is not None:
            try:
                devices = await controller.list_devices()
                known_udids = {d.udid for d in devices}
            except Exception as e:
                _proxy_logger.debug(
                    "list_devices() failed during proxy_status filtering, "
                    "falling back to unfiltered view: %s", e,
                )

    # Read cert setup from persistent cert-state.json and system proxy from state.json
    cert_setup = None
    system_proxy_info: SystemProxyInfo | None = None
    local_ip = _detect_local_ip()
    local_ips = [InterfaceInfo(**entry) for entry in enumerate_local_interfaces()]
    warnings: list[str] = []
    # Flag multi-interface ambiguity: more than one distinct /24 means
    # `local_ip` alone can't tell a caller which IP to point a physical
    # device at. The full list in `local_ips` is the right answer.
    distinct_subnets = {iface.subnet for iface in local_ips if iface.subnet}
    if len(distinct_subnets) >= 2:
        warnings.append("multi_interface_active")
    # The diagnostic path for the cases the preflight cannot reach: an app
    # launched by tapping its icon, or capture enabled before the device booted.
    from server.config import get_auto_install_cert
    from server.proxy.cert_preflight import simulators_without_cert, trust_is_stale

    auto_install_cert = get_auto_install_cert()
    untrusted = await simulators_without_cert(
        getattr(request.app.state, "device_controller", None)
    )
    if untrusted:
        warnings.append("capture_without_cert")
    # Which of those contradict what we recorded. The warning above says capture
    # would fail; this says which device's stored `cert_installed: true` is no
    # longer true, so a reader looking at one device does not have to correlate
    # it with a list somewhere else in the response.
    untrusted_udids = {d["udid"] for d in untrusted}
    try:
        from server.proxy.cert_state import read_cert_state, strip_noncanonical_fields
        device_certs = read_cert_state()
        if device_certs:
            current_ssid = detect_current_ssid()
            cert_setup = {}
            for udid, cert_data in device_certs.items():
                if known_udids is not None and udid not in known_udids:
                    continue
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
                        cert_trust_stale=trust_is_stale(
                            udid, cert_data, untrusted_udids
                        ),
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
                        cert_trust_stale=trust_is_stale(
                            udid, canonical, untrusted_udids
                        ),
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

    # Background network-monitor snapshot — populated by lifespan; None
    # in test apps that don't run lifespan.
    monitor_state = getattr(request.app.state, "network_state", None)
    network_state_dict = monitor_state.as_dict() if monitor_state else None

    if adapter is None:
        return ProxyStatusResponse(
            status="stopped",
            local_capture=local_capture,
            local_ip=local_ip,
            local_ips=local_ips,
            warnings=warnings,
            auto_install_cert=auto_install_cert,
            cert_setup=cert_setup,
            system_proxy=system_proxy_info,
            network_state=network_state_dict,
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
            tls_rejections=list(adapter._tls_rejections),
            mock_rules_count=len(adapter._mock_rules),
            bypass_patterns=adapter.get_bypass_patterns(),
            local_capture=local_capture,
            local_ip=local_ip,
            local_ips=local_ips,
            warnings=warnings,
            auto_install_cert=auto_install_cert,
            cert_setup=cert_setup,
            system_proxy=system_proxy_info,
            network_state=network_state_dict,
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
            tls_rejections=list(adapter._tls_rejections),
            mock_rules_count=len(adapter._mock_rules),
            bypass_patterns=adapter.get_bypass_patterns(),
            local_capture=local_capture,
            local_ip=local_ip,
            local_ips=local_ips,
            warnings=warnings,
            auto_install_cert=auto_install_cert,
            cert_setup=cert_setup,
            system_proxy=system_proxy_info,
            network_state=network_state_dict,
        )

    return ProxyStatusResponse(
        status="stopped",
        port=adapter.listen_port,
        listen_host=adapter.listen_host,
        flows_captured=flow_store.size if flow_store else 0,
        local_capture=local_capture,
        local_ip=local_ip,
        local_ips=local_ips,
        # Reported on the stopped branch too. The others here mirror addon state
        # and are meaningless once the addon is gone; a rejection history is not,
        # and this branch is reachable with data in it when mitmdump died on its
        # own -- which is exactly when there is no other trace to go on.
        tls_rejections=list(adapter._tls_rejections),
        warnings=warnings,
        cert_setup=cert_setup,
        system_proxy=system_proxy_info,
        network_state=network_state_dict,
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
async def proxy_status(
    request: Request,
    include_offline: bool = Query(
        default=False,
        description=(
            "Include cert_setup entries for devices that aren't currently "
            "visible (deleted simulators, offline physical devices). "
            "Default false — the routine response only shows currently "
            "reachable devices. The persisted cert-state.json file always "
            "retains the full history regardless of this flag."
        ),
    ),
) -> ProxyStatusResponse:
    """Get current proxy status and configuration."""
    return await _get_proxy_status(request, include_offline=include_offline)


@router.post("/start", response_model=ProxyStatusResponse)
@logged_action("start_proxy", category="proxy")
async def start_proxy(
    request: Request, body: StartProxyRequest | None = None,
) -> ProxyStatusResponse:
    """Start the mitmproxy network capture.

    Refuses with 428 when `system_proxy` is requested and a booted simulator
    does not trust the CA. Starting the listener alone routes nothing and is
    never refused; `system_proxy` calls the same `detect_and_configure` that
    `configure_system` does, so it is the same routing boundary.
    """
    import asyncio

    adapter = request.app.state.proxy_adapter
    if adapter is None:
        raise HTTPException(status_code=503, detail="Proxy adapter not configured")

    if adapter.is_running:
        raise HTTPException(status_code=409, detail="Proxy is already running")

    want_system_proxy = bool(body.system_proxy) if body else False

    # Before anything is started. The gate raises, and refusing a request after
    # having started the listener would leave a side effect behind on the path
    # that declined to act -- and `configure_system` next door refuses before
    # it touches anything.
    if want_system_proxy:
        await _ensure_ca_is_trusted(
            request, skip=body.skip_cert_check if body else False,
        )

    # Apply optional port/host reconfiguration
    if body:
        adapter.reconfigure(listen_port=body.port, listen_host=body.listen_host)

    await adapter.start()
    try:
        update_state(proxy_status="running")
    except Exception:
        _proxy_logger.debug("Could not update state file (test mode?)", exc_info=True)

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

    resp = await _get_proxy_status(request)
    resp.system_proxy = system_proxy_info
    return resp


@router.post("/stop")
@logged_action("stop_proxy", category="proxy")
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

    resp = (await _get_proxy_status(request)).model_dump()
    resp["system_proxy_restore"] = restore_info.model_dump() if restore_info else None
    return resp


# ---------------------------------------------------------------------------
# System proxy configuration
# ---------------------------------------------------------------------------


async def _ensure_ca_is_trusted(request: Request, *, skip: bool = False) -> None:
    """Refuse to start capture that would silently fail, or fix it if allowed.

    Called from both paths that begin routing a device's traffic through the
    proxy. Capture through a device that does not trust the CA fails every HTTPS
    request, and the symptom -- a blank screen, an app with no network -- points
    nowhere near the proxy. Both halves of that state are ours, so refuse to
    create it rather than let it be discovered later.

    One function rather than the same block in two endpoints. `local_capture`
    had no guard at all, which is how a field report reached exactly this
    failure: a simulator erased mid-session, every HTTPS request failing, and an
    hour spent concluding that staging authentication was down. A second copy
    would be a second place to forget.

    Raises 428 when the user has not opted into automatic installation, and 500
    when they have and it failed -- proceeding anyway would recreate the state
    they opted out of.
    """
    if skip:
        return

    from server.config import get_auto_install_cert
    from server.proxy.cert_preflight import refusal_detail, simulators_without_cert

    controller = getattr(request.app.state, "device_controller", None)
    missing = await simulators_without_cert(controller)
    if not missing:
        return

    if not get_auto_install_cert():
        # 428: the request is fine, the world is not ready for it yet.
        raise HTTPException(status_code=428, detail=refusal_detail(missing))

    from server.proxy.cert_manager import install_cert

    for dev in missing:
        try:
            await install_cert(controller, dev["udid"], device_name=dev["name"])
            _proxy_logger.info(
                "Auto-installed the CA on %s (%s)", dev["name"], dev["udid"][:8],
            )
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=(
                    f"auto_install_cert is set but installing the CA on "
                    f"{dev['name']} failed: {e}"
                ),
            ) from e


@router.post("/configure-system", response_model=SystemProxyInfo)
@logged_action("configure_system", category="proxy")
async def configure_system(
    request: Request, body: ConfigureSystemProxyRequest | None = None,
) -> SystemProxyInfo:
    """Manually configure macOS system proxy to route through mitmproxy."""
    import asyncio

    from server.lifecycle.state import read_state

    adapter = request.app.state.proxy_adapter
    if adapter is None or not adapter.is_running:
        raise HTTPException(status_code=503, detail="Proxy is not running")

    state = read_state()
    if state and state.get("system_proxy_configured"):
        raise HTTPException(status_code=409, detail="System proxy already configured by Quern")

    interface_override = body.interface if body else None
    skip_cert_check = body.skip_cert_check if body else False

    await _ensure_ca_is_trusted(request, skip=skip_cert_check)

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
@logged_action("unconfigure_system", category="proxy")
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
    hosts: list[str] | None = Query(default=None),
    exclude_hosts: list[str] | None = Query(default=None),
    path_contains: str | None = None,
    method: str | None = None,
    status_min: int | None = None,
    status_max: int | None = None,
    has_error: bool | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    device_id: str = "",
    simulator_udid: str | None = None,
    client_ip: str | None = None,
    detail: str = Query(default="full", pattern=r"^(full|summary)$"),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> FlowQueryResponse:
    """Query captured HTTP flows with filters and pagination."""
    flow_store = request.app.state.flow_store
    if flow_store is None:
        return FlowQueryResponse(total=0, has_more=False)

    params = FlowQueryParams(
        host=host,
        hosts=hosts,
        exclude_hosts=exclude_hosts,
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
    has_more = (offset + limit) < total

    if detail == "summary":
        from server.models import FlowSummaryItem

        summaries = [
            FlowSummaryItem(
                id=f.id,
                timestamp=f.timestamp,
                method=f.request.method,
                url=f.request.url,
                host=f.request.host,
                path=f.request.path,
                status_code=f.response.status_code if f.response else None,
                error=f.error,
                total_ms=f.timing.total_ms if f.timing else None,
            )
            for f in flows
        ]
        return FlowQueryResponse(
            flow_summaries=summaries, total=total, has_more=has_more,
        )

    return FlowQueryResponse(flows=flows, total=total, has_more=has_more)


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
        return generate_flow_summary(
            [], window=window, host=host,
            simulator_udid=simulator_udid, client_ip=client_ip,
        )

    now = datetime.now(UTC)

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

    return generate_flow_summary(
        flows, window=window, host=host,
        simulator_udid=simulator_udid, client_ip=client_ip,
    )


@router.get("/flows/stream")
async def stream_flows(
    request: Request,
    host: str | None = None,
    method: str | None = None,
    device_id: str | None = None,
    simulator_udid: str | None = None,
) -> EventSourceResponse:
    """Stream flow events in real time via Server-Sent Events.

    Emits lightweight FlowEvent payloads (no headers or bodies).
    Use GET /proxy/flows/{id} to fetch full details for a flow.
    """
    flow_store = request.app.state.flow_store

    def matches_filter(flow: FlowRecord) -> bool:
        if host and flow.request.host != host:
            return False
        if method and flow.request.method.upper() != method.upper():
            return False
        if device_id and flow.device_id != device_id:
            return False
        if simulator_udid and flow.simulator_udid != simulator_udid:
            return False
        return True

    async def event_generator():
        if flow_store is None:
            yield {
                "event": "error",
                "data": json.dumps({"message": "Proxy not running"}),
            }
            return

        queue = flow_store.subscribe()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    flow = await asyncio.wait_for(
                        queue.get(), timeout=15.0,
                    )
                    if matches_filter(flow):
                        event = FlowEvent.from_flow(flow)
                        yield {
                            "event": "flow",
                            "data": event.model_dump_json(),
                        }
                except TimeoutError:
                    yield {
                        "event": "heartbeat",
                        "data": json.dumps({
                            "time": datetime.now(UTC).isoformat(),
                            "store_size": flow_store.size,
                        }),
                    }
        finally:
            flow_store.unsubscribe(queue)

    return EventSourceResponse(event_generator())


@router.post("/flows/wait", response_model=WaitForFlowResponse)
@logged_action("wait_for_flow", category="proxy")
async def wait_for_flow(request: Request, body: WaitForFlowRequest) -> WaitForFlowResponse:
    """Block until a flow matching the filters appears, or timeout."""
    import asyncio
    import time

    flow_store = request.app.state.flow_store

    # Default since to now - 5s to catch flows that completed just before the call
    effective_since = body.since or (datetime.now(UTC) - timedelta(seconds=5))

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


@router.post("/capture/start", response_model=CaptureStartResponse)
@logged_action("start_capture", category="proxy")
async def start_capture(request: Request, body: CaptureStartRequest) -> CaptureStartResponse:
    """Start a capture session to bracket a UI action and isolate its flows."""
    manager = request.app.state.capture_sessions
    try:
        session = manager.start(body)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return CaptureStartResponse(session_id=session.id, start_time=session.start_time)


@router.post("/capture/stop", response_model=CaptureStopResponse)
@logged_action("stop_capture", category="proxy")
async def stop_capture(request: Request, body: CaptureStopRequest) -> CaptureStopResponse:
    """Stop a capture session and return the flows captured during that window."""
    manager = request.app.state.capture_sessions
    flow_store = request.app.state.flow_store
    if flow_store is None:
        return CaptureStopResponse(
            session_id=body.session_id, duration_seconds=0, total_flows=0,
        )
    try:
        return await manager.stop(body.session_id, flow_store)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


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
@logged_action("set_proxy_filter", category="proxy")
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
@logged_action("set_local_capture", category="proxy")
async def set_local_capture(
    request: Request, body: LocalCaptureRequest,
) -> ProxyStatusResponse:
    """Set the local capture process list. Restarts the proxy to apply.

    Body: {"processes": ["Metatext", "MobileSafari"], "skip_cert_check": false}
    Empty list disables local capture.

    Refuses with 428 when a booted simulator does not trust the mitmproxy CA,
    matching `configure_system`: capturing in that state fails every HTTPS
    request from the device with no indication the proxy is the cause. With
    `auto_install_cert` set, it installs instead of refusing. Disabling capture
    is never refused.
    """
    # FastAPI rejects a missing or non-list `processes` with 422 before this
    # runs; only the empty-string filtering is left to do.
    processes = [p for p in body.processes if p]

    # Widened unless the caller said `only`. Naming an app used to replace the
    # list, silently dropping the process its web traffic actually leaves
    # through -- and the result was zero flows with no error, which reads
    # exactly like an app that made no requests.
    added_defaults: list[str] = []
    if not body.only:
        processes, added_defaults = with_capture_minimum(processes)

    adapter = request.app.state.proxy_adapter
    if adapter is None:
        raise HTTPException(status_code=503, detail="Proxy adapter not configured")

    # The same gate `configure_system` has had. This path had none, and it is
    # the one the field report used: local capture routes a process's traffic
    # through the proxy just as surely, so a device that does not trust the CA
    # fails every HTTPS request with nothing pointing at the proxy.
    #
    # Only when enabling. Clearing the list stops capture, which cannot create
    # the broken state and must never be refused because of it -- that would
    # trap someone in exactly the situation they are trying to leave.
    if processes:
        await _ensure_ca_is_trusted(request, skip=body.skip_cert_check)

    # Say what this replaced. `set` semantics are right -- but they are silent,
    # and the response echoes only the new list, so dropping a process looks
    # identical to adding one. An agent told "capture MobileSafari" will send
    # `["MobileSafari"]` and delete whatever else was being watched without
    # either side noticing. The defaults are the common casualty: they are
    # applied only when nothing is specified, so naming one process removes
    # them and web-view traffic stops being captured.
    previous = list(getattr(request.app.state, "local_capture_processes", []) or [])
    removed = [p for p in previous if p not in processes]
    if removed:
        _proxy_logger.warning(
            "local_capture no longer includes %s (now %s). The web-view "
            "minimum is kept for you; everything else is set rather than "
            "merged, so pass every process you want captured.",
            ", ".join(removed), ", ".join(processes) or "nothing",
        )

    # Update app state
    request.app.state.local_capture_processes = processes

    # Persist to config
    from server.config import set_local_capture_processes
    set_local_capture_processes(processes)

    # `state.json` too, because `quern status` reads it and would otherwise
    # report the list as it was at boot. That matters most for
    # `local_capture_added`: start-up records what *it* added, and leaving
    # that behind after a runtime change lists processes beneath a capture
    # list that no longer contains them -- a stale record presented as
    # current fact, which is the failure this file already warns about for
    # certificate trust. Both fields move together or neither should.
    update_state(
        local_capture=processes,
        local_capture_added=added_defaults or [],
    )

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

    status = await _get_proxy_status(request)
    # Carried on the response, not only in the log. The mistake this guards
    # against -- naming an app and losing its web traffic -- produces zero
    # flows and no error, so the moment of the call is the only place a
    # caller can still connect cause to effect.
    status.capture_added = added_defaults or None
    status.capture_removed = removed or None
    return status
