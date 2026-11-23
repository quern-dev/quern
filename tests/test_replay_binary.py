"""A binary body is replayed and released as its bytes (#755).

The addon stores a body it cannot decode as UTF-8 as base64 text, with
`body_encoding: "base64"`. Replay sent that text, so the server got something
else than the app sent, and the answer said success.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from server.models import FlowRecord, FlowRequest, FlowResponse
from tests.test_redact_body import _fresh  # noqa: F401
from tests.test_redact_surfaces import _call, _sent, app, running  # noqa: F401

RAW = bytes(range(256))
B64 = base64.b64encode(RAW).decode()


def _flow(fid: str, body: str | None, encoding: str = "utf-8") -> FlowRecord:
    return FlowRecord(
        id=fid, timestamp=datetime.now(UTC),
        request=FlowRequest(method="POST", url="https://e.com/upload", host="e.com",
                            path="/upload", headers={"Content-Type": "application/x-protobuf"},
                            body=body, body_size=len(RAW), body_encoding=encoding),
        response=FlowResponse(status_code=200))


async def _replay(app, fid, json):  # noqa: F811
    with patch("server.api.proxy_intercept.httpx.AsyncClient") as cls:
        client = AsyncMock()
        client.request = AsyncMock(return_value=AsyncMock(status_code=200))
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        cls.return_value = client
        resp = await _call(app, "POST", f"/api/v1/proxy/replay/{fid}", json=json)
    sent = client.request.call_args.kwargs["content"] if client.request.called else None
    return resp, sent


async def test_a_binary_body_is_replayed_as_its_bytes(app, running):  # noqa: F811
    await app.state.flow_store.add(_flow("b1", B64, "base64"))
    resp, sent = await _replay(app, "b1", {})
    assert resp.status_code == 200 and resp.json()["status"] == "success", resp.text
    assert sent == RAW


async def test_a_text_body_is_replayed_as_before(app, running):  # noqa: F811
    await app.state.flow_store.add(_flow("t1", '{"a":1}'))
    _, sent = await _replay(app, "t1", {})
    assert sent == b'{"a":1}'


async def test_a_binary_modify_body_is_sent_as_its_bytes(app, running):  # noqa: F811
    await app.state.flow_store.add(_flow("b2", '{"a":1}'))
    edited = base64.b64encode(b"\x00\x01edited").decode()
    _, sent = await _replay(app, "b2", {"modify_body": edited, "modify_body_encoding": "base64"})
    assert sent == b"\x00\x01edited"
    # Text modify_body over a binary capture is text.
    await app.state.flow_store.add(_flow("b3", B64, "base64"))
    _, sent = await _replay(app, "b3", {"modify_body": "plain"})
    assert sent == b"plain"


async def test_bad_base64_is_refused_and_nothing_goes_out(app, running):  # noqa: F811
    await app.state.flow_store.add(_flow("b4", '{"a":1}'))
    resp, sent = await _replay(app, "b4", {"modify_body": "not base64!",
                                           "modify_body_encoding": "base64"})
    assert resp.status_code == 400 and "does not decode" in resp.text
    assert sent is None


async def test_a_release_passes_a_base64_body_on_and_refuses_a_bad_one(app, running):  # noqa: F811
    running._held_flows["h1"] = {
        "id": "h1", "held_at": datetime.now(UTC),
        "request": {"method": "POST", "url": "https://e.com/u", "host": "e.com", "path": "/u",
                    "headers": {}, "body": B64, "body_encoding": "base64"}}
    bad = await _call(app, "POST", "/api/v1/proxy/intercept/release", json={
        "flow_id": "h1", "modifications": {"body": "%%%", "body_encoding": "base64"}})
    assert bad.status_code == 400 and _sent(running, "modify_and_release") == []
    odd = await _call(app, "POST", "/api/v1/proxy/intercept/release", json={
        "flow_id": "h1", "modifications": {"body": B64, "body_encoding": "hex"}})
    assert odd.status_code == 400
    ok = await _call(app, "POST", "/api/v1/proxy/intercept/release", json={
        "flow_id": "h1", "modifications": {"body": B64, "body_encoding": "base64"}})
    assert ok.status_code == 200, ok.text
    (sent,) = _sent(running, "modify_and_release")
    assert sent["modifications"]["body_encoding"] == "base64"
    assert base64.b64decode(sent["modifications"]["body"]) == RAW
