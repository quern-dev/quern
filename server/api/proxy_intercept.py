"""API routes for proxy intercept, replay, and mock response management."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Query, Request

from server import visibility
from server.api.actions import logged_action
from server.models import (
    HeldFlow,
    InterceptRuleInfo,
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


def _intercept_owners(request: Request) -> dict[str, str]:
    """Whose each intercept is, by rule_id (#646): a client key's end with its
    hold, and it may clear and release only its own."""
    if not hasattr(request.app.state, "intercept_owners"):
        request.app.state.intercept_owners = {}
    return request.app.state.intercept_owners


def _is_owner_key() -> bool:
    from server import principal

    return principal.label_of(visibility.current_caller().principal) is None


@router.post("/intercept")
@logged_action("set_intercept", category="proxy")
async def set_intercept(request: Request, body: InterceptSetRequest) -> dict:
    """Add an intercept. Matching requests are held -- only one device's when
    `simulator_udid` is given, every device's otherwise (owner key only, which
    the route's access rule enforces). Several may be set at once (#646)."""
    adapter = _require_running_proxy(request)
    client_ip = await _android_mock_scope(request, body.simulator_udid, "intercept")
    try:
        rule_id = await adapter.set_intercept(
            body.pattern, simulator_udid=body.simulator_udid, client_ip=client_ip)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _intercept_owners(request)[rule_id] = visibility.current_caller().principal
    result = {
        "status": "accepted", "rule_id": rule_id, "pattern": body.pattern,
        "simulator_udid": body.simulator_udid,
        "platform": _scope_platform(body.simulator_udid),
        "note": "Matching requests will be held. Use GET /proxy/intercept/held to see them.",
    }
    warning = await _mock_scope_warning(request, body.simulator_udid, "intercept")
    if warning:
        result["warning"] = warning
    return result


def _check_intercept_scope(adapter, rule: dict) -> None:
    """Refuse to change an intercept scoped to a device another session
    holds, or, from a client key, one that holds every device's requests --
    as for mock rules (#254)."""
    from server import device_access

    if rule.get("simulator_udid"):
        device_access.check(rule["simulator_udid"])
    else:
        device_access.require_owner("changing an intercept with no simulator_udid")


@router.delete("/intercept/{rule_id}")
@logged_action("delete_intercept", category="proxy")
async def delete_intercept(request: Request, rule_id: str) -> dict:
    """Remove one intercept, releasing the flows it held."""
    adapter = _require_running_proxy(request)
    rule = next((r for r in adapter.intercept_rules() if r["rule_id"] == rule_id), None)
    if rule is None:
        # 404, as for a mock: a removal that removed nothing must say so, or
        # teardown cannot tell it left an intercept holding traffic (#182).
        raise HTTPException(status_code=404, detail=f"Intercept not found: {rule_id}")
    _check_intercept_scope(adapter, rule)
    held = sum(1 for h in adapter.get_held_flows() if h.get("rule_id") == rule_id)
    await adapter.clear_intercept(rule_id=rule_id)
    _intercept_owners(request).pop(rule_id, None)
    return {"status": "deleted", "rule_id": rule_id, "held_flows_released": held}


@router.delete("/intercept")
@logged_action("clear_intercept", category="proxy")
async def clear_intercept(request: Request) -> dict:
    """Remove intercepts and release the flows they held. The owner key
    clears every intercept; a client key clears its own (#646)."""
    adapter = _require_running_proxy(request)
    owners = _intercept_owners(request)
    if _is_owner_key():
        count, held = len(adapter.intercept_rules()), adapter.held_count()
        await adapter.clear_intercept()
        owners.clear()
        return {"status": "accepted", "count": count, "held_flows_released": held,
                "note": f"{count} intercept(s) cleared, {held} held flow(s) released."}
    caller = visibility.current_caller().principal
    mine = [r for r in adapter.intercept_rules() if owners.get(r["rule_id"]) == caller]
    held = 0
    for rule in mine:
        _check_intercept_scope(adapter, rule)
        held += sum(1 for h in adapter.get_held_flows() if h.get("rule_id") == rule["rule_id"])
        await adapter.clear_intercept(rule_id=rule["rule_id"])
        owners.pop(rule["rule_id"], None)
    return {"status": "accepted", "count": len(mine), "held_flows_released": held,
            "note": f"{len(mine)} of your intercept(s) cleared, {held} held flow(s) released."}


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
    if timeout > 0 and not adapter.held_count():
        await adapter.wait_for_held(timeout)

    held = adapter.get_held_flows()
    return InterceptStatusResponse(
        pattern=adapter._intercept_pattern,
        intercepts=[InterceptRuleInfo(rule_id=r["rule_id"], pattern=r["pattern"],
                                      simulator_udid=r.get("simulator_udid"))
                    for r in adapter.intercept_rules()],
        held_flows=[
            HeldFlow(
                id=h["id"],
                held_at=h["held_at"],
                age_seconds=h["age_seconds"],
                request=h["request"],
                rule_id=h.get("rule_id"),
            )
            for h in held
        ],
        total_held=len(held),
    )


@router.post("/intercept/release")
@logged_action("release_flow", category="proxy")
async def release_flow(request: Request, body: ReleaseFlowRequest) -> dict:
    """Release a single held flow, optionally with request modifications."""
    adapter = _require_running_proxy(request)

    if not adapter.holds(body.flow_id):
        raise HTTPException(status_code=404, detail=f"Flow {body.flow_id} not held")
    if not _is_owner_key():
        # A client key releases only what its own intercepts held (#646).
        rule_id = adapter.held_by(body.flow_id)
        if _intercept_owners(request).get(rule_id or "") != visibility.current_caller().principal:
            raise HTTPException(status_code=403, detail=(
                f"Flow {body.flow_id} was held by an intercept that is not yours; "
                "only the session that set it, or the owner, may release it."))

    await adapter.release_flow(body.flow_id, modifications=body.modifications)
    # Remove from server-side tracking (addon will also emit a released event)
    adapter.forget_held(body.flow_id)
    return {"status": "released", "flow_id": body.flow_id}


@router.post("/intercept/release-all")
@logged_action("release_all", category="proxy")
async def release_all(request: Request) -> dict:
    """Release all currently held flows."""
    adapter = _require_running_proxy(request)
    count = adapter.held_count()
    await adapter.release_all()
    return {"status": "released", "count": count}


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


@router.post("/replay/{flow_id}", response_model=ReplayResponse)
@logged_action("replay_flow", category="proxy")
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


#: How long the simulator listing behind a scope warning may take. The rule is
#: already live by then, so a slow CoreSimulator must not hang the response --
#: a caller that times out and retries ends up with the rule twice.
_SCOPE_LISTING_TIMEOUT_S = 5.0


async def _android_mock_scope(
    request: Request, scope: str | None, what: str = "mock",
) -> str | None:
    """Check a mock scope that names an Android device, and return how its
    requests are told (#582): None for an emulator, which its QEMU process
    identifies, or a physical phone's Wi-Fi address, which is the peer of
    every connection it makes. A simulator scope, or none, is left as it was.

    Refused (422) when the serial is not an Android device quern knows, and
    when a phone's address cannot be found: either would be a mock that never
    fires, which is the state scope validation exists to refuse."""
    from server.models import DeviceType, is_simulator_scope

    if scope is None or is_simulator_scope(scope):
        return None
    controller = getattr(request.app.state, "device_controller", None)
    if controller is None:
        raise HTTPException(503, "the device controller is not available")
    device_type = getattr(controller, "_device_type", None)
    kind = device_type(scope) if device_type else None
    if kind is None and device_type:
        # A cold cache is not "unknown device": list once, bounded. A listing
        # that did not finish is "could not tell", never "no such device".
        try:
            await asyncio.wait_for(controller.list_devices(), _SCOPE_LISTING_TIMEOUT_S)
        except Exception:
            raise HTTPException(503, (
                f"Could not list devices to check {scope} within "
                f"{_SCOPE_LISTING_TIMEOUT_S:.0f}s. Try again.")) from None
        kind = device_type(scope)
    if kind not in (DeviceType.ANDROID_EMULATOR, DeviceType.ANDROID_DEVICE):
        raise HTTPException(422, (
            f"{scope} is not a simulator UDID or the serial of an Android device "
            f"quern can see (list_devices). A {what} scoped to it would never fire."))
    if controller.adb.is_console_serial(scope):
        return None
    host = scope.rsplit(":", 1)[0] if ":" in scope else ""
    if kind == DeviceType.ANDROID_EMULATOR or host in ("localhost", "127.0.0.1", "::1", "[::1]"):
        # Its traffic is QEMU's, attributed to the console serial; its own
        # address is the emulator's NAT one, which no request carries (review).
        raise HTTPException(422, (
            f"{scope} is an emulator reached over TCP. Its traffic is attributed "
            "to its console serial, emulator-<port>, on the Mac running it: scope "
            f"the {what} to that."))
    ip = await controller.adb.get_lan_ip(scope)
    if not ip:
        from server.proxy.cert_state import read_cert_state_for_device

        record = read_cert_state_for_device(scope) or {}
        # The newest record's: an older network's address may belong to
        # another device by now, and would be mocked as this phone (review).
        recorded = [c for c in (record.get("wifi_proxy_configs") or {}).values()
                    if isinstance(c, dict) and c.get("client_ip")]
        recorded.sort(key=lambda c: str(c.get("set_at") or ""), reverse=True)
        ip = recorded[0]["client_ip"] if recorded else None
    if not ip:
        raise HTTPException(422, (
            f"Could not find {scope}'s network address, which is how its requests "
            "are told apart. Route it through the proxy with device-proxy-config "
            f"(apply=true), which reads and records it, then set the {what}."))
    return ip


def _scope_platform(scope: str | None) -> str | None:
    """Which platform a mock's scope is on, for the MCP's carried record
    (#582): its copies go to the same kind of device on the next server."""
    from server.models import is_simulator_scope

    if scope is None:
        return None
    return "ios" if is_simulator_scope(scope) else "android"


async def _android_scope_warning(request: Request, serial: str, what: str = "mock") -> str | None:
    """Why a mock scoped to an Android device may not fire: it is not routed
    through this proxy, as far as adb can tell."""
    from server.api.proxy_certs import _read_device_proxy

    controller = getattr(request.app.state, "device_controller", None)
    adapter = getattr(request.app.state, "proxy_adapter", None)
    if controller is None:
        return None
    proxy, read = await _read_device_proxy(controller, serial)
    port = getattr(adapter, "listen_port", None)
    if not read:
        return (f"Could not read {serial}'s proxy setting, so whether its traffic "
                f"reaches this proxy is unknown. The {what} applies only if it does.")
    if not proxy or (port and not proxy.endswith(f":{port}")):
        return (f"{serial} is not routed through this proxy (its proxy setting is "
                f"{proxy or 'empty'}), so the {what} cannot see its requests. Apply "
                "device-proxy-config with apply=true first.")
    return None


async def _mock_scope_warning(request: Request, udid: str | None, what: str = "mock") -> str | None:
    """Why a mock scoped to `udid` may not fire, or None if nothing is known.

    A scoped rule fails closed, so a scope that cannot match is a mock that
    never fires and says nothing. Accepted anyway -- the simulator may be
    created, booted or trusted later -- but said on the response, where the
    caller looks. Every reason found is reported, not only the first.
    """
    if udid is None:
        return None
    from server.models import is_simulator_scope

    if not is_simulator_scope(udid):
        return await _android_scope_warning(request, udid, what)
    reasons: list[str] = []

    adapter = getattr(request.app.state, "proxy_adapter", None)
    if adapter is not None and not getattr(adapter, "local_capture", False):
        reasons.append(
            f"Local capture is off. A scoped {what} is decided from the process "
            "that opened the connection; under the system proxy that is looked "
            "up after the connection opens and may not be ready when the "
            f"request arrives, and then the {what} does not apply to it."
        )

    try:
        from server.proxy import sim_tls
        tls = sim_tls.report(request.app) or []
    except Exception:
        _logger.debug("simulator TLS report for a mock scope failed", exc_info=True)
        tls = []
    entry = next((t for t in tls if t.udid.upper() == udid), None)
    if entry is not None and entry.tls == "passed_through":
        reasons.append(
            f"Simulator {udid}'s HTTPS is passed through, not decrypted "
            f"({entry.reason or 'it does not trust the CA'}), so an HTTPS "
            f"request from it cannot be {'mocked' if what == 'mock' else 'held'}. "
            "Install the CA on it first."
        )

    controller = getattr(request.app.state, "device_controller", None)
    try:
        sims = (
            await asyncio.wait_for(controller.simctl.list_devices(), _SCOPE_LISTING_TIMEOUT_S)
            if controller else []
        )
    except Exception:
        # TimeoutError included: the rule is live, and this is only a check.
        _logger.debug("simulator listing for a mock scope failed", exc_info=True)
        sims = []
    if not sims:
        # `simctl.list_devices` returns [] when it cannot parse, so an empty
        # listing is "could not check", never "no such simulator".
        reasons.append(
            f"Could not list this Mac's simulators to confirm {udid} exists. The "
            f"{what} applies only to requests attributed to that simulator."
        )
    else:
        match = next((d for d in sims if d.udid.upper() == udid), None)
        if match is None:
            reasons.append(
                f"No simulator with UDID {udid} in simctl's list, so this {what} "
                "fires only if one exists that it does not list (an XCTest "
                "parallel-testing clone, for example). Check the UDID with "
                "list_devices."
            )
        elif getattr(match.state, "value", match.state) != "booted":
            reasons.append(
                f"Simulator {udid} ({match.name}) is not booted; the {what} "
                "applies once it is."
            )
    return " ".join(reasons) or None


@router.post("/mocks")
@logged_action("set_mock", category="proxy")
async def set_mock(request: Request, body: SetMockRequest) -> dict:
    """Add a mock response rule. Matching requests get a synthetic response."""
    adapter = _require_running_proxy(request)
    client_ip = await _android_mock_scope(request, body.simulator_udid)
    try:
        rule_id = await adapter.set_mock(
            pattern=body.pattern,
            response=body.response.model_dump(),
            simulator_udid=body.simulator_udid,
            client_ip=client_ip,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Whose rule it is (#254): a blind server lists a client's rules to it
    # alone, and a client's rules end with its hold on the device.
    if not hasattr(request.app.state, "mock_owners"):
        request.app.state.mock_owners = {}
    request.app.state.mock_owners[rule_id] = visibility.current_caller().principal
    result = {
        "status": "accepted", "rule_id": rule_id, "pattern": body.pattern,
        "simulator_udid": body.simulator_udid,
        "platform": _scope_platform(body.simulator_udid),
    }
    warning = await _mock_scope_warning(request, body.simulator_udid)
    if warning:
        result["warning"] = warning
    return result


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
            simulator_udid=r.get("simulator_udid"),
            client_ip=r.get("client_ip"),
        )
        for r in adapter._mock_rules
    ]
    # Blind (#254): the caller's own rules on its own devices. Not "every rule
    # on its devices": a previous holder's rule on a device is not the next
    # holder's to read. Unscoped rules are the owner's set-up.
    view = visibility.data_view()
    if not view.everything:
        caller = visibility.current_caller()
        owners = getattr(request.app.state, "mock_owners", {}) or {}
        rules = [r for r in rules if view.allows(r.simulator_udid)
                 and owners.get(r.rule_id) == caller.principal]
    return MockListResponse(rules=rules, total=len(rules))


@router.patch("/mocks/{rule_id}")
@logged_action("update_mock", category="proxy")
async def update_mock(request: Request, rule_id: str, body: UpdateMockRequest) -> dict:
    """Update an existing mock rule's pattern and/or response."""
    adapter = _require_running_proxy(request)
    scope_given = "simulator_udid" in body.model_fields_set
    if body.pattern is None and body.response is None and not scope_given:
        raise HTTPException(
            status_code=400,
            detail="Must provide at least one of 'pattern', 'response' or 'simulator_udid'",
        )
    _check_mock_scope(adapter, rule_id)
    if not any(r.get("rule_id") == rule_id for r in getattr(adapter, "_mock_rules", []) or []):
        raise HTTPException(status_code=404, detail=f"Mock rule not found: {rule_id}")
    if scope_given and not body.simulator_udid:
        # A new scope that names a simulator reaches the access middleware as
        # a device; removing the scope names none, and makes the rule answer
        # every device's traffic: Mac-wide (#254 review).
        from server import device_access

        device_access.require_owner("removing a mock rule's simulator_udid")
    client_ip = await _android_mock_scope(request, body.simulator_udid) if scope_given else None
    try:
        rule = await adapter.update_mock(
            rule_id=rule_id,
            pattern=body.pattern,
            # The fields named, not the spec's defaults for the rest (#574).
            response=body.response.model_dump(exclude_unset=True) if body.response else None,
            simulator_udid=body.simulator_udid,
            scope_given=scope_given,
            client_ip=client_ip,
        )
    except ValueError as e:
        detail = str(e)
        status = 404 if "not found" in detail else 400
        raise HTTPException(status_code=status, detail=detail)
    result = {
        "status": "updated", "rule_id": rule_id,
        "pattern": rule["pattern"], "response": rule["response"],
        "simulator_udid": rule.get("simulator_udid"),
        "platform": _scope_platform(rule.get("simulator_udid")),
    }
    warning = (
        await _mock_scope_warning(request, rule.get("simulator_udid")) if scope_given else None
    )
    if warning:
        result["warning"] = warning
    return result


def _check_mock_scope(adapter, rule_id: str | None = None) -> None:
    """Refuse to change mock rules scoped to a device another session holds
    (#254): the rule is addressed by its id, so no device reaches the access
    middleware, and deleting it would quietly change what the holder's app
    sees (review). `rule_id` None means every rule (a clear-all)."""
    from server import device_access

    for rule in list(getattr(adapter, "_mock_rules", []) or []):
        if rule_id is not None and rule.get("rule_id") != rule_id:
            continue
        if rule.get("simulator_udid"):
            device_access.check(rule["simulator_udid"])
        else:
            # Unscoped, it answers every device's traffic: Mac-wide, so a
            # client key may not change it (#254, phase 2).
            device_access.require_owner("changing a mock rule with no simulator_udid")


@router.delete("/mocks/{rule_id}")
@logged_action("delete_mock", category="proxy")
async def delete_mock(request: Request, rule_id: str) -> dict:
    """Delete a specific mock rule."""
    adapter = _require_running_proxy(request)
    _check_mock_scope(adapter, rule_id)
    removed = await adapter.clear_mock(rule_id=rule_id)
    if not removed:
        # 404, matching PATCH on the same id. Reporting "deleted" for a rule
        # that never existed is a write that asserts something which did not
        # happen -- the failed-check-reads-as-passing convention, applied to
        # a write (#182).
        raise HTTPException(
            status_code=404, detail=f"Mock rule not found: {rule_id}",
        )
    return {"status": "deleted", "rule_id": rule_id}


@router.delete("/mocks")
@logged_action("delete_all_mocks", category="proxy")
async def delete_all_mocks(request: Request) -> dict:
    """Delete all mock rules."""
    adapter = _require_running_proxy(request)
    _check_mock_scope(adapter)
    # Clearing an empty set is legitimately a success here, unlike the
    # single-rule case: the caller asked for "none left", and none are left.
    count = await adapter.clear_mock()
    return {"status": "deleted", "count": count}


# ---------------------------------------------------------------------------
# Bypass (domain allowlist)
# ---------------------------------------------------------------------------


@router.get("/bypass")
async def get_bypass(request: Request) -> dict:
    """List current bypass patterns."""
    adapter = _require_running_proxy(request)
    patterns = adapter.get_bypass_patterns()
    return {"patterns": patterns, "total": len(patterns)}


@router.post("/bypass")
@logged_action("set_bypass", category="proxy")
async def set_bypass(request: Request, body: dict) -> dict:
    """Add bypass patterns. Flows matching these hosts are not captured.

    Body: {"patterns": ["*.apple.com", "*.icloud.com"]}
    Supports glob patterns (fnmatch).
    """
    patterns = body.get("patterns", [])
    if isinstance(patterns, str):
        patterns = [patterns]
    if not patterns:
        raise HTTPException(
            status_code=400,
            detail="Must provide 'patterns' list",
        )
    adapter = _require_running_proxy(request)
    current = await adapter.set_bypass(patterns)
    return {"status": "updated", "patterns": current}


@router.delete("/bypass")
@logged_action("clear_bypass", category="proxy")
async def clear_bypass(
    request: Request,
    patterns: str | None = Query(
        default=None,
        description=(
            "Comma-separated patterns to remove. "
            "Omit to clear all."
        ),
    ),
) -> dict:
    """Remove bypass patterns, or clear all if no patterns specified."""
    adapter = _require_running_proxy(request)
    if patterns:
        to_remove = [p.strip() for p in patterns.split(",")]
        remaining = await adapter.remove_bypass(to_remove)
        return {"status": "updated", "patterns": remaining}
    else:
        await adapter.clear_bypass()
        return {"status": "cleared", "patterns": []}
