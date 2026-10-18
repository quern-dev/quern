"""Held sessions and reservations over HTTP and a WebSocket (#254, phase 1).

    POST   /api/v1/sessions                       open (or locally take over)
    GET    /api/v1/sessions                       list, through the visibility policy
    GET    /api/v1/sessions/{id}                  one
    DELETE /api/v1/sessions/{id}                  release (clean end, or the owner forcing it)
    POST   /api/v1/sessions/{id}/reservations     reserve by udids, count + criteria, or group
    DELETE /api/v1/sessions/{id}/reservations     release some or all devices
    WS     /api/v1/sessions/{id}/live             the held connection

The socket is liveness only: API calls stay ordinary requests carrying
`X-Quern-Session`. A clean close (code 1000) releases at once; anything else
-- a dropped network, a killed process, missed pings -- starts the grace
period. uvicorn's own WebSocket pings detect a dead peer
(`ws_ping_interval`/`ws_ping_timeout`, set in server/main.py).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from server import principal, visibility
from server.api.actions import logged_action
from server.session import current_session, parse_session
from server.session_registry import (
    ON_RELEASE,
    HeldSession,
    SessionError,
    SessionRegistry,
    UnknownSession,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/sessions", tags=["sessions"])

#: Close codes for the live socket, 4000-4999 being the application's.
CLOSE_UNKNOWN = 4404       # no such session: it ended, expired, or the server restarted
CLOSE_BAD_TOKEN = 4403
CLOSE_REPLACED = 4000      # a newer connection for the same session took over
#: The header the resume token travels in on the socket's upgrade request.
TOKEN_HEADER = "x-quern-resume-token"


def _registry(request_or_ws) -> SessionRegistry:
    registry = getattr(request_or_ws.app.state, "sessions", None)
    if registry is None:
        raise HTTPException(503, "sessions are not available on this server")
    return registry


def _sockets(app) -> dict:
    """session id -> (generation, WebSocket) of its current connection."""
    if not hasattr(app.state, "session_sockets"):
        app.state.session_sockets = {}
    return app.state.session_sockets


async def close_socket(app, session_id: str, code: int, reason: str) -> None:
    """Close a session's current connection, if it has one. A release that
    left the socket open left the client thinking it was still held -- live,
    holding nothing, never told (review)."""
    entry = _sockets(app).pop(session_id, None)
    if entry is not None:
        with contextlib.suppress(Exception):
            await entry[1].close(code=code, reason=reason[:120])


def _refused(e: SessionError) -> HTTPException:
    return HTTPException(e.status, str(e))


def _describe(registry: SessionRegistry, session: HeldSession) -> dict:
    out = session.summary()
    remaining = registry.grace_remaining(session)
    out["grace_remaining_s"] = round(remaining, 1) if remaining is not None else None
    return out


class OpenRequest(BaseModel):
    name: str | None = Field(default=None, description=(
        "The session's id. Default: the caller's X-Quern-Session, else a new one. "
        "Locally, the same name takes over a session in its grace period."))


class ReserveRequest(BaseModel):
    udids: list[str] | None = None
    count: int | None = Field(default=None, ge=1, le=64, description=(
        "How many more devices to reserve, not counting ones the session already holds"))
    group: str | None = None
    device_type: str | None = None
    os_version: str | None = None
    name: str | None = None
    device_family: str | None = None
    auto_boot: bool = True
    on_release: str = Field(default="none", description=" or ".join(ON_RELEASE))


@router.post("")
@logged_action("open_session", category="session")
async def open_session(request: Request, body: OpenRequest | None = None) -> dict:
    """Open a session; returns its id and a resume token (shown once)."""
    registry = _registry(request)
    raw = (body.name if body else None) or current_session()
    name = parse_session(raw) if raw else f"s-{secrets.token_hex(4)}"
    # A client's names live in its own namespace (server/principal.py); the
    # header and the path are qualified by ScopeMiddleware, the body here.
    name = principal.qualify(name) if name else name
    if not name or parse_session(name) != name:
        raise HTTPException(400, "name must be a valid session id (letters, digits, . _ : @ -)")
    existed = registry.get(name) is not None
    try:
        # `local`: taking over a session in grace by name, with no resume
        # token, is allowed to the caller's own principal only -- the registry
        # checks that, so a client cannot take over another's session.
        session, token = registry.open(name, principal=visibility.current_caller().principal,
                                       local=True)
    except SessionError as e:
        raise _refused(e) from None
    # `resumed`: an existing session (in grace) was taken back, reservations
    # and all. False is a new session -- for a client that held one before,
    # the server restarted or its grace ran out, so its devices are gone.
    return {"id": session.id, "resume_token": token, "grace_seconds": registry.grace_seconds,
            "resumed": existed,
            "live_path": f"/api/v1/sessions/{session.id}/live",
            "reservations": [r.udid for r in session.reservations.values()]}


@router.get("")
async def list_sessions(request: Request) -> dict:
    registry = _registry(request)
    caller = visibility.current_caller()
    return {"grace_seconds": registry.grace_seconds,
            "sessions": [_describe(registry, s)
                         for s in visibility.visible_sessions(registry.sessions(), caller)]}


@router.get("/{session_id}")
async def get_session(request: Request, session_id: str) -> dict:
    registry = _registry(request)
    session = registry.get(session_id)
    if session is None or not visibility.visible_sessions([session], visibility.current_caller()):
        raise HTTPException(404, f"no held session {session_id}")
    return _describe(registry, session)


@router.delete("/{session_id}")
@logged_action("release_session", category="session")
async def release_session(request: Request, session_id: str) -> dict:
    """End a session and free its devices: the holder finishing, or the owner
    releasing a session that is stuck."""
    registry = _registry(request)
    session = registry.get(session_id)
    caller = visibility.current_caller()
    if session is None or not visibility.visible_sessions([session], caller):
        raise HTTPException(404, f"no held session {session_id}")
    if not caller.is_owner and (caller.session != session_id
                                or session.principal != caller.principal):
        raise HTTPException(403, "only the session or the server's owner may release it")
    registry.release(session_id)
    await close_socket(request.app, session_id, CLOSE_UNKNOWN, "session released")
    actions = await run_release_policies(request.app)
    freed = [r.udid for r in session.reservations.values()]
    # The queue is global: another session's expired policies may run here,
    # and are not this caller's to hear about (review).
    return {"released": session_id, "freed": freed,
            "actions": [a for a in actions if a["udid"] in freed]}


@router.post("/{session_id}/reservations")
@logged_action("reserve_devices", category="session")
async def reserve(request: Request, session_id: str, body: ReserveRequest) -> dict:
    """Reserve devices for a held session: all of them, or none (409)."""
    registry = _registry(request)
    if current_session() != session_id:
        raise HTTPException(403, (f"reserve as the session itself: send X-Quern-Session: "
                                  f"{session_id}"))
    session = registry.get(session_id)
    if session is None:
        raise _refused(UnknownSession(f"no held session {session_id}"))
    if session.principal != visibility.current_caller().principal:
        # The same id under another key: not the session itself (review).
        raise HTTPException(403, f"session {session_id} belongs to another key")
    if session.state != "live":
        raise HTTPException(409, (
            "reservations need a held session: open its live socket "
            f"(/api/v1/sessions/{session_id}/live) first -- the quern MCP and "
            "`quern session run` do this for you"))
    if body.on_release not in ON_RELEASE:
        raise HTTPException(400, f"on_release must be one of {', '.join(ON_RELEASE)}")
    chosen = sum(x is not None for x in (body.udids, body.group)) + (
        body.count is not None and body.group is None)
    if chosen != 1:
        raise HTTPException(400, "give exactly one of udids, count (with criteria), or group "
                                 "(optionally with count)")
    udids, to_boot = await _choose(request, registry, session_id, body)
    if not udids:
        # Nothing more to reserve: the session already holds the whole group.
        return {"session": session_id, "reserved": [], "holding": list(session.reservations)}
    # Claim first, boot after. Booting first let two concurrent requests both
    # boot the one free simulator; the loser's undo then shut down the device
    # the winner had just reserved (review). The claim is synchronous and all
    # or nothing, so a loser never boots anything.
    already = set(session.reservations)
    caller = visibility.current_caller()
    if visibility.blind_for(caller):
        # Another key's device is answered as one that does not exist, as
        # `_choose` answers an unknown udid: the registry's 409 names the
        # holding session, which is the key's label (review).
        foreign = [u for u in udids if (held := registry.holder_of(u)) is not None
                   and held.session.principal != caller.principal]
        if foreign:
            raise HTTPException(404, f"no such device: {', '.join(foreign)}")
    try:
        reservations = registry.reserve(session_id, udids, on_release=body.on_release,
                                        booted=to_boot)
    except SessionError as e:
        raise _refused(e) from None
    added = [u for u in udids if u not in already]
    controller = getattr(request.app.state, "device_controller", None)
    pool = getattr(controller, "_pool", None)
    started: list[str] = []
    try:
        for udid in to_boot:
            await pool._boot_and_wait(udid)
            started.append(udid)
    except Exception as e:  # noqa: BLE001 -- any failed boot withdraws the claim
        # Only what this request added: devices the session held before stay
        # held, and no other session's queued release policy is touched.
        registry.withdraw(session_id, added)
        for udid in started:
            with contextlib.suppress(Exception):
                await controller.shutdown(udid)
        raise HTTPException(502, f"could not boot {to_boot[len(started)]} for the "
                                 f"reservation, so nothing was reserved: {e}") from None
    if len(session.reservations) == 1:
        # One device: it becomes this session's active device, so unqualified
        # calls go to it (the per-session slot, #460).
        controller = getattr(request.app.state, "device_controller", None)
        if controller is not None:
            controller._active_udid = reservations[0].udid
    return {"session": session_id,
            "reserved": [{"udid": r.udid, "on_release": r.on_release,
                          "booted_here": r.booted_here} for r in reservations],
            "holding": list(session.reservations)}


@router.delete("/{session_id}/reservations")
@logged_action("release_devices", category="session")
async def unreserve(request: Request, session_id: str, udid: str | None = None) -> dict:
    registry = _registry(request)
    caller = visibility.current_caller()
    target = registry.get(session_id)
    if not caller.is_owner and (caller.session != session_id or (
            target is not None and target.principal != caller.principal)):
        raise HTTPException(403, "only the session or the server's owner may release its devices")
    try:
        freed = registry.unreserve(session_id, [udid] if udid else None)
    except SessionError as e:
        raise _refused(e) from None
    if freed and caller.session != session_id:
        # Someone else -- the owner -- freed some of this session's devices:
        # tell its holder, who would otherwise go on believing it has them.
        entry = _sockets(request.app).get(session_id)
        if entry is not None:
            # "the server's owner", never the owner's session name: the
            # holder may be a client, and the owner's sessions are not its to
            # know (review).
            by = ("the server's owner" if caller.is_owner
                  else caller.session or "the server's owner")
            with contextlib.suppress(Exception):
                await entry[1].send_json({"event": "released",
                                          "udids": [r.udid for r in freed], "by": by})
    actions = await run_release_policies(request.app)
    freed_ids = [r.udid for r in freed]
    return {"session": session_id, "freed": freed_ids,
            "actions": [a for a in actions if a["udid"] in freed_ids]}


async def _choose(request: Request, registry: SessionRegistry, session_id: str,
                  body: ReserveRequest) -> tuple[list[str], list[str]]:
    """The udids a reservation asks for, and which of them need booting --
    not booted here: the caller claims them first."""
    controller = getattr(request.app.state, "device_controller", None)
    pool = getattr(controller, "_pool", None)
    if body.udids is not None:
        from server.device_access import _canonical

        udids = [_canonical(u) for u in body.udids if u]
        if pool is not None:
            # Only devices that exist: a typo used to be reserved, and reported
            # as success, while the device it meant stayed free (review).
            await pool.refresh()
            known = set(pool._read_state().devices)
            unknown = [u for u in udids if u not in known]
            if unknown:
                raise HTTPException(404, f"no such device: {', '.join(unknown)}")
        return udids, []
    if pool is None:
        raise HTTPException(503, "the device pool is not available")
    from server.models import DeviceState, DeviceType

    await pool.refresh()
    devices = list(pool._read_state().devices.values())
    held = registry.held_udids(except_session=session_id)
    mine = registry.held_udids() - held
    if body.group is not None:
        devices = _group_members(body.group, devices, pool)
        # No count means "the whole group": only the members this session does
        # not hold yet -- holding them all already is success, not a 409 for a
        # shortage that does not exist (CodeRabbit). A count means that many more.
        wanted = body.count or len([d for d in devices if d.udid not in mine])
        if wanted == 0:
            return [], []
    else:
        devices = pool._find_candidates(
            devices, name=body.name, os_version=body.os_version,
            device_type=DeviceType(body.device_type) if body.device_type else None,
            device_family=body.device_family)
        wanted = body.count or 1
    # `count` is how many *more* devices: not ones another session holds, and
    # not ones this session already holds (CodeRabbit: they ranked first, so
    # count=2 with one held gave one new device).
    devices = sorted((d for d in devices
                      if d.udid not in held and d.udid not in mine and d.is_available),
                     key=pool._rank_candidate)
    booted = [d for d in devices if d.state == DeviceState.BOOTED]
    shutdown = [d for d in devices if d.state == DeviceState.SHUTDOWN]
    picked = booted[:wanted]
    to_boot: list[str] = []
    if len(picked) < wanted and body.auto_boot:
        for d in shutdown[: wanted - len(picked)]:
            picked.append(d)
            to_boot.append(d.udid)
    if len(picked) < wanted:
        raise HTTPException(409, (
            f"asked for {wanted} device(s), {len(picked)} free"
            + ("" if body.auto_boot else " and booted (auto_boot is off)")
            # Blind: how many others hold is not this caller's to know.
            + ("" if visibility.blind_for() else f"; {len(held)} held by other sessions")))
    return [d.udid for d in picked], to_boot


def _group_members(group: str, devices: list, pool) -> list:
    """The devices a named group (`device_groups` in config.json) covers: each
    entry a udid, or criteria matched like the pool's."""
    from server.config import read_user_config
    from server.models import DeviceType

    groups = read_user_config().get("device_groups")
    entries = groups.get(group) if isinstance(groups, dict) else None
    if not isinstance(entries, list) or not entries:
        raise HTTPException(404, f"no device group {group!r} in config.json's device_groups")
    members: dict[str, object] = {}
    for entry in entries:
        if isinstance(entry, str):
            members.update({d.udid: d for d in devices if d.udid == entry})
        elif isinstance(entry, dict):
            dtype = entry.get("device_type")
            for d in pool._find_candidates(
                    devices, name=entry.get("name"), os_version=entry.get("os_version"),
                    device_type=DeviceType(dtype) if dtype else None,
                    device_family=entry.get("device_family")):
                members[d.udid] = d
    return list(members.values())


async def _end_client_work(app, registry: SessionRegistry, session: HeldSession) -> list[dict]:
    """A client key's mock rules and recordings on a device end when its hold
    on the device does. Left in place, the next holder's traffic was answered
    by the previous holder's rules -- whose patterns and bodies a blind
    server then showed it -- and the previous holder's recording went on
    filming the next holder's work (review). The owner's are left: the
    owner's work is the owner's to end. Never raises."""
    if principal.label_of(session.principal) is None:
        return []
    actions: list[dict] = []
    owners = getattr(app.state, "mock_owners", {}) or {}
    adapter = getattr(app.state, "proxy_adapter", None)
    manager = getattr(app.state, "recordings", None)
    for udid in session.reservations:
        held = registry.holder_of(udid)
        if held is not None and held.session.principal == session.principal:
            continue                       # still the key's, through another session
        for rule in list(getattr(adapter, "_mock_rules", []) or []):
            if rule.get("simulator_udid") != udid or owners.get(rule.get("rule_id")) \
                    != session.principal:
                continue
            try:
                await adapter.clear_mock(rule_id=rule["rule_id"])
                owners.pop(rule["rule_id"], None)
                actions.append({"udid": udid, "action": "mock_removed", "ok": True,
                                "rule_id": rule["rule_id"]})
            except Exception as e:  # noqa: BLE001 -- reported, never raised
                actions.append({"udid": udid, "action": "mock_removed", "ok": False,
                                "rule_id": rule.get("rule_id"), "error": str(e)})
        for rec in list(manager.list() if manager is not None else []):
            if rec.udid != udid or rec.principal != session.principal \
                    or rec.state not in ("recording", "interrupted"):
                continue
            try:
                await manager.stop(rec.id)
                actions.append({"udid": udid, "action": "recording_stopped", "ok": True,
                                "recording": rec.id})
            except Exception as e:  # noqa: BLE001 -- reported, never raised
                actions.append({"udid": udid, "action": "recording_stopped", "ok": False,
                                "recording": rec.id, "error": str(e)})
    return actions


async def run_release_policies(app) -> list[dict]:
    """Act on released reservations' `on_release`: shut down the simulators a
    session booted for them, when it asked to. Never raises: a release has
    already happened, and a failed shutdown is reported, not undone."""
    registry = getattr(app.state, "sessions", None)
    controller = getattr(app.state, "device_controller", None)
    if registry is None:
        return []
    actions = []
    for session in registry.take_released():
        actions.extend(await _end_client_work(app, registry, session))
        for r in session.reservations.values():
            if r.on_release != "shutdown_booted" or not r.booted_here:
                continue
            # Only if nobody reserved it again meanwhile.
            if registry.holder_of(r.udid) is not None or controller is None:
                continue
            try:
                await controller.shutdown(r.udid)
                actions.append({"udid": r.udid, "action": "shutdown", "ok": True})
            except Exception as e:  # noqa: BLE001 -- reported, never raised
                logger.warning("Could not shut down %s on release: %s", r.udid, e)
                actions.append({"udid": r.udid, "action": "shutdown", "ok": False,
                                "error": str(e)})
    return actions


async def release_revoked(app) -> list[str]:
    """End every session whose client key has been revoked: close its socket
    (4401, as a refused key's handshake is) and free its devices. Requests
    already in flight finish; the next one is refused by APIKeyMiddleware."""
    registry = getattr(app.state, "sessions", None)
    clients = getattr(app.state, "clients", None)
    if registry is None or clients is None:
        return []
    from server.auth import WS_UNAUTHORISED

    ended = []
    for session in registry.sessions():
        label = principal.label_of(session.principal)
        if label is None or clients.is_active(label):
            continue
        registry.release(session.id)
        await close_socket(app, session.id, WS_UNAUTHORISED, "client key revoked")
        logger.info("Session %s released: client key %s was revoked", session.id, label)
        ended.append(session.id)
    return ended


async def sweep(app, interval: float = 1.0) -> None:
    """Release sessions whose grace ran out or whose client key was revoked,
    and run their policies. The lifespan runs this for as long as the server
    does."""
    registry = app.state.sessions
    while True:
        await asyncio.sleep(interval)
        try:
            expired = registry.expire_due()
            for s in expired:
                logger.info("Session %s released: its grace period ran out", s.id)
            await release_revoked(app)
            await run_release_policies(app)
        except Exception:  # noqa: BLE001 -- the sweeper must outlive one bad pass
            logger.exception("Session sweep failed")


@router.websocket("/{session_id}/live")
async def live(websocket: WebSocket, session_id: str) -> None:
    """The held connection. Authenticated by APIKeyMiddleware like any request,
    then by the session's resume token."""
    registry = getattr(websocket.app.state, "sessions", None)
    token = websocket.headers.get(TOKEN_HEADER, "")
    await websocket.accept()
    if registry is None:
        await websocket.close(code=CLOSE_UNKNOWN, reason="sessions are not available")
        return
    try:
        session, generation = registry.attach(session_id, token)
    except UnknownSession as e:
        await websocket.close(code=CLOSE_UNKNOWN, reason=str(e)[:120])
        return
    except SessionError as e:
        await websocket.close(code=CLOSE_BAD_TOKEN, reason=str(e)[:120])
        return
    # The newest connection holds the session; an older one is closed, and its
    # end changes nothing (its generation is no longer current). The slot is
    # taken before any await: popping the old entry, awaiting its close and
    # only then storing let two reconnects at once overwrite each other, and a
    # release in that window find nothing to close (review).
    sockets = _sockets(websocket.app)
    previous = sockets.get(session_id)
    sockets[session_id] = (generation, websocket)
    if previous is not None:
        with contextlib.suppress(Exception):
            await previous[1].close(code=CLOSE_REPLACED, reason="replaced by a newer connection")
    # After that await: is this still the session's current connection, of a
    # session that still exists?
    current = registry.get(session_id)
    if current is None or current.generation != generation:
        if sockets.get(session_id, (None,))[0] == generation:
            sockets.pop(session_id, None)
        with contextlib.suppress(Exception):
            await websocket.close(
                code=CLOSE_UNKNOWN if current is None else CLOSE_REPLACED,
                reason="session released" if current is None else "replaced")
        return
    clean = False
    try:
        await websocket.send_json({"event": "attached", "session": session.id,
                                   "grace_seconds": registry.grace_seconds,
                                   "reservations": list(session.reservations)})
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                clean = message.get("code") == 1000
                break
            registry.touch(session_id)
    except WebSocketDisconnect as e:
        clean = e.code == 1000
    except Exception:  # noqa: BLE001 -- any other end is an abrupt one
        clean = False
    finally:
        sockets = _sockets(websocket.app)
        if sockets.get(session_id, (None,))[0] == generation:
            sockets.pop(session_id, None)
        acted = registry.detach(session_id, generation, clean=clean)
        if acted and clean:
            with contextlib.suppress(Exception):
                await run_release_policies(websocket.app)
        if acted:
            logger.info("Session %s connection ended (%s)", session_id,
                        "clean: released" if clean else
                        f"abrupt: in grace for {registry.grace_seconds}s")
