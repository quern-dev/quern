"""API routes for proxy intercept, replay, and mock response management."""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Query, Request

from server.models import (
    FlowRecord,
    HeldFlow,
    InterceptSetRequest,
    InterceptStatusResponse,
    MockListResponse,
    MockResponseSpec,
    MockRuleInfo,
    ReleaseFlowRequest,
    ReplayRequest,
    ReplayResponse,
    SetMockRequest,
    UpdateMockRequest,
)

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/proxy", tags=["proxy"])


def _require_running_proxy(request: Request):
    """Return the proxy adapter, raising 503 if not running."""
    adapter = request.app.state.proxy_adapter
    if adapter is None or not adapter.is_running:
        raise HTTPException(status_code=503, detail="Proxy is not running")
    return adapter


# ---------------------------------------------------------------------------
# Intercept
# ---------------------------------------------------------------------------


@router.post("/intercept")
async def set_intercept(request: Request, body: InterceptSetRequest) -> dict:
    """Set an intercept pattern. Matching requests will be held."""
    adapter = _require_running_proxy(request)
    try:
        await adapter.set_intercept(body.pattern)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
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
    try:
        rule_id = await adapter.set_mock(
            pattern=body.pattern,
            response=body.response.model_dump(),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
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
            response=MockResponseSpec(**r["response"]) if r.get("response") else MockResponseSpec(),
        )
        for r in adapter._mock_rules
    ]
    return MockListResponse(rules=rules, total=len(rules))


@router.patch("/mocks/{rule_id}")
async def update_mock(request: Request, rule_id: str, body: UpdateMockRequest) -> dict:
    """Update an existing mock rule's pattern and/or response."""
    adapter = _require_running_proxy(request)
    if body.pattern is None and body.response is None:
        raise HTTPException(status_code=400, detail="Must provide at least one of 'pattern' or 'response'")
    try:
        rule = await adapter.update_mock(
            rule_id=rule_id,
            pattern=body.pattern,
            response=body.response.model_dump() if body.response else None,
        )
    except ValueError as e:
        detail = str(e)
        status = 404 if "not found" in detail else 400
        raise HTTPException(status_code=status, detail=detail)
    return {"status": "updated", "rule_id": rule_id, "pattern": rule["pattern"], "response": rule["response"]}


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
