"""Which agent session asked for each action (#254, attribution).

The chain under test: the MCP process sends X-Quern-Session, SessionMiddleware
puts it in a ContextVar for the request, `logging_ext.action` stamps it on the
action record, the server-log adapter copies it to `LogEntry.session`, and the
trace returns and filters on it -- live and over a recording.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import config as config_mod
from server import session as session_mod
from server.api.actions import logged_action
from server.models import LogEntry, LogLevel, LogSource
from server.recording import recorder as rec_mod
from server.recording.recorder import Filters, RecordingManager
from server.session import SessionMiddleware, current_session, parse_session
from server.sources.server_log import ServerLogAdapter
from server.storage.ring_buffer import RingBuffer

SIM = "SIM-A"


@pytest.fixture(autouse=True)
def _state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path / "state")


class TestParsing:
    @pytest.mark.parametrize("raw, expected", [
        (b"mcp-1a2b3c4d", "mcp-1a2b3c4d"),
        ("ci-run:4471@runner.local", "ci-run:4471@runner.local"),
        (b"  mcp-1a2b3c4d  ", "mcp-1a2b3c4d"),
        (None, ""),
        (b"", ""),
        # Copied into every entry, the log and published recordings, so no
        # line breaks, markup or unbounded length.
        (b"mcp-1\nFAKE ENTRY", ""),
        (b"<script>", ""),
        (b"a" * 65, ""),
        (b"-leading-dash", ""),
        ("café".encode(), ""),
    ])
    def test_parse(self, raw, expected):
        assert parse_session(raw) == expected

    def test_sixty_four_characters_is_the_limit(self):
        assert parse_session(b"a" * 64) == "a" * 64


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(SessionMiddleware)

    @app.post("/act")
    @logged_action("tap", category="device.action")
    async def act():
        return {"session": current_session()}

    @app.post("/spawn")
    async def spawn():
        # A task the handler starts inherits the request's session, because
        # create_task copies the context.
        return {"session": await asyncio.create_task(_read_later())}

    return app


async def _read_later() -> str:
    await asyncio.sleep(0)
    return current_session()


class TestTheChain:
    async def test_header_to_log_entry(self):
        entries: list[LogEntry] = []

        async def collect(entry: LogEntry) -> None:
            entries.append(entry)

        adapter = ServerLogAdapter(on_entry=collect)
        await adapter.start()
        try:
            with TestClient(_app()) as client:
                said = client.post("/act", headers={"X-Quern-Session": "mcp-1a2b3c4d"}).json()
                client.post("/act")
                client.post("/act", headers={"X-Quern-Session": "bad\tvalue"})
            for _ in range(20):
                await asyncio.sleep(0)
        finally:
            await adapter.stop()

        assert said == {"session": "mcp-1a2b3c4d"}
        finished = [e for e in entries if e.action == "tap" and e.outcome == "ok"]
        assert [e.session for e in finished] == ["mcp-1a2b3c4d", "", ""]

    def test_a_task_the_handler_starts_keeps_the_session(self):
        with TestClient(_app()) as client:
            got = client.post("/spawn", headers={"X-Quern-Session": "mcp-9"}).json()
        assert got == {"session": "mcp-9"}

    def test_it_does_not_leak_between_requests(self):
        """Behaviour, not a guard on the reset: each request already runs in
        its own task with a copied context, so this passes without it. The
        reset is guarded by the next test."""
        with TestClient(_app()) as client:
            client.post("/act", headers={"X-Quern-Session": "mcp-first"})
            assert client.post("/act").json() == {"session": ""}

    async def test_the_middleware_restores_the_callers_context(self):
        """Called within an existing context -- not a fresh request task -- the
        session must not outlive the call. Found by deleting the reset and
        watching the request-level tests stay green."""
        seen: list[str] = []

        async def inner(scope, receive, send):
            seen.append(current_session())

        middleware = SessionMiddleware(inner)
        await middleware({"type": "http", "headers": [(b"x-quern-session", b"mcp-x")]},
                         None, None)

        assert seen == ["mcp-x"]
        assert current_session() == ""

    def test_no_request_means_no_session(self):
        assert current_session() == ""

    def test_the_real_app_installs_the_middleware(self):
        """The chain above is only real if create_app wires it in."""
        from server.main import create_app

        app = create_app()
        assert any(m.cls is session_mod.SessionMiddleware for m in app.user_middleware)


def _action(at: datetime, session: str, udid: str = SIM) -> LogEntry:
    return LogEntry(id=uuid.uuid4().hex, timestamp=at, device_id="server",
                    process="server.api.actions", category="device.action",
                    level=LogLevel.INFO, message="tap ok", source=LogSource.SERVER,
                    action="tap", udid=udid, duration_ms=100, outcome="ok",
                    started_monotonic=100.0, session=session)


class TestTheTrace:
    @staticmethod
    def _request(server_buffer):
        return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            server_buffer=server_buffer,
            crash_buffer=RingBuffer(max_size=10),
            ring_buffer=RingBuffer(max_size=10),
            flow_store=None, proxy_adapter=None,
        )))

    async def test_live_trace_returns_and_filters_by_session(self):
        from server.api.trace import get_trace

        buf = RingBuffer(max_size=100)
        t = datetime.now(UTC) - timedelta(seconds=30)
        # Two agents on ONE device: udid cannot separate them, session can.
        await buf.append(_action(t, "mcp-aaaa"))
        await buf.append(_action(t + timedelta(seconds=1), "mcp-bbbb"))
        await buf.append(_action(t + timedelta(seconds=2), ""))

        everything = await get_trace(request=self._request(buf), since=None, udid=None, limit=10)
        assert [a["session"] for a in everything["actions"]] == ["mcp-aaaa", "mcp-bbbb", ""]

        mine = await get_trace(request=self._request(buf), since=None, udid=SIM, limit=10,
                               session="mcp-bbbb")
        assert [a["session"] for a in mine["actions"]] == ["mcp-bbbb"]

    def test_the_http_response_carries_session(self):
        """Through the route's response model, not a direct call: FastAPI drops
        any field the model does not declare, and the direct-call tests above
        cannot see that. The first version passed them and returned no
        `session` at all over HTTP -- found by the live two-agent test."""
        from server.api.trace import router

        app = FastAPI()
        app.include_router(router)
        app.state.server_buffer = RingBuffer(max_size=100)
        app.state.ring_buffer = RingBuffer(max_size=10)
        app.state.crash_buffer = RingBuffer(max_size=10)
        app.state.flow_store = None
        t = datetime.now(UTC) - timedelta(seconds=30)
        with TestClient(app) as client:
            client.portal.call(app.state.server_buffer.append, _action(t, "mcp-aaaa"))
            client.portal.call(app.state.server_buffer.append,
                               _action(t + timedelta(seconds=1), "mcp-bbbb"))
            body = client.get("/api/v1/trace", params={"session": "mcp-aaaa"}).json()
        assert [a["session"] for a in body["actions"]] == ["mcp-aaaa"]

    def test_filtering_by_session_does_not_change_what_an_action_says(self):
        """Two sessions, overlapping actions, one device -- the case `session`
        exists for. The first version filtered before attributing, so the other
        session's action was gone when overlaps were computed: A lost its
        overlap caveat and kept a log line B's action may have caused. Found
        by review, reproduced through the route."""
        from server.api.trace import router

        app = FastAPI()
        app.include_router(router)
        app.state.server_buffer = RingBuffer(max_size=100)
        app.state.ring_buffer = RingBuffer(max_size=100)
        app.state.crash_buffer = RingBuffer(max_size=10)
        app.state.flow_store = None
        t = datetime.now(UTC) - timedelta(seconds=30)
        # A: tap from t to t+2s. B: launch_app from t+0.5 to t+1.5. Entries are
        # written when an action ends, so the timestamp is the end.
        a = _action(t + timedelta(seconds=2), "mcp-aaaa")
        a.duration_ms = 2000
        b = _action(t + timedelta(seconds=1.5), "mcp-bbbb")
        b.action, b.duration_ms = "launch_app", 1000
        line = LogEntry(id=uuid.uuid4().hex, timestamp=t + timedelta(seconds=1), device_id=SIM,
                        process="MyApp", level=LogLevel.INFO, message="x",
                        source=LogSource.SIMULATOR)
        since = (t - timedelta(seconds=5)).isoformat()
        with TestClient(app) as client:
            for entry in (a, b):
                client.portal.call(app.state.server_buffer.append, entry)
            client.portal.call(app.state.ring_buffer.append, line)
            whole = client.get("/api/v1/trace", params={"since": since}).json()
            mine = client.get("/api/v1/trace",
                              params={"since": since, "session": "mcp-aaaa"}).json()

        [a_whole] = [x for x in whole["actions"] if x["session"] == "mcp-aaaa"]
        [a_mine] = mine["actions"]
        assert a_whole["overlaps"] == ["launch_app"], "the scenario must overlap"
        for field in ("overlaps", "caveats", "logs", "flows"):
            assert a_mine[field] == a_whole[field], field

    async def test_trace_over_a_recording_filters_by_session(self, tmp_path):
        from server.api.trace import get_trace

        server = RingBuffer(max_size=100)
        manager = RecordingManager(server_buffer=server, ring_buffer=RingBuffer(max_size=10),
                                   crash_buffer=RingBuffer(max_size=10), flow_store=None)
        rec = await manager.start(SIM, str(tmp_path / "r"), Filters(kinds=["actions"]))
        t = datetime.now(UTC)
        await server.append(_action(t, "mcp-aaaa"))
        await server.append(_action(t + timedelta(milliseconds=10), "mcp-bbbb"))
        for _ in range(10):
            await asyncio.sleep(0)
        await manager.stop(rec.id)

        request = self._request(RingBuffer(max_size=10))
        # An id lookup that refuses, so the reference is taken as the directory
        # -- as test_request_starts does.
        request.app.state.recordings = SimpleNamespace(
            get=lambda _: (_ for _ in ()).throw(rec_mod.RecordingError("x")),
            is_live=lambda _: False)
        trace = await get_trace(request=request, since=None, udid=None, limit=10,
                                recording=str(tmp_path / "r"), session="mcp-aaaa")
        assert [a["session"] for a in trace["actions"]] == ["mcp-aaaa"]

    def test_a_recording_written_before_sessions_loads_unattributed(self):
        old = _action(datetime.now(UTC), "x").model_dump(mode="json")
        del old["session"]
        assert LogEntry.model_validate(old).session == ""


@pytest.fixture(autouse=True)
def _actions_are_logged():
    """The actions logger at DEBUG for these tests, restored afterwards: it is
    process-global, and leaving it changed would alter later tests."""
    log = logging.getLogger("server.api.actions")
    before = log.level
    log.setLevel(logging.DEBUG)
    yield
    log.setLevel(before)


class TestTheSharedRule:
    """QUERN_SESSION, else the Claude Code session, else the fallback. The MCP
    server's copy of this rule is `mintSessionId` in mcp/src/http.ts."""

    CLAUDE = "1256bb48-e4fc-5e5c-95cc-3a3bde6898a3"

    @pytest.mark.parametrize("env, expected", [
        ({}, "cli"),
        ({"CLAUDE_CODE_SESSION_ID": CLAUDE}, f"claude-{CLAUDE}"),
        ({"QUERN_SESSION": "ci-1", "CLAUDE_CODE_SESSION_ID": CLAUDE}, "ci-1"),
        # A name the server would refuse falls through rather than being sent
        # and discarded, which would leave the caller unattributed.
        ({"QUERN_SESSION": "bad name", "CLAUDE_CODE_SESSION_ID": CLAUDE}, f"claude-{CLAUDE}"),
        ({"CLAUDE_CODE_SESSION_ID": "bad id"}, "cli"),
        ({"CLAUDE_CODE_SESSION_ID": "   "}, "cli"),
    ])
    def test_rule(self, env, expected):
        assert session_mod.session_from_env(env, fallback="cli") == expected

    def test_the_record_cli_sends_it(self, monkeypatch, tmp_path):
        """`quern record` called from an agent's Bash lands under that agent's
        session, the same one its MCP server reports."""
        import io
        import json as json_mod

        from server import __main__ as main_mod
        from server.recording import cli

        sent: list[dict] = []

        class _Resp(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def urlopen(req, timeout=None):
            sent.append(dict(req.header_items()))
            return _Resp(json_mod.dumps({"ok": True}).encode())

        key = tmp_path / "api-key"
        key.write_text("k")
        monkeypatch.setattr("server.config.API_KEY_FILE", key)
        monkeypatch.setattr(main_mod, "_server_base_url", lambda: "http://127.0.0.1:1")
        monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", self.CLAUDE)
        monkeypatch.delenv("QUERN_SESSION", raising=False)

        assert cli._call("GET", "/api/v1/recordings") == (200, {"ok": True})
        # urllib title-cases header names.
        assert sent[0]["X-quern-session"] == f"claude-{self.CLAUDE}"


class TestTheEdges:
    async def test_a_begin_entry_carries_the_session(self):
        """What is left of a hung or cut-off action is its begin entry, and
        that is when "which agent started this" matters most."""
        from server.api.actions import action
        from server.logging_ext import session_of

        records: list[logging.LogRecord] = []

        class _Keep(logging.Handler):
            def emit(self, record):
                records.append(record)

        log = logging.getLogger("server.api.actions")
        handler = _Keep(level=logging.DEBUG)
        log.addHandler(handler)
        token = session_mod._SESSION.set("mcp-hung")
        try:
            with action("tap", category="device.action"):
                pass
        finally:
            session_mod._SESSION.reset(token)
            log.removeHandler(handler)

        begins = [r for r in records if getattr(r, "quern_outcome", "") == "started"]
        assert begins, "no begin entry was written"
        assert session_of(begins[0]) == "mcp-hung"

    def test_the_recording_summary_view_keeps_the_session(self):
        from server.recording.recorder import _summary

        data = _action(datetime.now(UTC), "mcp-aaaa").model_dump(mode="json")
        assert _summary("action", data)["session"] == "mcp-aaaa"
