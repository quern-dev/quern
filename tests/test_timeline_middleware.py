"""`TimelineMiddleware`: it buffers the body, and must not swallow the rest.

While a timeline is active this middleware reads the request body so it can
label a screenshot, then hands the endpoint a `receive` that replays it. The
first version replayed it *forever*, so everything after the body -- including
`http.disconnect` -- never reached the endpoint.

That mattered because of #208: the auth middleware was fixed so handlers can
see a caller leave, and these ten action endpoints would have been the only
ones where they still could not. `tap_element` is among them, and its sweep is
what motivated the guard.
"""

from __future__ import annotations

import pytest
from starlette.requests import Request

from server.screenshot_timeline import ACTION_FORMATTERS, TimelineMiddleware


class _Timeline:
    udid = "SIM"
    entries: list = []

    def add_entry(self, *a, **k):
        pass


def _scope(app_state, path: str):
    return {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": [(b"content-type", b"application/json")],
        "app": app_state,
        "query_string": b"",
    }


class _App:
    """Just enough of a Starlette app for `request.app.state`."""

    class state:  # noqa: N801
        active_timeline = _Timeline()
        device_controller = None


ACTION_PATH = next(iter(ACTION_FORMATTERS))


@pytest.mark.asyncio
async def test_the_endpoint_still_reads_the_body():
    seen = {}

    async def app(scope, receive, send):
        seen["body"] = await Request(scope, receive).body()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    messages = [{"type": "http.request", "body": b'{"udid": "SIM"}', "more_body": False}]

    async def receive():
        return messages.pop(0)

    await TimelineMiddleware(app)(_scope(_App, ACTION_PATH), receive, _sink())

    assert seen["body"] == b'{"udid": "SIM"}'


@pytest.mark.asyncio
async def test_a_disconnect_after_the_body_reaches_the_endpoint():
    """The regression this file exists for: replaying the body forever hid
    every later message, so `is_disconnected()` always answered False."""
    disconnected = []

    async def app(scope, receive, send):
        request = Request(scope, receive)
        await request.body()
        disconnected.append(await request.is_disconnected())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    messages = [
        {"type": "http.request", "body": b'{"udid": "SIM"}', "more_body": False},
        {"type": "http.disconnect"},
    ]

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    await TimelineMiddleware(app)(_scope(_App, ACTION_PATH), receive, _sink())

    assert disconnected == [True], (
        "the endpoint could not see the client had gone, so the ten action "
        "endpoints stay unable to stop work the caller no longer wants"
    )


def _sink():
    async def send(message):
        pass
    return send
