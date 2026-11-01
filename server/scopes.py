"""What a client key may reach: every route, classified (#254, phase 2).

The owner's key reaches everything. A client key (server/clients.py) reaches
what this table grants, and nothing else -- **closed by default**: a route
missing from `ROUTES` is owner-only, and `tests/test_scopes.py` fails until
every route is in it, so a new route is a decision rather than an accident.

    PUBLIC   no key needed (APIKeyMiddleware's PUBLIC_PATHS)
    VIEW     reads: watching a device, its logs, flows and records
    RESERVE  the session routes: open, reserve, release (`/api/v1/sessions`),
             and what is the session's own: its capture (#558), its knowledge
             base (#560)
    CONTROL  changes to one device, which a client may make only to a device
             its own session holds (server/device_access.py)
    OWNER    anything Mac-wide: the proxy, capture, the host log, updates,
             server-wide landmark sets, and anything that reads or writes a
             Mac path

A view grant never widens into control: VIEW holds only reads (GET, and the
POSTs `device_access.READ_POSTS` lists), and CONTROL only changes, which the
test checks.

How a CONTROL route finds its device decides how it is guarded:

    named     the request names it, in the field the route scopes by (`keys`:
              `simulator_udid` for a mock rule, not just any device field --
              a decoy `udid` beside an unscoped rule passed the first version,
              review). Naming none means every device -- "stop all previews",
              "clear all crashes", a mock for all traffic -- which is
              Mac-wide, so owner-only.
    resolved  named, or else the caller's active device, which must exist:
              the middleware checks it before the route runs, so a client with
              nothing reserved is refused rather than handed whatever the
              route would pick.
    path-id   addressed by an id in the path (a recording, a mock rule); the
              route checks the device behind the id, and refuses a client one
              with no device.

A rule's `owner_fields` are query or body fields only the owner may set, on a
route a client may otherwise use: each names a path on the Mac (a recording's
output directory, a knowledge base to scan) or changes something every caller
shares (a log preset purges the shared buffer).

Client keys guard against mistakes between cooperating users, not against a
hostile one: a client installing an app on its own simulator runs that app as
the Mac's user, and the simulator sandbox is not a security boundary. Builds
are owner-only for that reason -- a build runs a project's scripts, and a
Gradle build whatever arguments it is given -- but nothing here makes a client
key safe to hand to someone you would not let use the Mac (#254).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal
from urllib.parse import parse_qsl, urlencode

from starlette.types import ASGIApp, Receive, Scope, Send

from server import device_access, principal, visibility
from server.session import _SESSION, HEADER, parse_session
from server.session_registry import OWNER


class Access(StrEnum):
    PUBLIC = "public"
    VIEW = "view"
    RESERVE = "reserve"
    CONTROL = "control"
    OWNER = "owner"


#: "resolved-read" is not written in ROUTES: ScopeMiddleware applies it to a
#: blind client's VIEW "device" reads.
Device = Literal["named", "resolved", "path-id", "resolved-read"] | None


@dataclass(frozen=True)
class Rule:
    access: Access
    device: Device = None
    #: For `named`: the fields the route scopes by, one of which must name a device.
    keys: tuple[str, ...] = ()
    owner_fields: frozenset[str] = frozenset()


def _named(*keys: str, owner_fields: frozenset[str] = frozenset()) -> Rule:
    return Rule(Access.CONTROL, "named", keys or ("udid",), owner_fields)


_PUBLIC = Rule(Access.PUBLIC)
_VIEW = Rule(Access.VIEW)
_RESERVE = Rule(Access.RESERVE)
_OWNER = Rule(Access.OWNER)
_NAMED = _named("udid")
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
    ("POST", "/api/v1/remote/pair/hello"): _PUBLIC,
    ("POST", "/api/v1/remote/pair/finish"): _PUBLIC,
    ("POST", "/api/v1/remote/pairing-codes"): _OWNER,
    ("GET", "/api/v1/remote/status"): _OWNER,
    ("GET", "/docs/oauth2-redirect"): _OWNER,

    # -- sessions and reservations ------------------------------------------
    ("GET", "/api/v1/sessions"): _RESERVE,
    ("POST", "/api/v1/sessions"): _RESERVE,
    ("GET", "/api/v1/sessions/{session_id}"): _RESERVE,
    ("DELETE", "/api/v1/sessions/{session_id}"): _RESERVE,
    ("POST", "/api/v1/sessions/{session_id}/reservations"): _RESERVE,
    ("DELETE", "/api/v1/sessions/{session_id}/reservations"): _RESERVE,
    ("WS", "/api/v1/sessions/{session_id}/live"): _RESERVE,
    # Uploaded apps (#254, phase 3b): any key may send one, kept as its own.
    ("POST", "/api/v1/uploads"): _RESERVE,
    ("GET", "/api/v1/uploads"): _RESERVE,
    ("DELETE", "/api/v1/uploads/{upload_id}"): _RESERVE,

    # -- devices: reading -----------------------------------------------------
    ("GET", "/api/v1/device/list"): _VIEW,
    ("GET", "/api/v1/device/app/list"): _VIEW,
    ("GET", "/api/v1/device/app/performance"): _VIEW,
    ("GET", "/api/v1/device/app/state/list"): _VIEW,
    ("GET", "/api/v1/device/app/state/plist"): _VIEW,
    ("GET", "/api/v1/device/app/state/plist/diff"): _VIEW,
    ("GET", "/api/v1/device/app/state/plist/watch/config"): _VIEW,
    ("GET", "/api/v1/device/build-progress"): _VIEW,
    ("GET", "/api/v1/device/hinge"): _VIEW,
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
    # A preset rewrites the shared ingestion filter and purges the buffer.
    ("POST", "/api/v1/device/logging/start"): Rule(Access.VIEW, owner_fields=frozenset({"preset"})),
    ("POST", "/api/v1/device/logging/device/start"): Rule(
        Access.VIEW, owner_fields=frozenset({"preset"})),

    # -- devices: changing one ------------------------------------------------
    ("POST", "/api/v1/device/active"): _NAMED,
    # A path is the server reading the Mac's disk: a client key installs what
    # it uploaded, by `upload_id` (#254, phase 3b).
    ("POST", "/api/v1/device/app/install"): Rule(Access.CONTROL, "resolved",
                                                 owner_fields=frozenset({"app_path"})),
    ("POST", "/api/v1/device/app/launch"): _RESOLVED,
    ("POST", "/api/v1/device/app/terminate"): _RESOLVED,
    ("POST", "/api/v1/device/app/uninstall"): _RESOLVED,
    ("POST", "/api/v1/device/app/state/plist"): _RESOLVED,
    ("POST", "/api/v1/device/app/state/plist/batch"): _RESOLVED,
    ("DELETE", "/api/v1/device/app/state/plist/key"): _RESOLVED,
    ("POST", "/api/v1/device/app/state/plist/watch/start"): _RESOLVED,
    ("POST", "/api/v1/device/app/state/plist/watch/stop"): _RESOLVED,
    ("POST", "/api/v1/device/boot"): _RESOLVED,
    ("POST", "/api/v1/device/display-density"): _RESOLVED,
    ("POST", "/api/v1/device/erase"): _RESOLVED,
    ("POST", "/api/v1/device/font-scale"): _RESOLVED,
    ("POST", "/api/v1/device/hinge"): _RESOLVED,
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
    # None named: all traffic. client_ip filters by a LAN address, which no
    # reservation covers.
    ("POST", "/api/v1/proxy/capture/start"): _named(
        "simulator_udid", "device_serial", owner_fields=frozenset({"client_ip"})),
    ("POST", "/api/v1/proxy/capture/stop"): _PATH_ID,        # by session id in the body
    ("POST", "/api/v1/proxy/mocks"): _named("simulator_udid"),  # unscoped: every device's
    ("PATCH", "/api/v1/proxy/mocks/{rule_id}"): _PATH_ID,
    ("DELETE", "/api/v1/proxy/mocks/{rule_id}"): _PATH_ID,
    # output_dir is a path on the Mac. `viewer` is the client's own recording's
    # link (#559), which the route refuses when the owner has turned that off.
    ("POST", "/api/v1/recordings"): Rule(Access.CONTROL, "resolved",
                                         owner_fields=frozenset({"output_dir"})),
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
    ("POST", "/api/v1/builds/parse"): _OWNER,                # replaces the shared latest result
    ("POST", "/api/v1/builds/parse-file"): _OWNER,           # reads a path on the Mac
    ("POST", "/api/v1/builds/android/record"): _OWNER,
    ("GET", "/api/v1/builds/latest"): _OWNER,
    ("GET", "/api/v1/trace"): Rule(Access.VIEW, owner_fields=frozenset({"recording"})),

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
    # The caller's own capture of apps on a simulator it holds (#558): names
    # only, which the handler checks, and released with the hold.
    ("POST", "/api/v1/proxy/session-capture"): _named("simulator_udid"),
    ("DELETE", "/api/v1/proxy/session-capture"): _RESERVE,   # the caller's own
    ("GET", "/api/v1/proxy/session-capture"): _VIEW,
    ("POST", "/api/v1/proxy/filter"): _OWNER,
    ("POST", "/api/v1/proxy/start"): _OWNER,
    ("POST", "/api/v1/proxy/stop"): _OWNER,
    ("GET", "/api/v1/proxy/setup-guide"): _OWNER,

    # -- recordings ------------------------------------------------------------
    ("GET", "/api/v1/recordings"): _VIEW,
    ("GET", "/api/v1/recordings/events"): _OWNER,            # reads any directory on the Mac
    # Downloaded by their own principal, from another machine (#254, phase 3b).
    ("GET", "/api/v1/recordings/{recording_id}/files"): _VIEW,
    ("GET", "/api/v1/screenshots"): _VIEW,     # the caller's own session's only
    ("GET", "/api/v1/recordings/{recording_id}/files/{name}"): _VIEW,
    # A client's own recording's link (#559): the route checks it is the
    # caller's, and that the owner has not turned client links off.
    ("POST", "/api/v1/recordings/{recording_id}/viewer"): _PATH_ID,
    ("DELETE", "/api/v1/recordings/{recording_id}/viewer"): _PATH_ID,

    # -- Mac-wide state -----------------------------------------------------------
    ("POST", "/api/v1/device/screenshot/timeline/start"): _OWNER,   # one timeline per server
    ("POST", "/api/v1/device/screenshot/timeline/stop"): _OWNER,
    # App-state checkpoints are one store, keyed by bundle and label: a
    # client's save would replace the owner's, and a restore of the owner's
    # would put the owner's app data on the client's simulator (review).
    ("POST", "/api/v1/device/app/state/save"): _OWNER,
    ("POST", "/api/v1/device/app/state/restore"): _OWNER,
    ("DELETE", "/api/v1/device/app/state/{label}"): _OWNER,  # a snapshot on the Mac's disk
    # Runs xcodebuild or Gradle on a project path: code on the Mac (#254).
    ("POST", "/api/v1/device/build-and-install"): _OWNER,
    ("POST", "/api/v1/device/app/state/plist/watch/configure"): _OWNER,
    ("DELETE", "/api/v1/device/app/state/plist/watch/configure"): _OWNER,
    ("GET", "/api/v1/landmarks/"): _VIEW,
    ("POST", "/api/v1/landmarks/identify"): _VIEW,
    ("POST", "/api/v1/landmarks/validate"): Rule(Access.VIEW,
                                                 owner_fields=frozenset({"source"})),
    # A client loads an uploaded knowledge base for its own held session
    # (#560); a path, inline sets and remembering stay the owner's.
    ("POST", "/api/v1/landmarks/load"): Rule(Access.RESERVE, owner_fields=frozenset(
        {"source", "remember", "landmarks", "global_leads_to"})),
    # A client unloads only its session's own; the route refuses the rest.
    ("DELETE", "/api/v1/landmarks/"): Rule(Access.RESERVE, owner_fields=frozenset({"forget"})),
    ("GET", "/api/v1/system/channel"): _VIEW,
    ("PUT", "/api/v1/system/channel"): _OWNER,
    ("POST", "/api/v1/system/update"): _OWNER,
    ("GET", "/api/v1/system/update-status"): _OWNER,
}

#: What a caller held to blind mode (#254, phase 2) gets from each route a
#: client may read. `tests/test_scopes.py` fails until every VIEW route has
#: an entry, as ROUTES does for every route.
#:
#:     device    reads one device: another principal's device answers 404,
#:               as one that does not exist would (device_access.check)
#:     filtered  lists data: the route keeps only what is attributable to the
#:               caller's own devices (visibility.data_view)
#:     hidden    describes the Mac or the owner's set-up, not a device: 403
#:     shared    nothing about anyone: answered as for everyone
BLIND: dict[tuple[str, str], str] = {
    ("GET", "/api/v1/device/app/list"): "device",
    ("GET", "/api/v1/device/app/performance"): "device",
    ("GET", "/api/v1/device/app/state/plist"): "device",
    ("GET", "/api/v1/device/app/state/plist/diff"): "device",
    ("GET", "/api/v1/device/hinge"): "device",
    ("GET", "/api/v1/device/screen-summary"): "device",
    ("GET", "/api/v1/device/screenshot"): "device",
    ("GET", "/api/v1/device/screenshot/annotated"): "device",
    ("GET", "/api/v1/device/settings"): "device",
    ("GET", "/api/v1/device/ui"): "device",
    ("GET", "/api/v1/device/ui/element"): "device",
    ("GET", "/api/v1/device/video"): "device",
    ("POST", "/api/v1/device/ui/wait-for-element"): "device",
    ("POST", "/api/v1/device/ui/wait-settled"): "device",
    ("POST", "/api/v1/device/preview/start"): "device",
    ("POST", "/api/v1/device/logging/start"): "device",
    ("POST", "/api/v1/device/logging/device/start"): "device",
    ("POST", "/api/v1/landmarks/identify"): "device",
    ("GET", "/api/v1/device/list"): "filtered",
    ("GET", "/api/v1/device/preview/devices"): "filtered",
    ("GET", "/api/v1/device/preview/status"): "filtered",
    ("GET", "/api/v1/device/screenshot/timeline"): "filtered",
    ("GET", "/api/v1/crashes/latest"): "filtered",
    ("GET", "/api/v1/logs/errors"): "filtered",
    ("GET", "/api/v1/logs/query"): "filtered",
    ("GET", "/api/v1/logs/stream"): "filtered",
    ("GET", "/api/v1/logs/summary"): "filtered",
    ("GET", "/api/v1/logs/sources"): "filtered",
    ("GET", "/api/v1/proxy/status"): "filtered",
    ("GET", "/api/v1/proxy/session-capture"): "filtered",   # the caller's own claims
    ("GET", "/api/v1/proxy/cert/status"): "filtered",
    ("GET", "/api/v1/proxy/flows"): "filtered",
    ("GET", "/api/v1/proxy/flows/pending"): "filtered",
    ("GET", "/api/v1/proxy/flows/stream"): "filtered",
    ("GET", "/api/v1/proxy/flows/summary"): "filtered",
    ("GET", "/api/v1/proxy/flows/{flow_id}"): "filtered",
    ("POST", "/api/v1/proxy/flows/wait"): "filtered",
    ("GET", "/api/v1/proxy/mocks"): "filtered",
    ("GET", "/api/v1/recordings"): "filtered",
    ("GET", "/api/v1/recordings/{recording_id}/files"): "filtered",
    ("GET", "/api/v1/screenshots"): "filtered",
    ("GET", "/api/v1/recordings/{recording_id}/files/{name}"): "filtered",
    ("GET", "/api/v1/trace"): "filtered",
    # The owner's set-up: which apps have knowledge bases, which hosts are
    # bypassed or filtered, what builds ran, the flows held for intercept
    # (which carry no device), app checkpoints, the Mac's tool layout.
    ("GET", "/api/v1/device/app/state/list"): "hidden",
    ("GET", "/api/v1/device/app/state/plist/watch/config"): "hidden",
    ("GET", "/api/v1/device/build-progress"): "hidden",
    ("GET", "/api/v1/device/tools/sites"): "hidden",
    ("GET", "/api/v1/landmarks/"): "hidden",
    ("POST", "/api/v1/landmarks/validate"): "hidden",
    ("GET", "/api/v1/logs/filter"): "hidden",
    ("GET", "/api/v1/proxy/bypass"): "hidden",
    ("GET", "/api/v1/proxy/intercept/held"): "hidden",
    ("GET", "/api/v1/system/channel"): "shared",
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
    literal path wins over a template, as the routes themselves are ordered.

    A path that differs from a route only by its trailing slash is that route:
    Starlette answers it with a redirect to the route, and judged as unmapped
    it was refused to a client before the redirect could happen -- the MCP's
    `list_landmarks` asks for `/api/v1/landmarks`, the route is
    `/api/v1/landmarks/` (found live)."""
    method = method.upper()
    if method == "HEAD":
        method = "GET"
    found = _match_exact(method, path)
    if found is None and path not in ("", "/"):
        found = _match_exact(method, path[:-1] if path.endswith("/") else path + "/")
    return found


def _match_exact(method: str, path: str) -> tuple[str, Rule] | None:
    found = _LITERAL.get((method, path))
    if found is not None:
        return found
    for m, pattern, template, rule in _TEMPLATED:
        if m == method and pattern.match(path):
            return template, rule
    return None


def _blind_kind(method: str, path: str) -> str:
    """A VIEW route's blind-mode treatment; "hidden" when undecided."""
    found = match(method, path)
    return BLIND.get((method.upper() if method.upper() != "HEAD" else "GET", found[0]),
                     "hidden") if found else "hidden"


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


def _fields(query: bytes, body: bytes) -> dict[str, bool]:
    """Which query parameters a request sets to any text, and which top-level
    JSON body fields to something other than null, empty or false."""
    found: dict[str, bool] = {}
    # Any non-empty query value: a query string carries text, so "false" is a
    # value a handler may read as a path, not a boolean (CodeRabbit).
    for key, value in parse_qsl(query.decode("latin-1"), keep_blank_values=False):
        if value:
            found[key] = True
    if body:
        try:
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            data = None
        if isinstance(data, dict):
            for key, value in data.items():
                if value not in (None, "", False, [], {}):
                    found[key] = True
    return found


def _ends_on_revoke(scope: Scope, who: str, receive: Receive) -> Receive:
    """A receive that reports the client gone once its key is revoked.

    Streams -- logs, flows, video -- run until `request.is_disconnected()`,
    which polls this; without it a revoked key went on reading for as long as
    it kept the connection open (review)."""
    label = principal.label_of(who)
    clients = getattr(getattr(scope.get("app"), "state", None), "clients", None)
    if label is None or clients is None:
        return receive
    gone = {"type": "websocket.disconnect" if scope["type"] == "websocket" else "http.disconnect"}

    async def guarded():
        if not clients.is_active(label):
            return dict(gone)
        return await receive()

    return guarded


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
        if rule.access is Access.VIEW and visibility.blind_for():
            kind = _blind_kind(method, path)
            if kind == "hidden":
                await _deny(send, 403, (f"{method} {path} describes this server's own set-up, "
                                        "which a blind server does not share with client keys"))
                return
            if kind == "device":
                # Unnamed, a read falls back to the active device and then to
                # "the only booted one" -- which on a blind server could be
                # another key's, named back in the refusal, or a list of every
                # booted device (review). So it needs a device of its own.
                rule = Rule(Access.VIEW, "resolved-read", owner_fields=rule.owner_fields)
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
        receive = _ends_on_revoke(scope, who, receive)
        token = _SESSION.set(qualified)
        try:
            if rule is not None and (rule.owner_fields or rule.device == "resolved-read" or (
                    rule.access is Access.CONTROL and rule.device != "path-id")):
                body, receive = await device_access._buffer(receive)
                refusal = await self._refusal(scope, rule, body)
                if refusal is not None:
                    await _deny(send, *refusal)
                    return
            await self.app(scope, receive, send)
        finally:
            _SESSION.reset(token)

    async def _refusal(self, scope: Scope, rule: Rule, body: bytes) -> tuple[int, object] | None:
        """Why a client's request is refused before the route runs, or None."""
        method, path = scope.get("method", ""), scope.get("path", "")
        fields = _fields(scope.get("query_string", b""), body)
        given = sorted(f for f in rule.owner_fields if fields.get(f))
        if given:
            return 403, (f"{', '.join(given)} on {method} {path} is owner-only: it names a "
                         "path on the Mac or changes what every caller shares")
        if rule.device == "resolved-read":
            if device_access._named_devices(scope.get("query_string", b""), body):
                return None
            app = scope.get("app")
            controller = getattr(getattr(app, "state", None), "device_controller", None)
            if not (getattr(controller, "_active_udid", None) if controller is not None else None):
                return 403, ("this session has no device: name one (udid), or reserve one "
                             "(reserve_devices) to make it this session's active device")
            return None
        if rule.access is not Access.CONTROL or rule.device == "path-id":
            return None
        if rule.device == "named":
            if any(fields.get(k) for k in rule.keys):
                return None
            return 403, (f"{method} {path} without {' or '.join(rule.keys)} acts on every "
                         "device: owner-only. Name a device your session holds.")
        if device_access._named_devices(scope.get("query_string", b""), body):
            return None
        return await self._no_device(scope, rule)

    async def _no_device(self, scope: Scope, rule: Rule) -> tuple[int, object] | None:
        """A resolved request that names no device: refused when the caller
        has no device of its own for it to mean."""
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
