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


# -- who may have a placeholder put back (#756 review) -----------------------------


def _blind_world(world):
    from unittest.mock import AsyncMock

    from server.sources.proxy import ProxyAdapter

    adapter = ProxyAdapter(flow_store=world.app.state.flow_store, listen_port=9101)
    adapter._running = True
    adapter.started_at = datetime.now(UTC)
    adapter.send_command = AsyncMock()
    world.app.state.proxy_adapter = adapter
    return adapter


def _secret_on(udid: str, fid: str) -> FlowRecord:
    flow = _flow()
    flow.id, flow.simulator_udid = fid, udid
    flow.response.headers["Set-Cookie"] = f"sid={fid}-c00kie; Path=/"   # one value a device
    return flow


def test_a_blind_client_cannot_restore_a_placeholder_from_a_device_it_cannot_see(tmp_path):
    """Under blind sharing, a client key restores only a placeholder from a
    flow its view shows -- what reveal would show it. Otherwise it is refused
    as unknown, which does not say the placeholder exists."""
    import asyncio

    from tests.test_blind_mode import KEY, SIM_A, SIM_B, _h, world  # noqa: F401

    gen = world.__wrapped__(tmp_path)
    w = next(gen)
    try:
        _blind_world(w)
        asyncio.run(w.app.state.flow_store.add(_secret_on(SIM_B, "bob-secret")))
        asyncio.run(w.app.state.flow_store.add(_secret_on(SIM_A, "alice-secret")))
        owner = {"X-API-Key": KEY}
        bobs = w.client.get("/api/v1/proxy/flows/bob-secret", headers=owner).json()
        alices = w.client.get("/api/v1/proxy/flows/alice-secret", headers=_h(w.alice)).json()

        def set_from(key, shown):
            return w.client.post("/api/v1/proxy/mocks", headers=_h(key), json={
                "pattern": "~d e.com", "simulator_udid": SIM_A, "return_restored": True,
                "response": {"status_code": 200, "body": "", "headers": {
                    "Set-Cookie": shown["response"]["headers"]["Set-Cookie"]}}})

        refused = set_from(w.alice, bobs)                     # bob's device: never alice's
        assert refused.status_code == 400 and "not known to this server" in refused.text
        assert "bob-secret-c00kie" not in refused.text
        allowed = set_from(w.alice, alices)                   # her own device: as reveal
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["restored_response"]["headers"]["Set-Cookie"] == \
            "sid=alice-secret-c00kie; Path=/"
        # Released and reserved again, her view starts at the new reservation,
        # after the flow: she still holds the device the mock is for, and the
        # placeholder from before is refused like any other.
        from datetime import timedelta

        from tests.test_blind_mode import _hold

        w.registry.release("alice:s1")
        _hold(w.registry, "alice:s2", "client:alice", SIM_A,
              since=datetime.now(UTC) + timedelta(seconds=1))
        again = w.client.post("/api/v1/proxy/mocks", headers=_h(w.alice, "s2"), json={
            "pattern": "~d e.com", "simulator_udid": SIM_A, "return_restored": True,
            "response": {"status_code": 200, "body": "",
                         "headers": {"Set-Cookie": alices["response"]["headers"]["Set-Cookie"]}}})
        assert again.status_code == 400 and "not known to this server" in again.text
        assert "alice-secret-c00kie" not in again.text
    finally:
        gen.close()


def test_a_blind_client_releases_its_own_held_flow_with_the_placeholders_it_was_shown(tmp_path):
    """A held request has no stored flow to attribute it to a device by: its
    placeholders are the principal's whose intercept holds it -- the one
    that may release it (#756 review). Another client's are refused."""
    from tests.test_blind_mode import SIM_A, _h, world  # noqa: F401

    gen = world.__wrapped__(tmp_path)
    w = next(gen)
    try:
        adapter = _blind_world(w)
        adapter._intercept_rules = [{"rule_id": "ir-alice", "pattern": "~d e.com",
                                     "simulator_udid": SIM_A}]
        w.app.state.intercept_owners = {"ir-alice": "client:alice"}
        adapter._held_flows["h1"] = {
            "id": "h1", "held_at": datetime.now(UTC), "rule_id": "ir-alice",
            "request": {"method": "GET", "url": "https://e.com/me", "host": "e.com",
                        "path": "/me", "headers": {"Authorization": "Bearer held-t0k3n"}}}
        held = w.client.get("/api/v1/proxy/intercept/held", headers=_h(w.alice)).json()
        shown = held["held_flows"][0]["request"]["headers"]["Authorization"]
        assert "held-t0k3n" not in shown
        # bob, holding nothing of alice's, cannot have it put back anywhere.
        bob = w.client.post("/api/v1/proxy/mocks", headers=_h(w.bob), json={
            "pattern": "~d e.com", "return_restored": True,
            "response": {"status_code": 200, "body": "", "headers": {"Authorization": shown}}})
        assert "held-t0k3n" not in bob.text
        resp = w.client.post("/api/v1/proxy/intercept/release", headers=_h(w.alice), json={
            "flow_id": "h1", "modifications": {"headers": {"Authorization": shown}}})
        assert resp.status_code == 200, resp.text
        (sent,) = [c.args[0] for c in adapter.send_command.call_args_list
                   if c.args[0].get("action") == "modify_and_release"]
        assert sent["modifications"]["headers"]["Authorization"] == "Bearer held-t0k3n"
    finally:
        gen.close()


def test_a_reused_secrets_sources_stay_one_entry_a_device(monkeypatch):
    """A cookie sent on every request must not grow without bound (#756 review)."""
    from datetime import timedelta

    from server import visibility
    from server.proxy import redact

    monkeypatch.setattr(visibility, "MODE", "blind")
    base = datetime.now(UTC)
    flows = []
    for i in range(50):
        flow = _flow()
        flow.simulator_udid, flow.timestamp = "SIM-X", base + timedelta(seconds=i)
        flows.append(flow)
    redact.redact_flows(flows)
    entries = [v for v in redact._sources.values() if "SIM-X" in v]
    assert entries and all(len(v) == 1 for v in entries)
    assert all(v["SIM-X"] == base + timedelta(seconds=49) for v in entries)   # the latest


def test_a_blind_client_reuses_a_placeholder_from_its_own_listed_mock(tmp_path):
    """It set the real value, so what its listing shows is its to send back
    (#756 review). Another client's listed values never reach it at all."""
    from tests.test_blind_mode import SIM_A, SIM_B, _h, world  # noqa: F401

    gen = world.__wrapped__(tmp_path)
    w = next(gen)
    try:
        adapter = _blind_world(w)

        def set_mock(key, udid, cookie):
            r = w.client.post("/api/v1/proxy/mocks", headers=_h(key), json={
                "pattern": "~d e.com", "simulator_udid": udid,
                "response": {"status_code": 200, "body": "", "headers": {"Set-Cookie": cookie}}})
            assert r.status_code == 200, r.text
            return r.json()["rule_id"]

        mine = set_mock(w.alice, SIM_A, "sid=alice-real")
        set_mock(w.bob, SIM_B, "sid=bob-real")
        listed = w.client.get("/api/v1/proxy/mocks", headers=_h(w.alice)).json()["rules"]
        assert [r["rule_id"] for r in listed] == [mine]               # only her own
        shown = listed[0]["response"]["headers"]["Set-Cookie"]
        assert "alice-real" not in shown
        upd = w.client.patch(f"/api/v1/proxy/mocks/{mine}", headers=_h(w.alice), json={
            "response": {"status_code": 201, "headers": {"Set-Cookie": shown}}})
        assert upd.status_code == 200, upd.text
        sent = [c.args[0] for c in adapter.send_command.call_args_list
                if c.args[0].get("action") == "set_mock"][-1]
        assert sent["response"]["headers"]["Set-Cookie"] == "sid=alice-real"
    finally:
        gen.close()


def test_a_client_changes_and_removes_only_its_own_mock(tmp_path):
    """Holding the device a rule is scoped to is not enough: a previous
    holder's rule there is not the next holder's to change, or to read back
    through its answer (#756 review)."""
    from tests.test_blind_mode import SIM_A, _h, world  # noqa: F401

    gen = world.__wrapped__(tmp_path)
    w = next(gen)
    try:
        adapter = _blind_world(w)
        adapter._mock_rules = [{"rule_id": "bobs", "pattern": "~d e.com", "simulator_udid": SIM_A,
                                "response": {"status_code": 200, "body": "",
                                             "headers": {"Set-Cookie": "sid=bob-real"}}}]
        w.app.state.mock_owners = {"bobs": "client:bob"}
        upd = w.client.patch("/api/v1/proxy/mocks/bobs", headers=_h(w.alice),
                             json={"response": {"status_code": 201}})
        assert upd.status_code == 403 and "not yours" in upd.text
        assert "bob-real" not in upd.text
        gone = w.client.delete("/api/v1/proxy/mocks/bobs", headers=_h(w.alice))
        assert gone.status_code == 403
        assert [r["rule_id"] for r in adapter._mock_rules] == ["bobs"]        # still there
    finally:
        gen.close()
