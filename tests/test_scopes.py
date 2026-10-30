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
        for key in [("POST", "/api/v1/device/build-and-install"),
                    ("POST", "/api/v1/device/app/state/save"),
                    ("POST", "/api/v1/device/app/state/restore"),
                    ("POST", "/api/v1/builds/parse"),
                    ("POST", "/api/v1/proxy/stop"), ("POST", "/api/v1/proxy/configure-system"),
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

    def test_a_trailing_slash_is_the_same_route(self):
        """Starlette redirects between the two spellings; the table must not
        refuse the one it does not list before the redirect happens."""
        assert scopes.match("GET", "/api/v1/landmarks")[0] == "/api/v1/landmarks/"
        assert scopes.match("GET", "/api/v1/device/list/")[0] == "/api/v1/device/list"
        assert scopes.rule_for("GET", "/api/v1/landmarks").access is scopes.Access.VIEW

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
        assert keys.principal_for(key.encode()) == "client:alice", "loaded before the damage"
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

    @app.post("/api/v1/device/ui/press")
    async def press_without_check():
        """A route that reads the active device without calling check(), as
        the build path's readers do: only the middleware's precheck guards it."""
        return {"pressed": app.state.device_controller._active_udid}

    @app.post("/api/v1/proxy/mocks")
    async def set_mock(request: Request):
        return {"rule": await request.json()}

    @app.post("/api/v1/proxy/capture/start")
    async def capture_start(request: Request):
        return {"capture": await request.json()}

    @app.post("/api/v1/recordings")
    async def start_recording(request: Request):
        return {"recording": await request.json()}

    @app.post("/api/v1/device/logging/start")
    async def logging_start(request: Request):
        return {"logging": await request.json()}

    @app.get("/api/v1/logs/stream")
    async def stream(request: Request):
        """Revokes its caller's key mid-stream, then asks whether the caller
        is still there -- the question every stream loop asks."""
        app.state.clients.revoke("alice")
        return {"disconnected": await request.is_disconnected()}

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
        # Query text, not a boolean: a directory may be called "false" (CodeRabbit).
        r = c.get("/api/v1/trace", params={"recording": "false"}, headers=_h(alice))
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
        monkeypatch.setenv("QUERN_CLIENT_KEY", "  qc_client  ")
        assert config.client_api_key() == "qc_client"
        monkeypatch.setenv("QUERN_CLIENT_KEY", " ")
        assert config.client_api_key() == "owner-key"
        monkeypatch.delenv("QUERN_CLIENT_KEY")
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

    def test_stopping_an_unscoped_capture_is_the_owners(self, registry):
        import asyncio
        from types import SimpleNamespace

        from server.api.proxy import stop_capture
        from server.models import CaptureStopRequest

        stopped = []
        manager = SimpleNamespace(simulator_udid=lambda sid: None,
                                  stop=lambda sid: stopped.append(sid))
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            capture_sessions=manager, flow_store=None)))
        tokens = self._as("client:alice")
        try:
            with pytest.raises(device_access.OwnerOnly):
                asyncio.run(stop_capture.__wrapped__(request, CaptureStopRequest(session_id="c1"))
                            if hasattr(stop_capture, "__wrapped__") else
                            stop_capture(request, CaptureStopRequest(session_id="c1")))
        finally:
            self._reset(tokens)
        assert stopped == [], "refused before anything was stopped"


class FakeMockAdapter:
    is_running = True

    def __init__(self):
        self._mock_rules = []

    async def set_mock(self, pattern, response, simulator_udid=None):
        rid = f"r{len(self._mock_rules) + 1}"
        self._mock_rules.append({"rule_id": rid, "pattern": pattern, "response": response,
                                 "simulator_udid": simulator_udid})
        return rid

    async def update_mock(self, rule_id, pattern=None, response=None, simulator_udid=None,
                          scope_given=False):
        rule = next(r for r in self._mock_rules if r["rule_id"] == rule_id)
        if scope_given:
            rule["simulator_udid"] = simulator_udid
        return rule


SIM = "32593887-01F3-47B8-AF34-B2D444DB37FA"


class TestReviewFindings:
    """Each a way past the first version, found by review (#254 phase 2a)."""

    def test_a_decoy_udid_does_not_make_an_unscoped_mock_a_clients(self, world, registry):
        c, _, alice, _ = world
        _hold(registry, "alice:s1", "client:alice", SIM)
        r = c.post("/api/v1/proxy/mocks", headers=_h(alice),
                   json={"pattern": "~d x.test", "udid": SIM})
        assert r.status_code == 403 and "simulator_udid" in r.json()["detail"]
        ok = c.post("/api/v1/proxy/mocks", headers=_h(alice),
                    json={"pattern": "~d x.test", "simulator_udid": SIM})
        assert ok.status_code == 200, ok.text

    def test_a_decoy_udid_does_not_make_a_capture_of_all_traffic_a_clients(self, world, registry):
        c, _, alice, _ = world
        _hold(registry, "alice:s1", "client:alice", SIM)
        r = c.post("/api/v1/proxy/capture/start", headers=_h(alice), json={"udid": SIM})
        assert r.status_code == 403
        r = c.post("/api/v1/proxy/capture/start", headers=_h(alice),
                   json={"simulator_udid": SIM, "client_ip": "192.168.1.9"})
        assert r.status_code == 403 and "client_ip" in r.json()["detail"]
        ok = c.post("/api/v1/proxy/capture/start", headers=_h(alice), json={"simulator_udid": SIM})
        assert ok.status_code == 200, ok.text

    def test_a_client_cannot_unscope_its_own_mock_rule(self, registry, keys):
        from types import SimpleNamespace

        from server.api.proxy_intercept import router as intercept_router

        alice = keys.add("alice")
        app = FastAPI()
        app.state.sessions, app.state.clients = registry, keys
        app.state.proxy_adapter = FakeMockAdapter()
        app.state.device_controller = SimpleNamespace(_active_udid=None)
        app.include_router(intercept_router)
        app.add_middleware(AccessMiddleware)
        app.add_middleware(ScopeMiddleware)
        app.add_middleware(APIKeyMiddleware, api_key=KEY, clients=keys)
        app.add_middleware(SessionMiddleware)
        _hold(registry, "alice:s1", "client:alice", SIM)
        with TestClient(app) as c:
            rid = c.post("/api/v1/proxy/mocks", headers=_h(alice), json={
                "pattern": "~d x.test", "status_code": 500,
                "simulator_udid": SIM}).json()["rule_id"]
            # null names no device, so the access middleware never sees it.
            r = c.patch(f"/api/v1/proxy/mocks/{rid}", headers=_h(alice),
                        json={"simulator_udid": None})
            assert r.status_code == 403, r.text
            assert app.state.proxy_adapter._mock_rules[0]["simulator_udid"] == SIM
            registry.release("alice:s1")     # the owner, too, waits for the holder
            owner = c.patch(f"/api/v1/proxy/mocks/{rid}", headers=_h(KEY),
                            json={"simulator_udid": None})
            assert owner.status_code == 200, owner.text

    @pytest.mark.parametrize("path, body", [
        ("/api/v1/recordings", {"udid": SIM, "output_dir": "/tmp/anywhere"}),
        ("/api/v1/device/logging/start", {"udid": SIM, "preset": "quiet"}),
    ])
    def test_owner_fields_are_refused_to_a_client(self, world, registry, path, body):
        c, _, alice, _ = world
        _hold(registry, "alice:s1", "client:alice", SIM)
        r = c.post(path, headers=_h(alice), json=body)
        assert r.status_code == 403 and "owner-only" in r.json()["detail"], r.text
        plain = {k: v for k, v in body.items() if k in ("udid", "video")}
        assert c.post(path, headers=_h(alice), json=plain).status_code == 200
        # The owner may -- on a device alice does not hold.
        theirs = {**body, "udid": "SIM-OWNERS"}
        assert c.post(path, headers=_h(KEY), json=theirs).status_code == 200

    def test_a_client_may_ask_for_its_own_recordings_viewer_link(self, world, registry):
        """No longer owner-only (#559): the route decides, by whose recording
        it is and whether the owner has turned client links off."""
        c, _, alice, _ = world
        _hold(registry, "alice:s1", "client:alice", SIM)
        r = c.post("/api/v1/recordings", headers=_h(alice),
                   json={"udid": SIM, "video": True, "viewer": True})
        assert r.status_code == 200, r.text

    def test_resolved_precheck_guards_a_route_that_never_calls_check(self, world, registry):
        c, app, alice, _ = world
        app.state.device_controller.slots["alice:s1"] = "SIM-FREE"
        r = c.post("/api/v1/device/ui/press", headers=_h(alice), json={})
        assert r.status_code == 403 and "not reserved" in r.json()["detail"]
        _hold(registry, "alice:s1", "client:alice", "SIM-FREE")
        assert c.post("/api/v1/device/ui/press", headers=_h(alice), json={}).status_code == 200

    def test_a_revoked_key_sees_its_stream_end(self, world):
        c, _, alice, _ = world
        r = c.get("/api/v1/logs/stream", headers=_h(alice))
        assert r.json() == {"disconnected": True}

    def test_an_owner_session_in_a_clients_namespace_is_not_that_clients(self, world, registry):
        """The owner's CI named a session `alice:nightly`: client alice sending
        `nightly` qualifies to the same id, but is not that session."""
        c, _, alice, _ = world
        _hold(registry, "alice:nightly", OWNER, SIM)
        r = c.post("/api/v1/device/ui/tap", headers=_h(alice, "nightly"), json={"udid": SIM})
        assert r.status_code == 423
        r = c.delete("/api/v1/sessions/nightly/reservations", headers=_h(alice, "nightly"))
        assert r.status_code == 403
        r = c.delete("/api/v1/sessions/nightly", headers=_h(alice, "nightly"))
        assert r.status_code == 403
        r = c.post("/api/v1/sessions/nightly/reservations", headers=_h(alice, "nightly"),
                   json={"udids": ["SIM-X"]})
        assert r.status_code == 403
        assert registry.holder_of(SIM).session.id == "alice:nightly"

    def test_revoking_closes_the_session_socket(self, registry, keys):
        import asyncio
        from types import SimpleNamespace

        from server.api.sessions import _sockets

        keys.add("alice")
        app = SimpleNamespace(state=SimpleNamespace(sessions=registry, clients=keys))
        _hold(registry, "alice:s1", "client:alice", SIM)
        closed = []

        class WS:
            async def close(self, code=1000, reason=""):
                closed.append(code)

        _sockets(app)["alice:s1"] = (1, WS())
        keys.revoke("alice")
        asyncio.run(release_revoked(app))
        assert closed == [WS_UNAUTHORISED]

    def test_the_sweep_ends_a_revoked_clients_sessions(self, registry, keys):
        import asyncio
        from types import SimpleNamespace

        from server.api.sessions import sweep

        keys.add("alice")
        app = SimpleNamespace(state=SimpleNamespace(sessions=registry, clients=keys))
        _hold(registry, "alice:s1", "client:alice", SIM)
        keys.revoke("alice")

        async def run():
            task = asyncio.create_task(sweep(app, interval=0.01))
            for _ in range(200):
                await asyncio.sleep(0.01)
                if registry.holder_of(SIM) is None:
                    break
            task.cancel()

        asyncio.run(run())
        assert registry.holder_of(SIM) is None


class TestTheRealAppIsWired:
    """The behaviour tests build their own app; this one is `create_app`'s,
    so removing ScopeMiddleware or the client keys from it fails here."""

    def test_a_client_key_is_a_client_in_the_real_app(self, tmp_path):
        import server.main as m
        from server.config import ServerConfig

        app = m.create_app(ServerConfig(api_key=KEY))
        app.state.clients.path = tmp_path / "clients.json"
        app.state.clients.seen_path = tmp_path / "clients-seen.json"
        key = app.state.clients.add("alice")
        c = TestClient(app)
        r = c.post("/api/v1/proxy/stop", headers=_h(key))
        assert r.status_code == 403 and "owner-only" in r.json()["detail"]
        r = c.get("/api/v1/system/channel", headers=_h(key))
        assert r.status_code not in (401, 403), r.text
        assert c.get("/api/v1/system/channel", headers=_h(key, None)).status_code == 400

    def test_the_cli_binds_loopback_by_default(self):
        import argparse

        from server.main import _add_server_flags

        parser = argparse.ArgumentParser()
        _add_server_flags(parser)
        assert parser.parse_args([]).host == "127.0.0.1"


def test_the_video_stream_ends_when_its_caller_is_gone():
    """It never asked, so a revoked key -- which reads as disconnected -- kept
    receiving frames for as long as the connection stayed open (review)."""
    import asyncio
    from types import SimpleNamespace

    from server.api.device import video_stream

    shots = []

    class Controller:
        async def screenshot(self, **kw):
            shots.append(kw)
            return b"jpeg", None

    answers = iter([False, False, True])

    class FakeRequest:
        app = SimpleNamespace(state=SimpleNamespace(device_controller=Controller()))

        async def is_disconnected(self):
            return next(answers)

    async def run():
        response = await video_stream(FakeRequest(), udid="SIM", fps=10, scale=0.5, quality=70)
        return [chunk async for chunk in response.body_iterator]

    frames = asyncio.run(asyncio.wait_for(run(), timeout=5))
    assert len(frames) == 2 and len(shots) == 2


def test_session_runs_name_fits_a_client_keys_longest_label(monkeypatch):
    """A 43-character caller id -- `claude-<uuid>` -- plus the run suffix, plus
    a 16-character label and its colon, must stay within the 64 the server
    accepts, or `quern session run` on a client key fails before it opens."""
    import re

    from server.session import parse_session

    base = "claude-" + "x" * 36
    name = f"{base[:33]}.run-abcd"
    src = open("server/session_cli.py").read()
    assert 'f"{base[:33]}.run-{secrets.token_hex(2)}"' in src
    qualified = principal.qualify(name, "client:" + "a" * 16)
    assert len(qualified) <= 64 and parse_session(qualified.encode()) == qualified
    assert re.fullmatch(r"a{16}:claude-x+\.run-abcd", qualified)


def test_a_client_told_to_reserve_a_free_device_wda_needs(registry):
    """For a free device, "wait for its holder to release it" is advice that
    never comes true: nobody holds it (CodeRabbit)."""
    from server.device.ios.wda_client import _refuse_if_held
    from server.models import DeviceError
    from server.principal import reset_principal, set_principal
    from server.session import _SESSION

    p, s = set_principal("client:alice"), _SESSION.set("alice:s1")
    try:
        with pytest.raises(DeviceError, match="reserve it"):
            _refuse_if_held("SIM-FREE", "start")
    finally:
        reset_principal(p)
        _SESSION.reset(s)


@pytest.mark.parametrize("host, shown", [
    ("127.0.0.1", "127.0.0.1"), ("0.0.0.0", "192.168.1.5"), ("10.0.0.7", "10.0.0.7")])
def test_status_shows_the_address_the_api_answers_on(capsys, host, shown):
    from server.lifecycle import daemon

    daemon._print_status({"pid": 1, "server_host": host, "local_ip": "192.168.1.5",
                          "server_port": 9110, "proxy_enabled": False})
    assert f"http://{shown}:9110" in capsys.readouterr().out
