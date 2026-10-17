"""The viewer listener's app: viewer pages and streams, nothing else (#449).

    GET  /v/<token>/            the page
    GET  /v/<token>/viewer.js   its script (the page's CSP allows nothing inline)
    GET  /v/<token>/frames      the recording's live stream, relayed
    POST /v/<token>/keyframe    a picture now, for a viewer that just attached
    GET  /v/<token>/fmp4.js     the MSE muxer (server/device/media/web)
    GET  /v/<token>/jmuxer.js   only when viewer.jmuxer_path is set, for A/B

Everything else is 404 (Starlette's own), and so is any request whose token is unknown, revoked
or expired: the listener never confirms that a token exists. Requests must
also name an expected Host, and a browser's Origin, when sent, must too --
the DNS-rebinding and cross-site checks from the baguette notes, in addition
to the token. `Origin: null` is refused: it is what a sandboxed frame or a
file:// page sends, and the viewer is never either.

A stream is re-checked against its token about once a second, so revoking
a link or its expiry ends the streams already open on it, not only new
ones. Streams are capped per recording and in all, since each holds a
connection to quern-media and two file descriptors here.

The stream is relayed from the recording's loopback quern-media, looked up
per connection, with `?source=<udid>` so a port reused by another simulator
answers 409 rather than showing the wrong screen (#425). quern-media labels
/frames application/octet-stream (#451), which WebKit's fetch can read.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import Counter
from collections.abc import Callable
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
_SCRIPT = Path(__file__).resolve().parent / "viewer.js"
_NOT_FOUND = PlainTextResponse("not found", status_code=404)

#: Open streams allowed per recording, and over all recordings.
MAX_STREAMS_PER_RECORDING = 8
MAX_STREAMS = 32
#: How often an open stream re-checks its token.
REVALIDATE_SECONDS = 1.0
#: Of a non-200 from quern-media, at most this much is passed on.
UPSTREAM_ERROR_BYTES = 300

#: The page loads its script, the muxer and jmuxer from this origin, reads the
#: stream with fetch, and plays it in <video> from a MediaSource blob: URL.
#: Its one <style> is inline.
_CSP = ("default-src 'none'; script-src 'self'; connect-src 'self'; media-src blob:; "
        "style-src 'unsafe-inline'; img-src 'self'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'")
_PAGE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                 "Content-Security-Policy": _CSP, "X-Content-Type-Options": "nosniff",
                 "X-Frame-Options": "DENY"}
_SCRIPT_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def _host_of(value: str) -> str:
    """'host:port' or '[v6]:port' to its host, lowercased."""
    value = value.strip().lower()
    if value.startswith("["):
        return value[1:value.find("]")] if "]" in value else value
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def create_viewer_app(*, recordings, grants, allowed_hosts: Callable[[], set[str]],
                      jmuxer_path: str | None = None) -> Starlette:
    """The ASGI app the viewer listener serves. `allowed_hosts` is asked per
    request, so a Mac that moves networks keeps accepting its own address."""
    loopback = {"localhost", "127.0.0.1", "::1"}
    streams: Counter[str] = Counter()

    def recording(request: Request) -> str | None:
        return grants.recording_for(request.path_params.get("token", ""))

    async def page(request: Request) -> Response:
        if recording(request) is None:
            return _NOT_FOUND
        if not request.url.path.endswith("/"):
            # Relative URLs in the page -- frames, keyframe, fmp4.js -- resolve
            # against the directory.
            return RedirectResponse(request.url.path + "/", status_code=307)
        return FileResponse(_PAGE, media_type="text/html; charset=utf-8", headers=_PAGE_HEADERS)

    async def script(request: Request) -> Response:
        if recording(request) is None:
            return _NOT_FOUND
        return FileResponse(_SCRIPT, media_type="text/javascript", headers=_SCRIPT_HEADERS)

    async def frames(request: Request) -> Response:
        token = request.path_params.get("token", "")
        rec_id = grants.recording_for(token)
        if rec_id is None:
            return _NOT_FOUND
        if request.method != "GET":
            # Starlette answers HEAD on a GET route; a HEAD must not open a
            # relay that nothing reads.
            return Response(status_code=405, headers={"Allow": "GET"})
        target = recordings.viewer_stream(rec_id)
        if target is None:
            return PlainTextResponse("this recording is not filming right now", status_code=503)
        if streams[rec_id] >= MAX_STREAMS_PER_RECORDING or streams.total() >= MAX_STREAMS:
            return PlainTextResponse("too many viewers are watching right now", status_code=503)
        port, udid = target
        streams[rec_id] += 1
        client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=None))
        upstream: httpx.Response | None = None
        released = False

        async def release() -> None:
            nonlocal released
            if released:
                return
            released = True
            streams[rec_id] -= 1
            if streams[rec_id] <= 0:
                del streams[rec_id]
            with contextlib.suppress(Exception):
                if upstream is not None:
                    await upstream.aclose()
            with contextlib.suppress(Exception):
                await client.aclose()

        try:
            upstream = await client.send(
                client.build_request("GET", f"http://127.0.0.1:{port}/frames",
                                     params={"source": udid}), stream=True)
            if upstream.status_code != 200:
                # Read no more than is passed on, and never answered as 404:
                # the page reads 404 as "this link has expired".
                body = b""
                async for chunk in upstream.aiter_raw():
                    body += chunk
                    if len(body) >= UPSTREAM_ERROR_BYTES:
                        break
                text = body[:UPSTREAM_ERROR_BYTES].decode("utf-8", "replace").strip()
                await release()
                return PlainTextResponse(
                    f"quern-media answered {upstream.status_code}" + (f": {text}" if text else ""),
                    status_code=502)
        except httpx.HTTPError as e:
            await release()
            return PlainTextResponse(f"quern-media is not answering: {type(e).__name__}",
                                     status_code=502)
        except BaseException:
            await release()
            raise

        async def relay():
            chunks = upstream.aiter_raw().__aiter__()
            pending: asyncio.Future | None = None
            checked = time.monotonic()
            try:
                while True:
                    if pending is None:
                        pending = asyncio.ensure_future(chunks.__anext__())
                    done, _ = await asyncio.wait({pending}, timeout=REVALIDATE_SECONDS)
                    now = time.monotonic()
                    if not done or now - checked >= REVALIDATE_SECONDS:
                        # A still screen sends nothing for seconds, so this
                        # runs on a timer and not only between chunks.
                        checked = now
                        if grants.recording_for(token) != rec_id:
                            return
                    if not done:
                        continue
                    try:
                        chunk = pending.result()
                    except (StopAsyncIteration, httpx.HTTPError):
                        return
                    pending = None
                    yield chunk
            finally:
                if pending is not None and not pending.done():
                    pending.cancel()
                    with contextlib.suppress(BaseException):
                        await pending

        return _Relay(relay(), release, media_type="application/octet-stream",
                      headers={"Cache-Control": "no-store"})

    async def keyframe(request: Request) -> Response:
        rec_id = recording(request)
        if rec_id is None:
            return _NOT_FOUND
        asked = await recordings.viewer_keyframe(rec_id)
        if asked is None:
            return Response(status_code=429, headers={"Retry-After": "1"})
        return Response(status_code=204 if asked else 503)

    async def muxer(request: Request) -> Response:
        if recording(request) is None:
            return _NOT_FOUND
        return FileResponse(_WEB / "fmp4.js", media_type="text/javascript",
                            headers=_SCRIPT_HEADERS)

    async def jmuxer(request: Request) -> Response:
        if recording(request) is None or not jmuxer_path or not Path(jmuxer_path).is_file():
            return _NOT_FOUND
        return FileResponse(jmuxer_path, media_type="text/javascript",
                            headers=_SCRIPT_HEADERS)

    app = Starlette(routes=[
        Route("/v/{token}", page, methods=["GET"]),
        Route("/v/{token}/", page, methods=["GET"]),
        Route("/v/{token}/viewer.js", script, methods=["GET"]),
        Route("/v/{token}/frames", frames, methods=["GET"]),
        Route("/v/{token}/keyframe", keyframe, methods=["POST"]),
        Route("/v/{token}/fmp4.js", muxer, methods=["GET"]),
        Route("/v/{token}/jmuxer.js", jmuxer, methods=["GET"]),
    ])

    async def guarded(scope, receive, send):
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}
        allowed = loopback | {h.lower() for h in allowed_hosts()}
        refusal = None
        if _host_of(headers.get("host", "")) not in allowed:
            refusal = (421, "unexpected Host")
        elif headers.get("sec-fetch-site", "").strip().lower() == "cross-site":
            refusal = (403, "cross-site request")
        elif "origin" in headers:
            origin = headers["origin"].split("://", 1)[-1]
            if _host_of(origin) not in allowed:     # "null" included
                refusal = (403, "unexpected Origin")
        if refusal is not None:
            await PlainTextResponse(refusal[1], status_code=refusal[0])(scope, receive, send)
            return
        await app(scope, receive, send)

    return guarded


class _Relay(StreamingResponse):
    """A stream whose slot and upstream connection are released however the
    response ends -- finished, failed, or the viewer gone before its first
    byte, when the generator never runs and its own `finally` never would."""

    def __init__(self, content, release, **kw) -> None:
        super().__init__(content, **kw)
        self._release = release

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with contextlib.suppress(BaseException):
                await self.body_iterator.aclose()
            await self._release()
