"""Intercepts scoped to one device, several at once (#646).

There was one intercept for the whole server: each set_intercept replaced the
last, every request was matched against it whatever device sent it, and
clear_intercept released every held flow, whoever's. Now an intercept may name
the device whose requests it holds -- decided as a scoped mock's is, failing
closed -- several coexist, each has a rule_id and an owner, and a client key
may set them on devices it holds, clear and release only its own, and loses
them with its hold.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.api.proxy_intercept import router as intercept_router
from server.auth import APIKeyMiddleware
from server.device_access import AccessMiddleware
from server.proxy.addon import IOSDebugAddon
from server.proxy.flow_store import FlowStore
from server.scopes import ScopeMiddleware
from server.session import SessionMiddleware
from server.sources.proxy import ProxyAdapter
from tests.test_scopes import KEY, _h, _hold, keys, registry  # noqa: F401 -- fixtures

SIM_A = "32593887-01F3-47B8-AF34-B2D444DB37FA"
SIM_B = "8B17095C-B057-4E34-ADE9-52DCFB6F850F"


# ---------------------------------------------------------------------------
# The addon: which requests each intercept holds
# ---------------------------------------------------------------------------


def _flow(host="api.example.com"):
    flow = MagicMock()
    flow.request.pretty_host = host
    flow.request.method = "GET"
    flow.request.pretty_url = f"https://{host}/v1"
    flow.request.path = "/v1"
    flow.request.scheme = "https"
    flow.request.raw_content = b""
    flow.request.headers.items.return_value = []
    flow.request.timestamp_start = time.time()
    flow.response = None
    flow.metadata = {}
    flow.client_conn = MagicMock()
    return flow


def _rule(rule_id, scope=None, host="api.example.com"):
    return {"rule_id": rule_id, "pattern_str": f"~d {host}",
            "compiled": lambda f: f.request.pretty_host == host,
            "simulator_udid": scope, "client_ip": None}


@pytest.fixture
def addon():
    a = IOSDebugAddon()
    a._running = True
    a._report_start = lambda flow: None
    return a


def _from(simulator):
    """Requests attributed to `simulator`, as the process lookup would."""
    return patch("server.proxy.addon._flow_simulator_udid", return_value=simulator)


class TestTheAddon:
    def test_a_scoped_intercept_holds_only_its_simulators_requests(self, addon):
        addon._intercept_rules = [_rule("a", SIM_A)]
        mine, theirs = _flow(), _flow()
        with _from(SIM_A):
            addon.request(mine)
        with _from(SIM_B):
            addon.request(theirs)
        mine.intercept.assert_called_once()
        theirs.intercept.assert_not_called()
        assert list(addon._held_rule.values()) == ["a"]

    def test_a_request_whose_simulator_cannot_be_told_is_not_held(self, addon):
        """Fails closed, as a scoped mock does."""
        addon._intercept_rules = [_rule("a", SIM_A)]
        flow = _flow()
        with _from(None):
            addon.request(flow)
        flow.intercept.assert_not_called()

    def test_an_unscoped_intercept_still_holds_every_devices_requests(self, addon):
        addon._intercept_rules = [_rule("all")]
        flow = _flow()
        with _from(SIM_B):
            addon.request(flow)
        flow.intercept.assert_called_once()

    def test_a_scoped_intercept_is_checked_before_an_unscoped_one(self, addon):
        addon._intercept_rules = [_rule("all"), _rule("a", SIM_A)]
        flow = _flow()
        with _from(SIM_A):
            addon.request(flow)
        assert list(addon._held_rule.values()) == ["a"]

    def test_several_intercepts_coexist(self, addon):
        addon._handle_set_intercept({"rule_id": "a", "pattern": "~d a.test",
                                     "simulator_udid": SIM_A.lower()})
        addon._handle_set_intercept({"rule_id": "b", "pattern": "~d b.test",
                                     "simulator_udid": SIM_B})
        assert [(r["rule_id"], r["simulator_udid"]) for r in addon._intercept_rules] == [
            ("a", SIM_A), ("b", SIM_B)]

    def test_setting_a_rule_id_again_replaces_it_where_it_stands(self, addon):
        for rid in ("a", "b"):
            addon._handle_set_intercept({"rule_id": rid, "pattern": f"~d {rid}.test"})
        addon._handle_set_intercept({"rule_id": "a", "pattern": "~d new.test"})
        assert [(r["rule_id"], r["pattern_str"]) for r in addon._intercept_rules] == [
            ("a", "~d new.test"), ("b", "~d b.test")]

    def test_clearing_one_releases_only_the_flows_it_held(self, addon):
        addon._intercept_rules = [_rule("a", SIM_A), _rule("b", SIM_B)]
        held_a, held_b = _flow(), _flow()
        addon._held_flows = {"fa": (held_a, time.time()), "fb": (held_b, time.time())}
        addon._held_rule = {"fa": "a", "fb": "b"}
        addon._handle_clear_intercept({"rule_id": "a"})
        held_a.resume.assert_called_once()
        held_b.resume.assert_not_called()
        assert [r["rule_id"] for r in addon._intercept_rules] == ["b"]
        assert addon._held_rule == {"fb": "b"}

    def test_clearing_all_releases_everything(self, addon):
        addon._intercept_rules = [_rule("a", SIM_A), _rule("b")]
        flows = {"f1": (_flow(), time.time()), "f2": (_flow(), time.time())}
        addon._held_flows = dict(flows)
        addon._held_rule = {"f1": "a", "f2": "b"}
        addon._handle_clear_intercept({})
        assert addon._intercept_rules == [] and addon._held_flows == {}
        assert addon._held_rule == {}

    def test_a_release_forgets_which_intercept_held_it(self, addon):
        addon._held_flows = {"f": (_flow(), time.time())}
        addon._held_rule = {"f": "a"}
        addon._handle_release_flow({"flow_id": "f"})
        assert addon._held_rule == {}


# ---------------------------------------------------------------------------
# The routes: who may set, clear and release which
# ---------------------------------------------------------------------------


@pytest.fixture
def world(registry, keys):  # noqa: F811 -- the fixtures imported above
    alice, bob = keys.add("alice"), keys.add("bob")
    app = FastAPI()
    app.state.sessions, app.state.clients = registry, keys
    adapter = ProxyAdapter(flow_store=FlowStore(), listen_port=9101)
    adapter._running = True
    adapter.started_at = datetime.now(UTC)
    adapter.send_command = AsyncMock()
    app.state.proxy_adapter = adapter
    app.state.device_controller = SimpleNamespace(_active_udid=None)
    app.include_router(intercept_router)
    app.add_middleware(AccessMiddleware)
    app.add_middleware(ScopeMiddleware)
    app.add_middleware(APIKeyMiddleware, api_key=KEY, clients=keys)
    app.add_middleware(SessionMiddleware)
    _hold(registry, "alice:s1", "client:alice", SIM_A)
    _hold(registry, "bob:s1", "client:bob", SIM_B)
    with TestClient(app) as c:
        yield c, app, adapter, alice, bob


def _set(c, key, **body):
    return c.post("/api/v1/proxy/intercept", headers=_h(key), json={"pattern": "~d x.test", **body})


def _held(adapter, flow_id, rule_id):
    adapter._held_flows[flow_id] = {"id": flow_id, "held_at": datetime.now(UTC),
                                    "request": {"method": "GET", "url": "https://x.test/",
                                                "host": "x.test", "path": "/"},
                                    "rule_id": rule_id}


class TestTheRoutes:
    def test_a_client_sets_an_intercept_on_the_simulator_it_holds(self, world):
        c, app, adapter, alice, _ = world
        r = _set(c, alice, simulator_udid=SIM_A)
        assert r.status_code == 200, r.text
        rid = r.json()["rule_id"]
        assert [(x["rule_id"], x["simulator_udid"]) for x in adapter.intercept_rules()] == [
            (rid, SIM_A)]
        assert app.state.intercept_owners[rid] == "client:alice"

    def test_a_client_may_not_set_one_on_every_device(self, world):
        c, _, adapter, alice, _ = world
        r = _set(c, alice)
        assert r.status_code == 403 and "simulator_udid" in r.json()["detail"]
        assert adapter.intercept_rules() == []

    def test_a_client_may_not_set_one_on_a_simulator_it_does_not_hold(self, world):
        c, _, adapter, alice, _ = world
        r = _set(c, alice, simulator_udid=SIM_B)
        assert r.status_code in (403, 423), r.text
        assert adapter.intercept_rules() == []

    def test_one_sessions_intercept_does_not_replace_anothers(self, world):
        c, _, adapter, alice, bob = world
        a = _set(c, alice, simulator_udid=SIM_A).json()["rule_id"]
        b = _set(c, bob, simulator_udid=SIM_B).json()["rule_id"]
        owner = _set(c, KEY).json()["rule_id"]
        assert [x["rule_id"] for x in adapter.intercept_rules()] == [a, b, owner]
        # The every-device one is what active_intercept has always reported.
        assert adapter._intercept_pattern == "~d x.test"

    def test_a_clients_clear_removes_only_its_own_and_their_flows(self, world):
        c, _, adapter, alice, bob = world
        a = _set(c, alice, simulator_udid=SIM_A).json()["rule_id"]
        b = _set(c, bob, simulator_udid=SIM_B).json()["rule_id"]
        _held(adapter, "fa", a)
        _held(adapter, "fb", b)
        r = c.delete("/api/v1/proxy/intercept", headers=_h(alice))
        assert r.status_code == 200, r.text
        assert r.json()["count"] == 1 and r.json()["held_flows_released"] == 1
        assert [x["rule_id"] for x in adapter.intercept_rules()] == [b]
        assert set(adapter._held_flows) == {"fb"}

    def test_the_owners_clear_waits_for_the_devices_holders(self, world, registry):  # noqa: F811
        """As a mock clear-all does (#646 review): an intercept on a device
        another session holds is that session's to end, and a refusal leaves
        every intercept in place, not some."""
        c, app, adapter, alice, bob = world
        _set(c, alice, simulator_udid=SIM_A)
        _set(c, bob, simulator_udid=SIM_B)
        r = c.delete("/api/v1/proxy/intercept", headers=_h(KEY, session=None))
        assert r.status_code == 423, r.text
        assert len(adapter.intercept_rules()) == 2
        registry.release("alice:s1")
        registry.release("bob:s1")
        r = c.delete("/api/v1/proxy/intercept", headers=_h(KEY, session=None))
        assert r.status_code == 200 and r.json()["count"] == 2
        assert adapter.intercept_rules() == [] and app.state.intercept_owners == {}

    def test_removing_one_that_is_not_there_is_a_404(self, world):
        c, *_ = world
        r = c.delete("/api/v1/proxy/intercept/icpt_nope", headers=_h(KEY, session=None))
        assert r.status_code == 404

    def test_a_client_may_not_remove_an_every_device_intercept(self, world):
        c, _, adapter, alice, _ = world
        owner = _set(c, KEY).json()["rule_id"]
        r = c.delete(f"/api/v1/proxy/intercept/{owner}", headers=_h(alice))
        assert r.status_code == 403, r.text
        assert [x["rule_id"] for x in adapter.intercept_rules()] == [owner]

    def test_a_client_may_not_remove_one_on_a_device_another_holds(self, world):
        c, _, adapter, alice, bob = world
        b = _set(c, bob, simulator_udid=SIM_B).json()["rule_id"]
        r = c.delete(f"/api/v1/proxy/intercept/{b}", headers=_h(alice))
        assert r.status_code in (403, 423), r.text
        assert [x["rule_id"] for x in adapter.intercept_rules()] == [b]

    def test_a_client_releases_only_what_its_own_intercepts_held(self, world):
        c, _, adapter, alice, bob = world
        a = _set(c, alice, simulator_udid=SIM_A).json()["rule_id"]
        b = _set(c, bob, simulator_udid=SIM_B).json()["rule_id"]
        _held(adapter, "fa", a)
        _held(adapter, "fb", b)
        theirs = c.post("/api/v1/proxy/intercept/release", headers=_h(alice),
                        json={"flow_id": "fb"})
        assert theirs.status_code == 403, theirs.text
        mine = c.post("/api/v1/proxy/intercept/release", headers=_h(alice),
                      json={"flow_id": "fa"})
        assert mine.status_code == 200, mine.text
        assert set(adapter._held_flows) == {"fb"}

    def test_held_flows_say_which_intercept_held_them(self, world):
        c, _, adapter, alice, _ = world
        a = _set(c, alice, simulator_udid=SIM_A).json()["rule_id"]
        _held(adapter, "fa", a)
        body = c.get("/api/v1/proxy/intercept/held", headers=_h(KEY, session=None)).json()
        assert body["held_flows"][0]["rule_id"] == a
        assert [(i["rule_id"], i["simulator_udid"]) for i in body["intercepts"]] == [(a, SIM_A)]


# ---------------------------------------------------------------------------
# The adapter: what a restarted mitmdump is sent, and the hold ending
# ---------------------------------------------------------------------------


async def test_a_restarted_proxy_is_sent_every_intercept():
    adapter = ProxyAdapter(flow_store=FlowStore(), listen_port=9101)
    adapter.send_command = AsyncMock()
    adapter.broadcast = AsyncMock()
    a = await adapter.set_intercept("~d a.test", simulator_udid=SIM_A)
    b = await adapter.set_intercept("~d b.test")
    adapter.send_command.reset_mock()
    assert await adapter._send_rules()
    sent = [c.args[0] for c in adapter.send_command.await_args_list
            if c.args[0]["action"] == "set_intercept"]
    assert [(s["rule_id"], s["pattern"], s["simulator_udid"]) for s in sent] == [
        (a, "~d a.test", SIM_A), (b, "~d b.test", None)]


async def test_a_clients_intercepts_end_with_its_hold():
    from server.api import sessions as sessions_api

    adapter = ProxyAdapter(flow_store=FlowStore(), listen_port=9101)
    adapter.broadcast = AsyncMock()
    mine = await adapter.set_intercept("~d a.test", simulator_udid=SIM_A)
    other_device = await adapter.set_intercept("~d b.test", simulator_udid=SIM_B)
    owners_own = await adapter.set_intercept("~d c.test", simulator_udid=SIM_A)
    owners = {mine: "client:alice", other_device: "client:alice", owners_own: "owner"}
    app = SimpleNamespace(state=SimpleNamespace(
        device_controller=None, session_proxies=None, proxy_adapter=adapter,
        recordings=None, mock_owners={}, intercept_owners=owners, device_proxy_holds={}))
    # Released from both devices; SIM_B is still the key's through another
    # session, so its intercept there stays (review: this was untested).
    session = SimpleNamespace(id="alice:s1", principal="client:alice", opened_at=object(),
                              reservations=[SIM_A, SIM_B])
    still_hers = SimpleNamespace(session=SimpleNamespace(principal="client:alice"))
    holds = SimpleNamespace(holder_of=lambda udid: still_hers if udid == SIM_B else None,
                            get=lambda sid: None)
    actions = await sessions_api._end_client_work(app, holds, session)
    assert {"udid": SIM_A, "action": "intercept_removed", "ok": True, "rule_id": mine} in actions
    # Its intercept on a device it still holds, and the owner's, stay.
    assert [r["rule_id"] for r in adapter.intercept_rules()] == [other_device, owners_own]


async def test_the_addons_held_event_keeps_which_intercept_held_it():
    # Async: the adapter emits its log entry on the running loop, as it does
    # in the server.
    """The rule_id the addon reports is what a client's release and a
    one-intercept clear go by; dropped here, both act on the wrong flows."""
    adapter = ProxyAdapter(flow_store=FlowStore(), listen_port=9101)
    adapter._handle_intercepted({
        "type": "intercepted", "id": "f_1", "rule_id": "icpt_a", "timestamp": time.time(),
        "request": {"method": "GET", "url": "https://x.test/", "host": "x.test", "path": "/"}})
    assert adapter.held_by("f_1") == "icpt_a"
    assert adapter.get_held_flows()[0]["rule_id"] == "icpt_a"


async def test_clearing_one_intercept_keeps_the_others_held_flows_listed():
    """Found live: the addon's intercept_cleared event used to empty the
    server's whole list, so a flow another intercept still held vanished from
    list_held_flows while the request hung on."""
    adapter = ProxyAdapter(flow_store=FlowStore(), listen_port=9101)
    for flow_id, rule_id in (("fa", "icpt_a"), ("fu", "icpt_all")):
        adapter._handle_intercepted({
            "type": "intercepted", "id": flow_id, "rule_id": rule_id, "timestamp": time.time(),
            "request": {"method": "GET", "url": "https://x.test/", "host": "x.test", "path": "/"}})
    await adapter._handle_status_event({"type": "status", "event": "intercept_cleared",
                                        "rule_id": "icpt_a"})
    assert [h["id"] for h in adapter.get_held_flows()] == ["fu"]
    await adapter._handle_status_event({"type": "status", "event": "intercept_cleared",
                                        "rule_id": None})
    assert adapter.get_held_flows() == []


async def test_setting_a_scoped_intercept_does_not_report_it_as_every_devices():
    adapter = ProxyAdapter(flow_store=FlowStore(), listen_port=9101)
    adapter.broadcast = AsyncMock()
    await adapter.set_intercept("~d a.test", simulator_udid=SIM_A)
    await adapter._handle_status_event({"type": "status", "event": "intercept_set",
                                        "pattern": "~d a.test", "simulator_udid": SIM_A})
    assert adapter._intercept_pattern is None


# ---------------------------------------------------------------------------
# Gaps an adversarial review found (#646)
# ---------------------------------------------------------------------------


class TestReviewFindings:
    def test_a_scoped_intercept_on_an_emulator_holds_once_its_serial_is_known(self, addon):
        """The deferred path: the emulator is told off the hook's thread, and
        the request is held then, with the intercept that held it."""
        import server.proxy.addon as addon_module

        addon._intercept_rules = [_rule("emu", "emulator-5554")]
        flow = _flow()
        with patch.object(addon_module, "_flow_emulator_serial_now",
                          return_value=addon_module._UNDECIDED), \
                patch.object(addon_module, "_flow_emulator_serial_waiting",
                             return_value="emulator-5554"), \
                patch.object(addon_module._SCOPE_POOL, "submit", side_effect=lambda fn: fn()):
            addon.request(flow)
        flow.intercept.assert_called()
        assert list(addon._held_rule.values()) == ["emu"]
        flow.resume.assert_not_called()

    def test_a_clear_between_choosing_an_intercept_and_holding_wins(self, addon):
        """The rule is chosen outside the lock; a clear that lands before the
        hold must leave the request unheld, not held by a rule that is gone."""
        rule = _rule("a")
        matched = rule["compiled"]

        def compiled_then_cleared(flow):
            hit = matched(flow)
            addon._handle_clear_intercept({"rule_id": "a"})
            return hit

        rule["compiled"] = compiled_then_cleared
        addon._intercept_rules = [rule]
        flow = _flow()
        with _from(None):
            addon.request(flow)
        flow.intercept.assert_not_called()
        assert addon._held_flows == {} and addon._held_rule == {}

    def test_a_decoy_udid_does_not_make_an_unscoped_intercept_a_clients(self, world):
        c, _, adapter, alice, _ = world
        r = c.post("/api/v1/proxy/intercept", headers=_h(alice),
                   json={"pattern": "~d x.test", "udid": SIM_A})
        assert r.status_code == 403 and "simulator_udid" in r.json()["detail"]
        assert adapter.intercept_rules() == []

    def test_a_client_may_not_remove_the_owners_intercept_on_its_device(self, world, registry):  # noqa: F811
        """Clear, release and remove agree: a client's own only."""
        c, _, adapter, alice, _ = world
        registry.release("alice:s1")
        owner = _set(c, KEY, simulator_udid=SIM_A).json()["rule_id"]
        _hold(registry, "alice:s1", "client:alice", SIM_A)
        r = c.delete(f"/api/v1/proxy/intercept/{owner}", headers=_h(alice))
        assert r.status_code == 403 and "not yours" in r.json()["detail"]
        assert [x["rule_id"] for x in adapter.intercept_rules()] == [owner]

    def test_a_blind_client_sees_and_so_can_release_its_own_held_flows(self, world):
        from server import visibility

        c, _, adapter, alice, bob = world
        a = _set(c, alice, simulator_udid=SIM_A).json()["rule_id"]
        b = _set(c, bob, simulator_udid=SIM_B).json()["rule_id"]
        _held(adapter, "fa", a)
        _held(adapter, "fb", b)
        before, visibility.MODE = visibility.MODE, "blind"
        try:
            r = c.get("/api/v1/proxy/intercept/held", headers=_h(alice))
            assert r.status_code == 200, r.text
            body = r.json()
            assert [f["id"] for f in body["held_flows"]] == ["fa"]
            assert [i["rule_id"] for i in body["intercepts"]] == [a]
            assert body["pattern"] is None
            ok = c.post("/api/v1/proxy/intercept/release", headers=_h(alice),
                        json={"flow_id": "fa"})
            assert ok.status_code == 200, ok.text
        finally:
            visibility.MODE = before

    def test_blind_proxy_status_lists_only_the_callers_intercepts(self):
        from datetime import timedelta

        from server import visibility
        from server.api.proxy import _blind_status
        from server.models import InterceptRuleInfo, ProxyStatusResponse

        adapter = ProxyAdapter(flow_store=FlowStore(), listen_port=9101)
        for flow_id, rule_id in (("fa", "ia"), ("fb", "ib")):
            _held(adapter, flow_id, rule_id)
        app = SimpleNamespace(state=SimpleNamespace(
            flow_store=None, proxy_adapter=adapter,
            intercept_owners={"ia": "client:alice", "ib": "client:bob"}, mock_owners={}))
        status = ProxyStatusResponse(status="running", intercepts=[
            InterceptRuleInfo(rule_id="ia", pattern="~d a", simulator_udid=SIM_A),
            InterceptRuleInfo(rule_id="ib", pattern="~d b", simulator_udid=SIM_B)])
        view = visibility.View(since={SIM_A: datetime.now(UTC) - timedelta(seconds=30)})
        alice = visibility.Caller(session=None, principal="client:alice")
        with patch("server.visibility.current_caller", return_value=alice):
            import asyncio
            out = asyncio.run(_blind_status(SimpleNamespace(app=app), status, view))
        assert [i.rule_id for i in out.intercepts] == ["ia"]
        assert out.held_flows_count == 1


async def test_a_restart_waits_for_every_intercept_to_be_confirmed():
    """Counted, not just present: two intercepts sent and one echoed is a
    restart that did not bring them all back."""
    from collections import Counter

    adapter = ProxyAdapter(flow_store=FlowStore(), listen_port=9101)
    adapter._intercept_rules = [{"rule_id": "a"}, {"rule_id": "b"}]
    adapter._running = True
    adapter.restart_confirm_timeout = 0.2
    before = Counter()
    adapter._addon_events = Counter({"running": 1, "intercept_set": 1})
    assert await adapter._serving_with_rules(before) is False
    adapter._addon_events["intercept_set"] = 2
    assert await adapter._serving_with_rules(before) is True
