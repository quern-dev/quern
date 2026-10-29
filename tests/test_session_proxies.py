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
                      "confirmed": None}
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
    await m.release(session="s1", principal="client:a")
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

    assert await m.release(session="s1", principal="client:a") == []
    assert follower.is_running and leader.followers == [follower]

    assert await m.release(session="s2", principal="client:b") == ["Metatext"]
    assert not follower.is_running and leader.followers == []
    assert m.claims() == []


async def test_a_release_names_its_own_session_only(tmp_path):
    """Same session id under another principal is someone else's claim."""
    leader = _leader()
    m = _manager(leader, tmp_path)
    await m.claim(session="s1", principal="client:a", udid=ALPHA, names=["Metatext"])
    assert await m.release(session="s1", principal="client:b") == []
    assert m.made[0].is_running


async def test_releasing_one_name_keeps_the_others(tmp_path):
    leader = _leader()
    m = _manager(leader, tmp_path)
    await m.claim(session="s1", principal="client:a", udid=ALPHA, names=["Metatext"])
    await m.claim(session="s1", principal="client:a", udid=ALPHA, names=["Ivory"])
    assert await m.release(session="s1", principal="client:a",
                           names=["Ivory"]) == ["Ivory"]
    assert [c.names for c in m.claims()] == [["Metatext"]]


async def test_a_proxy_that_did_not_start_is_not_listed_or_claimed(tmp_path):
    leader = _leader()
    m = _manager(leader, tmp_path, starts=False)
    with pytest.raises(sp_mod.SessionCaptureError, match="mitmdump not found"):
        await m.claim(session="s1", principal="client:a", udid=ALPHA, names=["Metatext"])
    assert leader.followers == [] and m.claims() == [] and m.describe() == []


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
    await m.release(session="s1", principal="client:a")
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
    await m.release(session="s1", principal="client:a")
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
