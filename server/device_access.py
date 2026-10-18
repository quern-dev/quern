"""Refusing changes to a device another session holds (#254, phase 1).

A reservation stops other callers *changing* a device; reading it -- a
screenshot, the UI tree, its logs -- stays open (#254 decision 1). Whether a
request changes anything is decided by its method: GET, HEAD and OPTIONS read,
and so do the few POST routes listed in `READ_POSTS`. Everything else is a
change, which errs the safe way for a route nobody classified.

Two places check, and between them every route is covered without a
per-route audit:

- **`AccessMiddleware`**, for a device a request *names*: any of
  `DEVICE_KEYS` in the query string or a JSON body, before the handler runs.
  A test fails if a route grows a device parameter under another name.
- **`check()`**, called from `DeviceController.resolve_udid`, for the device a
  request *doesn't* name -- the active device or the pool's pick.

Callers with no session are refused like any other (#254 decision 4): an
agent on an MCP from before sessions must not tap a device a CI run holds.

A client key's holder (phase 2) may change only a device its own session
holds: a free device is refused too (403, "reserve it first"), where the owner
may change any device nobody holds. Principal decides what a caller sees;
the session decides what it may change.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from server import visibility
from server.models import DeviceError
from server.session_registry import SessionRegistry

logger = logging.getLogger(__name__)

#: Request fields that name a device. Checked in the query string and in a
#: JSON body's top level; `udids` may be a list.
DEVICE_KEYS = frozenset({"udid", "udids", "device_id", "simulator_udid",
                         "device_serial", "serial"})

#: POST routes that only read. Anything not listed, and not a GET, is a change.
#: Starting something that only watches a device -- a preview, a screenshot
#: timeline, log capture -- reads it. A recording is not here: its
#: start may install a CA on the device.
READ_POSTS = frozenset({
    "/api/v1/device/ui/wait-for-element",
    "/api/v1/device/ui/wait-settled",
    "/api/v1/landmarks/identify",
    "/api/v1/landmarks/validate",
    "/api/v1/proxy/flows/wait",
    "/api/v1/builds/parse",
    "/api/v1/builds/parse-file",
    "/api/v1/device/preview/start",
    "/api/v1/device/screenshot/timeline/start",
    "/api/v1/device/logging/start",
    "/api/v1/device/logging/device/start",
})
# Not the matching *stops*: stopping someone's preview, timeline or log
# capture on their device is interfering with them, not watching (review).

#: Routes the middleware leaves to themselves: the session routes decide who
#: may reserve and release, and answer 409 for a device someone else holds --
#: the middleware's 423 used to get there first, and refused the owner's
#: release of a single device (review).
_OWN_RULES = "/api/v1/sessions"

#: UUID-shaped ids: simulators, and iOS devices' hyphenated identifiers. Upper-
#: cased before comparing -- a lowercase spelling of a held simulator passed the
#: check (review). Android serials are case-sensitive and left as they are.
_UUIDISH = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4,16}(-[0-9A-Fa-f]{4}){0,3}(-[0-9A-Fa-f]{12})?")

_REGISTRY: SessionRegistry | None = None
_WRITE: contextvars.ContextVar[bool] = contextvars.ContextVar("quern_write", default=False)
#: Refusals raised while handling the current request. A list the middleware
#: owns and `check()` appends to, so the middleware learns of a refusal however
#: the route handled the exception: 71 routes map DeviceError through their own
#: helpers, some to 500, and 56 have a broad `except Exception`.
_REFUSALS: contextvars.ContextVar[list | None] = contextvars.ContextVar(
    "quern_refusals", default=None)


def install(registry: SessionRegistry | None) -> None:
    """The registry checks consult; None turns enforcement off (tests, CLI)."""
    global _REGISTRY
    _REGISTRY = registry


class DeviceHeld(DeviceError):
    """A change to a device another session holds: HTTP 423.

    A DeviceError, so routes that map device errors still treat it as one;
    the middleware makes the status 423 whatever they mapped it to."""

    status = 423

    def __init__(self, udid: str, detail: dict) -> None:
        super().__init__(detail["message"], tool="reservations")
        self.udid = udid
        self.detail = detail


class DeviceNotFound(DeviceError):
    """Blind mode: another principal's device reads as not there (404)."""

    status = 404

    def __init__(self, message: str) -> None:
        super().__init__(message, tool="reservations")
        self.detail = message


class DeviceNotReserved(DeviceError):
    """A client changing a device its session does not hold: HTTP 403."""

    status = 403

    def __init__(self, udid: str) -> None:
        message = (f"{udid} is not reserved by this session; a client key may change only "
                   "devices it holds -- reserve it first (reserve_devices, or POST "
                   "/api/v1/sessions/{id}/reservations)")
        super().__init__(message, tool="reservations")
        self.udid = udid
        self.detail = {"message": message, "udid": udid}


class OwnerOnly(DeviceError):
    """Something only the server's owner may do: HTTP 403."""

    status = 403

    def __init__(self, what: str) -> None:
        message = f"{what} is owner-only: a client key cannot do it"
        super().__init__(message, tool="scopes")
        self.detail = message


#: Every refusal the middleware answers with its own status and detail.
REFUSALS = (DeviceHeld, DeviceNotFound, DeviceNotReserved, OwnerOnly)


def _refusing(error: DeviceError) -> DeviceError:
    """Record a refusal for the middleware, and return it to raise."""
    refusals = _REFUSALS.get()
    if refusals is not None:
        refusals.append((error.status, error.detail))
    return error


def _canonical(udid: str) -> str:
    udid = udid.strip()
    if _UUIDISH.fullmatch(udid):
        udid = udid.upper()
    try:
        from server.device.ios.devicectl import canonical_device_id

        return canonical_device_id(udid)
    except Exception:  # noqa: BLE001 -- an odd spelling is checked as given
        return udid


def check(udid: str | None) -> None:
    """Refuse a change to `udid` when another session holds it -- and, for a
    caller held to blind mode, a read of a device another principal holds,
    which reads as a device that does not exist (#254, phase 2).

    A no-op outside a request, with enforcement off, or for a device nobody
    holds; and for other reads."""
    if not udid or _REGISTRY is None:
        return
    if not _WRITE.get():
        _check_read(udid)
        return
    held = _REGISTRY.holder_of(_canonical(udid))
    caller = visibility.current_caller()
    if held is None:
        if caller.is_owner:
            return
        raise _refusing(DeviceNotReserved(udid))
    if _is_callers(held, caller):
        return
    detail = visibility.holder_detail(held, caller)
    if detail is None:
        raise _refusing(DeviceNotFound(f"Device {udid} not found"))
    raise _refusing(DeviceHeld(udid, {
        "message": (f"{udid} is reserved by session {detail['held_by']} ({detail['state']}); "
                    "reads still work, changes are refused until it is released"),
        "udid": udid, **detail,
    }))


def _is_callers(held, caller) -> bool:
    """Whether the session holding a device is the caller's own: the same id
    *and* the same principal. The id alone let a client whose namespace an
    owner session's name fell into -- the owner's CI named `alice:nightly` --
    act as that session (review)."""
    return held.session.id == caller.session and held.session.principal == caller.principal


def _check_read(udid: str) -> None:
    """Blind mode: another principal's device is not there to read."""
    caller = visibility.current_caller()
    if not visibility.blind_for(caller):
        return
    held = _REGISTRY.holder_of(_canonical(udid)) if _REGISTRY is not None else None
    if held is not None and held.session.principal != caller.principal:
        raise _refusing(DeviceNotFound(f"Device {udid} not found"))


def require_owner(what: str) -> None:
    """Refuse a non-owner caller something Mac-wide, from inside a route --
    for a change whose reach depends on what it addresses, such as a mock
    rule with no device scope."""
    if not visibility.current_caller().is_owner:
        raise _refusing(OwnerOnly(what))


def refusals_are_per_item() -> None:
    """In a task that handles one of several devices: a refusal stays that
    device's error instead of turning the whole response into a 423. Tasks
    copy the request's context, so their refusals used to land in its list
    and replaced a multi-device result -- hiding the installs that worked
    (review). Affects only the calling task's context."""
    _REFUSALS.set(None)


#: `why_not`'s answers, which callers may compare against.
NOT_RESERVED = "not reserved by this session"
HELD_ELSEWHERE = "reserved by another session"


def why_not(udid: str | None) -> str | None:
    """Why the caller may not change `udid` now, or None if it may: what
    `check()` decides, as a reason rather than a refusal, for work that
    re-checks a device later -- after a build -- or acts behind a read.

    Another session's hold refuses everyone; a free device refuses a client,
    which may change only what its own session holds."""
    if not udid or _REGISTRY is None:
        return None
    held = _REGISTRY.holder_of(_canonical(udid))
    caller = visibility.current_caller()
    if held is None:
        return None if caller.is_owner else NOT_RESERVED
    return None if _is_callers(held, caller) else HELD_ELSEWHERE


def is_write(method: str, path: str) -> bool:
    return method.upper() not in ("GET", "HEAD", "OPTIONS") and path not in READ_POSTS


def _named_devices(query: bytes, body: bytes) -> list[str]:
    from urllib.parse import parse_qsl

    found: list[str] = []
    for key, value in parse_qsl(query.decode("latin-1"), keep_blank_values=False):
        if key in DEVICE_KEYS and value:
            found.extend(v for v in value.split(",") if v)
    # Every body that parses as JSON, whatever its size or its Content-Type
    # claims. Deciding by the header was a way past the check: FastAPI reads
    # `Application/JSON`, or the first of two Content-Type headers, as JSON and
    # parsed the body this skipped (review, reproduced on WDA stop and crash
    # clearing). A size cap was another (first review).
    if body:
        try:
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            data = None
        if isinstance(data, dict):
            for key in DEVICE_KEYS:
                value = data.get(key)
                if isinstance(value, str) and value:
                    found.append(value)
                elif isinstance(value, list):
                    found.extend(v for v in value if isinstance(v, str) and v)
    return found


async def _refuse(send: Send, status: int, detail) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class AccessMiddleware:
    """Classify each request as a read or a change, and refuse a change to a
    device the request names when another session holds it.

    Pure ASGI, for the reason APIKeyMiddleware is: a `BaseHTTPMiddleware`
    hides `http.disconnect` from handlers. The body of a change is read here
    and replayed to the app unchanged."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        write = is_write(scope.get("method", "GET"), path) and not path.startswith(_OWN_RULES)
        # A read is checked too when the caller is blind: naming another
        # principal's device must read as naming one that does not exist.
        guarded = (write or (visibility.blind_for() and not path.startswith(_OWN_RULES)))
        token = _WRITE.set(write)
        refusals: list = []
        refusals_token = _REFUSALS.set(refusals)
        try:
            if guarded and _REGISTRY is not None:
                body, receive = await _buffer(receive)
                try:
                    for udid in _named_devices(scope.get("query_string", b""), body):
                        check(udid)
                except REFUSALS as e:
                    await _refuse(send, e.status, e.detail)
                    return
            if not guarded or _REGISTRY is None:
                await self.app(scope, receive, send)
                return
            started = []

            async def tracking(message: Message) -> None:
                if message["type"] == "http.response.start":
                    started.append(True)
                await send(message)

            try:
                await self.app(scope, receive, _refusal_wins(tracking, refusals))
            except REFUSALS as e:
                # A route that does not catch it at all: the exception would
                # reach Starlette as a 500. Answer with the refusal instead,
                # if nothing has been sent yet (review).
                if started:
                    raise
                await _refuse(send, e.status, e.detail)
        finally:
            _WRITE.reset(token)
            _REFUSALS.reset(refusals_token)


def _refusal_wins(send: Send, refusals: list) -> Send:
    """A send that answers with the recorded refusal, if `check()` refused
    anything while the route ran, in place of whatever the route sent."""
    replaced = False

    async def wrapped(message: Message) -> None:
        nonlocal replaced
        if message["type"] == "http.response.start" and refusals:
            replaced = True
            status, detail = refusals[0]
            await _refuse(send, status, detail)
            return
        if replaced:
            return                              # the route's own body: dropped
        await send(message)

    return wrapped


async def _buffer(receive: Receive) -> tuple[bytes, Receive]:
    """Read the whole request body, and a receive that replays it."""
    chunks: list[bytes] = []
    disconnected: Message | None = None
    while True:
        message = await receive()
        if message["type"] == "http.request":
            chunks.append(message.get("body", b""))
            if not message.get("more_body"):
                break
        else:                                  # http.disconnect before the body ended
            disconnected = message
            break
    body = b"".join(chunks)
    replayed = False

    async def replay() -> Message:
        nonlocal replayed
        if not replayed:
            replayed = True
            if disconnected is not None:
                return disconnected
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return body, replay
