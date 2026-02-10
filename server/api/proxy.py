"""API routes for network proxy flow inspection."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request

from server.models import FlowQueryParams, FlowQueryResponse, FlowRecord

router = APIRouter(prefix="/api/v1/proxy", tags=["proxy"])


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
