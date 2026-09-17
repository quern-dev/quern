"""API key authentication middleware.

Requires a valid API key on all endpoints except a short public list.
Supports both Authorization: Bearer <key> and X-API-Key: <key> headers.

Pure ASGI, deliberately. As a Starlette `BaseHTTPMiddleware` this replaced the
receive channel with one that never delivered `http.disconnect` to the
endpoint, so `request.is_disconnected()` answered False however long ago the
caller had gone -- and it is installed unconditionally, so *no* handler could
notice a disconnect. `scroll_to_element` was measured sweeping a simulator for
413s and 523s on behalf of a client that left at 180s, holding a device the
next caller wanted and swiping it while they used it (#208, #84).

`TimelineMiddleware` in server/screenshot_timeline.py is the other pure-ASGI
middleware here, and the pattern to compare against.
"""

from __future__ import annotations

import hmac
import json

#: Reachable without a key. Matched exactly, as the paths themselves are.
PUBLIC_PATHS = frozenset({
    "/", "/health", "/api/v1/health", "/tools", "/docs", "/redoc",
    "/openapi.json", "/api/v1/proxy/cert", "/video-test",
})

_UNAUTHORISED_BODY = json.dumps(
    {"detail": "Invalid or missing API key"}, separators=(",", ":"),
).encode()


class APIKeyMiddleware:
    """Validate an API key on every request outside `PUBLIC_PATHS`."""

    def __init__(self, app, api_key: str) -> None:  # noqa: ANN001
        self.app = app
        self.api_key = api_key

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        # lifespan and websocket scopes carry no headers to check and no way
        # to answer 401; they are not this middleware's business.
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if scope.get("path") in PUBLIC_PATHS or self._has_valid_key(scope):
            await self.app(scope, receive, send)
            return

        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(_UNAUTHORISED_BODY)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": _UNAUTHORISED_BODY})

    def _has_valid_key(self, scope) -> bool:  # noqa: ANN001
        """Is either header carrying the configured key?

        An empty configured key authorises nobody. The previous version
        compared `X-API-Key` against it using `""` for a missing header, so an
        empty key accepted every request that sent no header at all.
        `ServerConfig` always generates one, so this guards a state that should
        not arise -- which is what the previous version assumed too.
        """
        if not self.api_key:
            return False

        # ASGI headers are a list of (lowercase name, value) byte pairs, not a
        # mapping: the name is already folded, the value is not decoded, and a
        # header can repeat. First occurrence wins, as Starlette's Headers.get
        # does.
        authorization = api_key_header = None
        for raw_name, raw_value in scope.get("headers") or ():
            if raw_name == b"authorization":
                if authorization is None:
                    authorization = raw_value
            elif raw_name == b"x-api-key":
                if api_key_header is None:
                    api_key_header = raw_value

        expected = self.api_key.encode("latin-1", errors="ignore")
        if authorization is not None and authorization.startswith(b"Bearer "):
            if hmac.compare_digest(authorization[7:], expected):
                return True
        return api_key_header is not None and hmac.compare_digest(
            api_key_header, expected,
        )
