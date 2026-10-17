"""The viewer listener's app: viewer pages and streams, nothing else (#449).

    GET  /v/<token>/            the page
    GET  /v/<token>/frames      the recording's live stream, relayed
    POST /v/<token>/keyframe    a picture now, for a viewer that just attached
    GET  /v/<token>/fmp4.js     the MSE muxer (server/device/media/web)
    GET  /v/<token>/jmuxer.js   only when viewer.jmuxer_path is set, for A/B

Everything else is 404, and so is any request whose token is unknown, revoked
or expired: the listener never confirms that a token exists. Requests must
also name an expected Host, and a browser's Origin, when sent, must too --
the DNS-rebinding and cross-site checks from the baguette notes, in addition
to the token.

The stream is relayed from the recording's loopback quern-media, looked up
per connection, with `?source=<udid>` so a port reused by another simulator
answers 409 rather than showing the wrong screen (#425). quern-media labels
/frames application/octet-stream (#451), which WebKit's fetch can read.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Route

logger = logging.getLogger(__name__)

_WEB = Path(__file__).resolve().parents[1] / "device" / "media" / "web"
_PAGE = Path(__file__).resolve().parent / "page.html"
_NOT_FOUND = PlainTextResponse("not found", status_code=404)


def _host_of(value: str) -> str:
    """'host:port' or '[v6]:port' to its host, lowercased."""
    value = value.strip().lower()
    if value.startswith("["):
        return value[1:value.find("]")] if "]" in value else value
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def create_viewer_app(*, recordings, grants, allowed_hosts: set[str],
                      jmuxer_path: str | None = None) -> Starlette:
    """The ASGI app the viewer listener serves."""
    allowed = {h.lower() for h in allowed_hosts} | {"localhost", "127.0.0.1", "::1"}

    def recording(request: Request) -> str | None:
        return grants.recording_for(request.path_params.get("token", ""))

    async def page(request: Request) -> Response:
        if recording(request) is None:
            return _NOT_FOUND
        if not request.url.path.endswith("/"):
            # Relative URLs in the page -- frames, keyframe, fmp4.js -- resolve
            # against the directory.
            return RedirectResponse(request.url.path + "/", status_code=307)
        return FileResponse(_PAGE, media_type="text/html; charset=utf-8",
                            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    async def frames(request: Request) -> Response:
        rec_id = recording(request)
        if rec_id is None:
            return _NOT_FOUND
        target = recordings.viewer_stream(rec_id)
        if target is None:
            return PlainTextResponse("this recording is not filming right now", status_code=503)
        port, udid = target
        client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=None))
        try:
            upstream = await client.send(
                client.build_request("GET", f"http://127.0.0.1:{port}/frames",
                                     params={"source": udid}), stream=True)
        except httpx.HTTPError as e:
            await client.aclose()
            return PlainTextResponse(f"quern-media is not answering: {type(e).__name__}",
                                     status_code=502)
        if upstream.status_code != 200:
            body = (await upstream.aread()).decode("utf-8", "replace")[:300]
            await upstream.aclose()
            await client.aclose()
            return PlainTextResponse(body or f"quern-media answered {upstream.status_code}",
                                     status_code=upstream.status_code)

        async def relay():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            except httpx.HTTPError:
                return
            finally:
                await upstream.aclose()
                await client.aclose()

        return StreamingResponse(relay(), media_type="application/octet-stream",
                                 headers={"Cache-Control": "no-store"})

    async def keyframe(request: Request) -> Response:
        rec_id = recording(request)
        if rec_id is None:
            return _NOT_FOUND
        asked = await recordings.viewer_keyframe(rec_id)
        return Response(status_code=204 if asked else 503)

    async def muxer(request: Request) -> Response:
        if recording(request) is None:
            return _NOT_FOUND
        return FileResponse(_WEB / "fmp4.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-store"})

    async def jmuxer(request: Request) -> Response:
        if recording(request) is None or not jmuxer_path or not Path(jmuxer_path).is_file():
            return _NOT_FOUND
        return FileResponse(jmuxer_path, media_type="text/javascript",
                            headers={"Cache-Control": "no-store"})

    async def not_found(_request: Request) -> Response:
        return _NOT_FOUND

    app = Starlette(routes=[
        Route("/v/{token}", page, methods=["GET"]),
        Route("/v/{token}/", page, methods=["GET"]),
        Route("/v/{token}/frames", frames, methods=["GET"]),
        Route("/v/{token}/keyframe", keyframe, methods=["POST"]),
        Route("/v/{token}/fmp4.js", muxer, methods=["GET"]),
        Route("/v/{token}/jmuxer.js", jmuxer, methods=["GET"]),
        Route("/{rest:path}", not_found, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"]),
    ])

    async def guarded(scope, receive, send):
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}
        refusal = None
        if _host_of(headers.get("host", "")) not in allowed:
            refusal = (421, "unexpected Host")
        elif headers.get("sec-fetch-site", "").lower() == "cross-site":
            refusal = (403, "cross-site request")
        elif "origin" in headers and headers["origin"] != "null":
            origin = headers["origin"].split("://", 1)[-1]
            if _host_of(origin) not in allowed:
                refusal = (403, "unexpected Origin")
        if refusal is not None:
            await PlainTextResponse(refusal[1], status_code=refusal[0])(scope, receive, send)
            return
        await app(scope, receive, send)

    return guarded
