"""Session proxies: capture apps the main proxy does not, without restarting it (#558).

These drive real `ProxyAdapter`s -- leader and followers -- with only the spawn
(`_spawn`) and the stdin pipe (`send_command`) faked, so the rule broadcast,
held-flow routing, pending drops and claim bookkeeping are the production code.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from server.api import session_capture as api
from server.models import SessionCaptureRequest
from server.proxy import session_proxies as sp_mod
from server.proxy import sim_tls
from server.proxy.flow_store import FlowStore
from server.sources import proxy as proxy_mod
from server.sources.proxy import ProxyAdapter

ALPHA = "32593887-01F3-47B8-AF34-B2D444DB37FA"
BRAVO = "C50EEEA6-F45D-4E9E-9DC6-F804F6BA0146"
ECHOES = {"set_mock": "mock_set", "set_bypass": "bypass_updated",
          "set_intercept": "intercept_set"}


@pytest.fixture(autouse=True)
def _no_state_file(monkeypatch):
    writes: list[dict] = []
    monkeypatch.setattr(proxy_mod, "update_state", lambda **kw: writes.append(kw))
    return writes


def _fake_io(a: ProxyAdapter, *, starts: bool = True) -> ProxyAdapter:
    """Fake the process: record what is sent, echo rules as the addon does."""
    a.sent = []
    a.starts = starts

    async def send(command):
        if not a._running:
            return
        a.sent.append(command)
        if event := ECHOES.get(command["action"]):
            await a._handle_status_event(
                {"event": event, **{k: v for k, v in command.items() if k != "action"}})

    async def spawn():
        a._running = a.starts
        if a.starts:
            await a._handle_status_event({"event": "running"})
        else:
            a._error = "mitmdump not found"
        return a.starts

    a.send_command = send
    a._spawn = spawn
    return a


def _leader(store: FlowStore | None = None) -> ProxyAdapter:
    a = _fake_io(ProxyAdapter(flow_store=store or FlowStore(),
                              local_capture_processes=["MobileSafari"]))
    a._running = True
    a.restart_confirm_timeout = 0.3
    return a


def _manager(leader: ProxyAdapter, tmp_path, *, starts: bool = True):
    made: list[ProxyAdapter] = []

    def make(names):
        a = _fake_io(ProxyAdapter(flow_store=leader.flow_store, listen_port=None,
                                  local_capture_processes=names), starts=starts)
        made.append(a)
        return a

    m = sp_mod.SessionProxies(leader, tmp_path / "session-proxies.json", make_adapter=make)
    m.made = made
    return m


# ---------------------------------------------------------------------------
# Claims: what is served by whom
# ---------------------------------------------------------------------------


async def test_names_the_main_proxy_captures_start_nothing(tmp_path):
    leader = _leader()
    m = _manager(leader, tmp_path)
    result = await m.claim(session="s1", principal="client:a", udid=ALPHA,
                           names=["MobileSafari"])
    assert result == {"main_proxy": ["MobileSafari"], "shared": [], "started": [],
                      "confirmed": None, "added": []}
    assert m.made == [] and leader.followers == []


async def test_a_new_name_gets_a_session_proxy_in_local_mode_only(tmp_path):
    leader = _leader()
    m = _manager(leader, tmp_path)
    result = await m.claim(session="s1", principal="client:a", udid=ALPHA,
                           names=["MobileSafari", "Metatext"])
    assert result["main_proxy"] == ["MobileSafari"]
    assert result["started"] == ["Metatext"] and result["confirmed"] is True
    follower = m.made[0]
    assert follower.local_capture_processes == ["Metatext"]
    assert follower.listen_port is None      # no listener, no port to collide on
    assert follower.leader is leader and leader.followers == [follower]
    assert follower._rules_lock is leader._rules_lock


async def test_a_running_session_proxy_counts_as_local_capture(tmp_path):
    """So the CA gate and the trust refresh run for its simulators, on a server
    whose own list is empty."""
    leader = _leader()
    leader.local_capture_processes = []
    assert not leader.local_capture
    m = _manager(leader, tmp_path)
    await m.claim(session="s1", principal="client:a", udid=ALPHA, names=["Metatext"])
    assert leader.local_capture
    await m.release(principal="client:a")
    assert not leader.local_capture


async def test_a_second_session_shares_the_proxy(tmp_path):
    leader = _leader()
    m = _manager(leader, tmp_path)
    await m.claim(session="s1", principal="client:a", udid=ALPHA, names=["Metatext"])
    result = await m.claim(session="s2", principal="client:b", udid=BRAVO,
                           names=["Metatext"])
    assert result["shared"] == ["Metatext"] and result["started"] == []
    assert len(m.made) == 1


async def test_the_proxy_stops_when_the_last_claim_on_it_goes(tmp_path):
    leader = _leader()
    m = _manager(leader, tmp_path)
    await m.claim(session="s1", principal="client:a", udid=ALPHA, names=["Metatext"])
    await m.claim(session="s2", principal="client:b", udid=BRAVO, names=["Metatext"])
    follower = m.made[0]

    assert await m.release(principal="client:a") == []
    assert follower.is_running and leader.followers == [follower]

    assert await m.release(principal="client:b") == ["Metatext"]
    assert not follower.is_running and leader.followers == []
    assert m.claims() == []


async def test_a_release_names_its_own_principal_only(tmp_path):
    leader = _leader()
    m = _manager(leader, tmp_path)
    await m.claim(session="s1", principal="client:a", udid=ALPHA, names=["Metatext"])
    assert await m.release(principal="client:b") == []
    assert m.made[0].is_running


async def test_releasing_one_name_keeps_the_others(tmp_path):
    leader = _leader()
    m = _manager(leader, tmp_path)
    await m.claim(session="s1", principal="client:a", udid=ALPHA, names=["Metatext"])
    await m.claim(session="s1", principal="client:a", udid=ALPHA, names=["Ivory"])
    assert await m.release(principal="client:a",
                           names=["Ivory"]) == ["Ivory"]
    assert [c.names for c in m.claims()] == [["Metatext"]]


async def test_a_proxy_that_did_not_start_is_not_listed_or_claimed(tmp_path):
    leader = _leader()
    m = _manager(leader, tmp_path, starts=False)
    with pytest.raises(sp_mod.SessionCaptureError, match="mitmdump not found"):
        await m.claim(session="s1", principal="client:a", udid=ALPHA, names=["Metatext"])
    assert leader.followers == [] and m.claims() == [] and m.describe() == []


async def test_a_start_that_raises_after_spawning_stops_the_process(tmp_path):
    """Found live: the start log raised after mitmdump had spawned, and the
    process stayed up, unlisted, holding its name."""
    leader = _leader()
    m = _manager(leader, tmp_path)
    real_make = m._make_adapter

    def make(names):
        a = real_make(names)
        real_spawn = a._spawn

        async def spawn_then_fail():
            await real_spawn()
            raise TypeError("a log line that cannot format")

        a._spawn = spawn_then_fail
        return a

    m._make_adapter = make
    with pytest.raises(sp_mod.SessionCaptureError, match="cannot format"):
        await m.claim(session="s1", principal="client:a", udid=ALPHA, names=["Metatext"])
    assert not m.made[0].is_running
    assert leader.followers == [] and m.claims() == []


async def test_a_proxy_that_died_is_started_again(tmp_path):
    leader = _leader()
    m = _manager(leader, tmp_path)
    await m.claim(session="s1", principal="client:a", udid=ALPHA, names=["Metatext"])
    m.made[0]._running = False                      # mitmdump exited on its own
    result = await m.claim(session="s2", principal="client:b", udid=BRAVO,
                           names=["Metatext"])
    assert result["started"] == ["Metatext"] and len(m.made) == 2
    assert leader.followers == [m.made[1]]


async def test_stop_all_stops_every_proxy_and_forgets_every_claim(tmp_path):
    leader = _leader()
    m = _manager(leader, tmp_path)
    await m.claim(session="s1", principal="client:a", udid=ALPHA, names=["Metatext"])
    await m.claim(session="s2", principal="client:b", udid=BRAVO, names=["Ivory"])
    assert sorted(await m.stop_all()) == ["Ivory", "Metatext"]
    assert leader.followers == [] and m.claims() == []


async def test_describe_shows_a_client_only_what_its_claims_use(tmp_path):
    leader = _leader()
    m = _manager(leader, tmp_path)
    await m.claim(session="s1", principal="client:a", udid=ALPHA, names=["Metatext"])
    await m.claim(session="s2", principal="client:b", udid=BRAVO, names=["Ivory"])
    mine = m.describe("client:a")
    assert [d["processes"] for d in mine] == [["Metatext"]]
    assert mine[0]["udids"] == [ALPHA]
    assert len(m.describe()) == 2


# ---------------------------------------------------------------------------
# A session proxy follows the main one
# ---------------------------------------------------------------------------


async def test_a_new_proxy_gets_the_rules_already_set(tmp_path):
    leader = _leader()
    await leader.set_mock("~d api.example.com", {"status_code": 503}, rule_id="m1",
                          simulator_udid=ALPHA)
    await leader.set_bypass(["apple.com"])
    await leader.set_intercept("~d auth.example.com")
    m = _manager(leader, tmp_path)
    await m.claim(session="s1", principal="client:a", udid=ALPHA, names=["Metatext"])
    sent = m.made[0].sent
    assert [c["rule_id"] for c in sent if c["action"] == "set_mock"] == ["m1"]
    assert {"action": "set_bypass", "patterns": ["apple.com"]} in sent
    assert {"action": "set_intercept", "pattern": "~d auth.example.com"} in sent


async def test_a_rule_set_later_reaches_every_proxy(tmp_path):
    leader = _leader()
    m = _manager(leader, tmp_path)
    await m.claim(session="s1", principal="client:a", udid=ALPHA, names=["Metatext"])
    await leader.set_mock("~d late.example.com", {"status_code": 418}, rule_id="late")
    await leader.clear_mock(rule_id="late")
    await leader.set_trusted_simulators([ALPHA])
    actions = [c["action"] for c in m.made[0].sent]
    assert actions == ["set_mock", "clear_mock", "set_trusted_simulators"]


async def test_a_rule_set_while_a_proxy_starts_is_not_lost(tmp_path):
    """The start sends the rules and the follower is listed before it runs, so
    a rule changed in between reaches it one way or the other."""
    leader = _leader()
    m = _manager(leader, tmp_path)
    gate, spawning = asyncio.Event(), asyncio.Event()
    real_make = m._make_adapter

    def make(names):
        a = real_make(names)
        real_spawn = a._spawn

        async def slow_spawn():
            spawning.set()
            await asyncio.wait_for(gate.wait(), timeout=1)
            return await real_spawn()

        a._spawn = slow_spawn
        return a

    m._make_adapter = make
    claim = asyncio.create_task(
        m.claim(session="s1", principal="client:a", udid=ALPHA, names=["Metatext"]))
    await asyncio.wait_for(spawning.wait(), timeout=1)
    mock = asyncio.create_task(
        leader.set_mock("~d mid.example.com", {"status_code": 418}, rule_id="mid"))
    await asyncio.sleep(0)
    gate.set()
    await asyncio.wait_for(asyncio.gather(claim, mock), timeout=2)
    assert "mid" in [c["rule_id"] for c in m.made[0].sent if c["action"] == "set_mock"]


async def test_a_held_flow_is_listed_and_released_through_the_main_adapter(tmp_path):
    leader = _leader()
    m = _manager(leader, tmp_path)
    await m.claim(session="s1", principal="client:a", udid=ALPHA, names=["Metatext"])
    follower = m.made[0]
    waiting = asyncio.create_task(leader.wait_for_held(2))
    await asyncio.sleep(0)
    follower._handle_intercepted({"id": "f1", "request": {"method": "GET", "path": "/"}})

    assert await asyncio.wait_for(waiting, timeout=2) is True
    assert [h["id"] for h in leader.get_held_flows()] == ["f1"]
    assert leader.held_count() == 1 and leader.holds("f1")
    await leader.release_flow("f1")
    assert {"action": "release_flow", "flow_id": "f1"} in follower.sent
    assert {"action": "release_flow", "flow_id": "f1"} not in leader.sent
    leader.forget_held("f1")
    assert leader.held_count() == 0


async def test_a_session_proxy_never_writes_state_json(tmp_path, _no_state_file):
    leader = _leader()
    m = _manager(leader, tmp_path)
    await m.claim(session="s1", principal="client:a", udid=ALPHA, names=["Metatext"])
    follower = m.made[0]
    await follower._handle_status_event({"event": "started"})
    await follower._handle_status_event({"event": "stopped"})
    await m.release(principal="client:a")
    assert _no_state_file == []
    await leader._handle_status_event({"event": "started"})
    assert _no_state_file == [{"proxy_status": "running"}]


async def test_passthrough_and_rejections_count_every_proxy(tmp_path):
    leader = _leader()
    m = _manager(leader, tmp_path)
    await m.claim(session="s1", principal="client:a", udid=ALPHA, names=["Metatext"])
    follower = m.made[0]
    leader._handle_tls_passthrough({"simulator_udid": ALPHA, "sni": "a.com", "timestamp": 1})
    follower._handle_tls_passthrough({"simulator_udid": ALPHA, "sni": "b.com", "timestamp": 2})
    counts = leader.passthrough_counts()
    assert counts[ALPHA]["connections"] == 2 and counts[ALPHA]["last_host"] == "b.com"
    follower._tls_rejections.append(SimpleNamespace(simulator_udid=ALPHA))
    assert len(leader.tls_rejections()) == 1


# ---------------------------------------------------------------------------
# The shared flow store: one proxy stopping drops only its own requests
# ---------------------------------------------------------------------------


def _flow(flow_id: str):
    return SimpleNamespace(id=flow_id)


def test_dropping_named_pending_leaves_the_others():
    store = FlowStore()
    store._pending.update({"a": _flow("a"), "b": _flow("b")})
    told: list[int] = []
    store.pending_dropped_listeners.append(told.append)
    assert store.drop_pending({"a"}) == 1
    assert list(store._pending) == ["b"] and told == [1]


def test_a_quiet_drop_of_nothing_tells_no_listener():
    """A session proxy stopping with nothing in flight is not news to every
    recording; the main proxy stopping is, even with nothing in flight."""
    store = FlowStore()
    told: list[int] = []
    store.pending_dropped_listeners.append(told.append)
    store.drop_pending(set(), announce=False)
    assert told == []
    store.drop_pending(set())
    assert told == [0]


async def test_each_adapter_tracks_its_own_pending(tmp_path):
    leader = _leader()
    m = _manager(leader, tmp_path)
    await m.claim(session="s1", principal="client:a", udid=ALPHA, names=["Metatext"])
    follower = m.made[0]
    started = {"request": {"method": "GET", "url": "http://x/", "host": "x", "path": "/"},
               "timestamp": 1}
    leader._handle_started({**started, "id": "lead"})
    follower._handle_started({**started, "id": "foll"})
    assert follower._pending_ids == {"foll"} and leader._pending_ids == {"lead"}

    # Finished, it is no longer this proxy's to drop.
    await follower._handle_flow({**started, "id": "foll", "response": {"status_code": 200}})
    assert follower._pending_ids == set()


# ---------------------------------------------------------------------------
# Leftovers from a server that died
# ---------------------------------------------------------------------------


def test_leftovers_are_stopped_only_when_still_session_mitmdumps(tmp_path, monkeypatch):
    pid_file = tmp_path / "session-proxies.json"
    pid_file.write_text('{"pids": [101, 102, 103]}')
    addon = str(proxy_mod.ADDON_PATH)
    commands = {
        "101": f"/v/bin/mitmdump -s {addon} --quiet --mode local:Metatext",
        "102": "/usr/bin/vim notes.txt",                         # pid reused
        "103": f"/v/bin/mitmdump -s {addon} --mode regular@9101 --mode local:Safari",
    }
    monkeypatch.setattr(sp_mod.subprocess, "run", lambda cmd, **kw: SimpleNamespace(
        stdout=commands[cmd[2]]))
    killed: list[int] = []
    monkeypatch.setattr(sp_mod.os, "kill", lambda pid, sig: killed.append(pid))

    assert sp_mod.SessionProxies.clean_up_leftovers(pid_file) == [101]
    assert killed == [101]
    assert not pid_file.exists()


def test_no_pid_file_is_nothing_to_do(tmp_path):
    assert sp_mod.SessionProxies.clean_up_leftovers(tmp_path / "missing.json") == []


async def test_running_proxies_are_written_down(tmp_path):
    leader = _leader()
    m = _manager(leader, tmp_path)
    await m.claim(session="s1", principal="client:a", udid=ALPHA, names=["Metatext"])
    m.made[0]._process = SimpleNamespace(pid=4242, returncode=0)
    m._write_pids()
    assert '"pids": [4242]' in m.pid_file.read_text()
    await m.release(principal="client:a")
    assert not m.pid_file.exists()


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------


def _request(leader, manager):
    req = MagicMock()
    req.app.state.proxy_adapter = leader
    req.app.state.session_proxies = manager
    return req


@pytest.fixture
def as_client(monkeypatch):
    caller = SimpleNamespace(session="client:a/s1", principal="client:a", is_owner=False)
    monkeypatch.setattr(api.visibility, "current_caller", lambda: caller)
    monkeypatch.setattr(api.device_access, "why_not", lambda udid: None)
    monkeypatch.setattr(api.sim_tls, "ensure_capturable",
                        AsyncMock(return_value=sim_tls.CaptureCheck()))
    return caller


async def _post(leader, manager, **body):
    return await api.add_session_capture.__wrapped__(
        _request(leader, manager), SessionCaptureRequest(simulator_udid=ALPHA, **body))


@pytest.mark.parametrize("processes", [["!Safari"], ["4242"], []])
async def test_a_client_names_apps_only(tmp_path, as_client, processes):
    leader = _leader()
    with pytest.raises(HTTPException) as e:
        await _post(leader, _manager(leader, tmp_path), processes=processes)
    assert e.value.status_code == 400


async def test_a_simulator_the_caller_does_not_hold_is_refused(tmp_path, as_client,
                                                               monkeypatch):
    monkeypatch.setattr(api.device_access, "why_not", lambda udid: "not reserved")
    leader = _leader()
    m = _manager(leader, tmp_path)
    with pytest.raises(HTTPException) as e:
        await _post(leader, m, processes=["Metatext"])
    assert e.value.status_code == 403 and "Reserve" in e.value.detail
    assert m.made == []


async def test_a_stopped_proxy_is_refused(tmp_path, as_client):
    leader = _leader()
    leader._running = False
    with pytest.raises(HTTPException) as e:
        await _post(leader, _manager(leader, tmp_path), processes=["Metatext"])
    assert e.value.status_code == 409


async def test_a_ca_refusal_hands_the_names_back(tmp_path, as_client, monkeypatch):
    monkeypatch.setattr(api.sim_tls, "ensure_capturable", AsyncMock(
        side_effect=sim_tls.CaptureNotReady({"message": "no CA"})))
    leader = _leader()
    m = _manager(leader, tmp_path)
    with pytest.raises(HTTPException) as e:
        await _post(leader, m, processes=["Metatext"], only=True)
    assert e.value.status_code == 428
    assert m.claims() == [] and leader.followers == []


async def test_the_web_view_minimum_is_added_unless_only(tmp_path, as_client):
    leader = _leader()
    m = _manager(leader, tmp_path)
    resp = await _post(leader, m, processes=["Metatext"])
    assert "com.apple.WebKit.Networking" in resp.processes
    assert resp.main_proxy == ["MobileSafari"]
    assert "Metatext" in resp.started and resp.confirmed is True
    assert any("relaunch" in w for w in resp.warnings)


# ---------------------------------------------------------------------------
# A client's capture ends with its hold on the device
# ---------------------------------------------------------------------------


async def test_ending_a_clients_hold_releases_its_capture(tmp_path):
    from server.api import sessions as sessions_api

    leader = _leader()
    m = _manager(leader, tmp_path)
    await m.claim(session="client:a/s1", principal="client:a", udid=ALPHA,
                  names=["Metatext"])
    app = SimpleNamespace(state=SimpleNamespace(
        session_proxies=m, proxy_adapter=None, recordings=None, mock_owners={}))
    registry = SimpleNamespace(holder_of=lambda udid: None)
    session = SimpleNamespace(id="client:a/s1", principal="client:a",
                              reservations=[ALPHA])

    actions = await sessions_api._end_client_work(app, registry, session)

    assert {"udid": ALPHA, "action": "session_capture_released", "ok": True,
            "stopped": ["Metatext"]} in actions
    assert m.claims() == [] and leader.followers == []


async def test_a_hold_kept_through_another_session_keeps_the_capture(tmp_path):
    from server.api import sessions as sessions_api

    leader = _leader()
    m = _manager(leader, tmp_path)
    await m.claim(session="client:a/s1", principal="client:a", udid=ALPHA,
                  names=["Metatext"])
    app = SimpleNamespace(state=SimpleNamespace(
        session_proxies=m, proxy_adapter=None, recordings=None, mock_owners={}))
    still = SimpleNamespace(session=SimpleNamespace(principal="client:a"))
    registry = SimpleNamespace(holder_of=lambda udid: still)
    session = SimpleNamespace(id="client:a/s1", principal="client:a",
                              reservations=[ALPHA])

    await sessions_api._end_client_work(app, registry, session)
    assert [c.names for c in m.claims()] == [["Metatext"]]

    # ...and the key's other session, which holds it now, can hand it back:
    # a claim keyed by the session that made it could be released by neither.
    assert await m.release(principal="client:a", udid=ALPHA) == ["Metatext"]


# ---------------------------------------------------------------------------
# Review findings
# ---------------------------------------------------------------------------


async def test_a_claim_behind_a_stop_does_not_start_a_proxy(tmp_path):
    """stop_proxy took the lock first and stopped the main proxy; the claim
    that waited behind it must not start one that runs on after "stopped"."""
    leader = _leader()
    m = _manager(leader, tmp_path)
    leader._running = False
    with pytest.raises(sp_mod.MainProxyStopped):
        await m.claim(session="s1", principal="client:a", udid=ALPHA, names=["Metatext"])
    assert m.made == [] and m.claims() == []


async def test_stop_proxy_stops_the_session_proxies(tmp_path):
    from server.api import proxy as proxy_api

    leader = _leader()
    m = _manager(leader, tmp_path)
    await m.claim(session="s1", principal="client:a", udid=ALPHA, names=["Metatext"])
    req = _request(leader, m)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(proxy_api, "update_state", lambda **kw: None)
        mp.setattr(proxy_api, "_get_proxy_status", AsyncMock(
            return_value=proxy_api.ProxyStatusResponse(status="stopped")))
        await proxy_api.stop_proxy.__wrapped__(req)
    assert not m.made[0].is_running and m.claims() == [] and not leader.is_running


async def test_a_ca_refusal_keeps_names_an_earlier_call_claimed(tmp_path, as_client,
                                                                 monkeypatch):
    leader = _leader()
    m = _manager(leader, tmp_path)
    await _post(leader, m, processes=["Metatext"], only=True)
    monkeypatch.setattr(api.sim_tls, "ensure_capturable", AsyncMock(
        side_effect=sim_tls.CaptureNotReady({"message": "no CA"})))
    with pytest.raises(HTTPException):
        await _post(leader, m, processes=["Metatext", "Ivory"], only=True)
    assert [c.names for c in m.claims()] == [["Metatext"]]
    assert m.made[0].is_running


@pytest.mark.parametrize("name", ["MyApp,4242", "/Applications", "a/b"])
async def test_a_client_cannot_smuggle_a_pid_or_a_path(tmp_path, as_client, name):
    leader = _leader()
    with pytest.raises(HTTPException) as e:
        await _post(leader, _manager(leader, tmp_path), processes=[name])
    assert e.value.status_code == 400


async def test_a_client_sees_only_its_own_names_on_a_shared_proxy(tmp_path):
    leader = _leader()
    m = _manager(leader, tmp_path)
    await m.claim(session="s1", principal="client:a", udid=ALPHA,
                  names=["Metatext", "Ivory"])
    await m.claim(session="s2", principal="client:b", udid=BRAVO, names=["Ivory"])
    assert [d["processes"] for d in m.describe("client:b")] == [["Ivory"]]
    assert m.describe("client:b")[0]["udids"] == [BRAVO]


async def test_a_dead_session_proxy_holds_nothing(tmp_path):
    leader = _leader()
    m = _manager(leader, tmp_path)
    await m.claim(session="s1", principal="client:a", udid=ALPHA, names=["Metatext"])
    follower = m.made[0]
    follower._handle_intercepted({"id": "f1", "request": {}})
    assert leader.held_count() == 1
    follower._running = False                         # its mitmdump exited
    assert leader.held_count() == 0 and not leader.holds("f1")
    await leader.set_mock("~d x.com", {"status_code": 200}, rule_id="after")
    assert "after" not in [c.get("rule_id") for c in follower.sent]


async def test_a_stopping_proxy_drops_only_its_own_pending(tmp_path):
    store = FlowStore()
    leader = _leader(store)
    m = _manager(leader, tmp_path)
    await m.claim(session="s1", principal="client:a", udid=ALPHA, names=["Metatext"])
    follower = m.made[0]
    started = {"request": {"method": "GET", "url": "http://x/", "host": "x", "path": "/"},
               "timestamp": 1}
    leader._handle_started({**started, "id": "lead"})
    follower._handle_started({**started, "id": "foll"})

    follower._drop_own_pending()

    assert [f.id for f in store.pending()] == ["lead"]


async def test_only_a_session_proxy_is_told_to_pass_the_mac_through(tmp_path, monkeypatch):
    """Spawn's environment: the session proxy gets the variable; the main
    proxy never inherits it."""
    monkeypatch.setenv(proxy_mod.SIMULATORS_ONLY_ENV, "1")
    seen: dict[str, dict] = {}

    async def fake_exec(*cmd, env, **kw):
        seen["env"] = env
        raise OSError("not spawning in a test")

    monkeypatch.setattr(proxy_mod.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(ProxyAdapter, "_find_mitmdump", lambda self: "/bin/false")
    monkeypatch.setattr(ProxyAdapter, "_kill_stale_mitmdump", staticmethod(lambda port: None))

    main = ProxyAdapter(flow_store=FlowStore(), local_capture_processes=["Safari"])
    assert await main._spawn() is False
    assert proxy_mod.SIMULATORS_ONLY_ENV not in seen["env"]

    follower = ProxyAdapter(flow_store=FlowStore(), listen_port=None,
                            local_capture_processes=["Metatext"])
    follower.leader = main
    assert await follower._spawn() is False
    assert seen["env"][proxy_mod.SIMULATORS_ONLY_ENV] == "1"


def test_the_two_spellings_of_the_variable_agree():
    from server.proxy import addon as addon_mod
    assert addon_mod.SIMULATORS_ONLY_ENV == proxy_mod.SIMULATORS_ONLY_ENV


class _NextLayer:
    def __init__(self, client_id: str, protocol: str = "tcp"):
        self.context = SimpleNamespace(client=SimpleNamespace(
            id=client_id, transport_protocol=protocol))
        self.layer = None


def _addon(monkeypatch, simulators_only: bool, ancestry: dict[int, int | None]):
    from server.proxy import addon as addon_mod
    if simulators_only:
        monkeypatch.setenv(addon_mod.SIMULATORS_ONLY_ENV, "1")
    else:
        monkeypatch.delenv(addon_mod.SIMULATORS_ONLY_ENV, raising=False)
    monkeypatch.setattr(addon_mod, "_simulator_instance_for_pid",
                        lambda pid: (ALPHA if ancestry[pid] else None, ancestry[pid]))
    monkeypatch.setattr(addon_mod, "_client_process_info", {
        "sim": {"pid": 10}, "mac": {"pid": 20}, "unknown": {"pid": 30}})
    # The real layers need a live connection's context; what matters here is
    # which one is chosen, and that it ignores the connection.
    from mitmproxy.proxy import layers
    monkeypatch.setattr(layers, "TCPLayer", lambda ctx, ignore=False: ("tcp", ignore))
    monkeypatch.setattr(layers, "UDPLayer", lambda ctx, ignore=False: ("udp", ignore))
    return addon_mod, addon_mod.IOSDebugAddon()


@pytest.mark.parametrize("client_id,passed_through", [
    ("sim", False), ("mac", True), ("unknown", True), ("no-pid", True)])
def test_a_session_proxy_passes_the_macs_own_traffic_through(monkeypatch, client_id,
                                                              passed_through):
    """Found by review: a client's name matches any executable path on the
    Mac, so ["a"] redirected the owner's own apps through a proxy that
    decrypts. A session proxy now keeps only connections from a simulator."""
    addon_mod, addon = _addon(monkeypatch, True, {10: 555, 20: None, 30: None})
    nl = _NextLayer(client_id)
    addon.next_layer(nl)
    if passed_through:
        assert nl.layer == ("tcp", True)
    else:
        assert nl.layer is None


def test_udp_from_the_mac_passes_through_as_udp(monkeypatch):
    _, addon = _addon(monkeypatch, True, {10: 555, 20: None, 30: None})
    nl = _NextLayer("mac", protocol="udp")
    addon.next_layer(nl)
    assert nl.layer == ("udp", True)


def test_the_main_proxy_decides_nothing_here(monkeypatch):
    _, addon = _addon(monkeypatch, False, {10: 555, 20: None, 30: None})
    nl = _NextLayer("mac")
    addon.next_layer(nl)
    assert nl.layer is None


def test_a_lookup_that_raises_passes_through(monkeypatch):
    """Could not tell is not "a simulator": capturing would mean capturing the
    Mac's own traffic."""
    addon_mod, addon = _addon(monkeypatch, True, {10: 555, 20: None, 30: None})

    def boom(pid):
        raise OSError("ps failed")

    monkeypatch.setattr(addon_mod, "_simulator_instance_for_pid", boom)
    nl = _NextLayer("sim")
    addon.next_layer(nl)
    assert nl.layer == ("tcp", True)


async def test_one_key_on_one_device_is_one_claim(tmp_path):
    leader = _leader()
    m = _manager(leader, tmp_path)
    await m.claim(session="client:a/s1", principal="client:a", udid=ALPHA,
                  names=["Metatext"])
    await m.claim(session="client:a/s2", principal="client:a", udid=ALPHA,
                  names=["Ivory"])
    assert [(c.udid, c.names) for c in m.claims()] == [(ALPHA, ["Metatext", "Ivory"])]


# ---------------------------------------------------------------------------
# CodeRabbit on #566
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", [" ", "  ", " Metatext", "Metatext "])
async def test_a_client_name_is_not_blank_or_padded(tmp_path, as_client, name):
    leader = _leader()
    with pytest.raises(HTTPException) as e:
        await _post(leader, _manager(leader, tmp_path), processes=[name])
    assert e.value.status_code == 400


async def test_releasing_a_simulator_ends_the_owners_session_capture_too(tmp_path):
    from server.api import sessions as sessions_api
    from server.session_registry import OWNER

    leader = _leader()
    m = _manager(leader, tmp_path)
    await m.claim(session="ci", principal=OWNER, udid=ALPHA, names=["Metatext"])
    app = SimpleNamespace(state=SimpleNamespace(
        session_proxies=m, proxy_adapter=leader, recordings=None,
        mock_owners={"owners": OWNER}))   # the owner's own, as set_mock records it
    await leader.set_mock("~d x.com", {"status_code": 200}, rule_id="owners",
                          simulator_udid=ALPHA)
    registry = SimpleNamespace(holder_of=lambda udid: None)
    session = SimpleNamespace(id="ci", principal=OWNER, reservations=[ALPHA])

    actions = await sessions_api._end_client_work(app, registry, session)

    assert m.claims() == [] and not m.made[0].is_running
    assert [a["action"] for a in actions] == ["session_capture_released"]
    # The owner's mocks are still the owner's to end.
    assert [r["rule_id"] for r in leader._mock_rules] == ["owners"]


async def test_a_start_after_its_flow_is_not_tracked_as_in_flight(tmp_path):
    leader = _leader()
    started = {"request": {"method": "GET", "url": "http://x/", "host": "x", "path": "/"},
               "timestamp": 1, "id": "late"}
    await leader._handle_flow({**started, "response": {"status_code": 200}})
    leader._handle_started(started)
    assert leader._pending_ids == set()


async def test_tracked_ids_are_trimmed_to_what_the_store_still_holds(monkeypatch):
    monkeypatch.setattr(proxy_mod, "PENDING_IDS_TRIM", 3)
    store = FlowStore()
    store._pending_max = 2               # evicts all but the newest two
    leader = _leader(store)
    for n in range(5):
        leader._handle_started({"request": {"method": "GET", "url": "http://x/",
                                            "host": "x", "path": "/"},
                                "timestamp": 1, "id": f"r{n}"})
    assert leader._pending_ids <= {"r2", "r3", "r4"}
    assert {"r3", "r4"} <= leader._pending_ids


# ---------------------------------------------------------------------------
# A client key's session proxies are capped (#566, CodeRabbit)
# ---------------------------------------------------------------------------


async def _fill(m, principal="client:a", n=4):
    for i in range(n):
        await m.claim(session="s1", principal=principal, udid=ALPHA,
                      names=[f"App{i}"], max_proxies=4)


async def test_a_key_at_its_cap_cannot_start_another(tmp_path):
    leader = _leader()
    m = _manager(leader, tmp_path)
    await _fill(m)
    with pytest.raises(sp_mod.SessionCaptureLimit) as e:
        await m.claim(session="s1", principal="client:a", udid=ALPHA,
                      names=["App4"], max_proxies=4)
    assert len(m.made) == 4 and len(e.value.using) == 4
    assert "App4" not in [n for c in m.claims() for n in c.names]


async def test_joining_a_shared_proxy_counts_toward_the_cap(tmp_path):
    leader = _leader()
    m = _manager(leader, tmp_path)
    await m.claim(session="s2", principal="client:b", udid=BRAVO, names=["Shared"])
    await _fill(m, n=3)
    with pytest.raises(sp_mod.SessionCaptureLimit):
        await m.claim(session="s1", principal="client:a", udid=ALPHA,
                      names=["Shared", "Brand"], max_proxies=4)


async def test_names_already_captured_need_no_room(tmp_path):
    leader = _leader()
    m = _manager(leader, tmp_path)
    await _fill(m)
    result = await m.claim(session="s1", principal="client:a", udid=ALPHA,
                           names=["App0", "MobileSafari"], max_proxies=4)
    assert result["started"] == []


async def test_handing_one_back_makes_room(tmp_path):
    leader = _leader()
    m = _manager(leader, tmp_path)
    await _fill(m)
    await m.release(principal="client:a", names=["App0"])
    result = await m.claim(session="s1", principal="client:a", udid=ALPHA,
                           names=["App4"], max_proxies=4)
    assert result["started"] == ["App4"]


async def test_the_cap_is_per_key(tmp_path):
    leader = _leader()
    m = _manager(leader, tmp_path)
    await _fill(m)
    result = await m.claim(session="s2", principal="client:b", udid=BRAVO,
                           names=["Other"], max_proxies=4)
    assert result["started"] == ["Other"]


async def test_the_endpoint_refuses_past_the_cap_with_429(tmp_path, as_client):
    leader = _leader()
    m = _manager(leader, tmp_path)
    for i in range(sp_mod.MAX_PROXIES_PER_CLIENT):
        await _post(leader, m, processes=[f"App{i}"], only=True)
    with pytest.raises(HTTPException) as e:
        await _post(leader, m, processes=["OneMore"], only=True)
    assert e.value.status_code == 429
    assert len(e.value.detail["using"]) == sp_mod.MAX_PROXIES_PER_CLIENT


async def test_the_owner_is_not_capped(tmp_path, as_client):
    as_client.is_owner = True
    leader = _leader()
    m = _manager(leader, tmp_path)
    for i in range(sp_mod.MAX_PROXIES_PER_CLIENT + 1):
        await _post(leader, m, processes=[f"App{i}"], only=True)
    assert len(m.made) == sp_mod.MAX_PROXIES_PER_CLIENT + 1


async def test_too_many_names_in_one_request_is_a_400(tmp_path, as_client):
    leader = _leader()
    names = [f"App{i}" for i in range(sp_mod.MAX_NAMES_PER_REQUEST + 1)]
    with pytest.raises(HTTPException) as e:
        await _post(leader, _manager(leader, tmp_path), processes=names, only=True)
    assert e.value.status_code == 400


# ---------------------------------------------------------------------------
# CodeRabbit on #566, second round
# ---------------------------------------------------------------------------


async def test_joining_only_shared_proxies_is_capped_too(tmp_path):
    leader = _leader()
    m = _manager(leader, tmp_path)
    await m.claim(session="s2", principal="client:b", udid=BRAVO, names=["Shared"])
    await _fill(m)
    with pytest.raises(sp_mod.SessionCaptureLimit):
        await m.claim(session="s1", principal="client:a", udid=ALPHA,
                      names=["Shared"], max_proxies=4)
    assert "Shared" not in [n for c in m.claims("client:a") for n in c.names]


async def test_the_limit_names_only_the_callers_own(tmp_path):
    leader = _leader()
    m = _manager(leader, tmp_path)
    await m.claim(session="s2", principal="client:b", udid=BRAVO,
                  names=["Secret", "Shared"])
    await m.claim(session="s1", principal="client:a", udid=ALPHA, names=["Shared"])
    await _fill(m, n=3)
    with pytest.raises(sp_mod.SessionCaptureLimit) as e:
        await m.claim(session="s1", principal="client:a", udid=ALPHA,
                      names=["Brand"], max_proxies=4)
    assert "Secret" not in [n for names in e.value.using for n in names]
    assert ["Shared"] in e.value.using


async def test_a_hold_that_ended_during_the_start_commits_nothing(tmp_path):
    """The release ran while the proxy started and found no claim to end;
    the claim must not land after it."""
    leader = _leader()
    m = _manager(leader, tmp_path)
    with pytest.raises(sp_mod.HoldEnded):
        await m.claim(session="s1", principal="client:a", udid=ALPHA,
                      names=["Metatext"], still_held=lambda: False)
    assert m.claims() == [] and leader.followers == []
    assert not m.made[0].is_running


async def test_a_hold_still_held_commits(tmp_path):
    leader = _leader()
    m = _manager(leader, tmp_path)
    result = await m.claim(session="s1", principal="client:a", udid=ALPHA,
                           names=["Metatext"], still_held=lambda: True)
    assert result["started"] == ["Metatext"] and len(m.claims()) == 1


async def test_the_endpoint_answers_409_for_a_hold_ended_mid_start(tmp_path, as_client,
                                                                   monkeypatch):
    held = iter([None, "not reserved"])     # held at the check, gone at commit
    monkeypatch.setattr(api.device_access, "why_not", lambda udid: next(held))
    leader = _leader()
    m = _manager(leader, tmp_path)
    with pytest.raises(HTTPException) as e:
        await _post(leader, m, processes=["Metatext"], only=True)
    assert e.value.status_code == 409 and m.claims() == []


# ---------------------------------------------------------------------------
# Capture on a device the key does not hold ends with the session that asked
# ---------------------------------------------------------------------------


def _ending(m, *, session_id="ci", reservations=(), holder=None, live=None):
    from server.session_registry import OWNER

    app = SimpleNamespace(state=SimpleNamespace(
        session_proxies=m, proxy_adapter=None, recordings=None, mock_owners={}))
    registry = SimpleNamespace(holder_of=lambda udid: holder, get=lambda sid: live)
    session = SimpleNamespace(id=session_id, principal=OWNER, reservations=list(reservations),
                              opened_at=1.0)
    return app, registry, session


async def test_owner_capture_on_an_unheld_simulator_ends_with_its_session(tmp_path):
    # The owner may add capture without reserving; with no hold to last for,
    # it outlived the session (and a use_remote switch away) until someone
    # held and released the device.
    from server.api import sessions as sessions_api
    from server.session_registry import OWNER

    leader = _leader()
    m = _manager(leader, tmp_path)
    await m.claim(session="ci", principal=OWNER, udid=ALPHA, names=["Metatext"], opened=1.0)
    app, registry, session = _ending(m)

    actions = await sessions_api._end_client_work(app, registry, session)

    assert {"udid": ALPHA, "action": "session_capture_released", "ok": True,
            "stopped": ["Metatext"]} in actions
    assert m.claims() == [] and not m.made[0].is_running


async def test_capture_another_session_last_asked_for_is_left(tmp_path):
    from server.api import sessions as sessions_api
    from server.session_registry import OWNER

    m = _manager(_leader(), tmp_path)
    await m.claim(session="other", principal=OWNER, udid=ALPHA, names=["Metatext"], opened=1.0)
    app, registry, session = _ending(m)
    assert await sessions_api._end_client_work(app, registry, session) == []
    assert [c.names for c in m.claims()] == [["Metatext"]]


async def test_capture_on_a_device_the_key_holds_lasts_for_that_hold(tmp_path):
    from server.api import sessions as sessions_api
    from server.session_registry import OWNER

    m = _manager(_leader(), tmp_path)
    await m.claim(session="ci", principal=OWNER, udid=ALPHA, names=["Metatext"], opened=1.0)
    still = SimpleNamespace(session=SimpleNamespace(principal=OWNER))
    app, registry, session = _ending(m, holder=still)
    await sessions_api._end_client_work(app, registry, session)
    assert [c.names for c in m.claims()] == [["Metatext"]]


async def test_a_partial_release_does_not_end_the_sessions_other_capture(tmp_path):
    # release_devices for one device runs this for a session that goes on.
    from server.api import sessions as sessions_api
    from server.session_registry import OWNER

    m = _manager(_leader(), tmp_path)
    await m.claim(session="ci", principal=OWNER, udid=ALPHA, names=["Metatext"], opened=1.0)
    live = SimpleNamespace(opened_at=1.0)
    app, registry, session = _ending(m, live=live)
    await sessions_api._end_client_work(app, registry, session)
    assert [c.names for c in m.claims()] == [["Metatext"]]


async def test_a_later_session_under_the_same_id_keeps_its_capture(tmp_path):
    # The old opening's clean-up, drained after the id was opened again, must
    # not take the new opening's capture (CodeRabbit).
    from server.api import sessions as sessions_api
    from server.session_registry import OWNER

    m = _manager(_leader(), tmp_path)
    await m.claim(session="ci", principal=OWNER, udid=ALPHA, names=["Metatext"], opened=2.0)
    app, registry, session = _ending(m)          # the opening at 1.0 ending
    assert await sessions_api._end_client_work(app, registry, session) == []
    assert [c.names for c in m.claims()] == [["Metatext"]]


async def test_a_claim_and_a_hold_spelled_differently_are_one_device(tmp_path):
    # Reservations hold canonical (upper-case) ids; a claim made with a lower-
    # case one must still read as held, and is stored canonical (CodeRabbit).
    from server.api import sessions as sessions_api
    from server.session_registry import OWNER

    m = _manager(_leader(), tmp_path)
    await m.claim(session="ci", principal=OWNER, udid=ALPHA.lower(), names=["Metatext"], opened=1.0)
    seen = []
    still = SimpleNamespace(session=SimpleNamespace(principal=OWNER))
    app, registry, session = _ending(m)
    registry.holder_of = lambda udid: (seen.append(udid), still if udid == ALPHA else None)[1]
    await sessions_api._end_client_work(app, registry, session)
    assert [c.names for c in m.claims()] == [["Metatext"]], "held under its canonical id"
    assert seen == [ALPHA]


async def test_a_claim_is_not_committed_after_its_session_ended(tmp_path, as_client):
    # The owner may claim a device it does not hold, so the hold check passes
    # at commit; the session ending while the proxy started must still stop it
    # (CodeRabbit) -- else its clean-up has already run and the claim stays.
    as_client.is_owner = True
    live = SimpleNamespace(opened_at=1.0)
    answers = iter([live, None])               # open when asked, ended at commit
    leader = _leader()
    m = _manager(leader, tmp_path)
    req = _request(leader, m)
    req.app.state.sessions = SimpleNamespace(get=lambda sid: next(answers))
    with pytest.raises(HTTPException) as e:
        await api.add_session_capture.__wrapped__(
            req, SessionCaptureRequest(simulator_udid=ALPHA, processes=["Metatext"], only=True))
    assert e.value.status_code == 409 and m.claims() == []
    assert not m.made[0].is_running, "the proxy it started is stopped again"


async def test_a_claim_is_stored_under_the_canonical_id(tmp_path, as_client):
    as_client.is_owner = True
    live = SimpleNamespace(opened_at=1.0)
    leader = _leader()
    m = _manager(leader, tmp_path)
    req = _request(leader, m)
    req.app.state.sessions = SimpleNamespace(get=lambda sid: live)
    await api.add_session_capture.__wrapped__(
        req, SessionCaptureRequest(simulator_udid=ALPHA.lower(), processes=["Metatext"], only=True))
    assert [(c.udid, c.opened) for c in m.claims()] == [(ALPHA, 1.0)]
