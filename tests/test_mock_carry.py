"""A mock set from redacted placeholders can be carried to another server (#751).

The server puts each placeholder back to the real value it stands for. A
caller that asks (`return_restored`, which the MCP always sends) gets those
values back in `restored_response`, for the copy the MCP sets on the next
server after a session switch -- that server never showed the placeholders,
and refuses them. The MCP side is tested in mcp/test/use-remote.test.mjs.
"""

from __future__ import annotations

from datetime import UTC, datetime

from server.models import FlowRecord, FlowRequest, FlowResponse
from tests.test_redact_body import _fresh  # noqa: F401
from tests.test_redact_surfaces import _call, _sent, app, running  # noqa: F401

COOKIE = "sid=c00kie; Path=/"
TOKENS = '{"access_token":"t0k3n","expires_in":5}'


def _flow() -> FlowRecord:
    return FlowRecord(
        id="m1", timestamp=datetime.now(UTC),
        request=FlowRequest(method="POST", url="https://e.com/token", host="e.com",
                            path="/token"),
        response=FlowResponse(status_code=200, body=TOKENS, body_size=len(TOKENS),
                              headers={"Set-Cookie": COOKIE,
                                       "Content-Type": "application/json"}))


async def _shown(app):  # noqa: F811
    await app.state.flow_store.add(_flow())
    return (await _call(app, "GET", "/api/v1/proxy/flows/m1")).json()["response"]


async def _set(app, shown, **extra):  # noqa: F811
    return await _call(app, "POST", "/api/v1/proxy/mocks", json={
        "pattern": "~d e.com", **extra,
        "response": {"status_code": 200, "body": shown["body"],
                     "headers": {"Set-Cookie": shown["headers"]["Set-Cookie"],
                                 "content-type": "application/json"}}})


async def test_asked_the_real_values_come_back_and_the_carry_warning_goes(app, running):  # noqa: F811
    resp = await _set(app, await _shown(app), return_restored=True)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["restored_response"] == {
        "headers": {"Set-Cookie": COOKIE, "content-type": "application/json"},
        "body": TOKENS}
    assert "not recreated on another server" not in body["note"]
    assert body["restored"] == ["Set-Cookie", "body"]


async def test_not_asked_nothing_comes_back_and_the_warning_stays(app, running):  # noqa: F811
    """REST, or an MCP from before #751: it keeps the placeholders."""
    body = (await _set(app, await _shown(app))).json()
    assert "restored_response" not in body
    assert "not recreated on another server" in body["note"]


async def test_with_no_placeholder_there_is_nothing_to_return(app, running):  # noqa: F811
    body = (await _call(app, "POST", "/api/v1/proxy/mocks", json={
        "pattern": "~d e.com", "return_restored": True,
        "response": {"status_code": 200, "body": "{}"}})).json()
    assert "restored_response" not in body and "restored" not in body


async def test_an_update_returns_what_it_restored(app, running):  # noqa: F811
    shown = await _shown(app)
    rule_id = (await _call(app, "POST", "/api/v1/proxy/mocks", json={
        "pattern": "~d e.com", "response": {"status_code": 200, "body": ""}})).json()["rule_id"]
    resp = await _call(app, "PATCH", f"/api/v1/proxy/mocks/{rule_id}", json={
        "return_restored": True, "response": {"body": shown["body"]}})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["restored_response"] == {"body": TOKENS}
    assert "t0k3n" not in repr(body["response"])           # the echoed rule stays redacted
    assert "not recreated on another server" not in body["note"]
    assert _sent(running, "set_mock")[-1]["response"]["body"] == TOKENS
