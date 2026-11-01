"""Capture apps on a simulator the caller holds, without restarting the proxy (#558).

`POST /proxy/local-capture` sets the Mac-wide list and restarts the main proxy,
so it is the owner's. A client -- or the owner, for work that should not touch
that list -- asks here instead, for names to be captured while it holds a
simulator. Names the main proxy or another session proxy already captures are
served by it; the rest get a session proxy (`server.proxy.session_proxies`).
A client's names are released with its hold on the device.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import APIRouter, HTTPException, Request

from server import device_access, visibility
from server.api.actions import logged_action
from server.config import with_capture_minimum
from server.models import (
    SessionCaptureReleaseRequest,
    SessionCaptureRequest,
    SessionCaptureResponse,
)
from server.proxy import sim_tls
from server.proxy.session_proxies import (
    MAX_NAMES_PER_REQUEST,
    MAX_PROXIES_PER_CLIENT,
    HoldEnded,
    MainProxyStopped,
    SessionCaptureError,
    SessionCaptureLimit,
    SessionProxies,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/proxy", tags=["proxy"])


def _manager(request: Request) -> SessionProxies:
    manager = getattr(request.app.state, "session_proxies", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="Session capture is not available")
    return manager


def _refusal(processes: list[str], *, owner: bool) -> str | None:
    """Why these entries cannot be a session's, or None. Names only, for a
    client: an exclusion (`!x`) captures every *other* process on the Mac, and a
    PID names a process anywhere on it -- neither is an app on a simulator."""
    if not processes:
        return "name at least one process to capture"
    if owner:
        return None
    if len(processes) > MAX_NAMES_PER_REQUEST:
        return (f"{len(processes)} processes in one request; ask for at most "
                f"{MAX_NAMES_PER_REQUEST}")
    for entry in processes:
        if not entry.strip() or entry != entry.strip():
            # " " is a substring of every path with a space in it, and padding
            # is not what any process is called (CodeRabbit).
            return f"{entry!r} is not a process name; name the app's process"
        if entry.startswith("!"):
            return (f"{entry!r} is an exclusion, which captures every other process on "
                    "the Mac; name the processes to capture instead")
        if entry.isdigit():
            return f"{entry!r} is a PID; name the app's process instead"
        if "," in entry or "/" in entry:
            # The list is joined with commas, so "App,4242" would pass a PID;
            # and a path fragment matches far more than one app.
            return f"{entry!r} is not a process name; name the app's process"
    return None


async def _not_a_simulator(request: Request, udid: str) -> str | None:
    """Why `udid` cannot have session capture, when quern knows it is not a
    simulator (#582). Session capture names processes on this Mac, which only
    a simulator's apps are: an Android device's traffic never reaches the
    redirector, so the call was accepted, claimed host processes, reported
    "started", and captured nothing. Unknown devices are left to the checks
    that follow, as before."""
    from server.models import DeviceType

    controller = getattr(request.app.state, "device_controller", None)
    device_type = getattr(controller, "_device_type", None)
    udid = device_access._canonical(udid)
    kind = device_type(udid) if device_type else None
    if kind is None and device_type:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(controller.list_devices(), 5.0)
        kind = device_type(udid)
    if kind in (DeviceType.ANDROID_EMULATOR, DeviceType.ANDROID_DEVICE):
        return (
            f"{udid} is an Android device, and session capture covers simulators: it "
            "names processes on this Mac, which an Android device's apps are not. "
            "Route the device through the proxy instead -- POST "
            "/api/v1/proxy/device-proxy-config with apply=true "
            "(record_device_proxy_config) -- and filter its flows by device_serial. "
            "Its HTTPS is decrypted only if it trusts the CA: see its ca_install.")
    if kind == DeviceType.DEVICE:
        return (
            f"{udid} is a physical iOS device, and session capture covers simulators. "
            "A physical device is captured through its Wi-Fi proxy: see "
            "proxy_setup_guide.")
    return None


@router.post("/session-capture", response_model=SessionCaptureResponse)
@logged_action("add_session_capture", category="proxy")
async def add_session_capture(
    request: Request, body: SessionCaptureRequest,
) -> SessionCaptureResponse:
    """Capture `processes` for work on `simulator_udid`, which the caller must
    hold. Nothing restarts: names already captured are served by the proxy
    capturing them, and the rest get a session proxy."""
    caller = visibility.current_caller()
    processes = [p for p in body.processes if p]
    if reason := _refusal(processes, owner=caller.is_owner):
        raise HTTPException(status_code=400, detail=reason)
    if reason := await _not_a_simulator(request, body.simulator_udid):
        raise HTTPException(status_code=422, detail=reason)
    if reason := device_access.why_not(body.simulator_udid):
        raise HTTPException(status_code=403, detail=(
            f"{reason}. Reserve {body.simulator_udid} first (reserve_devices); its "
            "capture lasts as long as your hold on it."))
    leader = request.app.state.proxy_adapter
    if leader is None or not leader.is_running:
        raise HTTPException(status_code=409, detail=(
            "The proxy is not running, so nothing can be captured. Its owner "
            "starts it (start_proxy)."))

    added: list[str] = []
    if not body.only:
        processes, added = with_capture_minimum(processes)

    manager = _manager(request)
    # The asking session, and which opening of it: its claim on a device the
    # key does not hold ends with it, and must not be committed after it ended
    # (CodeRabbit). Reservations hold canonical ids; so do claims.
    udid = device_access._canonical(body.simulator_udid)
    registry = getattr(request.app.state, "sessions", None)
    asking = registry.get(caller.session) if registry is not None and caller.session else None
    opened = asking.opened_at if asking is not None else None
    # A session id the registry does not hold -- the CLI and scripts send one
    # without holding a session -- is no session to end with: recorded as
    # none, as a caller without one is, not under an id nothing will end.
    claim_session = caller.session if asking is not None else ""

    def still_current() -> bool:
        if device_access.why_not(body.simulator_udid) is not None:
            return False                   # the hold ended
        if asking is None:
            return True                    # no session to end with
        now = registry.get(caller.session)
        return now is not None and now.opened_at == opened

    try:
        result = await manager.claim(
            session=claim_session, principal=caller.principal,
            udid=udid, names=processes,
            max_proxies=None if caller.is_owner else MAX_PROXIES_PER_CLIENT,
            still_held=still_current, opened=opened)
    except SessionCaptureLimit as e:
        raise HTTPException(status_code=429, detail={
            "message": str(e), "limit": MAX_PROXIES_PER_CLIENT,
            "using": e.using}) from e
    except HoldEnded as e:
        raise HTTPException(status_code=409, detail=(
            f"{e}; nothing was captured. Reserve it again to capture on it.")) from e
    except MainProxyStopped as e:
        raise HTTPException(status_code=409, detail=(
            "The proxy stopped, so nothing can be captured. Its owner starts it "
            "(start_proxy).")) from e
    except SessionCaptureError as e:
        raise HTTPException(status_code=500, detail=(
            f"Could not start a proxy for {', '.join(processes)}: {e}")) from e

    # After the claim, not before: the gate checks only while some proxy
    # captures in local mode, and for a server whose own list is empty that is
    # the session proxy just started. A refusal hands back what this call
    # added -- not names an earlier call claimed, which were working (review).
    try:
        check = await sim_tls.ensure_capturable(
            request.app, body.simulator_udid, allow_passthrough=body.allow_passthrough)
    except sim_tls.CaptureNotReady as e:
        if result["added"]:
            # The id the claim is stored under (CodeRabbit).
            await manager.release(principal=caller.principal,
                                  udid=udid, names=result["added"])
        raise HTTPException(status_code=e.status_code, detail=e.detail) from e

    warnings = list(check.warnings)
    if result["confirmed"] is False:
        warnings.append(
            "the new session proxy did not confirm within 5 s that it is capturing "
            "with the proxy's rules applied; traffic may not be captured or mocked yet")
    if result["started"] or result["shared"]:
        warnings.append(
            "local capture takes new connections only: an app already running keeps "
            "the connections it has open uncaptured, so relaunch it to capture "
            "everything it sends")
    return SessionCaptureResponse(
        simulator_udid=body.simulator_udid,
        processes=processes,
        capture_added=added or None,
        main_proxy=result["main_proxy"],
        shared=result["shared"],
        started=result["started"],
        confirmed=result["confirmed"],
        simulator_tls=check.entry,
        warnings=warnings,
    )


@router.delete("/session-capture")
@logged_action("remove_session_capture", category="proxy")
async def remove_session_capture(
    request: Request, body: SessionCaptureReleaseRequest | None = None,
) -> dict:
    """Hand back the caller's session capture -- for one simulator, or only
    some names, when given. A session proxy stops when nobody's capture uses
    it; a name the main proxy also lists passes back to it by itself."""
    caller = visibility.current_caller()
    body = body or SessionCaptureReleaseRequest()
    stopped = await _manager(request).release(
        principal=caller.principal, udid=body.simulator_udid,
        names=body.processes or None)
    return {"stopped": stopped, "claims": _claims(request, caller)}


@router.get("/session-capture")
async def list_session_capture(request: Request) -> dict:
    """The caller's session capture, and the session proxies it uses. The
    owner sees every session's."""
    caller = visibility.current_caller()
    manager = _manager(request)
    principal = None if caller.is_owner else caller.principal
    return {"claims": _claims(request, caller),
            "session_proxies": manager.describe(principal)}


def _claims(request: Request, caller: visibility.Caller) -> list[dict]:
    manager = _manager(request)
    principal = None if caller.is_owner else caller.principal
    return [{"simulator_udid": c.udid, "processes": list(c.names),
             "session": c.session} for c in manager.claims(principal)]
