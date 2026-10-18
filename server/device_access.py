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
"""

from __future__ import annotations

import contextvars
import json
import logging

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from server import visibility
from server.models import DeviceError
from server.session import current_session
from server.session_registry import SessionRegistry

logger = logging.getLogger(__name__)

#: Request fields that name a device. Checked in the query string and in a
#: JSON body's top level; `udids` may be a list.
DEVICE_KEYS = frozenset({"udid", "udids", "device_id", "simulator_udid",
                         "device_serial", "serial"})

#: POST routes that only read. Anything not listed, and not a GET, is a change.
#: Starting or stopping something that only watches a device -- a preview, a
#: screenshot timeline, log capture -- reads it. A recording is not here: its
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
    "/api/v1/device/preview/stop",
    "/api/v1/device/screenshot/timeline/start",
    "/api/v1/device/screenshot/timeline/stop",
    "/api/v1/device/logging/start",
    "/api/v1/device/logging/stop",
    "/api/v1/device/logging/device/start",
    "/api/v1/device/logging/device/stop",
})

#: Bodies larger than this are not parsed for device fields. No device-taking
#: route takes anything near it; a body that size is an upload.
_MAX_BODY = 1_000_000

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


def _canonical(udid: str) -> str:
    try:
        from server.device.ios.devicectl import canonical_device_id

        return canonical_device_id(udid)
    except Exception:  # noqa: BLE001 -- an odd spelling is checked as given
        return udid


def check(udid: str | None) -> None:
    """Refuse a change to `udid` when another session holds it.

    A no-op for reads, outside a request, with enforcement off, or for a
    device nobody holds."""
    if not udid or _REGISTRY is None or not _WRITE.get():
        return
    held = _REGISTRY.holder_of(_canonical(udid))
    if held is None or held.session.id == current_session():
        return
    caller = visibility.current_caller()
    detail = visibility.holder_detail(held, caller)
    refusals = _REFUSALS.get()
    if detail is None:
        error: DeviceError = DeviceNotFound(f"Device {udid} not found")
        if refusals is not None:
            refusals.append((404, str(error)))
        raise error
    error = DeviceHeld(udid, {
        "message": (f"{udid} is reserved by session {detail['held_by']} ({detail['state']}); "
                    "reads still work, changes are refused until it is released"),
        "udid": udid, **detail,
    })
    if refusals is not None:
        refusals.append((423, error.detail))
    raise error


def held_by_another(udid: str | None) -> bool:
    """Whether a session other than the caller's holds `udid`, read or write."""
    if not udid or _REGISTRY is None:
        return False
    held = _REGISTRY.holder_of(_canonical(udid))
    return held is not None and held.session.id != current_session()


def is_write(method: str, path: str) -> bool:
    return method.upper() not in ("GET", "HEAD", "OPTIONS") and path not in READ_POSTS


def _named_devices(query: bytes, body: bytes, content_type: str) -> list[str]:
    from urllib.parse import parse_qsl

    found: list[str] = []
    for key, value in parse_qsl(query.decode("latin-1"), keep_blank_values=False):
        if key in DEVICE_KEYS and value:
            found.extend(v for v in value.split(",") if v)
    if body and len(body) <= _MAX_BODY and "json" in content_type:
        try:
            data = json.loads(body)
        except ValueError:
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
        write = is_write(scope.get("method", "GET"), scope.get("path", ""))
        token = _WRITE.set(write)
        refusals: list = []
        refusals_token = _REFUSALS.set(refusals)
        try:
            if write and _REGISTRY is not None:
                body, receive = await _buffer(receive)
                headers = dict(scope.get("headers") or [])
                content_type = headers.get(b"content-type", b"").decode("latin-1")
                try:
                    for udid in _named_devices(scope.get("query_string", b""), body,
                                               content_type):
                        check(udid)
                except DeviceHeld as e:
                    await _refuse(send, 423, e.detail)
                    return
                except DeviceNotFound as e:
                    await _refuse(send, 404, str(e))
                    return
            if not write or _REGISTRY is None:
                await self.app(scope, receive, send)
                return
            await self.app(scope, receive, _refusal_wins(send, refusals))
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
