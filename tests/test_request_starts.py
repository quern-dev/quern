"""Requests that start and never finish (#364, phase 2).

The proxy reported a flow only when its response arrived or it errored, so a
request the server never answered was invisible while it hung -- the case that
started #364: a CI sign-up request that hung and cascaded into 19 failures.
Now the addon says when each request starts, and everything downstream can
tell "started, never answered" from "never sent".
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import config as config_mod
from server import recording as rec_mod
from server.api.proxy import router as proxy_router
from server.api.trace import router as trace_router
from server.models import FlowRecord, FlowRequest, FlowResponse, LogEntry, LogLevel, LogSource
from server.proxy import flow_store as flow_store_mod
from server.proxy.addon import IOSDebugAddon
from server.proxy.flow_store import FlowStore
from server.recording import Filters, RecordingManager
from server.sources.proxy import ProxyAdapter
from server.storage.ring_buffer import RingBuffer
from tests.test_addon_intercept import CapturedOutput, _make_mock_flow

SIM = "SIM-A"


@pytest.fixture(autouse=True)
def _state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path / "state")


def _flow(fid=None, *, sim=SIM, at=None, status=200, host="api.example.com"):
    return FlowRecord(
        id=fid or uuid.uuid4().hex, timestamp=at or datetime.now(UTC),
        request=FlowRequest(method="POST", url=f"https://{host}/signup", host=host,
                            path="/signup", body='{"email":"a@b.c"}'),
        response=FlowResponse(status_code=status) if status else None,
        simulator_udid=sim, started_monotonic=123.5)


# ── the addon ────────────────────────────────────────────────────────────────


@pytest.fixture
def output():
    cap = CapturedOutput().install()
    yield cap
    cap.restore()


def _real_flow():
    flow = _make_mock_flow()
    flow.metadata = {}                 # mitmproxy's flows carry a real dict
    flow.request.timestamp_start = time.time() - 0.25
    return flow


class TestTheAddon:
    def test_a_request_says_it_started(self, output):
        addon = IOSDebugAddon()
        addon._running = True
        flow = _real_flow()
        addon.request(flow)
        [started] = output.of_type("request_started")
        assert started["id"].startswith("f_")
        assert started["request"]["method"] == "GET"
        # On the monotonic clock, back-dated to when the request started.
        assert started["started_monotonic"] <= time.monotonic() - 0.2

    def test_the_finished_flow_has_the_same_id_and_start(self, output):
        addon = IOSDebugAddon()
        addon._running = True
        flow = _real_flow()
        addon.request(flow)
        addon.response(flow)
        [started] = output.of_type("request_started")
        [done] = output.of_type("flow")
        assert done["id"] == started["id"]
        assert done["started_monotonic"] == started["started_monotonic"]

    def test_two_requests_are_two_ids(self, output):
        addon = IOSDebugAddon()
        addon._running = True
        for flow in (_real_flow(), _real_flow()):
            addon.request(flow)
        ids = {m["id"] for m in output.of_type("request_started")}
        assert len(ids) == 2

    def test_a_mocked_request_is_answered_not_started(self, output):
        addon = IOSDebugAddon()
        addon._running = True
        # Matched with a lambda, as the intercept tests do: flowfilter cannot
        # match a MagicMock flow.
        addon._mock_rules.append({
            "rule_id": "m1", "pattern_str": "~d api.example.com",
            "compiled": lambda f: f.request.pretty_host == "api.example.com",
            "response": {"status_code": 200, "headers": {}, "body": "{}"}})
        addon.request(_real_flow())
        assert output.of_type("request_started") == []
        assert len(output.of_type("mock_hit")) == 1

    def test_a_held_request_is_in_flight(self, output):
        addon = IOSDebugAddon()
        addon._running = True
        addon._intercept_compiled = lambda f: f.request.pretty_host == "api.example.com"
        addon._intercept_pattern = "~d api.example.com"
        addon.request(_real_flow())
        assert len(output.of_type("intercepted")) == 1
        assert len(output.of_type("request_started")) == 1

    def test_a_report_that_fails_never_breaks_the_request(self, output, monkeypatch):
        import server.proxy.addon as addon_mod
        addon = IOSDebugAddon()
        addon._running = True
        real = addon_mod._serialize_request

        def broken(req):
            raise RuntimeError("unserialisable")
        monkeypatch.setattr(addon_mod, "_serialize_request", broken)
        addon.request(_real_flow())               # does not raise
        monkeypatch.setattr(addon_mod, "_serialize_request", real)
        [err] = output.of_type("error")
        assert "request_started not reported" in err["message"]


# ── the store's pending set ──────────────────────────────────────────────────


class TestPending:
    async def test_started_is_pending_until_it_finishes(self):
        store = FlowStore()
        store.note_started(_flow("f1", status=None))
        assert [f.id for f in store.pending()] == ["f1"]
        assert store.size == 0, "a request with no response is not a flow"
        await store.add(_flow("f1"))
        assert store.pending() == [] and store.size == 1

    async def test_a_start_after_the_finish_says_nothing_new(self):
        store = FlowStore()
        await store.add(_flow("f1"))
        store.note_started(_flow("f1", status=None))
        assert store.pending() == []

    def test_the_bound_counts_what_it_pushes_out(self, monkeypatch):
        store = FlowStore()
        store._pending_max = 3
        for i in range(5):
            store.note_started(_flow(f"f{i}", status=None))
        assert [f.id for f in store.pending()] == ["f2", "f3", "f4"]
        assert store.pending_evicted == 2

    def test_dropping_says_how_many(self):
        store = FlowStore()
        for i in range(3):
            store.note_started(_flow(f"f{i}", status=None))
        assert store.drop_pending() == 3 and store.pending() == []

    async def test_starts_are_published_to_subscribers(self):
        store = FlowStore()
        queue = store.starts.subscribe(lambda f: f.simulator_udid == SIM)
        store.note_started(_flow("mine", status=None))
        store.note_started(_flow("theirs", sim="SIM-B", status=None))
        assert queue.get_nowait().id == "mine" and queue.empty()
        store.starts.unsubscribe(queue)

    def test_the_bound_is_named(self):
        assert flow_store_mod.PENDING_MAX == 2000


class TestTheAdapter:
    def test_a_start_event_lands_in_the_pending_set(self):
        store = FlowStore()
        adapter = ProxyAdapter(flow_store=store)
        adapter._handle_started({"type": "request_started", "id": "f_1", "timestamp": time.time(),
                                 "started_monotonic": 42.0,
                                 "request": {"method": "POST", "url": "https://x/y",
                                             "host": "x", "path": "/y"},
                                 "simulator_udid": SIM})
        [pending] = store.pending()
        assert pending.id == "f_1" and pending.started_monotonic == 42.0
        assert pending.simulator_udid == SIM

    def test_a_flows_start_is_kept(self):
        adapter = ProxyAdapter(flow_store=FlowStore())
        flow = adapter._parse_flow({"id": "f_2", "timestamp": time.time(),
                                    "started_monotonic": 7.5, "request": {"method": "GET"}})
        assert flow.started_monotonic == 7.5


# ── the route ────────────────────────────────────────────────────────────────


def _app(store: FlowStore) -> FastAPI:
    app = FastAPI()
    app.include_router(proxy_router)
    app.include_router(trace_router)
    app.state.flow_store = store
    app.state.server_buffer = RingBuffer(max_size=100)
    app.state.ring_buffer = RingBuffer(max_size=100)
    app.state.crash_buffer = RingBuffer(max_size=100)
    return app


class TestTheRoute:
    def test_in_flight_requests_with_their_age(self):
        store = FlowStore()
        store.note_started(_flow("old", status=None, at=datetime.now(UTC) - timedelta(minutes=2)))
        store.note_started(_flow("other", sim="SIM-B", status=None))
        with TestClient(_app(store)) as client:
            body = client.get("/api/v1/proxy/flows/pending",
                              params={"simulator_udid": SIM}).json()
        [row] = body["pending"]
        assert row["id"] == "old" and row["age_s"] >= 119 and row["started_monotonic"] == 123.5
        assert body["evicted"] == 0 and body["proxy_running"] is False


# ── recordings ───────────────────────────────────────────────────────────────


class Sources:
    def __init__(self):
        self.server = RingBuffer(max_size=1000)
        self.ring = RingBuffer(max_size=1000)
        self.crash = RingBuffer(max_size=1000)
        self.flows = FlowStore()

    def manager(self) -> RecordingManager:
        return RecordingManager(server_buffer=self.server, ring_buffer=self.ring,
                                crash_buffer=self.crash, flow_store=self.flows)


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


def _events(directory: Path) -> list[dict]:
    return [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]


class TestRecordingStarts:
    async def test_a_start_is_written_and_paired_with_its_flow(self, tmp_path):
        src = Sources()
        manager = src.manager()
        rec = await manager.start(SIM, str(tmp_path / "r"), Filters())
        src.flows.note_started(_flow("f1", status=None))
        src.flows.note_started(_flow("other-device", sim="SIM-B", status=None))
        await src.flows.add(_flow("f1"))
        await _settle()
        await manager.stop(rec.id)
        kinds = [e["type"] for e in _events(tmp_path / "r")]
        assert kinds.count("request_started") == 1 and kinds.count("flow") == 1
        assert rec.counts["request_started"] == 1
        loaded = rec_mod.load(tmp_path / "r")
        assert loaded.unfinished == [] and [f.id for f in loaded.flows] == ["f1"]

    async def test_a_request_that_never_finished_is_said_to_have_hung(self, tmp_path):
        src = Sources()
        manager = src.manager()
        rec = await manager.start(SIM, str(tmp_path / "r"), Filters())
        src.flows.note_started(_flow("hung", status=None))
        await _settle()
        await manager.stop(rec.id)
        [hung] = rec_mod.load(tmp_path / "r").unfinished
        assert hung.id == "hung" and "never finished" in hung.error
        assert hung.request.body == '{"email":"a@b.c"}', "what it sent is kept"

    async def test_one_still_running_is_in_flight(self, tmp_path):
        src = Sources()
        manager = src.manager()
        rec = await manager.start(SIM, str(tmp_path / "r"), Filters())
        src.flows.note_started(_flow("now", status=None))
        await _settle()
        await manager._flush(rec)
        [now] = rec_mod.load(tmp_path / "r", live=True).unfinished
        assert now.error == "in flight: no response yet"
        await manager.stop(rec.id)

    async def test_a_gap_after_it_started_is_named_not_called_a_hang(self, tmp_path):
        src = Sources()
        first = src.manager()
        rec = await first.start(SIM, str(tmp_path / "r"), Filters())
        src.flows.note_started(_flow("cut", status=None))
        await _settle()
        await first.shutdown()
        second = Sources().manager()
        await second.resume_all()
        await second.stop(rec.id)
        [cut] = rec_mod.load(tmp_path / "r").unfinished
        assert "quern was not running" in cut.error and "never finished" not in cut.error

    def test_a_version_2_gap_is_exact_for_flows(self, tmp_path):
        """No five-minute lookback: a request in flight at the gap is in the
        file as started."""
        t = datetime(2026, 10, 1, 12, tzinfo=UTC)
        lines = [
            {"type": "started", "at": t.isoformat(), "monotonic": 1.0, "udid": SIM,
             "format_version": 2},
            {"type": "resumed", "at": (t + timedelta(minutes=10)).isoformat(), "monotonic": 2.0,
             "gap": {"from": t.isoformat(), "to": (t + timedelta(minutes=10)).isoformat(),
                     "reason": "quern was not running"}},
            {"type": "stopped", "at": (t + timedelta(minutes=11)).isoformat(),
             "monotonic": 3.0}]
        (tmp_path / "events.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
        loaded = rec_mod.load(tmp_path)
        before = (t - timedelta(minutes=3), t - timedelta(seconds=5))
        assert not rec_mod.holes_in(loaded, {"flow"}, *before)
        assert rec_mod.holes_in(loaded, {"flow"}, t, t + timedelta(minutes=1))

    def test_a_version_1_gap_still_reaches_back(self, tmp_path):
        t = datetime(2026, 10, 1, 12, tzinfo=UTC)
        lines = [
            {"type": "started", "at": t.isoformat(), "monotonic": 1.0, "udid": SIM,
             "format_version": 1},
            {"type": "resumed", "at": t.isoformat(), "monotonic": 2.0,
             "gap": {"from": t.isoformat(), "to": t.isoformat(), "reason": "gap"}},
            {"type": "stopped", "at": t.isoformat(), "monotonic": 3.0}]
        (tmp_path / "events.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
        assert rec_mod.holes_in(rec_mod.load(tmp_path), {"flow"}, t - timedelta(minutes=3),
                                t - timedelta(seconds=5))

    async def test_only_flows_takes_the_starts_too(self, tmp_path):
        src = Sources()
        manager = src.manager()
        rec = await manager.start(SIM, str(tmp_path / "r"), Filters(kinds=("actions",)))
        assert "request_started" not in [k for k, _, _ in rec._subs]
        await manager.stop(rec.id)

    async def test_reading_flows_back_includes_starts(self, tmp_path):
        src = Sources()
        manager = src.manager()
        rec = await manager.start(SIM, str(tmp_path / "r"), Filters())
        src.flows.note_started(_flow("hung", status=None))
        await _settle()
        await manager.stop(rec.id)
        page = rec_mod.read_events(tmp_path / "r", ("flows",), detail="summary")
        [start] = [e for e in page["events"] if e["type"] == "request_started"]
        assert start["data"]["url"] == "https://api.example.com/signup"
        assert start["data"]["status"] is None


# ── the trace, live and recorded ────────────────────────────────────────────


def _action(at, udid=SIM):
    return LogEntry(id=uuid.uuid4().hex, timestamp=at, device_id="server",
                    process="server.api.actions", category="device.action",
                    level=LogLevel.INFO, message="tap_element ok", source=LogSource.SERVER,
                    action="tap_element", udid=udid, duration_ms=1000, outcome="ok",
                    started_monotonic=100.0)


class TestTheTrace:
    def test_a_hung_request_shows_in_the_live_trace(self):
        store = FlowStore()
        app = _app(store)
        t = datetime.now(UTC) - timedelta(seconds=30)
        store.note_started(_flow("hung", status=None, at=t + timedelta(milliseconds=200)))
        with TestClient(app) as client:
            client.portal.call(app.state.server_buffer.append, _action(t + timedelta(seconds=1)))
            trace = client.get("/api/v1/trace", params={
                "since": (t - timedelta(seconds=5)).isoformat()}).json()
        [action] = trace["actions"]
        [flow] = action["flows"]
        assert flow["id"] == "hung" and flow["status"] is None
        assert flow["error"].startswith("in flight: no response after")
        assert flow["started_monotonic"] == 123.5

    async def test_a_hung_request_shows_in_the_trace_over_a_recording(self, tmp_path):
        src = Sources()
        manager = src.manager()
        t = datetime.now(UTC) - timedelta(minutes=5)
        rec = await manager.start(SIM, str(tmp_path / "r"), Filters())
        await src.server.append(_action(t + timedelta(seconds=1)))
        src.flows.note_started(_flow("hung", status=None, at=t + timedelta(milliseconds=200)))
        await _settle()
        await manager.stop(rec.id)
        app = _app(FlowStore())
        app.state.recordings = SimpleNamespace(get=lambda _: (_ for _ in ()).throw(
            rec_mod.RecordingError("x")), is_live=lambda _: False)
        with TestClient(app) as client:
            trace = client.get("/api/v1/trace",
                               params={"recording": str(tmp_path / "r")}).json()
        [action] = trace["actions"]
        [flow] = action["flows"]
        assert flow["id"] == "hung" and "never finished" in flow["error"]


class TestTheReadLoop:
    async def test_a_start_line_is_dispatched_and_cleared_when_mitmdump_ends(self):
        """Driven through `_read_loop`, so the `msg_type` branch is exercised
        rather than the handler being called directly (a renamed branch left
        every handler-level test green)."""
        store = FlowStore()
        adapter = ProxyAdapter(flow_store=store)
        line = json.dumps({"type": "request_started", "id": "f_9", "timestamp": time.time(),
                           "started_monotonic": 1.5,
                           "request": {"method": "GET", "url": "https://x/", "host": "x"},
                           "simulator_udid": SIM}).encode() + b"\n"

        class _Stdout:
            def __aiter__(self):
                async def gen():
                    yield line
                return gen()

        queue = store.starts.subscribe()
        proc = MagicMock()
        proc.stdout = _Stdout()
        adapter._process = proc
        adapter._running = True
        await adapter._read_loop()
        assert queue.get_nowait().id == "f_9", "the addon reported a start and it was dropped"
        # mitmdump's output ended: nothing it carried will finish.
        assert store.pending() == []
