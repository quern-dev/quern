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
from server.models import (
    CrashReport,
    DeviceInfo,
    DeviceState,
    DeviceType,
    LogEntry,
    LogLevel,
    LogSource,
    ProxyStatusResponse,
    SimulatorTls,
    TlsRejection,
)
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
        world.app.state.mock_owners = {"r1": "client:alice", "r2": "client:bob", "r3": "owner"}
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
    from server.api.sessions import run_release_policies

    world.registry.release("bob:s1")
    asyncio.run(run_release_policies(world.app))    # bob's clean-up, which frees it
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

    def test_case_and_spaces_do_not_fall_back_to_shared(self, monkeypatch):
        """Falling back to shared over a capital letter would show every
        client everything."""
        from server import config

        monkeypatch.setattr(config, "read_user_config",
                            lambda: {"sessions": {"visibility": " Blind "}})
        assert config.sessions_settings()["visibility"] == "blind"

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


# ---------------------------------------------------------------------------
# Leaks review found in the first version; each test failed against it
# ---------------------------------------------------------------------------

NOPE = "DDDDDDDD-0000-0000-0000-00000000DEAD"


def test_reserving_anothers_device_does_not_name_its_holder(world):
    r = world.client.post("/api/v1/sessions/s1/reservations", headers=_h(world.alice),
                          json={"udids": [SIM_B]})
    assert "bob" not in r.text, (r.status_code, r.text)


def test_a_write_cannot_tell_anothers_device_from_a_nonexistent_one(world):
    held = world.client.post("/api/v1/device/ui/tap", headers=_h(world.alice),
                             json={"udid": SIM_B, "x": 1, "y": 1})
    missing = world.client.post("/api/v1/device/ui/tap", headers=_h(world.alice),
                                json={"udid": NOPE, "x": 1, "y": 1})
    assert held.status_code == missing.status_code, (held.text, missing.text)


def test_an_unqualified_read_does_not_name_anothers_only_booted_device(world, monkeypatch):
    from server.device.controller import DeviceController
    from server.models import DeviceInfo, DeviceState, DeviceType
    controller = DeviceController()
    world.app.state.device_controller = controller
    bob_dev = DeviceInfo(udid=SIM_B, name="Bob's secret iPhone", state=DeviceState.BOOTED,
                         device_type=DeviceType.SIMULATOR, os_version="iOS 18.0",
                         runtime="iOS-18-0", is_available=True)

    async def list_devices(*a, **k):
        return [bob_dev]

    async def ensure(*a, **k):
        return None

    monkeypatch.setattr(controller, "list_devices", list_devices)
    monkeypatch.setattr(controller, "_ensure_device_type_cached", ensure)
    # alice:s2 has no device slot of its own yet.
    world.registry.open("alice:s2", principal="client:alice")
    r = world.client.get("/api/v1/device/settings", headers=_h(world.alice, "s2"))
    assert SIM_B not in r.text and "Bob" not in r.text, (r.status_code, r.text)
    listed = world.client.get("/api/v1/device/list", headers=_h(world.alice, "s2"))
    assert SIM_B not in listed.text, listed.text


def test_an_unqualified_read_does_not_list_every_booted_device(world, monkeypatch):
    from server.device.controller import DeviceController
    from server.models import DeviceInfo, DeviceState, DeviceType
    controller = DeviceController()
    world.app.state.device_controller = controller

    def dev(u, n):
        return DeviceInfo(udid=u, name=n, state=DeviceState.BOOTED,
                          device_type=DeviceType.SIMULATOR, os_version="iOS 18.0",
                          runtime="iOS-18-0", is_available=True)

    async def list_devices(*a, **k):
        return [dev(SIM_B, "Bob's secret iPhone"), dev(FREE, "Free sim")]

    async def ensure(*a, **k):
        return None

    monkeypatch.setattr(controller, "list_devices", list_devices)
    monkeypatch.setattr(controller, "_ensure_device_type_cached", ensure)
    world.registry.open("alice:s2", principal="client:alice")
    r = world.client.get("/api/v1/device/settings", headers=_h(world.alice, "s2"))
    assert "Bob" not in r.text and SIM_B[:8] not in r.text, (r.status_code, r.text)


def test_preview_status_does_not_list_anothers_usb_phone(world):
    pm = SimpleNamespace(status=lambda: {
        "status": "stopped", "active": {}, "active_count": 0,
        "available": [{"name": "Bob's iPhone", "id": "cmio-1234", "kind": "device"}],
        "available_count": 1})
    world.app.state.preview_manager = pm
    world.app.state.scrcpy_preview = SimpleNamespace(
        status=lambda: {"available": False, "active": {}, "active_count": 0})
    # Bob holds that phone: its udid is reserved, but the preview list keys it by cmio id.
    world.registry.reserve("bob:s1", ["00008110-000A1B2C3D4E5F60"], on_release="none")
    import server.api.device as device_api

    async def no_sims():
        return [], None

    orig = device_api.simulator_entries
    device_api.simulator_entries = no_sims
    try:
        r = world.client.get("/api/v1/device/preview/status", headers=_h(world.alice))
    finally:
        device_api.simulator_entries = orig
    assert "Bob" not in r.text, r.text


def test_previous_holders_mock_rule_is_not_shown_to_next_holder(world):
    world.app.state.proxy_adapter = SimpleNamespace(_mock_rules=[
        {"rule_id": "bob-rule", "pattern": "~d secret.bob", "response": {"body": "bob-token"},
         "simulator_udid": SIM_B}])
    from server.api.sessions import run_release_policies

    world.registry.release("bob:s1")
    asyncio.run(run_release_policies(world.app))
    world.registry.reserve("alice:s1", [SIM_B], on_release="none")
    r = world.client.get("/api/v1/proxy/mocks", headers=_h(world.alice))
    assert "bob-rule" not in r.text, r.text


def test_stream_heartbeats_do_not_count_everyones_data(world):
    """The heartbeat carried the shared buffer's and store's sizes."""
    import inspect

    import server.api.logs as logs_api
    import server.api.proxy as proxy_api

    for module, key in ((logs_api, "buffer_size"), (proxy_api, "store_size")):
        source = inspect.getsource(module)
        line = next(x for x in source.splitlines() if f'"{key}"' in x)
        assert "if seen is not None" in line or "if blind" in line, (module.__name__, line)


def test_flow_detail_404_is_identical_for_anothers_and_a_missing_flow(world):
    world.app.state.flow_store._evicted = 7  # as if the store had shed flows
    other = world.client.get("/api/v1/proxy/flows/f-bob", headers=_h(world.alice))
    missing = world.client.get("/api/v1/proxy/flows/nope", headers=_h(world.alice))
    assert other.json()["detail"].replace("f-bob", "X") == \
        missing.json()["detail"].replace("nope", "X"), (other.text, missing.text)


def test_recording_principal_and_blind_survive_a_restart(tmp_path, monkeypatch):
    from server.recording import recorder

    rec = recorder.Recording(id="rec_x", udid=SIM_A, dir=tmp_path, filters=recorder.Filters(),
                             started_at=datetime.now(UTC), principal="client:alice", blind=True)
    rec.state = "recording"
    mgr = recorder.RecordingManager.__new__(recorder.RecordingManager)
    mgr._recordings = {"rec_x": rec}
    snap = mgr._snapshot()["recordings"][0]
    assert snap.get("principal") == "client:alice" and snap.get("blind") is True, snap


def test_a_clients_release_does_not_report_anothers_release_policy(world):
    shut = []

    async def shutdown(udid):
        shut.append(udid)

    world.app.state.device_controller = SimpleNamespace(shutdown=shutdown)
    bob = world.registry.get("bob:s1")
    bob.reservations[SIM_B].on_release = "shutdown_booted"
    bob.reservations[SIM_B].booted_here = True
    bob.state, bob.grace_until = "grace", 0.0      # bob's grace has run out
    r = world.client.delete("/api/v1/sessions/s1/reservations", headers=_h(world.alice))
    assert SIM_B not in r.text, r.text


def test_since_is_inclusive(world):
    start = datetime.now(UTC)
    assert visibility.View(since={SIM_A: start}).allows(SIM_A, start)
    _ = asyncio, timedelta


def test_a_refused_read_does_not_adopt_anothers_device_as_active(world, monkeypatch):
    from server.device.controller import DeviceController
    from server.models import DeviceInfo, DeviceState, DeviceType

    controller = DeviceController()
    world.app.state.device_controller = controller
    bob_dev = DeviceInfo(udid=SIM_B, name="Bob", state=DeviceState.BOOTED,
                         device_type=DeviceType.SIMULATOR)

    async def list_devices(*a, **k):
        return [bob_dev]

    async def ensure(*a, **k):
        return None

    monkeypatch.setattr(controller, "list_devices", list_devices)
    monkeypatch.setattr(controller, "_ensure_device_type_cached", ensure)
    world.registry.open("alice:s2", principal="client:alice")
    world.client.get("/api/v1/device/settings", headers=_h(world.alice, "s2"))
    from server.session import _SESSION
    tok = _SESSION.set("alice:s2")
    try:
        assert controller._active_udid != SIM_B, "slot now points at bob's device"
    finally:
        _SESSION.reset(tok)


def test_alices_refused_probe_of_bobs_device_is_not_in_bobs_logs(world):
    """A refusal logged against bob's device carries alice's session name: bob
    must not read it, though it names his device."""
    from server.models import LogSource
    from tests.test_blind_mode import _entry

    asyncio.run(world.app.state.server_buffer.append(_entry(
        "server", source=LogSource.SERVER, message="alice:secretproject probe", action="screenshot",
        udid=SIM_B, outcome="failed", session="alice:secretproject")))
    asyncio.run(world.app.state.server_buffer.append(_entry(
        "server", source=LogSource.SERVER, message="bob's own tap", action="tap",
        udid=SIM_B, outcome="ok", session="bob:s1")))
    r = world.client.get("/api/v1/logs/query", params={"limit": 100}, headers=_h(world.bob))
    assert "secretproject" not in r.text, r.text[:400]
    assert "bob's own tap" in r.text, "the holder's own actions stay visible"
    owner = world.client.get("/api/v1/logs/query", params={"limit": 100}, headers=_h(KEY))
    assert "secretproject" in owner.text


def test_an_unnamed_read_with_no_device_of_its_own_is_refused_before_resolving(world):
    """Not resolved to "the only booted device", which could be another key's."""
    world.registry.open("alice:s2", principal="client:alice")
    r = world.client.get("/api/v1/device/settings", headers=_h(world.alice, "s2"))
    assert r.status_code == 403 and "reserve" in r.json()["detail"]
    assert SIM_B not in r.text


# ---------------------------------------------------------------------------
# Guard by guard: each test fails if its filter is removed (review found 25
# guards whose removal no test noticed)
# ---------------------------------------------------------------------------

def _devices():
    def dev(udid, name):
        return DeviceInfo(udid=udid, name=name, state=DeviceState.BOOTED,
                          device_type=DeviceType.SIMULATOR, os_version="iOS 18.0",
                          runtime="iOS-18-0", is_available=True)
    return [dev(SIM_A, "A"), dev(SIM_B, "Bob's secret iPhone"), dev(FREE, "Free")]


class TestDevices:
    def test_the_device_list_hides_another_keys_device(self, world):
        async def list_devices(*a, **k):
            return _devices()

        async def check_tools(*a, **k):
            return {}

        world.app.state.device_controller = SimpleNamespace(
            list_devices=list_devices, check_tools=check_tools, _active_udid=None)
        alice = world.client.get("/api/v1/device/list", headers=_h(world.alice))
        assert alice.status_code == 200, alice.text
        assert {d["udid"] for d in alice.json()["devices"]} == {SIM_A, FREE}
        owner = world.client.get("/api/v1/device/list", headers=_h(KEY))
        assert {d["udid"] for d in owner.json()["devices"]} == {SIM_A, SIM_B, FREE}

    def test_a_blind_write_to_another_keys_device_reads_as_one_nobody_holds(self, world):
        held = world.client.post("/api/v1/device/ui/tap", headers=_h(world.alice),
                                 json={"udid": SIM_B, "x": 1, "y": 1})
        free = world.client.post("/api/v1/device/ui/tap", headers=_h(world.alice),
                                 json={"udid": FREE, "x": 1, "y": 1})
        assert held.status_code == free.status_code == 403, (held.text, free.text)
        assert "bob" not in held.text
        assert held.json()["detail"]["message"].replace(SIM_B, "X") == \
            free.json()["detail"]["message"].replace(FREE, "X")


class TestLogGuards:
    def test_one_source_query_is_filtered_too(self, world):
        """One buffer has a fast path that skipped the filter."""
        r = world.client.get("/api/v1/logs/query",
                             params={"limit": 100, "source": "simulator"}, headers=_h(world.alice))
        assert {e["message"] for e in r.json()["entries"]} == {"alice-now", "alice-error"}

    def test_the_servers_own_lines_are_not_a_device(self, world):
        asyncio.run(world.app.state.server_buffer.append(
            _entry("server", source=LogSource.SERVER, message="server-line")))
        r = world.client.get("/api/v1/logs/query", params={"limit": 100}, headers=_h(world.alice))
        assert "server-line" not in r.text

    def test_sources_list_only_the_callers_devices(self, world):
        world.app.state.source_adapters = {
            "a": SimpleNamespace(status=lambda: SimpleNamespace(
                model_dump=lambda: {"id": "a", "device_id": SIM_A})),
            "b": SimpleNamespace(status=lambda: SimpleNamespace(
                model_dump=lambda: {"id": "b", "device_id": SIM_B})),
        }
        r = world.client.get("/api/v1/logs/sources", headers=_h(world.alice))
        assert [s["id"] for s in r.json()["sources"]] == ["a"]

    def test_the_stream_sends_only_the_callers_entries(self, world):
        from server.api.logs import stream_logs

        class FakeRequest:
            app = world.app

            async def is_disconnected(self):
                return False

        async def run():
            from server.principal import reset_principal, set_principal
            from server.session import _SESSION

            p, s = set_principal("client:alice"), _SESSION.set("alice:s1")
            try:
                response = await stream_logs(FakeRequest())
            finally:
                reset_principal(p)
                _SESSION.reset(s)
            got = []

            async def read():
                async for event in response.body_iterator:
                    got.append(event)
                    if len(got) >= 1:
                        return

            task = asyncio.create_task(read())
            await asyncio.sleep(0.05)
            await world.app.state.ring_buffer.append(_entry(SIM_B, message="bob-live"))
            await world.app.state.ring_buffer.append(_entry(SIM_A, message="alice-live"))
            await asyncio.wait_for(task, 3)
            return got

        events = asyncio.run(run())
        text = str(events)
        assert "alice-live" in text and "bob-live" not in text


class TestFlowGuards:
    def test_pending_flows(self, world):
        world.app.state.flow_store.note_started(_flow("p-alice", SIM_A))
        world.app.state.flow_store.note_started(_flow("p-bob", SIM_B))
        r = world.client.get("/api/v1/proxy/flows/pending", headers=_h(world.alice))
        assert [p["id"] for p in r.json()["pending"]] == ["p-alice"]

    def test_a_physical_device_flow_counts_only_while_its_address_is_trusted(
            self, world, monkeypatch):
        import server.api.trace as trace_api

        monkeypatch.setattr(trace_api, "read_ip_map", lambda: {
            "10.0.0.5": (SIM_A, True), "10.0.0.6": (SIM_A, False)})

        async def add():
            for fid, ip in (("ip-fresh", "10.0.0.5"), ("ip-stale", "10.0.0.6")):
                flow = _flow(fid, None)
                flow.client_ip = ip
                await world.app.state.flow_store.add(flow)

        asyncio.run(add())
        r = world.client.get("/api/v1/proxy/flows", headers=_h(world.alice))
        ids = {f["id"] for f in r.json()["flows"]}
        assert "ip-fresh" in ids and "ip-stale" not in ids

    def test_a_blind_client_may_not_filter_by_address(self, world):
        r = world.client.get("/api/v1/proxy/flows", params={"client_ip": "10.0.0.9"},
                             headers=_h(world.alice))
        assert r.status_code == 403
        assert world.client.get("/api/v1/proxy/flows", params={"client_ip": "10.0.0.9"},
                                headers=_h(KEY)).status_code == 200

    def test_the_flow_stream_sends_only_the_callers_flows(self, world):
        from server.api.proxy import stream_flows

        class FakeRequest:
            app = world.app

            async def is_disconnected(self):
                return False

        async def run():
            from server.principal import reset_principal, set_principal
            from server.session import _SESSION

            p, s = set_principal("client:alice"), _SESSION.set("alice:s1")
            try:
                response = await stream_flows(FakeRequest())
            finally:
                reset_principal(p)
                _SESSION.reset(s)
            got = []

            async def read():
                async for event in response.body_iterator:
                    got.append(event)
                    return

            task = asyncio.create_task(read())
            await asyncio.sleep(0.05)
            await world.app.state.flow_store.add(_flow("live-bob", SIM_B))
            await world.app.state.flow_store.add(_flow("live-alice", SIM_A))
            await asyncio.wait_for(task, 3)
            return got

        text = str(asyncio.run(run()))
        assert "live-alice" in text and "live-bob" not in text


class TestProxyStatus:
    def test_the_route_applies_the_blind_view(self, world):
        alice = world.client.get("/api/v1/proxy/status", headers=_h(world.alice)).json()
        owner = world.client.get("/api/v1/proxy/status", headers=_h(KEY)).json()
        assert alice["flow_store"] is None and owner["flow_store"] is not None

    def test_every_field_is_narrowed(self, world):
        from server.api.proxy import _blind_status
        from server.models import DeviceCertState

        world.app.state.proxy_adapter = SimpleNamespace(_mock_rules=[
            {"rule_id": "r-a", "simulator_udid": SIM_A},
            {"rule_id": "r-b", "simulator_udid": SIM_B}])
        world.app.state.mock_owners = {"r-a": "client:alice", "r-b": "client:bob"}
        cert = {"name": "x", "cert_installed": True}
        status = ProxyStatusResponse(
            status="running",
            simulator_tls=[SimulatorTls(udid=SIM_A, tls="decrypted"),
                           SimulatorTls(udid=SIM_B, tls="decrypted")],
            cert_setup={SIM_A: DeviceCertState(**cert), SIM_B: DeviceCertState(**cert)},
            tls_rejections=[TlsRejection(sni="corp.example")], mock_rules_count=2)
        view = visibility.View(since={SIM_A: datetime.now(UTC) - timedelta(seconds=30)},
                               session_prefix="alice:")
        from server.principal import reset_principal, set_principal

        p = set_principal("client:alice")
        try:
            out = asyncio.run(_blind_status(SimpleNamespace(app=world.app), status, view))
        finally:
            reset_principal(p)
        assert list(out.cert_setup) == [SIM_A]
        assert out.tls_rejections == []
        assert out.mock_rules_count == 1

    def test_cert_status_lists_only_the_callers_devices(self, world):
        from server.proxy.cert_state import update_cert_state

        for udid in (SIM_A, SIM_B):
            update_cert_state(udid, {"name": udid[:4], "cert_installed": True})
        r = world.client.get("/api/v1/proxy/cert/status", headers=_h(world.alice))
        assert SIM_B not in r.json()["devices"] and SIM_A in r.json()["devices"]


class TestCrashesAndTrace:
    def test_a_mac_process_crash_is_never_a_devices(self, world):
        world.app.state.crash_adapter = SimpleNamespace(crash_reports=[
            CrashReport(crash_id="c-mac", timestamp=datetime.now(UTC), device_id=SIM_A,
                        process="Finder", mac_process=True)])
        r = world.client.get("/api/v1/crashes/latest", params={"symbolicate": "false"},
                             headers=_h(world.alice))
        assert r.json()["crashes"] == []

    def test_the_trace_drops_another_keys_crashes(self, world):
        async def seed():
            await world.app.state.server_buffer.append(_entry(
                "server", ago=1, source=LogSource.SERVER, message="alice-tap", action="tap",
                udid=SIM_A, outcome="ok", session="alice:s1"))
            for udid, msg in ((SIM_B, "crash-bob"), ("", "crash-nowhere")):
                await world.app.state.crash_buffer.append(_entry(
                    udid, source=LogSource.CRASH, message=msg))

        asyncio.run(seed())
        r = world.client.get("/api/v1/trace", headers=_h(world.alice))
        # A crash naming no device would be attached to alice's action by
        # time, as a shared trace does: on a blind server it is not hers.
        assert "crash-bob" not in r.text and "crash-nowhere" not in r.text
        owner = world.client.get("/api/v1/trace", headers=_h(KEY))
        assert "crash-nowhere" in owner.text, "the owner's trace attaches it"


class TestRecordings:
    def test_a_blind_clients_recording_is_its_own_and_attributable_only(self, world):
        started = {}

        async def check_start(*a, **k):
            return None

        async def start(udid, output_dir, filters, requested_by=None, **kw):
            started.update(kw)
            return SimpleNamespace(summary=lambda: {"id": "r1"}, filters=filters, warnings=[])

        world.app.state.recordings = SimpleNamespace(check_start=check_start, start=start)

        async def ensure(*a, **k):
            return None

        world.app.state.device_controller = SimpleNamespace(
            _active_udid=SIM_A, _ensure_device_type_cached=ensure,
            _device_type=lambda u: DeviceType.SIMULATOR)
        r = world.client.post("/api/v1/recordings", headers=_h(world.alice),
                              json={"udid": SIM_A, "kinds": ["logs"]})
        assert r.status_code == 200, r.text
        assert started == {"principal": "client:alice", "blind": True}

    def test_the_recorder_keeps_only_attributable_data_for_a_blind_recording(self, tmp_path):
        from server.recording import recorder

        rec = recorder.Recording(id="r", udid=SIM_A, dir=tmp_path, filters=recorder.Filters(),
                                 started_at=datetime.now(UTC), blind=True)
        nowhere = _entry("", source=LogSource.CRASH, message="host crash")
        assert not recorder.RecordingManager._wants_crash(rec, nowhere)
        rec.blind = False
        assert recorder.RecordingManager._wants_crash(rec, nowhere)


class TestPreviewsAndTimeline:
    def _preview(self, world, monkeypatch):
        import server.api.device as device_api

        world.app.state.preview_manager = SimpleNamespace(
            status=lambda: {"status": "running", "pid": 4, "active": {SIM_B: {}, SIM_A: {}},
                            "active_count": 2, "available": [], "available_count": 0})
        world.app.state.scrcpy_preview = SimpleNamespace(
            status=lambda: {"available": False, "active": {}, "active_count": 0})

        async def sims():
            return [{"id": SIM_A, "kind": "simulator"}, {"id": SIM_B, "kind": "simulator"},
                    {"id": FREE, "kind": "simulator"}], None

        monkeypatch.setattr(device_api, "simulator_entries", sims)

    def test_preview_status(self, world, monkeypatch):
        self._preview(world, monkeypatch)
        ios = world.client.get("/api/v1/device/preview/status",
                               headers=_h(world.alice)).json()["ios"]
        assert {d["id"] for d in ios["available"]} == {SIM_A, FREE}
        assert list(ios["active"]) == [SIM_A]
        assert "pid" not in ios

    def test_preview_devices(self, world, monkeypatch):
        self._preview(world, monkeypatch)

        async def list_devices():
            return {"devices": [{"id": "cmio-1", "name": "Bob's iPhone", "kind": "device"}]}

        world.app.state.preview_manager.list_devices = list_devices
        r = world.client.get("/api/v1/device/preview/devices", headers=_h(world.alice))
        assert {d["id"] for d in r.json()["devices"]} == {SIM_A, FREE}

    def test_the_timeline_of_another_keys_device_is_not_there(self, world):
        world.app.state.active_timeline = SimpleNamespace(udid=SIM_B,
                                                          get_manifest=lambda: {"udid": SIM_B})
        assert world.client.get("/api/v1/device/screenshot/timeline",
                                headers=_h(world.alice)).status_code == 404
        world.app.state.active_timeline.udid = SIM_A
        assert world.client.get("/api/v1/device/screenshot/timeline",
                                headers=_h(world.alice)).status_code == 200


def test_a_blind_client_is_not_told_the_owners_session_name(world):
    """The owner freeing a client's device: the client's socket is told who
    did it, but not the owner's session name."""
    from server.api.sessions import _sockets

    sent = []

    class WS:
        async def send_json(self, data):
            sent.append(data)

        async def close(self, code=1000, reason=""):
            pass

    _sockets(world.app)["alice:s1"] = (1, WS())
    r = world.client.delete("/api/v1/sessions/alice:s1/reservations",
                            params={"udid": SIM_A}, headers=_h(KEY, "owner-secret-ci"))
    assert r.status_code == 200, r.text
    assert sent and sent[0]["by"] == "the server's owner", sent


def test_a_clients_rules_and_recordings_end_with_its_hold(world):
    """Left in place, the next holder's traffic was answered by them, and a
    recording went on filming the next holder's work."""
    from server.api.sessions import run_release_policies

    cleared, stopped = [], []

    async def clear_mock(rule_id=None):
        cleared.append(rule_id)

    async def stop(rec_id):
        stopped.append(rec_id)

    world.app.state.proxy_adapter = SimpleNamespace(clear_mock=clear_mock, _mock_rules=[
        {"rule_id": "alice-rule", "simulator_udid": SIM_A},
        {"rule_id": "owner-rule", "simulator_udid": SIM_A},
        {"rule_id": "bob-rule", "simulator_udid": SIM_B}])
    world.app.state.mock_owners = {"alice-rule": "client:alice", "owner-rule": "owner",
                                   "bob-rule": "client:bob"}
    rec = SimpleNamespace(id="rec-alice", udid=SIM_A, principal="client:alice", state="recording")
    other = SimpleNamespace(id="rec-owner", udid=SIM_A, principal="owner", state="recording")
    world.app.state.recordings = SimpleNamespace(list=lambda: [rec, other], stop=stop)
    world.registry.release("alice:s1")
    actions = asyncio.run(run_release_policies(world.app))
    assert cleared == ["alice-rule"], "only the client's own rules on its released device"
    assert stopped == ["rec-alice"]
    assert {a["action"] for a in actions} == {"mock_removed", "recording_stopped"}


class TestReleasingADevice:
    """A client's device is not another key's until its clean-up has run
    (CodeRabbit): in the gap its recording filmed, and its rules answered,
    the next holder."""

    def test_another_key_cannot_reserve_it_until_the_clean_up_has_run(self, world):
        from server.api.sessions import run_release_policies
        from server.session_registry import SessionConflict

        world.registry.release("bob:s1")
        import pytest as _pytest

        with _pytest.raises(SessionConflict, match="being released"):
            world.registry.reserve("alice:s1", [SIM_B], on_release="none")
        assert SIM_B in world.registry.held_udids(), "the pool does not offer it either"
        asyncio.run(run_release_policies(world.app))
        world.registry.reserve("alice:s1", [SIM_B], on_release="none")

    def test_the_same_key_may_take_it_back(self, world):
        world.registry.unreserve("bob:s1", [SIM_B])
        world.registry.open("bob:s2", principal="client:bob")
        world.registry.get("bob:s2").state = "live"
        world.registry.reserve("bob:s2", [SIM_B], on_release="none")

    def test_the_owners_releases_do_not_wait(self, world):
        world.registry.open("ci", principal="owner")
        world.registry.get("ci").state = "live"
        world.registry.reserve("ci", [FREE], on_release="none")
        world.registry.release("ci")
        world.registry.reserve("alice:s1", [FREE], on_release="none")

    def test_a_failed_clean_up_keeps_the_device_and_tries_again(self, world):
        """Freed anyway, another key could reserve a device the previous
        client's rule still answered (CodeRabbit). Fails closed."""
        from server.api.sessions import run_release_policies
        from server.session_registry import SessionConflict

        calls = []

        async def clear_mock(rule_id=None):
            calls.append(rule_id)
            if len(calls) == 1:
                raise RuntimeError("proxy busy")

        world.app.state.proxy_adapter = SimpleNamespace(
            clear_mock=clear_mock, _mock_rules=[{"rule_id": "bob-rule", "simulator_udid": SIM_B}])
        world.app.state.mock_owners = {"bob-rule": "client:bob"}
        world.registry.release("bob:s1")
        asyncio.run(run_release_policies(world.app))          # fails
        import pytest as _pytest

        with _pytest.raises(SessionConflict, match="being released"):
            world.registry.reserve("alice:s1", [SIM_B], on_release="none")
        asyncio.run(run_release_policies(world.app))          # retried, succeeds
        assert calls == ["bob-rule", "bob-rule"]
        world.registry.reserve("alice:s1", [SIM_B], on_release="none")

    def test_the_owner_can_take_a_device_whose_clean_up_keeps_failing(self, world):
        world.registry.release("bob:s1")                      # clean-up never ran
        world.registry.open("ci", principal="owner")
        world.registry.get("ci").state = "live"
        world.registry.reserve("ci", [SIM_B], on_release="none")

    def test_a_blind_release_answers_only_its_own_actions(self):
        from server.api.sessions import _answerable

        actions = [{"udid": SIM_B, "action": "mock_removed", "_session": "bob:s1"},
                   {"udid": SIM_B, "action": "shutdown", "_session": "alice:s1"}]
        assert _answerable(actions, "alice:s1", blind=True) == [
            {"udid": SIM_B, "action": "shutdown"}]
        assert len(_answerable(actions, "alice:s1", blind=False)) == 2
        assert all("_session" not in a for a in _answerable(actions, "x", blind=False))


class TestDoctorReportsTheRunningMode:
    def test_a_config_change_without_a_restart_is_called_out(self, monkeypatch, capsys):
        import server.main as main_mod
        from server import config

        monkeypatch.setattr(main_mod, "read_state",
                            lambda: {"server_host": "127.0.0.1", "visibility": "shared"})
        monkeypatch.setattr(config, "read_user_config",
                            lambda: {"sessions": {"visibility": "blind"}})
        main_mod._report_bind()
        out = capsys.readouterr().out
        assert "sharing: shared" in out and "restart" in out

    def test_the_running_mode_is_reported(self, monkeypatch, capsys):
        import server.main as main_mod
        from server import config

        monkeypatch.setattr(main_mod, "read_state",
                            lambda: {"server_host": "127.0.0.1", "visibility": "blind"})
        monkeypatch.setattr(config, "read_user_config",
                            lambda: {"sessions": {"visibility": "blind"}})
        main_mod._report_bind()
        out = capsys.readouterr().out
        assert "sharing: blind" in out and "restart" not in out
