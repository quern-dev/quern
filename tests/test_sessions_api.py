"""Held sessions and reservations through HTTP and the WebSocket (#254, phase 1).

A small app with the real middlewares, the real sessions router and registry,
and a fake controller whose `resolve_udid` calls the real `check()` -- so the
enforcement path is the production one, without devices.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.websockets import WebSocketDisconnect

from server import device_access
from server.api.sessions import router as sessions_router
from server.auth import APIKeyMiddleware
from server.device_access import AccessMiddleware
from server.models import DeviceError
from server.session import SessionMiddleware
from server.session_registry import SessionRegistry

KEY = "k" * 32


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeController:
    """resolve_udid like the real one: implicit device, then the real check."""

    def __init__(self):
        self.active = "SIM-1"
        self.shutdowns = []
        self._pool = None

    @property
    def _active_udid(self):
        return self.active

    @_active_udid.setter
    def _active_udid(self, value):
        self.active = value

    async def resolve_udid(self, udid=None):
        resolved = udid or self.active
        device_access.check(resolved)
        return resolved

    async def shutdown(self, udid):
        self.shutdowns.append(udid)


class TapBody(BaseModel):
    udid: str | None = None
    note: str = ""


def _app(registry):
    app = FastAPI()
    app.state.sessions = registry
    app.state.device_controller = FakeController()
    app.include_router(sessions_router)

    @app.post("/api/v1/device/ui/tap")
    async def tap(request: Request, body: TapBody):
        udid = await app.state.device_controller.resolve_udid(body.udid)
        return {"tapped": udid, "note": body.note}

    @app.post("/api/v1/device/ui/swipe")
    async def swipe_hiding_errors(request: Request):
        """A route that maps every DeviceError to 500, as some real ones do:
        the refusal must still come out as 423."""
        try:
            await app.state.device_controller.resolve_udid()
        except DeviceError as e:
            raise HTTPException(500, str(e)) from None
        return {"swiped": True}

    @app.post("/api/v1/device/raw-op")
    async def raw_op(body: TapBody):
        """A route that never calls resolve_udid: only the middleware guards it."""
        return {"did": body.udid}

    @app.get("/api/v1/device/screenshot")
    async def screenshot(udid: str | None = None):
        return {"shot": await app.state.device_controller.resolve_udid(udid)}

    app.add_middleware(AccessMiddleware)
    app.add_middleware(APIKeyMiddleware, api_key=KEY)
    app.add_middleware(SessionMiddleware)
    return app


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def registry(clock):
    reg = SessionRegistry(grace_seconds=180, clock=clock)
    device_access.install(reg)
    yield reg
    device_access.install(None)


@pytest.fixture
def client(registry):
    with TestClient(_app(registry)) as c:
        yield c


def _h(session=None):
    h = {"Authorization": f"Bearer {KEY}"}
    if session:
        h["X-Quern-Session"] = session
    return h


def _open(client, name):
    r = client.post("/api/v1/sessions", headers=_h(name), json={})
    assert r.status_code == 200, r.text
    return r.json()


class Held:
    """A session with its live socket open, as the MCP holds one."""

    def __init__(self, client, name):
        self.name = name
        self.opened = _open(client, name)
        self.ctx = client.websocket_connect(
            self.opened["live_path"],
            headers={**_h(name), "X-Quern-Resume-Token": self.opened["resume_token"]})
        self.ws = self.ctx.__enter__()
        assert self.ws.receive_json()["event"] == "attached"

    def close(self, code=1000):
        self.ws.close(code=code)
        try:
            self.ctx.__exit__(None, None, None)
        except Exception:  # noqa: BLE001 -- the server already closed
            pass


def _reserve(client, name, **body):
    return client.post(f"/api/v1/sessions/{name}/reservations", headers=_h(name), json=body)


class TestEnforcement:
    def test_another_sessions_change_is_refused_with_the_holder_named(self, client):
        a = Held(client, "claude-a")
        assert _reserve(client, "claude-a", udids=["SIM-1"]).status_code == 200
        r = client.post("/api/v1/device/ui/tap", headers=_h("claude-b"), json={"udid": "SIM-1"})
        assert r.status_code == 423
        detail = r.json()["detail"]
        assert detail["held_by"] == "claude-a" and detail["state"] == "live"
        assert detail["yours"] is False
        a.close()

    def test_the_holder_and_readers_are_not_refused(self, client):
        a = Held(client, "claude-a")
        _reserve(client, "claude-a", udids=["SIM-1"])
        mine = client.post("/api/v1/device/ui/tap", headers=_h("claude-a"),
                           json={"udid": "SIM-1", "note": "kept"})
        assert mine.status_code == 200 and mine.json() == {"tapped": "SIM-1", "note": "kept"}, \
            "the body reached the handler intact after the middleware read it"
        read = client.get("/api/v1/device/screenshot?udid=SIM-1", headers=_h("claude-b"))
        assert read.status_code == 200, "reads stay open (decision 1)"
        a.close()

    def test_a_caller_with_no_session_is_refused_too(self, client):
        a = Held(client, "claude-a")
        _reserve(client, "claude-a", udids=["SIM-1"])
        r = client.post("/api/v1/device/ui/tap", headers=_h(), json={"udid": "SIM-1"})
        assert r.status_code == 423, "decision 4: old MCPs must not tap a held device"
        a.close()

    def test_the_device_a_call_does_not_name_is_checked_too(self, client):
        """resolve_udid's implicit device (here the active one), through a
        route that turns every DeviceError into a 500: the refusal wins."""
        a = Held(client, "claude-a")
        _reserve(client, "claude-a", udids=["SIM-1"])
        r = client.post("/api/v1/device/ui/swipe", headers=_h("claude-b"))
        assert r.status_code == 423 and r.json()["detail"]["held_by"] == "claude-a"
        a.close()

    def test_a_named_device_in_the_query_string_or_a_list_is_checked(self, client):
        a = Held(client, "claude-a")
        _reserve(client, "claude-a", udids=["SIM-1"])
        q = client.post("/api/v1/device/ui/tap?udid=SIM-1", headers=_h("claude-b"), json={})
        assert q.status_code == 423
        a.close()

    def test_a_route_that_never_resolves_is_guarded_by_the_middleware(self, client):
        a = Held(client, "claude-a")
        _reserve(client, "claude-a", udids=["SIM-1"])
        r = client.post("/api/v1/device/raw-op", headers=_h("claude-b"), json={"udid": "SIM-1"})
        assert r.status_code == 423
        a.close()

    def test_a_free_device_is_open_to_everyone(self, client):
        r = client.post("/api/v1/device/ui/tap", headers=_h(), json={"udid": "SIM-9"})
        assert r.status_code == 200


class TestLifecycle:
    def test_reserving_needs_the_live_socket(self, client):
        _open(client, "claude-a")
        r = _reserve(client, "claude-a", udids=["SIM-1"])
        assert r.status_code == 409 and "live socket" in r.json()["detail"]

    def test_only_the_session_itself_reserves(self, client):
        a = Held(client, "claude-a")
        r = client.post("/api/v1/sessions/claude-a/reservations", headers=_h("claude-b"),
                        json={"udids": ["SIM-1"]})
        assert r.status_code == 403
        a.close()

    def test_one_device_becomes_the_sessions_active_device(self, client):
        a = Held(client, "claude-a")
        _reserve(client, "claude-a", udids=["SIM-7"])
        assert client.app.state.device_controller.active == "SIM-7"
        a.close()

    def test_a_clean_close_frees_the_device(self, client, registry):
        a = Held(client, "claude-a")
        _reserve(client, "claude-a", udids=["SIM-1"])
        a.close(code=1000)
        assert registry.holder_of("SIM-1") is None

    def test_an_abrupt_drop_holds_through_grace_then_frees(self, client, registry, clock):
        a = Held(client, "claude-a")
        _reserve(client, "claude-a", udids=["SIM-1"])
        a.close(code=1001)
        held = registry.holder_of("SIM-1")
        assert held is not None and held.session.state == "grace"
        r = client.post("/api/v1/device/ui/tap", headers=_h("claude-b"), json={"udid": "SIM-1"})
        assert r.status_code == 423 and r.json()["detail"]["state"] == "grace"
        clock.t += 181
        assert client.post("/api/v1/device/ui/tap", headers=_h("claude-b"),
                           json={"udid": "SIM-1"}).status_code == 200

    def test_a_restarted_mcp_takes_its_session_back_by_name(self, client, registry):
        a = Held(client, "claude-a")
        _reserve(client, "claude-a", udids=["SIM-1"])
        a.close(code=1001)
        again = Held(client, "claude-a")              # same name, new process
        assert registry.holder_of("SIM-1").session.state == "live"
        assert again.opened["reservations"] == ["SIM-1"]
        again.close()

    def test_an_unknown_session_or_a_bad_token_closes_the_socket_with_a_code(self, client):
        with pytest.raises(WebSocketDisconnect) as gone:
            with client.websocket_connect("/api/v1/sessions/nope/live",
                                          headers={**_h("x"), "X-Quern-Resume-Token": "t"}) as ws:
                ws.receive_json()
        assert gone.value.code == 4404
        _open(client, "claude-a")
        with pytest.raises(WebSocketDisconnect) as bad:
            with client.websocket_connect("/api/v1/sessions/claude-a/live",
                                          headers={**_h("claude-a"),
                                                   "X-Quern-Resume-Token": "wrong"}) as ws:
                ws.receive_json()
        assert bad.value.code == 4403

    def test_the_socket_needs_the_api_key(self, client):
        opened = _open(client, "claude-a")
        with pytest.raises(WebSocketDisconnect) as refused:
            with client.websocket_connect(opened["live_path"],
                                          headers={"X-Quern-Resume-Token":
                                                   opened["resume_token"]}) as ws:
                ws.receive_json()
        assert refused.value.code == 4401

    def test_the_owner_can_release_a_stuck_session(self, client, registry):
        a = Held(client, "claude-a")
        _reserve(client, "claude-a", udids=["SIM-1"])
        r = client.delete("/api/v1/sessions/claude-a", headers=_h("someone-else"))
        assert r.status_code == 200 and r.json()["freed"] == ["SIM-1"]
        assert registry.holder_of("SIM-1") is None
        a.close()

    def test_listing_shows_state_and_grace(self, client, clock):
        a = Held(client, "claude-a")
        _reserve(client, "claude-a", udids=["SIM-1"])
        a.close(code=1001)
        clock.t += 30
        sessions = client.get("/api/v1/sessions", headers=_h()).json()["sessions"]
        assert sessions[0]["id"] == "claude-a" and sessions[0]["state"] == "grace"
        assert sessions[0]["grace_remaining_s"] == 150.0
        assert sessions[0]["reservations"][0]["udid"] == "SIM-1"


class TestReleasePolicies:
    def test_shutdown_booted_shuts_down_only_what_the_session_booted(self, client, registry):
        a = Held(client, "claude-a")
        registry.reserve("claude-a", ["SIM-1", "SIM-2"], on_release="shutdown_booted",
                         booted=["SIM-2"])
        a.close(code=1000)
        assert client.app.state.device_controller.shutdowns == ["SIM-2"]

    def test_a_none_policy_leaves_everything_running(self, client, registry):
        a = Held(client, "claude-a")
        registry.reserve("claude-a", ["SIM-1"], booted=["SIM-1"])
        a.close(code=1000)
        assert client.app.state.device_controller.shutdowns == []


class TestTheGuardCoversEveryRoute:
    def test_every_device_naming_field_is_one_the_middleware_checks(self):
        """A route that names a device under a key AccessMiddleware does not
        read would let a held device be changed through it. Every query
        parameter and JSON body field across the real app is checked."""
        import server.main as m
        from server.config import ServerConfig

        spec = m.create_app(ServerConfig(api_key=KEY)).openapi()
        schemas = spec.get("components", {}).get("schemas", {})

        def body_fields(op):
            ref = (op.get("requestBody", {}).get("content", {})
                   .get("application/json", {}).get("schema", {}).get("$ref", ""))
            return list(schemas.get(ref.rsplit("/", 1)[-1], {}).get("properties", {}))

        suspicious, seen = [], 0
        for path, ops in spec["paths"].items():
            for method, op in ops.items():
                names = [p["name"] for p in op.get("parameters", []) if p.get("in") == "query"]
                names += body_fields(op)
                for name in names:
                    low = name.lower()
                    if "udid" in low or "serial" in low or low in ("device", "device_id"):
                        seen += 1
                        if name not in device_access.DEVICE_KEYS:
                            suspicious.append(f"{method.upper()} {path}: {name}")
        # 85 when written: a walk that found none would pass on nothing.
        assert seen >= 50, f"only {seen} device fields found -- is the walk still working?"
        assert suspicious == [], f"device fields the middleware does not check: {suspicious}"

    def test_the_read_only_posts_are_real_routes(self):
        import server.main as m
        from server.config import ServerConfig

        paths = m.create_app(ServerConfig(api_key=KEY)).openapi()["paths"]
        missing = [p for p in device_access.READ_POSTS if "post" not in paths.get(p, {})]
        assert missing == [], f"READ_POSTS names routes that do not exist: {missing}"


class TestClassification:
    @pytest.mark.parametrize("method,path,write", [
        ("GET", "/api/v1/device/ui", False),
        ("HEAD", "/api/v1/device/ui", False),
        ("POST", "/api/v1/device/ui/tap", True),
        ("DELETE", "/api/v1/device/app", True),
        ("PUT", "/api/v1/whatever", True),
        ("post", "/api/v1/device/ui/wait-for-element", False),
        ("POST", "/api/v1/device/preview/start", False),
        ("POST", "/api/v1/recordings", True),    # may install a CA
    ])
    def test_reads_and_changes(self, method, path, write):
        assert device_access.is_write(method, path) is write


class TestThePoolAndTheListing:
    def test_the_pool_never_picks_a_device_another_session_holds(self, registry):
        from types import SimpleNamespace

        from server.device.pool import DevicePool
        from server.session import _SESSION

        a, token = registry.open("claude-a")
        registry.attach(a.id, token)
        registry.reserve("claude-a", ["SIM-1"])
        pool = DevicePool.__new__(DevicePool)
        pool._match_criteria = lambda d, **k: True
        pool._filter_by_name = lambda devices, name: devices
        devices = [SimpleNamespace(udid="SIM-1"), SimpleNamespace(udid="SIM-2")]
        token_b = _SESSION.set("claude-b")
        try:
            assert [d.udid for d in pool._find_candidates(devices)] == ["SIM-2"]
        finally:
            _SESSION.reset(token_b)
        token_a = _SESSION.set("claude-a")
        try:
            assert [d.udid for d in pool._find_candidates(devices)] == ["SIM-1", "SIM-2"], \
                "the holder's own device is a candidate for it"
        finally:
            _SESSION.reset(token_a)

    def test_list_devices_says_who_holds_each(self, registry):
        from types import SimpleNamespace

        from server.api.device import _with_reservations
        from server.session import _SESSION

        a, token = registry.open("claude-a")
        registry.attach(a.id, token)
        registry.reserve("claude-a", ["SIM-1"])
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(sessions=registry)))
        token_b = _SESSION.set("claude-b")
        try:
            out = _with_reservations(request, [{"udid": "SIM-1"}, {"udid": "SIM-2"}])
        finally:
            _SESSION.reset(token_b)
        assert out[0]["reservation"]["held_by"] == "claude-a"
        assert out[0]["reservation"]["yours"] is False
        assert out[1]["reservation"] is None
