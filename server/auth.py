"""API key authentication middleware.

Requires a valid API key on all endpoints except /health.
Supports both Authorization: Bearer <key> and X-API-Key: <key> headers.
"""

from __future__ import annotations

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse


class APIKeyMiddleware(BaseHTTPMiddleware):
    """Middleware that validates API key on all requests except health check."""

    def __init__(self, app, api_key: str) -> None:  # noqa: ANN001
        super().__init__(app)
        self.api_key = api_key

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # Health check and API docs are always public
        if request.url.path in ("/health", "/api/v1/health", "/docs", "/redoc", "/openapi.json"):
            return await call_next(request)

        # Check Authorization: Bearer <key>
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            if token == self.api_key:
                return await call_next(request)

        # Check X-API-Key: <key>
        api_key_header = request.headers.get("X-API-Key", "")
        if api_key_header == self.api_key:
            return await call_next(request)

        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid or missing API key"},
        )
