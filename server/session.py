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
import re

from starlette.types import ASGIApp, Receive, Scope, Send

HEADER = "x-quern-session"

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


class SessionMiddleware:
    """Read `X-Quern-Session` and make it the request's session."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        raw = next((v for k, v in scope.get("headers", []) if k == HEADER.encode()), None)
        token = _SESSION.set(parse_session(raw))
        try:
            await self.app(scope, receive, send)
        finally:
            _SESSION.reset(token)
