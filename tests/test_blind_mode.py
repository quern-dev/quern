"""Blind sharing (#254, phase 2b): a client key sees only its own devices' data.

Through `create_app`'s real routes, with the stores seeded directly: alice
holds SIM-A, bob holds SIM-B, and the buffers hold entries for both devices,
for SIM-A from before alice reserved it, and for no device at all. In blind
mode alice must see exactly her own device's entries from her reservation on;
the owner, and alice on a shared server, see everything.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from server import device_access, scopes, visibility
from server.config import ServerConfig
from server.main import create_app
from server.models import LogEntry, LogLevel, LogSource
from server.proxy.flow_store import FlowStore
from server.session_registry import SessionRegistry
from tests.test_flow_store import _make_flow

KEY = "k" * 32
SIM_A = "AAAAAAAA-0000-0000-0000-000000000001"
SIM_B = "BBBBBBBB-0000-0000-0000-000000000002"
FREE = "CCCCCCCC-0000-0000-0000-000000000003"


def _entry(device_id, *, ago=0.0, level=LogLevel.INFO, message="m", **kw):
    return LogEntry(id=uuid.uuid4().hex, timestamp=datetime.now(UTC) - timedelta(seconds=ago),
                    device_id=device_id, process="App", level=level, message=message,
                    source=kw.pop("source", LogSource.SIMULATOR), **kw)


def _flow(fid, udid, *, ago=0.0):
    flow = _make_flow(flow_id=fid, timestamp=datetime.now(UTC) - timedelta(seconds=ago))
    flow.simulator_udid = udid
    return flow


def _hold(registry, session_id, principal, udid, *, since=None):
    registry.open(session_id, principal=principal)
    registry.get(session_id).state = "live"
    registry.reserve(session_id, [udid], on_release="none", booted=[])
    if since is not None:
        registry.get(session_id).reservations[udid].since = since


@pytest.fixture
def world(tmp_path):
    app = create_app(config=ServerConfig(api_key=KEY, ring_buffer_size=100),
                     enable_oslog=False, enable_crash=False, enable_proxy=False)
    app.state.flow_store = FlowStore(max_size=100)
    app.state.clients.path = tmp_path / "clients.json"
    app.state.clients.seen_path = tmp_path / "clients-seen.json"
    alice, bob = app.state.clients.add("alice"), app.state.clients.add("bob")
    registry = SessionRegistry(grace_seconds=180)
    app.state.sessions = registry
    device_access.install(registry)
    reserved_at = datetime.now(UTC) - timedelta(seconds=30)
    _hold(registry, "alice:s1", "client:alice", SIM_A, since=reserved_at)
    _hold(registry, "bob:s1", "client:bob", SIM_B, since=reserved_at)

    async def seed():
        ring = app.state.ring_buffer
        for e in [_entry(SIM_A, message="alice-now"),
                  _entry(SIM_A, ago=120, message="alice-before-reserving"),
                  _entry(SIM_B, message="bob-now"),
                  _entry("", message="host-line"),
                  _entry(SIM_A, level=LogLevel.ERROR, message="alice-error"),
                  _entry(SIM_B, level=LogLevel.ERROR, message="bob-error")]:
            await ring.append(e)
        for f in [_flow("f-alice", SIM_A), _flow("f-alice-old", SIM_A, ago=120),
                  _flow("f-bob", SIM_B), _flow("f-host", None)]:
            await app.state.flow_store.add(f)

    asyncio.run(seed())
    before = visibility.MODE
    visibility.MODE = "blind"
    try:
        yield SimpleNamespace(app=app, client=TestClient(app), alice=alice, bob=bob,
                              registry=registry)
    finally:
        visibility.MODE = before
        device_access.install(None)


def _h(key, session="s1"):
    return {"Authorization": f"Bearer {key}", "X-Quern-Session": session}


def _messages(resp):
    return {e["message"] for e in resp.json()["entries"]}


# ---------------------------------------------------------------------------
# Every route a client can read has a blind-mode decision
# ---------------------------------------------------------------------------


def test_every_view_route_has_a_blind_decision():
    view = {k for k, r in scopes.ROUTES.items() if r.access is scopes.Access.VIEW}
    assert view - set(scopes.BLIND) == set(), "VIEW routes with no blind-mode decision"
    assert set(scopes.BLIND) - view == set(), "BLIND names routes that are not VIEW"
    assert set(scopes.BLIND.values()) <= {"device", "filtered", "hidden", "shared"}


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------


class TestLogs:
    def test_a_blind_client_sees_its_own_device_since_it_reserved_it(self, world):
        r = world.client.get("/api/v1/logs/query", params={"limit": 100}, headers=_h(world.alice))
        assert _messages(r) == {"alice-now", "alice-error"}
        assert r.json()["total"] == 2, "total counts what the caller may see"

    def test_the_owner_sees_everything(self, world):
        r = world.client.get("/api/v1/logs/query", params={"limit": 100}, headers=_h(KEY))
        assert {"bob-now", "host-line", "alice-before-reserving"} <= _messages(r)

    def test_a_shared_server_shows_a_client_everything(self, world):
        visibility.MODE = "shared"
        r = world.client.get("/api/v1/logs/query", params={"limit": 100}, headers=_h(world.alice))
        assert {"bob-now", "host-line"} <= _messages(r)

    def test_errors(self, world):
        r = world.client.get("/api/v1/logs/errors", headers=_h(world.alice))
        assert _messages(r) == {"alice-error"}

    def test_summary_counts_only_the_callers_entries(self, world):
        r = world.client.get("/api/v1/logs/summary", params={"window": "5m"},
                             headers=_h(world.alice))
        body = r.json()
        assert body["total_count"] == 2, body.get("summary")

    def test_sources_hide_everyones_buffer_totals(self, world):
        r = world.client.get("/api/v1/logs/sources", headers=_h(world.alice))
        assert r.json()["buffers"] == {}


# ---------------------------------------------------------------------------
# Flows
# ---------------------------------------------------------------------------


class TestFlows:
    def _ids(self, resp):
        return {f["id"] for f in resp.json()["flows"]}

    def test_query(self, world):
        r = world.client.get("/api/v1/proxy/flows", headers=_h(world.alice))
        assert self._ids(r) == {"f-alice"}
        assert r.json()["total"] == 1
        assert {"f-bob", "f-host"} <= self._ids(
            world.client.get("/api/v1/proxy/flows", headers=_h(KEY)))

    def test_another_clients_flow_reads_as_not_found(self, world):
        assert world.client.get("/api/v1/proxy/flows/f-bob",
                                headers=_h(world.alice)).status_code == 404
        assert world.client.get("/api/v1/proxy/flows/f-alice",
                                headers=_h(world.alice)).status_code == 200

    def test_summary(self, world):
        r = world.client.get("/api/v1/proxy/flows/summary", params={"window": "5m"},
                             headers=_h(world.alice))
        assert r.json()["total_flows"] == 1
        owner = world.client.get("/api/v1/proxy/flows/summary", params={"window": "5m"},
                                 headers=_h(KEY))
        assert owner.json()["total_flows"] == 4

    def test_another_clients_flow_does_not_end_a_wait(self, world):
        r = world.client.post("/api/v1/proxy/flows/wait", headers=_h(world.alice), json={
            "host": "api.example.com", "timeout": 0.3,
            "since": (datetime.now(UTC) - timedelta(seconds=5)).isoformat()})
        body = r.json()
        assert body["matched"] is True and body["flow"]["id"] == "f-alice"

    def test_proxy_status_shows_only_the_callers_devices(self, world):
        from server.api.proxy import _blind_status
        from server.models import ProxyStatusResponse, SimulatorTls

        status = ProxyStatusResponse(
            status="running", bypass_patterns=["corp.example"], local_capture=["Safari"],
            simulator_tls=[SimulatorTls(udid=SIM_A, tls="decrypted"),
                           SimulatorTls(udid=SIM_B, tls="decrypted")],
            flows_captured=4)
        view = visibility.View(since={SIM_A: datetime.now(UTC) - timedelta(seconds=30)})
        request = SimpleNamespace(app=world.app)
        out = asyncio.run(_blind_status(request, status, view))
        assert [t.udid for t in out.simulator_tls] == [SIM_A]
        assert out.bypass_patterns == [] and out.local_capture == []
        assert out.flows_captured == 1


# ---------------------------------------------------------------------------
# Devices, sessions, and the owner's set-up
# ---------------------------------------------------------------------------


class TestDevicesAndSetup:
    def test_reading_another_principals_device_is_not_found(self, world):
        r = world.client.get("/api/v1/device/screenshot", params={"udid": SIM_B},
                             headers=_h(world.alice))
        assert r.status_code == 404

    def test_a_shared_server_says_who_holds_it_instead(self, world):
        """Shared, reads stay open: the read is not refused at all."""
        visibility.MODE = "shared"
        device_access._WRITE.set(False)
        from server.principal import reset_principal, set_principal
        from server.session import _SESSION

        p, s = set_principal("client:alice"), _SESSION.set("alice:s1")
        try:
            device_access.check(SIM_B)          # no exception
        finally:
            reset_principal(p)
            _SESSION.reset(s)

    def test_sessions_list_only_the_callers(self, world):
        r = world.client.get("/api/v1/sessions", headers=_h(world.alice))
        assert [s["id"] for s in r.json()["sessions"]] == ["alice:s1"]

    @pytest.mark.parametrize("path", ["/api/v1/proxy/bypass", "/api/v1/logs/filter",
                                      "/api/v1/device/tools/sites", "/api/v1/landmarks/"])
    def test_the_owners_set_up_is_hidden(self, world, path):
        r = world.client.get(path, headers=_h(world.alice))
        assert r.status_code == 403 and "blind" in r.json()["detail"]
        visibility.MODE = "shared"
        assert world.client.get(path, headers=_h(world.alice)).status_code != 403

    def test_mocks_list_only_the_callers_scoped_rules(self, world):
        world.app.state.proxy_adapter = SimpleNamespace(_mock_rules=[
            {"rule_id": "r1", "pattern": "~d a", "response": {}, "simulator_udid": SIM_A},
            {"rule_id": "r2", "pattern": "~d b", "response": {}, "simulator_udid": SIM_B},
            {"rule_id": "r3", "pattern": "~d c", "response": {}, "simulator_udid": None}])
        r = world.client.get("/api/v1/proxy/mocks", headers=_h(world.alice))
        assert [m["rule_id"] for m in r.json()["rules"]] == ["r1"]

    def test_recordings_list_only_the_callers(self, world):
        def rec(rid, principal):
            return SimpleNamespace(id=rid, principal=principal, requested_by=None,
                                   summary=lambda: {"id": rid, "principal": principal})

        world.app.state.recordings = SimpleNamespace(
            list=lambda: [rec("ra", "client:alice"), rec("rb", "client:bob"),
                          rec("ro", "owner")])
        import server.api.recordings as recordings_api

        original = recordings_api._with_rejections
        recordings_api._with_rejections = lambda request, summary: summary
        try:
            r = world.client.get("/api/v1/recordings", headers=_h(world.alice))
            assert [x["id"] for x in r.json()["recordings"]] == ["ra"]
            r = world.client.get("/api/v1/recordings", headers=_h(KEY))
            assert len(r.json()["recordings"]) == 3
        finally:
            recordings_api._with_rejections = original


# ---------------------------------------------------------------------------
# The next holder of a device
# ---------------------------------------------------------------------------


def test_the_next_holder_does_not_read_the_previous_holders_entries(world):
    """bob releases SIM-B and alice reserves it: bob's lines from before her
    reservation stay his."""
    world.registry.release("bob:s1")
    world.registry.reserve("alice:s1", [SIM_B], on_release="none", booted=[])
    r = world.client.get("/api/v1/logs/query", params={"limit": 100}, headers=_h(world.alice))
    assert "bob-now" not in _messages(r)
    flows = world.client.get("/api/v1/proxy/flows", headers=_h(world.alice)).json()["flows"]
    assert "f-bob" not in {f["id"] for f in flows}


class TestView:
    def test_everything(self):
        assert visibility.View().allows(None) and visibility.View().allows("X")

    def test_unattributed_is_hidden(self):
        view = visibility.View(since={SIM_A: datetime.now(UTC)})
        assert not view.allows(None) and not view.allows("")

    def test_by_time(self):
        start = datetime.now(UTC)
        view = visibility.View(since={SIM_A: start})
        assert view.allows(SIM_A, start + timedelta(seconds=1))
        assert not view.allows(SIM_A, start - timedelta(seconds=1))
        assert view.allows(SIM_A.lower(), start), "matched as the access check matches"

    def test_a_naive_timestamp_is_read_as_utc(self):
        start = datetime.now(UTC)
        view = visibility.View(since={SIM_A: start})
        assert view.allows(SIM_A, (start + timedelta(seconds=1)).replace(tzinfo=None))


class TestSetting:
    def test_blind_and_shared_are_read(self, monkeypatch):
        from server import config

        for value in ("blind", "shared"):
            monkeypatch.setattr(config, "read_user_config",
                                lambda v=value: {"sessions": {"visibility": v}})
            assert config.sessions_settings()["visibility"] == value
            assert config.sessions_settings()["visibility_note"] is None

    def test_anything_else_is_shared_and_says_so(self, monkeypatch):
        from server import config

        monkeypatch.setattr(config, "read_user_config",
                            lambda: {"sessions": {"visibility": "blnd"}})
        settings = config.sessions_settings()
        assert settings["visibility"] == "shared" and "blnd" in settings["visibility_note"]


def test_crashes_show_only_the_callers_device_since_it_reserved_it(world):
    from server.models import CrashReport

    now = datetime.now(UTC)
    world.app.state.crash_adapter = SimpleNamespace(crash_reports=[
        CrashReport(crash_id="c-alice", timestamp=now, device_id=SIM_A, process="App"),
        CrashReport(crash_id="c-alice-old", timestamp=now - timedelta(seconds=120),
                    device_id=SIM_A, process="App"),
        CrashReport(crash_id="c-bob", timestamp=now, device_id=SIM_B, process="App"),
        CrashReport(crash_id="c-nowhere", timestamp=now, device_id="", process="App"),
    ])
    r = world.client.get("/api/v1/crashes/latest", params={"symbolicate": "false"},
                         headers=_h(world.alice))
    assert [c["crash_id"] for c in r.json()["crashes"]] == ["c-alice"]
    r = world.client.get("/api/v1/crashes/latest", params={"symbolicate": "false"},
                         headers=_h(KEY))
    assert len(r.json()["crashes"]) == 4


def test_the_trace_drops_what_is_not_the_callers(world):
    async def actions():
        for udid, name in ((SIM_A, "alice-tap"), (SIM_B, "bob-tap")):
            await world.app.state.server_buffer.append(_entry(
                "server", ago=1, source=LogSource.SERVER, message=name, action="tap",
                udid=udid, outcome="ok"))

    asyncio.run(actions())
    r = world.client.get("/api/v1/trace", headers=_h(world.alice))
    assert "alice-tap" in r.text and "f-alice" in r.text, "the caller's own action and flow"
    assert "bob-tap" not in r.text
    assert r.status_code == 200, r.text
    text = r.text
    assert "f-bob" not in text and "f-host" not in text
    assert "bob-now" not in text and "host-line" not in text
    owner = world.client.get("/api/v1/trace", headers=_h(KEY)).text
    assert "f-bob" in owner


def test_previews_list_free_and_own_devices_only(world):
    from server.principal import reset_principal, set_principal
    from server.session import _SESSION

    p, s = set_principal("client:alice"), _SESSION.set("alice:s1")
    try:
        assert visibility.listed(SIM_A) and visibility.listed(FREE)
        assert not visibility.listed(SIM_B)
    finally:
        reset_principal(p)
        _SESSION.reset(s)
    assert visibility.listed(SIM_B), "the owner sees every device"
