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


_OPEN: list = []


@pytest.fixture
def client(registry):
    with TestClient(_app(registry)) as c:
        try:
            yield c
        finally:
            # Close every socket a test held, pass or fail: one left open when
            # an assertion fired made the client's teardown wait for ever, so a
            # failing test hung the run instead of failing it.
            while _OPEN:
                _OPEN.pop().close()


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
        self.closed = False
        _OPEN.append(self)
        assert self.ws.receive_json()["event"] == "attached"

    def close(self, code=1000):
        if self.closed:
            return
        self.closed = True
        try:
            self.ws.close(code=code)
        except Exception:  # noqa: BLE001 -- already closed
            pass
        try:
            self.ctx.__exit__(None, None, None)
        except Exception:  # noqa: BLE001 -- the server already closed
            pass


def _closed_with(ws, timeout=3.0) -> int:
    """The close code the server ends `ws` with, or a test failure after
    `timeout`. A bare receive blocks for ever when the socket wrongly stays
    open, which hung the run instead of failing it (found by mutation)."""
    import threading

    box: dict = {}

    def wait():
        try:
            ws.receive_json()
            box["code"] = None                     # a message, not a close
        except WebSocketDisconnect as e:
            box["code"] = e.code
        except Exception as e:  # noqa: BLE001
            box["code"] = repr(e)

    t = threading.Thread(target=wait, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        pytest.fail(f"the socket was not closed within {timeout}s")
    return box["code"]


def _received_within(ws, timeout=3.0):
    """The next message on `ws`, or a test failure after `timeout`."""
    import threading

    box: dict = {}

    def wait():
        try:
            box["msg"] = ws.receive_json()
        except Exception as e:  # noqa: BLE001
            box["msg"] = repr(e)

    t = threading.Thread(target=wait, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        pytest.fail(f"nothing arrived on the socket within {timeout}s")
    return box["msg"]


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
        # A route that never resolves, so only the middleware's reading of
        # the query string stands between it and the held device.
        q = client.post("/api/v1/device/raw-op?udid=SIM-1", headers=_h("claude-b"), json={})
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
        detail = r.json()["detail"]
        assert r.status_code == 409 and "live socket" in detail
        # Enough for raw REST to act on (#561): where the socket is, why it
        # matters, how long is left, and the two ways quern holds it for you.
        assert "ws://testserver/api/v1/sessions/claude-a/live" in detail
        assert "X-Quern-Resume-Token" in detail
        assert "released in" in detail
        assert "quern MCP" in detail and "quern session hold claude-a" in detail

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
        assert a.opened["resumed"] is False, "a new session"
        _reserve(client, "claude-a", udids=["SIM-1"])
        a.close(code=1001)
        again = Held(client, "claude-a")              # same name, new process
        assert again.opened["resumed"] is True, "the session in grace, taken back"
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
        # And the client is told: its socket is closed with "unknown session",
        # so it reopens instead of believing it still holds the device.
        assert _closed_with(a.ws) == 4404
        a.close()

    def test_a_newer_connection_replaces_the_old_one(self, client, registry):
        a = Held(client, "claude-a")
        _reserve(client, "claude-a", udids=["SIM-1"])
        ctx = client.websocket_connect(
            a.opened["live_path"],
            headers={**_h("claude-a"), "X-Quern-Resume-Token": a.opened["resume_token"]})
        newer = ctx.__enter__()
        try:
            assert newer.receive_json()["event"] == "attached"
            assert _closed_with(a.ws) == 4000
            a.close(code=1000)                   # the old socket's clean close...
            assert registry.holder_of("SIM-1") is not None, "...does not release the session"
        finally:
            newer.close(code=1000)
            ctx.__exit__(None, None, None)
        assert registry.holder_of("SIM-1") is None

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

        def walk(schema, seen):
            """Every property name in a schema, at any depth: through $refs,
            Optional bodies (anyOf), nested objects and arrays. Only top-level
            $ref bodies were followed before (review)."""
            if not isinstance(schema, dict):
                return []
            ref = schema.get("$ref")
            if ref:
                name = ref.rsplit("/", 1)[-1]
                if name in seen:
                    return []
                return walk(schemas.get(name, {}), seen | {name})
            names = []
            for key, sub in (schema.get("properties") or {}).items():
                names.append(key)
                names += walk(sub, seen)
            for combo in ("anyOf", "oneOf", "allOf"):
                for sub in schema.get(combo) or []:
                    names += walk(sub, seen)
            names += walk(schema.get("items"), seen)
            names += walk(schema.get("additionalProperties"), seen)
            return names

        def body_fields(op):
            content = op.get("requestBody", {}).get("content", {})
            return [n for media in content.values() for n in walk(media.get("schema"), set())]

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

    #: Changes addressed by a path id, which no device field reaches. Each says
    #: how it is guarded, or why it changes no device. A new one fails the test
    #: below until someone decides (review: these were the unguarded ones).
    ID_ROUTES = {
        ("PATCH", "/api/v1/proxy/mocks/{rule_id}"): "route checks the rule's simulator_udid",
        ("DELETE", "/api/v1/proxy/mocks/{rule_id}"): "route checks the rule's simulator_udid",
        ("POST", "/api/v1/recordings/{recording_id}/stop"): "route checks the recording's udid",
        ("POST", "/api/v1/recordings/{recording_id}/keyframe"): "route checks the recording's udid",
        ("POST", "/api/v1/recordings/{recording_id}/viewer"):
            "route checks the recording is the caller's (#559); no device",
        ("DELETE", "/api/v1/recordings/{recording_id}/viewer"):
            "route checks the recording is the caller's (#559); no device",
        ("DELETE", "/api/v1/device/app/state/{label}"): "deletes a snapshot on the Mac's disk",
        ("POST", "/api/v1/proxy/replay/{flow_id}"): "replays a request from the Mac; no device",
    }

    def test_every_change_addressed_by_an_id_has_been_decided(self):
        import server.main as m
        from server.config import ServerConfig

        spec = m.create_app(ServerConfig(api_key=KEY)).openapi()
        found = {(method.upper(), path) for path, ops in spec["paths"].items() for method in ops
                 if "{" in path and device_access.is_write(method, path)
                 and not path.startswith(device_access._OWN_RULES)}
        undecided = sorted(found - set(self.ID_ROUTES))
        assert undecided == [], f"id-addressed changes with no reservation decision: {undecided}"
        stale = sorted(set(self.ID_ROUTES) - found)
        assert stale == [], f"decisions for routes that no longer exist: {stale}"

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


class TestReviewFindings:
    def test_reserving_a_held_device_is_409_not_the_middlewares_423(self, client):
        a = Held(client, "claude-a")
        b = Held(client, "claude-b")
        _reserve(client, "claude-a", udids=["SIM-1"])
        r = _reserve(client, "claude-b", udids=["SIM-1"])
        assert r.status_code == 409 and "claude-a" in r.json()["detail"]
        a.close()
        b.close()

    def test_the_owner_can_free_a_single_device(self, client, registry):
        a = Held(client, "claude-a")
        _reserve(client, "claude-a", udids=["SIM-1", "SIM-2"])
        r = client.delete("/api/v1/sessions/claude-a/reservations?udid=SIM-1",
                          headers=_h("someone-else"))
        assert r.status_code == 200 and r.json()["freed"] == ["SIM-1"]
        assert registry.holder_of("SIM-1") is None and registry.holder_of("SIM-2") is not None
        # The holder is told over its socket, so the MCP can tell the agent.
        event = _received_within(a.ws)
        assert event == {"event": "released", "udids": ["SIM-1"], "by": "someone-else"}
        a.close()

    def test_a_lowercase_spelling_of_a_held_simulator_is_refused(self, client):
        udid = "5E6E8B35-4384-447E-84E8-4951BA26B181"     # as the device list spells it
        a = Held(client, "claude-a")
        _reserve(client, "claude-a", udids=[udid])
        r = client.post("/api/v1/device/raw-op", headers=_h("claude-b"),
                        json={"udid": udid.lower()})
        assert r.status_code == 423
        a.close()

    @pytest.mark.parametrize("headers", [
        {"Content-Type": "Application/JSON"},
        {"Content-Type": "text/plain"},
    ])
    def test_the_content_type_header_does_not_decide(self, client, headers):
        """FastAPI parses `Application/JSON` as JSON; deciding by the header
        let that past the check (review). Any body that parses is checked."""
        a = Held(client, "claude-a")
        _reserve(client, "claude-a", udids=["SIM-1"])
        r = client.post("/api/v1/device/raw-op", headers={**_h("claude-b"), **headers},
                        content=b'{"udid": "SIM-1"}')
        assert r.status_code == 423
        a.close()

    def test_two_content_type_headers_do_not_get_past(self, client):
        a = Held(client, "claude-a")
        _reserve(client, "claude-a", udids=["SIM-1"])
        r = client.post("/api/v1/device/raw-op",
                        headers=[("authorization", f"Bearer {KEY}"),
                                 ("x-quern-session", "claude-b"),
                                 ("content-type", "application/json"),
                                 ("content-type", "text/plain")],
                        content=b'{"udid": "SIM-1"}')
        assert r.status_code == 423
        a.close()

    def test_a_large_json_body_is_still_checked(self, client):
        a = Held(client, "claude-a")
        _reserve(client, "claude-a", udids=["SIM-1"])
        r = client.post("/api/v1/device/raw-op", headers=_h("claude-b"),
                        json={"udid": "SIM-1", "note": "x" * 2_000_000})
        assert r.status_code == 423
        a.close()


class FakePool:
    def __init__(self, devices):
        from types import SimpleNamespace

        self.devices = {d: SimpleNamespace(udid=d, state=s, is_available=True)
                        for d, s in devices.items()}
        self.booted = []

    async def refresh(self):
        pass

    def _read_state(self):
        from types import SimpleNamespace

        return SimpleNamespace(devices=self.devices)

    def _find_candidates(self, devices, **criteria):
        return list(devices)

    def _rank_candidate(self, d):
        return d.udid

    async def _boot_and_wait(self, udid):
        from server.models import DeviceState

        self.booted.append(udid)
        self.devices[udid].state = DeviceState.BOOTED


class TestReservingThroughThePool:
    @pytest.fixture
    def pool(self, client):
        from server.models import DeviceState

        pool = FakePool({"SIM-1": DeviceState.BOOTED, "SIM-2": DeviceState.SHUTDOWN,
                         "SIM-3": DeviceState.SHUTDOWN})
        client.app.state.device_controller._pool = pool
        return pool

    def test_a_typo_is_not_reserved(self, client, pool):
        a = Held(client, "claude-a")
        r = _reserve(client, "claude-a", udids=["SIM-TYPO"])
        assert r.status_code == 404 and "SIM-TYPO" in r.json()["detail"]
        a.close()

    def test_count_takes_booted_first_then_boots(self, client, pool):
        a = Held(client, "claude-a")
        r = _reserve(client, "claude-a", count=2)
        assert r.status_code == 200, r.text
        assert [x["udid"] for x in r.json()["reserved"]] == ["SIM-1", "SIM-2"]
        assert pool.booted == ["SIM-2"]
        assert [x["booted_here"] for x in r.json()["reserved"]] == [False, True]
        a.close()

    def test_count_skips_devices_another_session_holds(self, client, pool, registry):
        b = Held(client, "claude-b")
        _reserve(client, "claude-b", udids=["SIM-1"])
        a = Held(client, "claude-a")
        r = _reserve(client, "claude-a", count=1, auto_boot=False)
        assert r.status_code == 409 and "held by other sessions" in r.json()["detail"]
        a.close()
        b.close()

    def test_losing_a_race_boots_nothing_and_shuts_down_nothing(self, client, pool, registry,
                                                                monkeypatch):
        """Two jobs ask for the one free (shut down) simulator. Booting before
        claiming let the loser's undo shut down the winner's device (review).
        Now the claim comes first: the loser is refused before any boot."""
        a = Held(client, "claude-a")
        b = Held(client, "claude-b")
        original = registry.reserve

        def winner_first(session_id, udids, **kw):
            if session_id == "claude-a":            # B claims it a moment earlier
                original("claude-b", ["SIM-2"])
            return original(session_id, udids, **kw)

        monkeypatch.setattr(registry, "reserve", winner_first)
        r = _reserve(client, "claude-a", count=2)    # SIM-1 booted + SIM-2 to boot
        assert r.status_code == 409 and "claude-b" in r.json()["detail"]
        assert pool.booted == [], "the loser booted nothing"
        assert client.app.state.device_controller.shutdowns == [], "and shut down nothing"
        assert registry.holder_of("SIM-2").session.id == "claude-b"
        a.close()
        b.close()

    def test_a_failed_boot_withdraws_the_whole_reservation(self, client, pool, registry):
        a = Held(client, "claude-a")
        calls = []

        async def flaky(udid):
            calls.append(udid)
            if len(calls) == 2:
                raise RuntimeError("simctl boot failed")
            from server.models import DeviceState
            pool.devices[udid].state = DeviceState.BOOTED

        pool._boot_and_wait = flaky
        r = _reserve(client, "claude-a", count=3)    # SIM-1 booted; boot SIM-2, SIM-3
        assert r.status_code == 502 and "nothing was reserved" in r.json()["detail"]
        assert all(registry.holder_of(u) is None for u in ("SIM-1", "SIM-2", "SIM-3"))
        assert client.app.state.device_controller.shutdowns == ["SIM-2"], \
            "what it did boot is shut down again"
        a.close()

    def test_a_failed_boot_keeps_what_the_session_held_before(self, client, pool, registry):
        """It withdrew SIM-1 too, held from an earlier request, while saying
        "nothing was reserved" -- and drained other sessions' queued release
        policies (CodeRabbit)."""
        a = Held(client, "claude-a")
        assert _reserve(client, "claude-a", udids=["SIM-1"]).status_code == 200
        other, token = registry.open("claude-z")
        registry.attach(other.id, token)
        registry.reserve("claude-z", ["SIM-9"], on_release="shutdown_booted", booted=["SIM-9"])
        registry.unreserve("claude-z", ["SIM-9"])        # queued for its policy

        async def failing(udid):
            raise RuntimeError("simctl boot failed")

        pool._boot_and_wait = failing
        r = _reserve(client, "claude-a", count=1)          # would boot SIM-2
        assert r.status_code == 502
        assert registry.holder_of("SIM-1").session.id == "claude-a", "kept"
        assert registry.holder_of("SIM-2") is None
        queued = registry.take_released()
        assert [list(s.reservations) for s in queued] == [["SIM-9"]], \
            "the other session's policy is still queued"
        a.close()

    @pytest.fixture
    def group(self, monkeypatch):
        monkeypatch.setattr("server.config.read_user_config",
                            lambda **k: {"device_groups": {"ios": ["SIM-1", "SIM-2"]}})

    def test_a_group_with_no_count_reserves_the_members_not_yet_held(self, client, pool,
                                                                    group):
        """Holding SIM-1 and asking for the group was a 409 "asked for 2, 1
        free" -- a shortage that did not exist (CodeRabbit)."""
        a = Held(client, "claude-a")
        _reserve(client, "claude-a", udids=["SIM-1"])
        r = _reserve(client, "claude-a", group="ios")
        assert r.status_code == 200, r.text
        assert [x["udid"] for x in r.json()["reserved"]] == ["SIM-2"]
        again = _reserve(client, "claude-a", group="ios")     # a repeat, after a reconnect
        assert again.status_code == 200 and again.json()["reserved"] == []
        assert sorted(again.json()["holding"]) == ["SIM-1", "SIM-2"]
        a.close()

    def test_count_means_more_devices(self, client, pool):
        a = Held(client, "claude-a")
        _reserve(client, "claude-a", udids=["SIM-1"])
        r = _reserve(client, "claude-a", count=1)
        assert r.status_code == 200
        assert [x["udid"] for x in r.json()["reserved"]] == ["SIM-2"], "not SIM-1 again"
        assert sorted(r.json()["holding"]) == ["SIM-1", "SIM-2"]
        a.close()

    def test_release_policy_skips_a_device_reserved_again_meanwhile(self, client, registry):
        from server.api.sessions import run_release_policies
        a = Held(client, "claude-a")
        registry.reserve("claude-a", ["SIM-2"], on_release="shutdown_booted", booted=["SIM-2"])
        registry.unreserve("claude-a", ["SIM-2"])          # queued for its policy
        b = Held(client, "claude-b")
        registry.reserve("claude-b", ["SIM-2"])            # ...and taken again first
        import asyncio
        asyncio.run(run_release_policies(client.app))
        assert client.app.state.device_controller.shutdowns == []
        a.close()
        b.close()


class TestRoutesAddressedById:
    def test_stopping_a_recording_of_a_held_device_is_refused(self, client, registry):
        from types import SimpleNamespace

        from server.api import recordings as rec_api

        class Manager:
            def get(self, rid):
                return SimpleNamespace(udid="SIM-1")

            async def stop(self, rid):
                raise AssertionError("must not be reached")

        app = client.app
        app.state.recordings = Manager()
        app.include_router(rec_api.router)
        a = Held(client, "claude-a")
        _reserve(client, "claude-a", udids=["SIM-1"])
        r = client.post("/api/v1/recordings/rec_1/stop", headers=_h("claude-b"))
        assert r.status_code == 423 and r.json()["detail"]["held_by"] == "claude-a"
        a.close()

    def test_a_mock_scoped_to_a_held_device_cannot_be_deleted_by_others(self, registry):
        from types import SimpleNamespace

        from server.api.proxy_intercept import _check_mock_scope
        from server.session import _SESSION

        a, token = registry.open("claude-a")
        registry.attach(a.id, token)
        registry.reserve("claude-a", ["SIM-1"])
        adapter = SimpleNamespace(_mock_rules=[
            {"rule_id": "r1", "simulator_udid": "SIM-1"},
            {"rule_id": "r2", "simulator_udid": None},
        ])
        write = device_access._WRITE.set(True)
        who = _SESSION.set("claude-b")
        try:
            _check_mock_scope(adapter, "r2")                  # unscoped: fine
            with pytest.raises(device_access.DeviceHeld):
                _check_mock_scope(adapter, "r1")
            with pytest.raises(device_access.DeviceHeld):
                _check_mock_scope(adapter)                    # clear-all includes r1
        finally:
            _SESSION.reset(who)
            device_access._WRITE.reset(write)

    def test_cert_install_skips_a_held_device_in_its_batch(self):
        import inspect

        from server.api import proxy_certs

        source = inspect.getsource(proxy_certs)
        assert "device_access.why_not(d.udid)" in source
        assert "device_access.check(target_udid)" in source

    def test_wda_is_not_restarted_behind_a_read_of_a_held_device(self, registry):
        from server.device.ios.wda_client import _refuse_if_held
        from server.models import DeviceError
        from server.session import _SESSION

        a, token = registry.open("claude-a")
        registry.attach(a.id, token)
        registry.reserve("claude-a", ["PHONE-1"])
        who = _SESSION.set("claude-b")
        try:
            with pytest.raises(DeviceError, match="only its holder"):
                _refuse_if_held("PHONE-1", "restart")
            _refuse_if_held("PHONE-2", "restart")             # free: fine
        finally:
            _SESSION.reset(who)
        holder = _SESSION.set("claude-a")
        try:
            _refuse_if_held("PHONE-1", "restart")             # the holder may
        finally:
            _SESSION.reset(holder)


class FakeWS:
    """Enough of a WebSocket to drive `live()` with controlled interleaving."""

    def __init__(self, app, token, session):
        import asyncio

        self.app = app
        self.headers = {"x-quern-resume-token": token}
        self.closed_with = None
        self.close_gate = None              # an Event the close waits on, if set
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.sent = []

    async def accept(self):
        pass

    async def close(self, code=1000, reason=""):
        if self.close_gate is not None:
            await self.close_gate.wait()
        if self.closed_with is None:
            self.closed_with = code
            await self.inbox.put({"type": "websocket.disconnect", "code": code})

    async def send_json(self, data):
        self.sent.append(data)

    async def receive(self):
        return await self.inbox.get()


class TestSocketBookkeepingRaces:
    async def test_two_reconnects_at_once_leave_the_newest_holding(self):
        """B awaits A's close while C attaches: storing after the await let B
        overwrite C, so a later release told B and left C, the real holder,
        never told (review, reproduced with fakes)."""
        import asyncio
        from types import SimpleNamespace

        from server.api.sessions import _sockets, live

        reg = SessionRegistry()
        app = SimpleNamespace(state=SimpleNamespace(sessions=reg))
        _, token = reg.open("s")
        a = FakeWS(app, token, "s")
        task_a = asyncio.create_task(live(a, "s"))
        await asyncio.sleep(0.01)
        gate = asyncio.Event()
        a.close_gate = gate                       # B's close of A will block here
        b, c = FakeWS(app, token, "s"), FakeWS(app, token, "s")
        task_b = asyncio.create_task(live(b, "s"))
        await asyncio.sleep(0.01)                 # B attached, waiting on A's close
        task_c = asyncio.create_task(live(c, "s"))
        await asyncio.sleep(0.01)                 # C attached and stored
        gate.set()
        await asyncio.sleep(0.05)
        assert _sockets(app)["s"][1] is c, "the newest connection holds the session"
        assert b.closed_with == 4000, "B was told it was replaced"
        assert c.closed_with is None and c.sent and c.sent[0]["event"] == "attached"
        await c.inbox.put({"type": "websocket.disconnect", "code": 1000})
        for t in (task_a, task_b, task_c):
            try:
                await asyncio.wait_for(t, 2)
            except TimeoutError:
                pytest.fail("a connection handler did not finish")
        assert reg.get("s") is None, "C's clean close released the session"

    async def test_a_release_during_attach_is_not_told_attached(self):
        import asyncio
        from types import SimpleNamespace

        from server.api.sessions import close_socket, live

        reg = SessionRegistry()
        app = SimpleNamespace(state=SimpleNamespace(sessions=reg))
        _, token = reg.open("s")
        a = FakeWS(app, token, "s")
        task_a = asyncio.create_task(live(a, "s"))
        await asyncio.sleep(0.01)
        gate = asyncio.Event()
        a.close_gate = gate
        b = FakeWS(app, token, "s")
        task_b = asyncio.create_task(live(b, "s"))
        await asyncio.sleep(0.01)                 # B attached, waiting on A's close
        reg.release("s")                          # ...and the owner releases it now
        try:
            # Bounded: if B had not taken its slot before awaiting, this would
            # wait on A's gated close and deadlock instead of failing.
            await asyncio.wait_for(close_socket(app, "s", 4404, "session released"), 1)
        except TimeoutError:
            pass
        gate.set()
        await asyncio.wait_for(task_b, 2)
        assert b.closed_with == 4404
        assert not any(m.get("event") == "attached" for m in b.sent)
        await asyncio.wait_for(task_a, 2)


class TestSecondReviewRoutes:
    def _hold(self, registry, udid="SIM-1"):
        a, token = registry.open("claude-a")
        registry.attach(a.id, token)
        registry.reserve("claude-a", [udid])

    async def test_stopping_all_previews_refuses_if_one_is_a_held_device(self, registry):
        from types import SimpleNamespace

        from server.api.device import PreviewStopRequest, preview_stop
        from server.session import _SESSION

        self._hold(registry)
        stopped = []

        class PM:
            def status(self):
                return {"active": {"SIM-1": {}, "SIM-9": {}}}

            async def stop(self):
                stopped.append("pm")
                return {"status": "stopped"}

        class SP:
            def status(self):
                return {"active": {}}

            async def stop(self):
                stopped.append("sp")

        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            device_controller=None, preview_manager=PM(), scrcpy_preview=SP())))
        write, who = device_access._WRITE.set(True), _SESSION.set("claude-b")
        try:
            with pytest.raises(device_access.DeviceHeld):
                await preview_stop(request, PreviewStopRequest())
        finally:
            _SESSION.reset(who)
            device_access._WRITE.reset(write)
        assert stopped == [], "nothing was stopped"

    async def test_stopping_a_capture_of_a_held_device_is_refused(self, registry):
        from types import SimpleNamespace

        from server.api.proxy import stop_capture
        from server.models import CaptureStopRequest
        from server.session import _SESSION

        self._hold(registry)
        manager = SimpleNamespace(simulator_udid=lambda sid: "SIM-1")
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            capture_sessions=manager, flow_store=None)))
        write, who = device_access._WRITE.set(True), _SESSION.set("claude-b")
        try:
            with pytest.raises(device_access.DeviceHeld):
                await stop_capture(request, CaptureStopRequest(session_id="cap-1"))
        finally:
            _SESSION.reset(who)
            device_access._WRITE.reset(write)

    async def test_a_per_device_task_keeps_its_refusal_to_itself(self, registry):
        """A multi-device install: one device's refusal must stay that device's
        error, not turn the whole response into a 423 (review)."""
        import asyncio

        from server.session import _SESSION

        self._hold(registry)
        shared: list = []
        refusals, write, who = (device_access._REFUSALS.set(shared),
                                device_access._WRITE.set(True), _SESSION.set("claude-b"))
        try:
            async def one(udid):
                device_access.refusals_are_per_item()
                try:
                    device_access.check(udid)
                    return "installed"
                except device_access.DeviceHeld:
                    return "refused"

            results = await asyncio.gather(one("SIM-1"), one("SIM-2"))
        finally:
            _SESSION.reset(who)
            device_access._WRITE.reset(write)
            device_access._REFUSALS.reset(refusals)
        assert results == ["refused", "installed"]
        assert shared == [], "the request's own refusal list was not touched"

    def test_both_build_routes_recheck_before_installing(self):
        import inspect

        from server.api import build_android, build_app

        for mod in (build_android, build_app):
            source = inspect.getsource(mod)
            assert "device_access.refusals_are_per_item()" in source, mod.__name__
            assert "device_access.why_not(" in source, mod.__name__
