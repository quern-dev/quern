"""Who set each mock rule and intercept, and its end with the session that set it (#650).

A client key's rules already ended with its hold on their device (#254,
#646). The owner key's never ended: two agents sharing it left each other's
mocks answering requests, and a listing could not say whose a rule was. Now
every rule records the held session that set it, ends when that session does,
and is listed with its `owner` and `session`.
"""

# ruff: noqa: F811 -- `world` and `registry` are fixtures imported from other test modules

from __future__ import annotations

import pytest

from server import rule_owners
from server.api import sessions as sessions_api
from tests.test_scoped_intercepts import SIM_A, world  # noqa: F401 -- fixture
from tests.test_scopes import KEY, _h, keys, registry  # noqa: F401 -- fixtures

_MOCK = {"pattern": "~d x.test", "response": {"status_code": 418}}
SIM_C = "0F6C9B2E-5B1A-4C1D-9E7F-3A2B1C0D9E8F"   # held by nobody


def _owner_session(registry, name):
    registry.open(name)
    registry.get(name).state = "live"
    return registry.get(name)


def _mock(c, key, session, **body):
    r = c.post("/api/v1/proxy/mocks", headers=_h(key, session), json={**_MOCK, **body})
    assert r.status_code == 200, r.text
    return r.json()["rule_id"]


def _intercept(c, key, session, **body):
    r = c.post("/api/v1/proxy/intercept", headers=_h(key, session),
               json={"pattern": "~d x.test", **body})
    assert r.status_code == 200, r.text
    return r.json()["rule_id"]


def _mock_ids(adapter):
    return {r["rule_id"] for r in adapter._mock_rules}


def _intercept_ids(adapter):
    return {r["rule_id"] for r in adapter.intercept_rules()}


class TestEachRuleSaysWhoSetIt:
    def test_an_owner_sessions_rules_are_labelled_with_it(self, world):
        c, app, adapter, _, _ = world
        _owner_session(app.state.sessions, "agent-1")
        m = _mock(c, KEY, "agent-1")
        i = _intercept(c, KEY, "agent-1")
        listed = c.get("/api/v1/proxy/mocks", headers=_h(KEY)).json()["rules"]
        assert [(r["rule_id"], r["owner"], r["session"]) for r in listed] == [
            (m, "owner", "agent-1")]
        held = c.get("/api/v1/proxy/intercept/held", headers=_h(KEY)).json()["intercepts"]
        assert [(r["rule_id"], r["owner"], r["session"]) for r in held] == [
            (i, "owner", "agent-1")]

    def test_a_client_keys_rule_is_labelled_with_its_key(self, world):
        c, app, adapter, alice, _ = world
        m = _mock(c, alice, "s1", simulator_udid=SIM_A)
        listed = c.get("/api/v1/proxy/mocks", headers=_h(KEY)).json()["rules"]
        assert [(r["rule_id"], r["owner"], r["session"]) for r in listed] == [
            (m, "alice", "alice:s1")]

    def test_a_rule_set_with_no_held_session_has_none(self, world):
        """curl or a script: no session to end with, so it stays."""
        c, app, adapter, _, _ = world
        m = _mock(c, KEY, "")
        assert rule_owners.label(app, "mock", m) == {"owner": "owner", "session": None}

    def test_proxy_status_labels_its_intercepts(self, world):
        from server.api.proxy import _intercept_infos

        c, app, adapter, _, _ = world
        _owner_session(app.state.sessions, "agent-1")
        i = _intercept(c, KEY, "agent-1")
        infos = _intercept_infos(app, adapter)
        assert [(x.rule_id, x.owner, x.session) for x in infos] == [(i, "owner", "agent-1")]


class TestAnOwnerSessionsRulesEndWithIt:
    async def test_everything_it_set_ends_and_nothing_else(self, world):
        c, app, adapter, _, _ = world
        registry = app.state.sessions
        one = _owner_session(registry, "agent-1")
        _owner_session(registry, "agent-2")
        m1, i1 = _mock(c, KEY, "agent-1"), _intercept(c, KEY, "agent-1")
        m1s = _mock(c, KEY, "agent-1", simulator_udid=SIM_C)
        m2, i2 = _mock(c, KEY, "agent-2"), _intercept(c, KEY, "agent-2")
        loose = _mock(c, KEY, "")
        registry.release("agent-1")
        actions = await sessions_api._end_client_work(app, registry, one)
        assert _mock_ids(adapter) == {m2, loose}
        assert _intercept_ids(adapter) == {i2}
        removed = {(a["action"], a["rule_id"]) for a in actions if a.get("ok")}
        assert {("mock_removed", m1), ("mock_removed", m1s),
                ("intercept_removed", i1)} <= removed
        assert rule_owners.label(app, "mock", m1) == {"owner": None, "session": None}

    async def test_a_release_that_does_not_end_the_session_keeps_them(self, world):
        """An owner agent may release its devices and go on working."""
        c, app, adapter, _, _ = world
        registry = app.state.sessions
        one = _owner_session(registry, "agent-1")
        m = _mock(c, KEY, "agent-1")
        await sessions_api._end_client_work(app, registry, one)
        assert m in _mock_ids(adapter)

    async def test_a_later_opening_keeps_what_it_set(self, world):
        c, app, adapter, _, _ = world
        registry = app.state.sessions
        first = _owner_session(registry, "agent-1")
        m1 = _mock(c, KEY, "agent-1")
        registry.release("agent-1")
        _owner_session(registry, "agent-1")
        m2 = _mock(c, KEY, "agent-1")
        await sessions_api._end_client_work(app, registry, first)
        assert _mock_ids(adapter) == {m2}
        assert m1 not in _mock_ids(adapter)

    async def test_a_removal_that_fails_is_reported_and_keeps_the_rule(self, world):
        from unittest.mock import AsyncMock

        c, app, adapter, _, _ = world
        registry = app.state.sessions
        one = _owner_session(registry, "agent-1")
        m = _mock(c, KEY, "agent-1")
        registry.release("agent-1")
        adapter.clear_mock = AsyncMock(side_effect=RuntimeError("addon gone"))
        actions = await sessions_api._end_client_work(app, registry, one)
        failed = [a for a in actions if a.get("rule_id") == m]
        assert failed and failed[0]["ok"] is False and "udid" not in failed[0]
        assert rule_owners.label(app, "mock", m)["session"] == "agent-1"


class TestForgetting:
    def test_deleting_a_rule_forgets_its_owner(self, world):
        c, app, adapter, _, _ = world
        _owner_session(app.state.sessions, "agent-1")
        m = _mock(c, KEY, "agent-1")
        assert c.delete(f"/api/v1/proxy/mocks/{m}", headers=_h(KEY)).status_code == 200
        assert rule_owners.set_by(app, "mock", "agent-1",
                                  app.state.sessions.get("agent-1").opened_at) == set()

    def test_clearing_every_rule_forgets_every_owner(self, world):
        c, app, adapter, _, _ = world
        _owner_session(app.state.sessions, "agent-1")
        _mock(c, KEY, "agent-1")
        _intercept(c, KEY, "agent-1")
        assert c.delete("/api/v1/proxy/mocks", headers=_h(KEY)).status_code == 200
        assert c.delete("/api/v1/proxy/intercept", headers=_h(KEY)).status_code == 200
        assert app.state.mock_sessions == {} and app.state.intercept_sessions == {}


class TestReviewFindings:
    def test_the_owner_naming_a_clients_session_is_not_recorded_under_it(self, world):
        """Recorded under alice's session, the owner's every-device mock would
        end when alice disconnected (review)."""
        c, app, adapter, _, _ = world
        m = _mock(c, KEY, "alice:s1")
        assert rule_owners.label(app, "mock", m) == {"owner": "owner", "session": None}

    async def test_a_client_rule_ended_with_its_hold_is_forgotten(self, world):
        c, app, adapter, alice, _ = world
        registry = app.state.sessions
        m = _mock(c, alice, "s1", simulator_udid=SIM_A)
        held = registry.get("alice:s1")
        registry.release("alice:s1")
        await sessions_api._end_client_work(app, registry, held)
        assert m not in _mock_ids(adapter)
        assert m not in app.state.mock_owners and m not in app.state.mock_sessions

    def test_deleting_one_intercept_forgets_its_owner(self, world):
        c, app, adapter, _, _ = world
        _owner_session(app.state.sessions, "agent-1")
        i = _intercept(c, KEY, "agent-1")
        assert c.delete(f"/api/v1/proxy/intercept/{i}", headers=_h(KEY)).status_code == 200
        assert i not in app.state.intercept_owners and i not in app.state.intercept_sessions

    def test_a_clients_clear_forgets_the_owners_of_what_it_cleared(self, world):
        c, app, adapter, alice, _ = world
        i = _intercept(c, alice, "s1", simulator_udid=SIM_A)
        assert c.delete("/api/v1/proxy/intercept", headers=_h(alice)).status_code == 200
        assert i not in app.state.intercept_owners and i not in app.state.intercept_sessions

    async def test_a_failed_removal_that_took_the_rule_anyway_is_forgotten(self, world):
        from unittest.mock import AsyncMock

        c, app, adapter, _, _ = world
        registry = app.state.sessions
        one = _owner_session(registry, "agent-1")
        m = _mock(c, KEY, "agent-1")
        registry.release("agent-1")

        async def gone_then_failed(rule_id=None):
            adapter._mock_rules[:] = [r for r in adapter._mock_rules if r["rule_id"] != rule_id]
            raise RuntimeError("broadcast failed")

        adapter.clear_mock = AsyncMock(side_effect=gone_then_failed)
        actions = await sessions_api._end_client_work(app, registry, one)
        assert [a["ok"] for a in actions if a.get("rule_id") == m] == [False]
        assert rule_owners.label(app, "mock", m) == {"owner": None, "session": None}


class TestRoundOneOfReview:
    """CodeRabbit on #655."""

    async def test_a_rule_whose_session_ended_while_it_was_set_is_removed(self, world):
        from unittest.mock import patch

        c, app, adapter, _, _ = world
        registry = app.state.sessions
        _owner_session(registry, "agent-1")
        real = adapter.set_mock

        async def session_ends_meanwhile(*a, **k):
            rule_id = await real(*a, **k)
            registry.release("agent-1")
            return rule_id

        with patch.object(adapter, "set_mock", side_effect=session_ends_meanwhile):
            r = c.post("/api/v1/proxy/mocks", headers=_h(KEY, "agent-1"), json=_MOCK)
        assert r.status_code == 409 and "session ended" in r.json()["detail"]
        assert _mock_ids(adapter) == set()

    async def test_a_new_opening_meanwhile_does_not_adopt_the_rule(self, world):
        from unittest.mock import patch

        c, app, adapter, _, _ = world
        registry = app.state.sessions
        _owner_session(registry, "agent-1")
        real = adapter.set_intercept

        async def reopened_meanwhile(*a, **k):
            rule_id = await real(*a, **k)
            registry.release("agent-1")
            _owner_session(registry, "agent-1")
            return rule_id

        with patch.object(adapter, "set_intercept", side_effect=reopened_meanwhile):
            r = c.post("/api/v1/proxy/intercept", headers=_h(KEY, "agent-1"),
                       json={"pattern": "~d x.test"})
        assert r.status_code == 409
        assert _intercept_ids(adapter) == set()

    async def test_a_clients_rule_ended_with_its_session_says_so(self, world):
        c, app, adapter, alice, _ = world
        registry = app.state.sessions
        m = _mock(c, alice, "s1", simulator_udid=SIM_A)
        held = registry.get("alice:s1")
        registry.release("alice:s1")
        actions = await sessions_api._end_client_work(app, registry, held)
        assert [a.get("reason") for a in actions if a.get("rule_id") == m] == ["session_ended"]


class TestTheBroadcastReachesEveryProxy:
    async def test_a_removal_the_main_proxy_refuses_still_reaches_the_followers(self):
        """Raised first, a session proxy kept a rule the server had dropped
        (CodeRabbit on #655)."""
        from unittest.mock import AsyncMock

        from server.proxy.flow_store import FlowStore
        from server.sources.proxy import ProxyAdapter

        leader = ProxyAdapter(flow_store=FlowStore(), listen_port=9101)
        leader.send_command = AsyncMock(side_effect=BrokenPipeError("gone"))
        follower = ProxyAdapter(flow_store=FlowStore(), listen_port=9102)
        follower.send_command = AsyncMock()
        leader.followers = [follower]
        with pytest.raises(BrokenPipeError):
            await leader.broadcast({"action": "clear_mock", "rule_id": "m1"})
        follower.send_command.assert_awaited_once_with({"action": "clear_mock", "rule_id": "m1"})

    async def test_a_follower_that_cannot_take_a_command_is_stopped(self):
        from unittest.mock import AsyncMock

        from server.proxy.flow_store import FlowStore
        from server.sources.proxy import ProxyAdapter

        leader = ProxyAdapter(flow_store=FlowStore(), listen_port=9101)
        leader.send_command = AsyncMock()
        follower = ProxyAdapter(flow_store=FlowStore(), listen_port=9102)
        follower.send_command = AsyncMock(side_effect=BrokenPipeError("stdin gone"))
        follower.stop = AsyncMock()
        leader.followers = [follower]
        await leader.broadcast({"action": "clear_mock", "rule_id": "m1"})
        follower.stop.assert_awaited_once()

    @pytest.mark.parametrize("kind", ["mock", "intercept"])
    async def test_a_create_the_main_proxy_refuses_is_taken_back_everywhere(self, kind):
        """The followers got the rule; raised with it still recorded, no route
        registered an owner for it, and it never ended (CodeRabbit on #655)."""
        from unittest.mock import AsyncMock

        from server.proxy.flow_store import FlowStore
        from server.sources.proxy import ProxyAdapter

        leader = ProxyAdapter(flow_store=FlowStore(), listen_port=9101)
        leader.send_command = AsyncMock(side_effect=BrokenPipeError("gone"))
        follower = ProxyAdapter(flow_store=FlowStore(), listen_port=9102)
        follower.send_command = AsyncMock()
        leader.followers = [follower]
        with pytest.raises(BrokenPipeError):
            if kind == "mock":
                await leader.set_mock("~d x.test", {"status_code": 418})
            else:
                await leader.set_intercept("~d x.test")
        assert leader._mock_rules == [] and leader.intercept_rules() == []
        sent = [c.args[0]["action"] for c in follower.send_command.await_args_list]
        assert sent == [f"set_{kind}", f"clear_{kind}"]
