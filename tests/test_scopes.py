"""Client keys, principals and scopes (#254, phase 2).

Two halves. The guard tests read the real app's routes and hold `ROUTES` to
them: every route classified, none stale, a view grant never a change. The
behaviour tests drive a small app with the production middlewares in their
production order, and a controller whose active device is per session, as the
real one's is -- so a client's unqualified call resolves through its own slot.
"""

from __future__ import annotations

import json
import os
import stat

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.websockets import WebSocketDisconnect

from server import device_access, principal, scopes
from server.api.sessions import release_revoked
from server.api.sessions import router as sessions_router
from server.auth import WS_UNAUTHORISED, APIKeyMiddleware
from server.clients import ClientKeyError, ClientKeys
from server.device_access import AccessMiddleware
from server.scopes import ScopeMiddleware
from server.session import SessionMiddleware, current_session
from server.session_registry import OWNER, SessionRegistry

KEY = "k" * 32


# ---------------------------------------------------------------------------
# The table against the real app
# ---------------------------------------------------------------------------


def _real_routes() -> set[tuple[str, str]]:
    from starlette.routing import Route, WebSocketRoute

    import server.main as m
    from server.config import ServerConfig

    app = m.create_app(ServerConfig(api_key=KEY))
    found = {(method.upper(), path) for path, ops in app.openapi()["paths"].items()
             for method in ops}

    def walk(routes):
        for r in routes:
            if isinstance(r, WebSocketRoute):
                found.add(("WS", r.path))
            elif isinstance(r, Route) and not r.include_in_schema:
                found.update((m, r.path) for m in r.methods - {"HEAD"})
            # FastAPI wraps an included router; its routes keep their full
            # paths (the routers here carry their prefix themselves).
            inner = getattr(r, "original_router", None)
            walk(getattr(r, "routes", None) or getattr(inner, "routes", None) or [])

    walk(app.router.routes)
    return found


class TestTheTableCoversTheApp:
    def test_every_route_is_classified(self):
        """Closed by default is only safe if nobody relies on it by accident:
        a new route must be put somewhere on purpose."""
        routes = _real_routes()
        assert len(routes) > 140, "the walk found too few routes -- is it still working?"
        undecided = sorted(routes - set(scopes.ROUTES))
        assert undecided == [], (
            f"routes with no scope decision (server/scopes.py ROUTES): {undecided}")

    def test_no_decision_names_a_route_that_is_gone(self):
        stale = sorted(set(scopes.ROUTES) - _real_routes())
        assert stale == [], f"ROUTES names routes that no longer exist: {stale}"

    def test_a_view_grant_never_reaches_a_change(self):
        """VIEW is reads only, so a client with nothing reserved cannot change
        anything through it; and CONTROL is changes only, because check()
        refuses only during a change."""
        for (method, path), rule in scopes.ROUTES.items():
            if method == "WS":
                continue
            write = device_access.is_write(method, path)
            if rule.access is scopes.Access.VIEW:
                assert not write, f"{method} {path} is VIEW but changes something"
            if rule.access is scopes.Access.CONTROL:
                assert write, f"{method} {path} is CONTROL but reads as a read: check() would pass"

    def test_every_control_rule_says_how_it_finds_its_device(self):
        for key, rule in scopes.ROUTES.items():
            if rule.access is scopes.Access.CONTROL:
                assert rule.device in ("named", "resolved", "path-id"), key
            else:
                assert rule.device is None, key

    def test_a_change_addressed_by_an_id_is_decided_by_the_route_or_the_owner(self):
        """No device field reaches the middleware for these, so a client may
        use one only if the route checks the device behind the id."""
        for (method, path), rule in scopes.ROUTES.items():
            if "{" in path and method != "WS" and device_access.is_write(method, path):
                assert rule.access in (scopes.Access.OWNER, scopes.Access.RESERVE) or \
                    rule.device == "path-id", f"{method} {path}"

    def test_mac_wide_routes_are_owner_only(self):
        for key in [("POST", "/api/v1/proxy/stop"), ("POST", "/api/v1/proxy/configure-system"),
                    ("POST", "/api/v1/proxy/local-capture"), ("POST", "/api/v1/system/update"),
                    ("POST", "/api/v1/logs/oslog/start"), ("POST", "/api/v1/devices/resolve"),
                    ("GET", "/api/v1/recordings/events"), ("POST", "/api/v1/builds/parse-file")]:
            assert scopes.ROUTES[key].access is scopes.Access.OWNER, key

    def test_public_rules_are_exactly_the_public_paths(self):
        from server.auth import PUBLIC_PATHS

        public = {p for (m, p), r in scopes.ROUTES.items() if r.access is scopes.Access.PUBLIC}
        assert public == set(PUBLIC_PATHS)


class TestMatching:
    def test_unmapped_is_owner_only(self):
        assert scopes.rule_for("POST", "/api/v1/nothing/here").access is scopes.Access.OWNER

    def test_templates_match_one_segment(self):
        assert scopes.rule_for("GET", "/api/v1/proxy/flows/abc").access is scopes.Access.VIEW
        assert scopes.rule_for("GET", "/api/v1/proxy/flows/a/b").access is scopes.Access.OWNER

    def test_a_literal_route_wins_over_a_template(self):
        assert scopes.match("GET", "/api/v1/proxy/flows/summary")[0] == \
            "/api/v1/proxy/flows/summary"

    def test_head_is_read_as_get(self):
        assert scopes.rule_for("HEAD", "/api/v1/device/list").access is scopes.Access.VIEW


# ---------------------------------------------------------------------------
# Session ids in a client's namespace
# ---------------------------------------------------------------------------


class TestQualify:
    def test_a_client_id_gains_its_label(self):
        assert principal.qualify("claude-x", "client:alice") == "alice:claude-x"

    def test_idempotent(self):
        assert principal.qualify("alice:claude-x", "client:alice") == "alice:claude-x"

    def test_another_clients_prefix_is_not_a_way_in(self):
        assert principal.qualify("alice:claude-x", "client:bob") == "bob:alice:claude-x"

    def test_the_owner_is_unchanged(self):
        assert principal.qualify("claude-x", OWNER) == "claude-x"


# ---------------------------------------------------------------------------
# The client key store
# ---------------------------------------------------------------------------


@pytest.fixture
def keys(tmp_path):
    return ClientKeys(tmp_path / "clients.json")


class TestClientKeys:
    def test_a_key_works_and_only_its_hash_is_stored(self, keys):
        key = keys.add("alice")
        assert key.startswith("qc_")
        assert keys.principal_for(key.encode()) == "client:alice"
        text = keys.path.read_text()
        assert key not in text and key[3:] not in text
        assert stat.S_IMODE(os.stat(keys.path).st_mode) == 0o600

    def test_a_wrong_key_is_nobody(self, keys):
        keys.add("alice")
        assert keys.principal_for(b"qc_wrong") is None
        assert keys.principal_for(KEY.encode()) is None

    def test_revoking_stops_the_key_in_a_running_reader(self, tmp_path):
        """The server and the CLI are separate processes: the server's reader
        must notice the CLI's revoke without a restart."""
        server_side = ClientKeys(tmp_path / "clients.json")
        cli_side = ClientKeys(tmp_path / "clients.json")
        key = cli_side.add("alice")
        assert server_side.principal_for(key.encode()) == "client:alice"
        cli_side.revoke("alice")
        assert server_side.principal_for(key.encode()) is None
        assert not server_side.is_active("alice")

    def test_a_label_with_a_live_key_is_not_reissued(self, keys):
        keys.add("alice")
        with pytest.raises(ClientKeyError, match="revoke it first"):
            keys.add("alice")
        keys.revoke("alice")
        assert keys.add("alice").startswith("qc_")

    @pytest.mark.parametrize("label", ["", "Alice", "-x", "a" * 17, "a:b", "a b"])
    def test_bad_labels_are_refused(self, keys, label):
        with pytest.raises(ClientKeyError):
            keys.add(label)

    def test_an_unreadable_file_admits_nobody_and_is_not_overwritten(self, keys):
        key = keys.add("alice")
        keys.path.write_text("{not json")
        assert keys.principal_for(key.encode()) is None
        with pytest.raises(ClientKeyError, match="not readable"):
            keys.add("bob")
        assert keys.path.read_text() == "{not json"

    def test_use_is_recorded_apart_from_the_keys(self, keys):
        key = keys.add("alice")
        before = keys.path.read_text()
        keys.principal_for(key.encode())
        assert "alice" in keys.last_used()
        assert keys.path.read_text() == before, "the server must never write clients.json"


# ---------------------------------------------------------------------------
# Behaviour, through the production middleware order
# ---------------------------------------------------------------------------


class SlotController:
    """A controller whose active device is per session, as the real one's is."""

    def __init__(self):
        self.slots: dict[str, str] = {}
        self._pool = None

    @property
    def _active_udid(self):
        return self.slots.get(current_session())

    @_active_udid.setter
    def _active_udid(self, value):
        self.slots[current_session()] = value

    async def resolve_udid(self, udid=None):
        resolved = udid or self._active_udid
        device_access.check(resolved)
        return resolved


class TapBody(BaseModel):
    udid: str | None = None


def _app(registry, keys):
    app = FastAPI()
    app.state.sessions = registry
    app.state.clients = keys
    app.state.device_controller = SlotController()
    app.include_router(sessions_router)

    @app.post("/api/v1/device/ui/tap")
    async def tap(body: TapBody):
        return {"tapped": await app.state.device_controller.resolve_udid(body.udid)}

    @app.post("/api/v1/device/preview/stop")
    async def preview_stop(body: TapBody):
        return {"stopped": body.udid or "all"}

    @app.post("/api/v1/proxy/stop")
    async def proxy_stop():
        return {"stopped": True}

    @app.get("/api/v1/device/list")
    async def device_list():
        return {"session": current_session(), "principal": principal.current_principal()}

    @app.get("/api/v1/trace")
    async def trace(session: str | None = None, recording: str | None = None):
        return {"session": session}

    @app.post("/api/v1/not/in/the/table")
    async def unmapped(request: Request):
        return {"reached": True}

    app.add_middleware(AccessMiddleware)
    app.add_middleware(ScopeMiddleware)
    app.add_middleware(APIKeyMiddleware, api_key=KEY, clients=keys)
    app.add_middleware(SessionMiddleware)
    return app


@pytest.fixture
def registry():
    reg = SessionRegistry(grace_seconds=180)
    device_access.install(reg)
    yield reg
    device_access.install(None)


@pytest.fixture
def world(registry, keys):
    alice, bob = keys.add("alice"), keys.add("bob")
    app = _app(registry, keys)
    with TestClient(app) as c:
        yield c, app, alice, bob


def _h(key, session="s1"):
    h = {"Authorization": f"Bearer {key}"}
    if session:
        h["X-Quern-Session"] = session
    return h


def _hold(registry, session_id, principal_, udid):
    """A live session holding `udid`, as the session routes would leave it."""
    registry.open(session_id, principal=principal_)
    s = registry.get(session_id)
    s.state = "live"
    registry.reserve(session_id, [udid], on_release="none", booted=[])


class TestTheOwnerIsUnchanged:
    def test_the_owner_reaches_everything_without_naming_a_session(self, world):
        c, *_ = world
        assert c.post("/api/v1/proxy/stop", headers=_h(KEY, None)).status_code == 200
        assert c.post("/api/v1/not/in/the/table", headers=_h(KEY, None)).status_code == 200

    def test_the_owner_may_change_a_free_device(self, world):
        c, *_ = world
        r = c.post("/api/v1/device/ui/tap", headers=_h(KEY), json={"udid": "SIM-9"})
        assert r.status_code == 200, r.text


class TestWhatAClientMayReach:
    def test_a_client_is_its_own_principal_in_its_own_namespace(self, world):
        c, _, alice, _ = world
        body = c.get("/api/v1/device/list", headers=_h(alice, "claude-1")).json()
        assert body == {"session": "alice:claude-1", "principal": "client:alice"}

    def test_a_client_must_name_its_session(self, world):
        c, _, alice, _ = world
        r = c.get("/api/v1/device/list", headers=_h(alice, None))
        assert r.status_code == 400 and "X-Quern-Session" in r.json()["detail"]

    def test_owner_only_routes_are_refused(self, world):
        c, _, alice, _ = world
        r = c.post("/api/v1/proxy/stop", headers=_h(alice))
        assert r.status_code == 403 and "owner-only" in r.json()["detail"]

    def test_an_unmapped_route_is_refused_closed_by_default(self, world):
        c, _, alice, _ = world
        assert c.post("/api/v1/not/in/the/table", headers=_h(alice)).status_code == 403

    def test_a_revoked_key_is_refused(self, world, keys):
        c, _, alice, _ = world
        keys.revoke("alice")
        assert c.get("/api/v1/device/list", headers=_h(alice)).status_code == 401

    def test_a_client_cannot_point_the_trace_at_a_mac_path(self, world):
        c, _, alice, _ = world
        r = c.get("/api/v1/trace", params={"recording": "/etc"}, headers=_h(alice))
        assert r.status_code == 403
        assert c.get("/api/v1/trace", headers=_h(alice)).status_code == 200

    def test_the_trace_filter_asks_about_the_clients_own_session(self, world):
        c, _, alice, _ = world
        r = c.get("/api/v1/trace", params={"session": "claude-1"}, headers=_h(alice))
        assert r.json()["session"] == "alice:claude-1"


class TestAClientChangesOnlyWhatItHolds:
    def test_a_free_device_is_refused(self, world):
        c, _, alice, _ = world
        r = c.post("/api/v1/device/ui/tap", headers=_h(alice), json={"udid": "SIM-9"})
        assert r.status_code == 403
        assert "reserve it first" in r.json()["detail"]["message"]

    def test_its_own_reserved_device_is_allowed(self, world, registry):
        c, _, alice, _ = world
        _hold(registry, "alice:s1", "client:alice", "SIM-1")
        r = c.post("/api/v1/device/ui/tap", headers=_h(alice), json={"udid": "SIM-1"})
        assert r.status_code == 200, r.text

    def test_a_device_another_session_holds_is_423(self, world, registry):
        c, _, alice, _ = world
        _hold(registry, "bob:s1", "client:bob", "SIM-2")
        r = c.post("/api/v1/device/ui/tap", headers=_h(alice), json={"udid": "SIM-2"})
        assert r.status_code == 423

    def test_a_sibling_session_of_the_same_client_is_refused_too(self, world, registry):
        """The session decides control, not the principal: phase 1 already
        refused the owner's other sessions."""
        c, _, alice, _ = world
        _hold(registry, "alice:other", "client:alice", "SIM-3")
        r = c.post("/api/v1/device/ui/tap", headers=_h(alice), json={"udid": "SIM-3"})
        assert r.status_code == 423

    def test_no_device_and_no_active_device_is_refused_before_the_route(self, world):
        c, _, alice, _ = world
        r = c.post("/api/v1/device/ui/tap", headers=_h(alice), json={})
        assert r.status_code == 403 and "reserve one first" in r.json()["detail"]

    def test_an_unqualified_change_goes_to_its_reserved_active_device(self, world, registry):
        c, app, alice, _ = world
        _hold(registry, "alice:s1", "client:alice", "SIM-1")
        app.state.device_controller.slots["alice:s1"] = "SIM-1"
        r = c.post("/api/v1/device/ui/tap", headers=_h(alice), json={})
        assert r.status_code == 200 and r.json() == {"tapped": "SIM-1"}

    def test_an_active_device_it_no_longer_holds_is_refused(self, world):
        c, app, alice, _ = world
        app.state.device_controller.slots["alice:s1"] = "SIM-1"
        r = c.post("/api/v1/device/ui/tap", headers=_h(alice), json={})
        assert r.status_code == 403

    def test_naming_no_device_where_that_means_every_device_is_owner_only(self, world, registry):
        c, _, alice, _ = world
        _hold(registry, "alice:s1", "client:alice", "SIM-1")
        r = c.post("/api/v1/device/preview/stop", headers=_h(alice), json={})
        assert r.status_code == 403 and "every device" in r.json()["detail"]
        ok = c.post("/api/v1/device/preview/stop", headers=_h(alice), json={"udid": "SIM-1"})
        assert ok.status_code == 200


class TestClientSessions:
    def test_a_client_opens_sessions_in_its_own_namespace(self, world):
        c, _, alice, _ = world
        r = c.post("/api/v1/sessions", headers=_h(alice, "claude-1"), json={})
        assert r.status_code == 200, r.text
        assert r.json()["id"] == "alice:claude-1"
        assert r.json()["live_path"] == "/api/v1/sessions/alice:claude-1/live"

    def test_the_body_name_is_qualified_too(self, world):
        c, _, alice, _ = world
        r = c.post("/api/v1/sessions", headers=_h(alice, "claude-1"), json={"name": "ci"})
        assert r.json()["id"] == "alice:ci"

    def test_a_client_cannot_release_another_clients_session(self, world):
        c, _, alice, bob = world
        c.post("/api/v1/sessions", headers=_h(alice, "claude-1"), json={})
        for sid in ("claude-1", "alice:claude-1"):
            r = c.delete(f"/api/v1/sessions/{sid}", headers=_h(bob, "claude-1"))
            assert r.status_code == 404, (sid, r.text)

    def test_a_client_holds_its_session_over_the_socket(self, world):
        c, _, alice, _ = world
        opened = c.post("/api/v1/sessions", headers=_h(alice, "claude-1"), json={}).json()
        # The raw id in the path, as the MCP sends it: qualified on the way in.
        with c.websocket_connect("/api/v1/sessions/claude-1/live", headers={
                **_h(alice, "claude-1"),
                "X-Quern-Resume-Token": opened["resume_token"]}) as ws:
            assert ws.receive_json()["event"] == "attached"

    def test_a_revoked_clients_sessions_end(self, world, registry, keys):
        import asyncio

        c, app, alice, _ = world
        _hold(registry, "alice:s1", "client:alice", "SIM-1")
        _hold(registry, "bob:s1", "client:bob", "SIM-2")
        keys.revoke("alice")
        ended = asyncio.run(release_revoked(app))
        assert ended == ["alice:s1"]
        assert registry.holder_of("SIM-1") is None
        assert registry.holder_of("SIM-2") is not None

    def test_a_revoked_key_cannot_open_the_socket(self, world, keys):
        c, _, alice, _ = world
        keys.revoke("alice")
        with pytest.raises(WebSocketDisconnect) as e, c.websocket_connect(
                "/api/v1/sessions/claude-1/live", headers=_h(alice, "claude-1")) as ws:
            ws.receive_json()
        assert e.value.code == WS_UNAUTHORISED


class TestWhyNot:
    def test_the_matrix(self, registry):
        from server.principal import reset_principal, set_principal
        from server.session import _SESSION

        _hold(registry, "alice:s1", "client:alice", "SIM-1")
        cases = [
            (OWNER, "x", "SIM-9", None),                     # the owner, a free device
            ("client:alice", "alice:s1", "SIM-9", device_access.NOT_RESERVED),
            ("client:alice", "alice:s1", "SIM-1", None),      # its own
            ("client:alice", "alice:s2", "SIM-1", device_access.HELD_ELSEWHERE),
            (OWNER, "x", "SIM-1", device_access.HELD_ELSEWHERE),
        ]
        for who, session, udid, expected in cases:
            p, s = set_principal(who), _SESSION.set(session)
            try:
                assert device_access.why_not(udid) == expected, (who, session, udid)
            finally:
                reset_principal(p)
                _SESSION.reset(s)


# ---------------------------------------------------------------------------
# The bind, and the key a client process sends
# ---------------------------------------------------------------------------


class TestBind:
    @pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
    def test_loopback_needs_no_warning(self, host):
        from server.config import bind_warning

        assert bind_warning(host) is None

    @pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "::"])
    def test_anything_else_is_warned_about(self, host):
        from server.config import bind_warning

        assert "unencrypted" in bind_warning(host)

    def test_the_default_is_loopback(self):
        from server.config import ServerConfig

        assert ServerConfig(api_key=KEY).host == "127.0.0.1"

    @pytest.mark.parametrize("held, probe", [("0.0.0.0", "127.0.0.1"), ("127.0.0.1", "0.0.0.0")])
    def test_a_port_held_on_the_other_address_is_not_free(self, held, probe):
        """Measured: a SO_REUSEADDR bind on one of these succeeds while a
        server listens on the other, so a one-address probe said free."""
        import socket

        from server.lifecycle.ports import is_port_available

        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((held, 0))
        s.listen()
        try:
            assert not is_port_available(s.getsockname()[1], probe)
        finally:
            s.close()


class TestClientApiKey:
    def test_quern_api_key_wins(self, monkeypatch, tmp_path):
        from server import config

        f = tmp_path / "api-key"
        f.write_text("owner-key")
        monkeypatch.setattr(config, "API_KEY_FILE", f)
        monkeypatch.setenv("QUERN_API_KEY", "  qc_client  ")
        assert config.client_api_key() == "qc_client"
        monkeypatch.setenv("QUERN_API_KEY", " ")
        assert config.client_api_key() == "owner-key"
        monkeypatch.delenv("QUERN_API_KEY")
        assert config.client_api_key() == "owner-key"


def test_clients_cli_round_trip(monkeypatch, tmp_path, capsys):
    from server import clients, clients_cli

    monkeypatch.setattr(clients, "default_path", lambda: tmp_path / "clients.json")
    assert clients_cli.main(["add", "alice"]) == 0
    key = next(line.strip() for line in capsys.readouterr().out.splitlines()
               if line.strip().startswith("qc_"))
    assert ClientKeys(tmp_path / "clients.json").principal_for(key.encode()) == "client:alice"
    assert clients_cli.main(["list"]) == 0
    assert "alice" in capsys.readouterr().out
    assert clients_cli.main(["revoke", "alice"]) == 0
    assert clients_cli.main(["revoke", "alice"]) == 1, "revoking twice is an error"
    assert clients_cli.main(["add"]) == 2
    data = json.loads((tmp_path / "clients.json").read_text())
    assert data["clients"][0]["revoked_at"]


class TestRouteLevelOwnerChecks:
    """Changes addressed by an id, whose reach depends on what the id names."""

    def _as(self, who):
        from server.principal import set_principal
        from server.session import _SESSION

        return set_principal(who), _SESSION.set("alice:s1" if who != OWNER else "x"), \
            device_access._WRITE.set(True)

    def _reset(self, tokens):
        from server.principal import reset_principal
        from server.session import _SESSION

        p, s, w = tokens
        reset_principal(p)
        _SESSION.reset(s)
        device_access._WRITE.reset(w)

    def test_an_unscoped_mock_rule_is_the_owners(self, registry):
        from types import SimpleNamespace

        from server.api.proxy_intercept import _check_mock_scope

        adapter = SimpleNamespace(_mock_rules=[{"rule_id": "r1", "simulator_udid": None}])
        tokens = self._as("client:alice")
        try:
            with pytest.raises(device_access.OwnerOnly):
                _check_mock_scope(adapter, "r1")
        finally:
            self._reset(tokens)
        tokens = self._as(OWNER)
        try:
            _check_mock_scope(adapter, "r1")
        finally:
            self._reset(tokens)

    def test_a_clients_own_scoped_mock_rule_is_its_to_change(self, registry):
        from types import SimpleNamespace

        from server.api.proxy_intercept import _check_mock_scope

        _hold(registry, "alice:s1", "client:alice", "SIM-1")
        adapter = SimpleNamespace(_mock_rules=[{"rule_id": "r1", "simulator_udid": "SIM-1"},
                                               {"rule_id": "r2", "simulator_udid": "SIM-9"}])
        tokens = self._as("client:alice")
        try:
            _check_mock_scope(adapter, "r1")
            with pytest.raises(device_access.DeviceNotReserved):
                _check_mock_scope(adapter, "r2")
        finally:
            self._reset(tokens)

    def test_stopping_an_unscoped_capture_is_the_owners(self):
        import inspect

        from server.api import proxy

        source = inspect.getsource(proxy.stop_capture)
        assert "device_access.require_owner(" in source
