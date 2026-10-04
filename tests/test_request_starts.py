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
from server.api.proxy import router as proxy_router
from server.api.trace import router as trace_router
from server.models import FlowRecord, FlowRequest, FlowResponse, LogEntry, LogLevel, LogSource
from server.proxy import flow_store as flow_store_mod
from server.proxy.addon import IOSDebugAddon
from server.proxy.flow_store import FlowStore
from server.recording import recorder as rec_mod
from server.recording.recorder import Filters, RecordingManager
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


@pytest.fixture(autouse=True)
def _no_real_process_lookups(monkeypatch):
    """Attribution runs ps, lsof and libproc against real pids; a test must
    never reach the machine that way, nor leave a pid cached for the next."""
    import server.proxy.addon as addon_mod
    monkeypatch.setattr(addon_mod, "_resolve_simulator_udid", lambda pid: None)
    monkeypatch.setattr(addon_mod, "_emulator_serial_for_pid", lambda pid: None)


def _wait_for(output, kind, n=1, timeout=2.0):
    """Reports are written from a worker pool, never the event loop: wait for
    them rather than reading the output at once."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(output.of_type(kind)) >= n:
            return output.of_type(kind)
        time.sleep(0.01)
    return output.of_type(kind)


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
        [started] = _wait_for(output, "request_started")
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
        [started] = _wait_for(output, "request_started")
        [done] = output.of_type("flow")
        assert done["id"] == started["id"]
        assert done["started_monotonic"] == started["started_monotonic"]

    def test_two_requests_are_two_ids(self, output):
        addon = IOSDebugAddon()
        addon._running = True
        for flow in (_real_flow(), _real_flow()):
            addon.request(flow)
        ids = {m["id"] for m in _wait_for(output, "request_started", 2)}
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
        flow = _real_flow()
        addon.request(flow)
        assert _wait_for(output, "request_started", timeout=0.3) == []
        # Answered: marked on the flow, which the response hook then records
        # once (#374) -- there is no separate mock_hit record any more.
        from server.proxy.addon import _mock_marker
        assert _mock_marker(flow)["rule_id"] == "m1"
        assert output.of_type("mock_hit") == []

    def test_a_held_request_is_in_flight(self, output):
        addon = IOSDebugAddon()
        addon._running = True
        addon._intercept_compiled = lambda f: f.request.pretty_host == "api.example.com"
        addon._intercept_pattern = "~d api.example.com"
        addon.request(_real_flow())
        assert len(output.of_type("intercepted")) == 1
        assert len(_wait_for(output, "request_started")) == 1

    def test_a_report_that_fails_never_breaks_the_request(self, output, monkeypatch):
        import server.proxy.addon as addon_mod
        addon = IOSDebugAddon()
        addon._running = True
        real = addon_mod._serialize_request

        def broken(req):
            raise RuntimeError("unserialisable")
        monkeypatch.setattr(addon_mod, "_serialize_request", broken)
        addon.request(_real_flow())               # does not raise
        [err] = _wait_for(output, "error")
        monkeypatch.setattr(addon_mod, "_serialize_request", real)
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

    async def test_a_late_start_after_its_flow_was_evicted_says_nothing_new(self):
        """The start is reported once the connection's lookup finishes, which
        can be after the response -- and after the flow has been pushed out
        of the store. It must not come back as in flight (CodeRabbit)."""
        store = FlowStore(max_size=1)
        await store.add(_flow("f1"))
        await store.add(_flow("f2"))                    # evicts f1
        assert store.size == 1
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
        assert hung.id == "hung"
        assert hung.error == "no response: it had not finished when the recording stopped"
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
        assert "quern was not running" in cut.error and "had not finished" not in cut.error

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
        assert flow["id"] == "hung" and "had not finished" in flow["error"]


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


class TestTheRequestHookNeverWaits:
    def test_a_pending_lookup_defers_the_report_and_not_the_request(self, output):
        """The hook runs in mitmproxy's event loop: waiting there for the
        connection's process lookup stalled every request (measured live)."""
        from concurrent.futures import Future

        import server.proxy.addon as addon_mod
        addon = IOSDebugAddon()
        addon._running = True
        flow = _real_flow()
        flow.client_conn.id = "conn-1"
        lookup: Future = Future()
        addon_mod._client_process_info["conn-1"] = {"future": lookup}
        try:
            started = time.monotonic()
            addon.request(flow)
            assert time.monotonic() - started < 0.1, "the request hook waited"
            assert output.of_type("request_started") == [], "reported before its device was known"
            lookup.set_result((4242, "MyApp"))
            [report] = _wait_for(output, "request_started")
            assert report["source_process"] == "MyApp" and report["source_pid"] == 4242
            # Its start was taken at the hook, not when the report went out.
            assert report["started_monotonic"] <= started
        finally:
            addon_mod._client_process_info.pop("conn-1", None)

    def test_a_start_reported_after_its_flow_is_not_unfinished(self, tmp_path):
        """A slow lookup can let the response win: the late start says nothing new."""
        t = datetime.now(UTC)
        done = _flow("f1", at=t).model_dump(mode="json")
        start = _flow("f1", status=None, at=t).model_dump(mode="json")
        lines = [{"type": "started", "at": t.isoformat(), "monotonic": 1.0, "udid": SIM,
                  "format_version": 2},
                 {"type": "flow", "at": t.isoformat(), "monotonic": 2.0, "data": done},
                 {"type": "request_started", "at": t.isoformat(), "monotonic": 3.0, "data": start},
                 {"type": "stopped", "at": t.isoformat(), "monotonic": 4.0}]
        (tmp_path / "events.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
        loaded = rec_mod.load(tmp_path)
        assert loaded.unfinished == [] and [f.id for f in loaded.flows] == ["f1"]


class TestTheReviewOfPhaseTwo:
    def _file(self, tmp_path, lines):
        (tmp_path / "events.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))

    def _start_line(self, t, fid="r1"):
        return {"type": "request_started", "at": t.isoformat(), "monotonic": 2.0,
                "data": _flow(fid, status=None, at=t).model_dump(mode="json")}

    def test_a_dropped_span_holding_its_timestamp_is_named_not_a_hang(self, tmp_path):
        """Dropped flows are stamped with their starts: the span begins before
        this request's, and it may be among them (review: called a hang)."""
        t = datetime(2026, 10, 1, 12, tzinfo=UTC)
        self._file(tmp_path, [
            {"type": "started", "at": t.isoformat(), "monotonic": 1.0, "udid": SIM,
             "format_version": 2},
            self._start_line(t),
            {"type": "dropped", "at": t.isoformat(), "monotonic": 3.0, "what": "flow",
             "count": 7, "first": (t - timedelta(seconds=5)).isoformat(),
             "last": (t + timedelta(seconds=5)).isoformat()},
            {"type": "stopped", "at": t.isoformat(), "monotonic": 4.0}])
        [r1] = rec_mod.load(tmp_path).unfinished
        assert "may be among 7 the recording dropped" in r1.error

    def test_a_dropped_span_elsewhere_does_not_excuse_it(self, tmp_path):
        t = datetime(2026, 10, 1, 12, tzinfo=UTC)
        self._file(tmp_path, [
            {"type": "started", "at": t.isoformat(), "monotonic": 1.0, "udid": SIM,
             "format_version": 2},
            self._start_line(t),
            {"type": "dropped", "at": t.isoformat(), "monotonic": 3.0, "what": "flow",
             "count": 2, "first": (t + timedelta(minutes=5)).isoformat(),
             "last": (t + timedelta(minutes=6)).isoformat()},
            {"type": "stopped", "at": t.isoformat(), "monotonic": 4.0}])
        [r1] = rec_mod.load(tmp_path).unfinished
        assert "had not finished when the recording stopped" in r1.error

    def test_a_live_recording_names_the_gap_not_in_flight(self, tmp_path):
        """A restart killed it: "in flight" for the rest of the run was false."""
        t = datetime(2026, 10, 1, 12, tzinfo=UTC)
        self._file(tmp_path, [
            {"type": "started", "at": t.isoformat(), "monotonic": 1.0, "udid": SIM,
             "format_version": 2},
            self._start_line(t),
            {"type": "resumed", "at": (t + timedelta(minutes=1)).isoformat(), "monotonic": 5.0,
             "gap": {"from": (t + timedelta(seconds=2)).isoformat(),
                     "to": (t + timedelta(minutes=1)).isoformat(),
                     "reason": "quern was not running"}}])
        [r1] = rec_mod.load(tmp_path, live=True).unfinished
        assert "quern was not running" in r1.error and "in flight" not in r1.error

    def test_a_proxy_stop_is_named(self, tmp_path):
        t = datetime(2026, 10, 1, 12, tzinfo=UTC)
        self._file(tmp_path, [
            {"type": "started", "at": t.isoformat(), "monotonic": 1.0, "udid": SIM,
             "format_version": 2},
            self._start_line(t),
            {"type": "proxy_stopped", "at": (t + timedelta(seconds=9)).isoformat(),
             "monotonic": 3.0, "in_flight": 1},
            {"type": "stopped", "at": t.isoformat(), "monotonic": 4.0}])
        [r1] = rec_mod.load(tmp_path).unfinished
        assert "the proxy stopped" in r1.error

    async def test_a_proxy_stop_is_written_into_recordings(self, tmp_path):
        src = Sources()
        manager = src.manager()
        rec = await manager.start(SIM, str(tmp_path / "r"), Filters())
        src.flows.note_started(_flow("cut", status=None))
        await _settle()
        assert src.flows.drop_pending() == 1
        await manager.stop(rec.id)
        assert "proxy_stopped" in [e["type"] for e in _events(tmp_path / "r")]
        [cut] = rec_mod.load(tmp_path / "r").unfinished
        assert "the proxy stopped" in cut.error

    def test_dropped_starts_are_holes_in_the_trace(self, tmp_path):
        t = datetime(2026, 10, 1, 12, tzinfo=UTC)
        (tmp_path / "r").mkdir()
        self._file(tmp_path / "r", [
            {"type": "started", "at": t.isoformat(), "monotonic": 1.0, "udid": SIM,
             "format_version": 2},
            {"type": "dropped", "at": t.isoformat(), "monotonic": 2.0,
             "what": "request_started", "count": 3, "first": t.isoformat(),
             "last": t.isoformat()},
            {"type": "stopped", "at": t.isoformat(), "monotonic": 4.0}])
        with TestClient(_app(FlowStore())) as client:
            trace = client.get("/api/v1/trace", params={"recording": str(tmp_path / "r")}).json()
        assert trace["flow_window_truncated"] is True
        assert any("request_started dropped" in h for h in trace["recording"]["holes"])

    async def test_a_start_only_request_can_be_read_in_full(self, tmp_path):
        src = Sources()
        manager = src.manager()
        rec = await manager.start(SIM, str(tmp_path / "r"), Filters())
        src.flows.note_started(_flow("hung", status=None))
        await _settle()
        await manager.stop(rec.id)
        page = rec_mod.read_events(tmp_path / "r", ("flows",), flow_id="hung")
        [event] = page["events"]
        assert event["type"] == "request_started"
        assert event["data"]["request"]["body"] == '{"email":"a@b.c"}'

    @pytest.mark.parametrize("params, keep", [({"device_serial": "emulator-5554"}, "emu"),
                                              ({"client_ip": "10.0.0.5"}, "phone")])
    def test_the_pending_route_filters_each_way(self, params, keep):
        store = FlowStore()
        store.note_started(_flow("sim", status=None))
        store.note_started(_flow("emu", sim=None, status=None).model_copy(
            update={"device_serial": "emulator-5554"}))
        store.note_started(_flow("phone", sim=None, status=None).model_copy(
            update={"client_ip": "10.0.0.5"}))
        with TestClient(_app(store)) as client:
            body = client.get("/api/v1/proxy/flows/pending", params=params).json()
        assert [p["id"] for p in body["pending"]] == [keep]

    def test_the_live_trace_takes_only_pending_in_its_window(self):
        store = FlowStore()
        app = _app(store)
        t = datetime.now(UTC) - timedelta(seconds=30)
        store.note_started(_flow("old", status=None, at=t - timedelta(hours=1)))
        store.note_started(_flow("now", status=None, at=t + timedelta(milliseconds=200)))
        with TestClient(app) as client:
            client.portal.call(app.state.server_buffer.append, _action(t + timedelta(seconds=1)))
            trace = client.get("/api/v1/trace", params={
                "since": (t - timedelta(seconds=5)).isoformat()}).json()
        ids = [f["id"] for a in trace["actions"] for f in a["flows"]]
        assert ids == ["now"]



class TestWhereReportsAreWritten:
    def test_a_slow_device_lookup_never_holds_the_request_hook(self, output, monkeypatch):
        """Resolving the device runs ps and lsof: on the event loop that
        stalled every request (review)."""
        import server.proxy.addon as addon_mod

        def slow(pid):
            time.sleep(0.3)
            return None
        monkeypatch.setattr(addon_mod, "_emulator_serial_for_pid", slow)
        addon = IOSDebugAddon()
        addon._running = True
        flow = _real_flow()
        flow.client_conn.id = "conn-2"
        addon_mod._client_process_info["conn-2"] = {"pid": 77, "process_name": "host-app"}
        try:
            started = time.monotonic()
            addon.request(flow)
            assert time.monotonic() - started < 0.1, "the request hook resolved the device"
            assert _wait_for(output, "request_started")
        finally:
            addon_mod._client_process_info.pop("conn-2", None)

    def test_a_deferred_report_runs_on_the_report_pool_and_leaves_the_entry_alone(
            self, monkeypatch):
        """Not on the lookup's worker -- other connections wait on that pool --
        and reading, not rewriting, the shared per-connection entry, which only
        the event loop may change (review: a racing read saw it empty)."""
        import threading
        from concurrent.futures import Future

        import server.proxy.addon as addon_mod
        threads = []
        real = addon_mod._write_json

        def spy(obj):
            if obj.get("type") == "request_started":
                threads.append(threading.current_thread().name)
            real(obj)
        monkeypatch.setattr(addon_mod, "_write_json", spy)
        cap = CapturedOutput().install()
        addon = IOSDebugAddon()
        addon._running = True
        flow = _real_flow()
        flow.client_conn.id = "conn-3"
        lookup: Future = Future()
        entry = {"future": lookup}
        addon_mod._client_process_info["conn-3"] = entry
        try:
            addon.request(flow)
            lookup.set_result((4243, "MyApp"))     # the callback runs in this thread
            [report] = _wait_for(cap, "request_started")
            assert report["source_process"] == "MyApp"
            assert threads and threads[0].startswith("quern-start"), threads
            assert entry == {"future": lookup}, "a worker rewrote the connection's entry"
        finally:
            cap.restore()
            addon_mod._client_process_info.pop("conn-3", None)


class TestCausesInTime:
    def test_a_gap_before_the_request_is_not_its_cause(self, tmp_path):
        t = datetime(2026, 10, 1, 12, tzinfo=UTC)
        lines = [
            {"type": "started", "at": t.isoformat(), "monotonic": 1.0, "udid": SIM,
             "format_version": 2},
            {"type": "resumed", "at": (t + timedelta(minutes=2)).isoformat(), "monotonic": 2.0,
             "gap": {"from": t.isoformat(), "to": (t + timedelta(minutes=1)).isoformat(),
                     "reason": "quern was not running"}},
            {"type": "request_started", "at": (t + timedelta(minutes=3)).isoformat(),
             "monotonic": 3.0, "data": _flow("late", status=None,
                                              at=t + timedelta(minutes=3)).model_dump(mode="json")},
            {"type": "stopped", "at": (t + timedelta(minutes=4)).isoformat(), "monotonic": 4.0}]
        (tmp_path / "events.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
        [late] = rec_mod.load(tmp_path).unfinished
        assert late.error == "no response: it had not finished when the recording stopped"

    def test_in_flight_requests_before_the_window_do_not_count_against_it(self):
        """The bound is spent on the window: requests in flight since long
        before it must not push `flows_over_limit` (limit 1 bounds flows at 10)."""
        store = FlowStore()
        app = _app(store)
        t = datetime.now(UTC) - timedelta(seconds=30)
        for i in range(12):
            store.note_started(_flow(f"old{i}", status=None, at=t - timedelta(hours=1)))
        with TestClient(app) as client:
            client.portal.call(app.state.server_buffer.append, _action(t + timedelta(seconds=1)))
            trace = client.get("/api/v1/trace", params={
                "since": (t - timedelta(seconds=5)).isoformat(), "limit": 1}).json()
        assert trace["flows_over_limit"] is False


class TestWithTheRecordingsReview:
    def test_a_markers_only_read_skips_request_starts_too(self, tmp_path, monkeypatch):
        t = datetime(2026, 10, 1, 12, tzinfo=UTC)
        start = _flow("r1", status=None, at=t).model_dump(mode="json")
        lines = [{"type": "started", "at": t.isoformat(), "monotonic": 1.0, "udid": SIM,
                  "format_version": 2},
                 {"type": "request_started", "at": t.isoformat(), "monotonic": 2.0,
                  "data": start},
                 {"type": "stopped", "at": t.isoformat(), "monotonic": 3.0}]
        (tmp_path / "events.jsonl").write_text(
            "".join(json.dumps(x, separators=(",", ":")) + "\n" for x in lines))

        def no_records(*a, **k):
            raise AssertionError("a request start was rebuilt for a markers-only read")
        monkeypatch.setattr(rec_mod.FlowRecord, "model_validate", no_records)
        assert rec_mod.load(tmp_path, markers_only=True).stopped

    def test_an_interrupted_stops_gap_explains_an_unfinished_request(self, tmp_path):
        t = datetime(2026, 10, 1, 12, tzinfo=UTC)
        start = _flow("r1", status=None, at=t).model_dump(mode="json")
        lines = [{"type": "started", "at": t.isoformat(), "monotonic": 1.0, "udid": SIM,
                  "format_version": 2},
                 {"type": "request_started", "at": t.isoformat(), "monotonic": 2.0,
                  "data": start},
                 {"type": "stopped", "at": (t + timedelta(hours=1)).isoformat(),
                  "monotonic": 3.0,
                  "gap": {"from": (t + timedelta(seconds=1)).isoformat(),
                          "to": (t + timedelta(hours=1)).isoformat(),
                          "reason": "not recording: its directory was gone"}}]
        (tmp_path / "events.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
        loaded = rec_mod.load(tmp_path)
        [r1] = loaded.unfinished
        assert "not recording" in r1.error
        # Exact for flows in a format-2 file: nothing reaches back before it.
        assert not rec_mod.holes_in(loaded, {"flow"}, t - timedelta(minutes=3),
                                    t - timedelta(seconds=5))
