"""Which caller an action came from: the session attribution half of #254.

Several agents can share one quern server -- measured, four Claude sessions on
one machine at once, each with its own quern MCP process -- and the action log
recorded *what* happened and *which device* it hit, but not *who asked*. Each
MCP process now mints a session id at startup and sends it as
`X-Quern-Session` on every request. This module carries it from the request
to the action entries written while handling it.

It is attribution, not authority. Nothing is refused for a missing or unknown
session, and any caller can claim any id; the header says who a request says
it is from, which is what a log reader needs. Reservations and credentials are
#254's later steps.

A ContextVar, set by a pure ASGI middleware, so it follows the request into the
handler and into any task the handler starts (`asyncio.create_task` copies the
context). Work started outside a request -- a stream restart, a recording's
background reap -- has no session, which is correct: nobody asked for it.
"""

from __future__ import annotations

import contextvars
import logging
import re
import time

from starlette.types import ASGIApp, Receive, Scope, Send

HEADER = "x-quern-session"

#: Set on the response to a request that did not name a valid session (#254,
#: the warning phase). The value is a reason token -- `missing` or
#: `malformed` -- then "; " and a sentence saying what to send. The token is
#: the contract: the conformance suite matches on it, and the prose may change.
WARNING_HEADER = "x-quern-session-warning"

#: Seconds between server-log lines about one kind of unattributed caller.
_WARN_LOG_INTERVAL = 300.0
#: Lines per interval across all kinds, which also bounds the table. The
#: per-kind limit alone is defeated by a caller that changes its User-Agent
#: on every request -- the key includes it, and the caller chooses it.
_WARN_LOG_BUDGET = 20

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _loggable(text: str, limit: int) -> str:
    """`text` safe for a one-line log record: control characters escaped, cut.

    ASGI paths arrive percent-decoded, so `%0A` in a URL is a real newline by
    the time it reaches us. Logged as-is, an unauthenticated caller could
    write a forged timestamped `[ERROR]` line into server.log -- and into
    `query_logs`, which agents read (#254 review).
    """
    cleaned = _CONTROL.sub(lambda m: f"\\x{ord(m.group()):02x}", text)
    return cleaned if len(cleaned) <= limit else cleaned[:limit] + "..."

logger = logging.getLogger(__name__)

#: What a session id may look like. Bounded and plain because it is copied into
#: every action entry, the server log and recordings that CI publishes: a
#: value with newlines or markup would be log injection, and an unbounded one a
#: way to bloat every entry.
_VALID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,63}")

_SESSION: contextvars.ContextVar[str] = contextvars.ContextVar(
    "quern_session", default="",
)


def current_session() -> str:
    """The calling session's id, or "" when the request named none."""
    return _SESSION.get()


def session_from_env(env: dict[str, str], *, fallback: str) -> str:
    """The session a client process should send, by the rule every quern client
    shares (the MCP server's copy is `mintSessionId` in mcp/src/http.ts):

    1. `QUERN_SESSION`, when it is a valid id -- a name the caller chose, such
       as a CI job's.
    2. `claude-<CLAUDE_CODE_SESSION_ID>` when running under Claude Code. That
       id names the agent's transcript in ~/.claude/projects, so a reader can
       open the conversation behind an action. It is in the environment of the
       agent's MCP server *and* of its Bash commands, so a `quern record` the
       agent runs lands under the same session as its MCP calls.
    3. `fallback`.
    """
    named = parse_session(env.get("QUERN_SESSION"))
    if named:
        return named
    claude_id = (env.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    if claude_id:
        claude = parse_session(f"claude-{claude_id}")
        if claude:
            return claude
    return fallback


def parse_session(raw: bytes | str | None) -> str:
    """A header value as a session id, or "" when absent or malformed.

    Malformed is not refused. The header is attribution, so a client sending a
    bad one should still get its work done -- recorded as unattributed, which
    is what it is.
    """
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("ascii")
        except UnicodeDecodeError:
            return ""
    raw = raw.strip()
    return raw if _VALID.fullmatch(raw) else ""


def _public_paths() -> frozenset[str]:
    """Paths that answer anyone, so a missing session there is not news."""
    from server.auth import PUBLIC_PATHS

    return PUBLIC_PATHS


def warning_for(raw: bytes | None, session: str) -> str:
    """The `X-Quern-Session-Warning` value for a request, or "" for none."""
    if session:
        return ""
    if raw is None or not raw.strip():
        return ("missing; name the caller with an X-Quern-Session header "
                "(letters, digits and . _ : @ -, up to 64) so its actions can be "
                "attributed and read back with /api/v1/trace?session=<name>")
    return ("malformed; X-Quern-Session must start with a letter or digit and use "
            "only letters, digits and . _ : @ -, up to 64 characters -- this "
            "request was recorded as unattributed")


class SessionMiddleware:
    """Read `X-Quern-Session` and make it the request's session.

    A request that names no valid session still succeeds -- this is the
    warning phase of #254 -- but its response carries
    `X-Quern-Session-Warning`, and the server log notes the caller once every
    few minutes. Not refused, and deliberately not soon: an MCP process keeps
    the code it started with, measured running for ten days, so every agent
    session older than an update sends no header until it restarts.
    Refusing would break those agents mid-session.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        # (reason, user agent) -> when it was last logged, so a script calling
        # in a loop is one line every few minutes rather than one per request.
        self._logged: dict[tuple[str, str], float] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = scope.get("headers", [])
        raw = next((v for k, v in headers if k == HEADER.encode()), None)
        session = parse_session(raw)
        warning = "" if scope.get("path") in _public_paths() else warning_for(raw, session)
        if warning:
            path = scope.get("path", "")

            async def send_with_warning(message):
                if message["type"] == "http.response.start":
                    message = {**message, "headers": [
                        *message.get("headers", []),
                        (WARNING_HEADER.encode(), warning.encode()),
                    ]}
                    # Logged once the status is known, and not for a 401: a
                    # caller without the key is a port scan or a wrong key,
                    # not a script that forgot to name itself, and logging it
                    # as one misleads whoever reads the log.
                    if message.get("status") != 401:
                        self._note(warning, headers, path)
                await send(message)
        else:
            send_with_warning = send

        token = _SESSION.set(session)
        try:
            await self.app(scope, receive, send_with_warning)
        finally:
            _SESSION.reset(token)

    def _note(self, warning: str, headers, path: str) -> None:
        """Log an unattributed caller, at most once per interval per kind."""
        reason = warning.split(";", 1)[0]
        agent = next((v for k, v in headers if k == b"user-agent"), b"").decode(
            "ascii", "replace")
        now = time.monotonic()
        key = (reason, agent)
        if now - self._logged.get(key, float("-inf")) < _WARN_LOG_INTERVAL:
            return
        # Expired kinds go first; a full table then refuses new kinds rather
        # than being cleared, which would log every remembered kind again.
        self._logged = {k: t for k, t in self._logged.items()
                        if now - t < _WARN_LOG_INTERVAL}
        if len(self._logged) >= _WARN_LOG_BUDGET:
            return
        self._logged[key] = now
        # Imported here: logging_ext reads this module's ContextVar at import.
        from server import logging_ext

        logging_ext.warning(
            logger, "Unattributed API caller (%s session header): %s, first seen on %s",
            reason, _loggable(agent, 80) or "no user agent", _loggable(path, 120),
            category="server.lifecycle",
        )
