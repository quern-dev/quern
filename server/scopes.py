"""What a client key may reach: every route, classified (#254, phase 2).

The owner's key reaches everything. A client key (server/clients.py) reaches
what this table grants, and nothing else -- **closed by default**: a route
missing from `ROUTES` is owner-only, and `tests/test_scopes.py` fails until
every route is in it, so a new route is a decision rather than an accident.

    PUBLIC   no key needed (APIKeyMiddleware's PUBLIC_PATHS)
    VIEW     reads: watching a device, its logs, flows and records
    RESERVE  the session routes: open, reserve, release (`/api/v1/sessions`)
    CONTROL  changes to one device, which a client may make only to a device
             its own session holds (server/device_access.py)
    OWNER    anything Mac-wide: the proxy, capture, the host log, updates,
             landmarks, and anything that reads or writes a Mac path

A view grant never widens into control: VIEW holds only reads (GET, and the
POSTs `device_access.READ_POSTS` lists), and CONTROL only changes, which the
test checks.

How a CONTROL route finds its device decides how it is guarded:

    named     the request names it (`device_access.DEVICE_KEYS`). Naming none
              means every device -- "stop all previews", "clear all crashes" --
              which is Mac-wide, so owner-only.
    resolved  named, or else the caller's active device, which must exist:
              the middleware checks it before the route runs, so a client with
              nothing reserved is refused rather than handed whatever the
              route would pick.
    path-id   addressed by an id in the path (a recording, a mock rule); the
              route checks the device behind the id, and refuses a client one
              with no device.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal
from urllib.parse import parse_qsl, urlencode

from starlette.types import ASGIApp, Receive, Scope, Send

from server import device_access, principal
from server.session import _SESSION, HEADER, parse_session
from server.session_registry import OWNER


class Access(StrEnum):
    PUBLIC = "public"
    VIEW = "view"
    RESERVE = "reserve"
    CONTROL = "control"
    OWNER = "owner"


Device = Literal["named", "resolved", "path-id"] | None


@dataclass(frozen=True)
class Rule:
    access: Access
    device: Device = None


_PUBLIC = Rule(Access.PUBLIC)
_VIEW = Rule(Access.VIEW)
_RESERVE = Rule(Access.RESERVE)
_OWNER = Rule(Access.OWNER)
_NAMED = Rule(Access.CONTROL, "named")
_RESOLVED = Rule(Access.CONTROL, "resolved")
_PATH_ID = Rule(Access.CONTROL, "path-id")

#: (method, path template) -> rule. "WS" for a WebSocket route.
ROUTES: dict[tuple[str, str], Rule] = {
    # -- public, and the API's own documentation --------------------------
    ("GET", "/"): _PUBLIC,
    ("GET", "/health"): _PUBLIC,
    ("GET", "/api/v1/health"): _PUBLIC,
    ("GET", "/tools"): _PUBLIC,
    ("GET", "/docs"): _PUBLIC,
    ("GET", "/redoc"): _PUBLIC,
    ("GET", "/openapi.json"): _PUBLIC,
    ("GET", "/api/v1/proxy/cert"): _PUBLIC,
    ("GET", "/docs/oauth2-redirect"): _OWNER,

    # -- sessions and reservations ------------------------------------------
    ("GET", "/api/v1/sessions"): _RESERVE,
    ("POST", "/api/v1/sessions"): _RESERVE,
    ("GET", "/api/v1/sessions/{session_id}"): _RESERVE,
    ("DELETE", "/api/v1/sessions/{session_id}"): _RESERVE,
    ("POST", "/api/v1/sessions/{session_id}/reservations"): _RESERVE,
    ("DELETE", "/api/v1/sessions/{session_id}/reservations"): _RESERVE,
    ("WS", "/api/v1/sessions/{session_id}/live"): _RESERVE,

    # -- devices: reading -----------------------------------------------------
    ("GET", "/api/v1/device/list"): _VIEW,
    ("GET", "/api/v1/device/app/list"): _VIEW,
    ("GET", "/api/v1/device/app/state/list"): _VIEW,
    ("GET", "/api/v1/device/app/state/plist"): _VIEW,
    ("GET", "/api/v1/device/app/state/plist/diff"): _VIEW,
    ("GET", "/api/v1/device/app/state/plist/watch/config"): _VIEW,
    ("GET", "/api/v1/device/build-progress"): _VIEW,
    ("GET", "/api/v1/device/preview/devices"): _VIEW,
    ("GET", "/api/v1/device/preview/status"): _VIEW,
    ("GET", "/api/v1/device/screen-summary"): _VIEW,
    ("GET", "/api/v1/device/screenshot"): _VIEW,
    ("GET", "/api/v1/device/screenshot/annotated"): _VIEW,
    ("GET", "/api/v1/device/screenshot/timeline"): _VIEW,
    ("GET", "/api/v1/device/settings"): _VIEW,
    ("GET", "/api/v1/device/tools/sites"): _VIEW,
    ("GET", "/api/v1/device/ui"): _VIEW,
    ("GET", "/api/v1/device/ui/element"): _VIEW,
    ("GET", "/api/v1/device/video"): _VIEW,
    ("POST", "/api/v1/device/ui/wait-for-element"): _VIEW,
    ("POST", "/api/v1/device/ui/wait-settled"): _VIEW,
    ("POST", "/api/v1/device/preview/start"): _VIEW,
    ("POST", "/api/v1/device/logging/start"): _VIEW,
    ("POST", "/api/v1/device/logging/device/start"): _VIEW,

    # -- devices: changing one ------------------------------------------------
    ("POST", "/api/v1/device/active"): _NAMED,
    ("POST", "/api/v1/device/app/install"): _RESOLVED,
    ("POST", "/api/v1/device/app/launch"): _RESOLVED,
    ("POST", "/api/v1/device/app/terminate"): _RESOLVED,
    ("POST", "/api/v1/device/app/uninstall"): _RESOLVED,
    ("POST", "/api/v1/device/app/state/plist"): _RESOLVED,
    ("POST", "/api/v1/device/app/state/plist/batch"): _RESOLVED,
    ("DELETE", "/api/v1/device/app/state/plist/key"): _RESOLVED,
    ("POST", "/api/v1/device/app/state/plist/watch/start"): _RESOLVED,
    ("POST", "/api/v1/device/app/state/plist/watch/stop"): _RESOLVED,
    ("POST", "/api/v1/device/app/state/restore"): _RESOLVED,
    ("POST", "/api/v1/device/app/state/save"): _RESOLVED,
    ("POST", "/api/v1/device/boot"): _RESOLVED,
    ("POST", "/api/v1/device/build-and-install"): _RESOLVED,
    ("POST", "/api/v1/device/display-density"): _RESOLVED,
    ("POST", "/api/v1/device/erase"): _RESOLVED,
    ("POST", "/api/v1/device/font-scale"): _RESOLVED,
    ("POST", "/api/v1/device/keyboard"): _RESOLVED,
    ("POST", "/api/v1/device/locale"): _RESOLVED,
    ("POST", "/api/v1/device/location"): _RESOLVED,
    ("POST", "/api/v1/device/logging/stop"): _RESOLVED,
    ("POST", "/api/v1/device/logging/device/stop"): _RESOLVED,
    ("POST", "/api/v1/device/open-url"): _RESOLVED,
    ("POST", "/api/v1/device/permission"): _RESOLVED,
    ("POST", "/api/v1/device/preview/stop"): _NAMED,         # none named: every preview
    ("POST", "/api/v1/device/settings"): _RESOLVED,
    ("POST", "/api/v1/device/shutdown"): _RESOLVED,
    ("POST", "/api/v1/device/ui/clear"): _RESOLVED,
    ("POST", "/api/v1/device/ui/gesture"): _RESOLVED,
    ("POST", "/api/v1/device/ui/press"): _RESOLVED,
    ("POST", "/api/v1/device/ui/restore-input"): _RESOLVED,
    ("POST", "/api/v1/device/ui/scroll-to-element"): _RESOLVED,
    ("POST", "/api/v1/device/ui/swipe"): _RESOLVED,
    ("POST", "/api/v1/device/ui/tap"): _RESOLVED,
    ("POST", "/api/v1/device/ui/tap-element"): _RESOLVED,
    ("POST", "/api/v1/device/ui/type"): _RESOLVED,
    ("POST", "/api/v1/device/ui/web-content"): _RESOLVED,
    ("POST", "/api/v1/device/wda/setup"): _RESOLVED,
    ("POST", "/api/v1/device/wda/start"): _RESOLVED,
    ("POST", "/api/v1/device/wda/stop"): _RESOLVED,
    ("POST", "/api/v1/crashes/device/clear"): _NAMED,
    ("POST", "/api/v1/proxy/device-proxy-config"): _NAMED,
    ("POST", "/api/v1/proxy/cert/install"): _NAMED,          # none named: every booted device
    ("POST", "/api/v1/proxy/cert/verify"): _NAMED,
    ("POST", "/api/v1/proxy/capture/start"): _NAMED,         # none named: all traffic
    ("POST", "/api/v1/proxy/capture/stop"): _PATH_ID,        # by session id in the body
    ("POST", "/api/v1/proxy/mocks"): _NAMED,                 # unscoped: every device's traffic
    ("PATCH", "/api/v1/proxy/mocks/{rule_id}"): _PATH_ID,
    ("DELETE", "/api/v1/proxy/mocks/{rule_id}"): _PATH_ID,
    ("POST", "/api/v1/recordings"): _RESOLVED,
    ("POST", "/api/v1/recordings/{recording_id}/stop"): _PATH_ID,
    ("POST", "/api/v1/recordings/{recording_id}/keyframe"): _PATH_ID,

    # -- the pool picks and boots devices; a client reserves instead ----------
    ("POST", "/api/v1/devices/resolve"): _OWNER,
    ("POST", "/api/v1/devices/ensure"): _OWNER,
    ("POST", "/api/v1/devices/refresh"): _OWNER,

    # -- logs, crashes, builds, trace -----------------------------------------
    ("GET", "/api/v1/logs/errors"): _VIEW,
    ("GET", "/api/v1/logs/filter"): _VIEW,
    ("GET", "/api/v1/logs/query"): _VIEW,
    ("GET", "/api/v1/logs/sources"): _VIEW,
    ("GET", "/api/v1/logs/stream"): _VIEW,
    ("GET", "/api/v1/logs/summary"): _VIEW,
    ("POST", "/api/v1/logs/filter"): _OWNER,
    ("POST", "/api/v1/logs/oslog/start"): _OWNER,
    ("POST", "/api/v1/logs/oslog/stop"): _OWNER,
    ("GET", "/api/v1/crashes/latest"): _VIEW,
    ("DELETE", "/api/v1/crashes"): _NAMED,                   # none named: every device's
    ("POST", "/api/v1/builds/parse"): _VIEW,
    ("POST", "/api/v1/builds/parse-file"): _OWNER,           # reads a path on the Mac
    ("POST", "/api/v1/builds/android/record"): _OWNER,
    ("GET", "/api/v1/builds/latest"): _OWNER,
    ("GET", "/api/v1/trace"): _VIEW,                         # ?recording=: OWNER_PARAMS

    # -- the proxy -----------------------------------------------------------
    ("GET", "/api/v1/proxy/status"): _VIEW,
    ("GET", "/api/v1/proxy/flows"): _VIEW,
    ("GET", "/api/v1/proxy/flows/pending"): _VIEW,
    ("GET", "/api/v1/proxy/flows/stream"): _VIEW,
    ("GET", "/api/v1/proxy/flows/summary"): _VIEW,
    ("GET", "/api/v1/proxy/flows/{flow_id}"): _VIEW,
    ("POST", "/api/v1/proxy/flows/wait"): _VIEW,
    ("GET", "/api/v1/proxy/mocks"): _VIEW,
    ("GET", "/api/v1/proxy/bypass"): _VIEW,
    ("GET", "/api/v1/proxy/intercept/held"): _VIEW,
    ("GET", "/api/v1/proxy/cert/status"): _VIEW,
    ("DELETE", "/api/v1/proxy/mocks"): _OWNER,
    ("POST", "/api/v1/proxy/bypass"): _OWNER,
    ("DELETE", "/api/v1/proxy/bypass"): _OWNER,
    ("POST", "/api/v1/proxy/intercept"): _OWNER,
    ("DELETE", "/api/v1/proxy/intercept"): _OWNER,
    ("POST", "/api/v1/proxy/intercept/release"): _OWNER,
    ("POST", "/api/v1/proxy/intercept/release-all"): _OWNER,
    ("POST", "/api/v1/proxy/replay/{flow_id}"): _OWNER,      # a request sent from the Mac
    ("POST", "/api/v1/proxy/configure-system"): _OWNER,
    ("POST", "/api/v1/proxy/unconfigure-system"): _OWNER,
    ("POST", "/api/v1/proxy/local-capture"): _OWNER,
    ("POST", "/api/v1/proxy/filter"): _OWNER,
    ("POST", "/api/v1/proxy/start"): _OWNER,
    ("POST", "/api/v1/proxy/stop"): _OWNER,
    ("GET", "/api/v1/proxy/setup-guide"): _OWNER,

    # -- recordings ------------------------------------------------------------
    ("GET", "/api/v1/recordings"): _VIEW,
    ("GET", "/api/v1/recordings/events"): _OWNER,            # reads any directory on the Mac
    ("POST", "/api/v1/recordings/{recording_id}/viewer"): _OWNER,
    ("DELETE", "/api/v1/recordings/{recording_id}/viewer"): _OWNER,

    # -- Mac-wide state -----------------------------------------------------------
    ("POST", "/api/v1/device/screenshot/timeline/start"): _OWNER,   # one timeline per server
    ("POST", "/api/v1/device/screenshot/timeline/stop"): _OWNER,
    ("DELETE", "/api/v1/device/app/state/{label}"): _OWNER,  # a snapshot on the Mac's disk
    ("POST", "/api/v1/device/app/state/plist/watch/configure"): _OWNER,
    ("DELETE", "/api/v1/device/app/state/plist/watch/configure"): _OWNER,
    ("GET", "/api/v1/landmarks/"): _VIEW,
    ("POST", "/api/v1/landmarks/identify"): _VIEW,
    ("POST", "/api/v1/landmarks/validate"): _VIEW,
    ("POST", "/api/v1/landmarks/load"): _OWNER,
    ("DELETE", "/api/v1/landmarks/"): _OWNER,
    ("GET", "/api/v1/system/channel"): _VIEW,
    ("PUT", "/api/v1/system/channel"): _OWNER,
    ("POST", "/api/v1/system/update"): _OWNER,
    ("GET", "/api/v1/system/update-status"): _OWNER,
}

#: Query parameters only the owner may pass, on routes a client may otherwise
#: use: each names a path on the Mac.
OWNER_PARAMS: dict[tuple[str, str], frozenset[str]] = {
    ("GET", "/api/v1/trace"): frozenset({"recording"}),
}


def _compile(template: str) -> re.Pattern:
    return re.compile("^" + re.sub(r"\\\{[^/]+?\\\}", "[^/]+", re.escape(template)) + "$")


_LITERAL: dict[tuple[str, str], tuple[str, Rule]] = {}
_TEMPLATED: list[tuple[str, re.Pattern, str, Rule]] = []
for (_method, _template), _rule in ROUTES.items():
    if "{" in _template:
        _TEMPLATED.append((_method, _compile(_template), _template, _rule))
    else:
        _LITERAL[(_method, _template)] = (_template, _rule)


def match(method: str, path: str) -> tuple[str, Rule] | None:
    """The template and rule a request falls under, or None (unmapped). A
    literal path wins over a template, as the routes themselves are ordered."""
    method = method.upper()
    if method == "HEAD":
        method = "GET"
    found = _LITERAL.get((method, path))
    if found is not None:
        return found
    for m, pattern, template, rule in _TEMPLATED:
        if m == method and pattern.match(path):
            return template, rule
    return None


def rule_for(method: str, path: str) -> Rule:
    """The rule for a request: unmapped is owner-only, closed by default."""
    found = match(method, path)
    return found[1] if found is not None else _OWNER


_SESSIONS_PREFIX = "/api/v1/sessions/"


def _qualified_path(path: str, who: str) -> str:
    """A session route's path with the session id in the caller's namespace."""
    if not path.startswith(_SESSIONS_PREFIX):
        return path
    rest = path[len(_SESSIONS_PREFIX):]
    sid, sep, tail = rest.partition("/")
    return _SESSIONS_PREFIX + principal.qualify(sid, who) + sep + tail


def _qualified_query(query: bytes, who: str) -> bytes:
    """`session=` in the query string, in the caller's namespace (the trace's
    filter): a client asks about its own sessions by the names it gave them."""
    if b"session=" not in query:
        return query
    pairs = parse_qsl(query.decode("latin-1"), keep_blank_values=True)
    return urlencode([(k, principal.qualify(v, who) if k == "session" else v)
                      for k, v in pairs]).encode("latin-1")


async def _deny(send: Send, status: int, detail) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


async def _close(receive: Receive, send: Send, code: int) -> None:
    message = await receive()
    if message.get("type") == "websocket.connect":
        await send({"type": "websocket.close", "code": code})


#: Close code for a WebSocket a client key may not open.
WS_FORBIDDEN = 4403


class ScopeMiddleware:
    """Hold a non-owner caller to `ROUTES`, and put its session ids in its own
    namespace. Inside APIKeyMiddleware, which decides the principal, and
    outside AccessMiddleware, which checks the devices a request names.

    Pure ASGI, for the reason APIKeyMiddleware is."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        who = principal.current_principal()
        if who == OWNER:
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if scope["type"] == "websocket":
            if rule_for("WS", path).access is not Access.RESERVE:
                await _close(receive, send, WS_FORBIDDEN)
                return
            await self._as_session(who, scope, receive, send)
            return

        method = scope.get("method", "GET")
        rule = rule_for(method, path)
        if rule.access is Access.PUBLIC:
            await self.app(scope, receive, send)
            return
        if principal.label_of(who) is None:
            # Not the owner and not a client: nobody, on a path that needs a
            # key. APIKeyMiddleware refuses these first; this is the backstop.
            await _deny(send, 401, "Invalid or missing API key")
            return
        if rule.access is Access.OWNER:
            await _deny(send, 403, f"{method} {path} is owner-only: a client key cannot use it")
            return
        found = match(method, path)
        forbidden = OWNER_PARAMS.get((method.upper(), found[0])) if found else None
        if forbidden:
            names = {k for k, _ in parse_qsl(scope.get("query_string", b"").decode("latin-1"))}
            if names & forbidden:
                await _deny(send, 403, (f"{', '.join(sorted(names & forbidden))} on {path} "
                                        "names a path on the Mac: owner-only"))
                return
        await self._as_session(who, scope, receive, send, rule=rule)

    async def _as_session(self, who: str, scope: Scope, receive: Receive, send: Send,
                          rule: Rule | None = None) -> None:
        """Run the request as the client's own, qualified session."""
        headers = scope.get("headers") or []
        raw = next((v for k, v in headers if k == HEADER.encode()), None)
        named = parse_session(raw)
        qualified = principal.qualify(named, who) if named else ""
        if not qualified or parse_session(qualified.encode()) != qualified:
            detail = ("a client key must name its session: send X-Quern-Session "
                      "(letters, digits, . _ : @ -; at most "
                      f"{64 - len(principal.label_of(who) or '') - 1} characters)")
            if scope["type"] == "websocket":
                await _close(receive, send, WS_FORBIDDEN)
            else:
                await _deny(send, 400, detail)
            return
        scope = dict(scope)
        new_path = _qualified_path(scope.get("path", ""), who)
        if new_path != scope.get("path"):
            scope["path"] = new_path
            scope["raw_path"] = new_path.encode()
        scope["query_string"] = _qualified_query(scope.get("query_string", b""), who)
        token = _SESSION.set(qualified)
        try:
            if rule is not None and rule.access is Access.CONTROL and rule.device != "path-id":
                body, receive = await device_access._buffer(receive)
                named_devices = device_access._named_devices(scope["query_string"], body)
                if not named_devices:
                    refusal = await self._no_device(scope, rule)
                    if refusal is not None:
                        await _deny(send, *refusal)
                        return
            await self.app(scope, receive, send)
        finally:
            _SESSION.reset(token)

    async def _no_device(self, scope: Scope, rule: Rule) -> tuple[int, object] | None:
        """A control request that names no device: refused when that means
        every device, or when the caller has no device of its own to mean."""
        method, path = scope.get("method", ""), scope.get("path", "")
        if rule.device == "named":
            return 403, (f"{method} {path} without a device acts on every device: "
                         "owner-only. Name a device your session holds.")
        app = scope.get("app")
        controller = getattr(getattr(app, "state", None), "device_controller", None)
        active = getattr(controller, "_active_udid", None) if controller is not None else None
        if not active:
            return 403, ("this session has no device: reserve one first "
                         "(reserve_devices, or POST /api/v1/sessions/{id}/reservations), "
                         "or name a device it holds")
        # Free: refused here. Held by another session: left to the route,
        # whose check() answers 423 naming the holder (or 404, when blind).
        if device_access.why_not(active) == device_access.NOT_RESERVED:
            return 403, (f"this session's active device {active} is not reserved by it; "
                         "reserve it first")
        return None
