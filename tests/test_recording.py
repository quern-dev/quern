"""Recording a device's actions, flows and logs to disk (#364).

Real buffers and a real flow store throughout -- they are in-memory and touch
nothing on the machine -- so what is tested is what the server wires up: the
subscriptions, their filters, and what reaches the file.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import config as config_mod
from server import record_cli
from server import recording as rec_mod
from server.api.recordings import router as recordings_router
from server.api.trace import router as trace_router
from server.models import FlowRecord, FlowRequest, FlowResponse, LogEntry, LogLevel, LogSource
from server.proxy.flow_store import FlowStore
from server.recording import Filters, RecordingError, RecordingManager
from server.storage.ring_buffer import RingBuffer

SIM = "SIM-A"
OTHER = "SIM-B"


@pytest.fixture(autouse=True)
def _state_dir(tmp_path, monkeypatch):
    """Where the running recordings are kept to resume: never ~/.quern."""
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path / "state")


def _now(offset_s: float = 0) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=offset_s)


def _action(name="tap", *, udid=SIM, outcome="ok", at=None, duration_ms=500):
    return LogEntry(id=uuid.uuid4().hex, timestamp=at or _now(), device_id="server",
                    process="server.api.actions", category="device.action",
                    level=LogLevel.INFO, message=f"{name} {outcome}", source=LogSource.SERVER,
                    action=name, udid=udid, duration_ms=duration_ms, outcome=outcome,
                    started_monotonic=1.0)


def _log(*, device=SIM, source=LogSource.SIMULATOR, at=None, message="hello"):
    return LogEntry(id=uuid.uuid4().hex, timestamp=at or _now(), device_id=device,
                    process="MyApp", level=LogLevel.INFO, message=message, source=source)


def _flow(*, sim=SIM, ip=None, host="api.example.com", at=None, fid=None, status=200):
    return FlowRecord(
        id=fid or uuid.uuid4().hex, timestamp=at or _now(),
        request=FlowRequest(method="POST", url=f"https://{host}/signup", host=host,
                            path="/signup", headers={"content-type": "application/json"},
                            body='{"email":"a@b.c"}'),
        response=FlowResponse(status_code=status, headers={}, body='{"ok":true}')
        if status else None,
        simulator_udid=sim, client_ip=ip)


class Sources:
    def __init__(self):
        self.server = RingBuffer(max_size=1000)
        self.ring = RingBuffer(max_size=1000)
        self.crash = RingBuffer(max_size=1000)
        self.flows = FlowStore()

    def manager(self, ip_map=None) -> RecordingManager:
        return RecordingManager(server_buffer=self.server, ring_buffer=self.ring,
                                crash_buffer=self.crash, flow_store=self.flows,
                                ip_map=(lambda: ip_map or {}))


def _events(directory: Path) -> list[dict]:
    return [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


async def _record(src: Sources, out: Path, feed, filters=None, ip_map=None):
    manager = src.manager(ip_map)
    rec = await manager.start(SIM, str(out), filters or Filters())
    await feed()
    await _settle()
    await manager.stop(rec.id)
    return manager, rec


# ── what is recorded ─────────────────────────────────────────────────────────


class TestWhatIsRecorded:
    async def test_the_devices_actions_flows_and_logs_and_nothing_else(self, tmp_path):
        src = Sources()

        async def feed():
            await src.server.append(_action("tap"))
            await src.server.append(_action("tap", udid=OTHER))
            await src.server.append(_log(device="server", source=LogSource.SERVER))
            await src.flows.add(_flow())
            await src.flows.add(_flow(sim=OTHER))
            await src.ring.append(_log())
            await src.ring.append(_log(device=OTHER))
            await src.ring.append(_log(source=LogSource.PROXY))     # not an app source
            await src.crash.append(_log(source=LogSource.CRASH, message="crashed"))

        _, rec = await _record(src, tmp_path / "r", feed)
        kinds = [e["type"] for e in _events(tmp_path / "r")]
        assert kinds.count("action") == 1 and kinds.count("flow") == 1
        assert kinds.count("log") == 1 and kinds.count("crash") == 1
        assert rec.counts == {"action": 1, "flow": 1, "log": 1, "crash": 1}

    async def test_a_flow_is_recorded_in_full(self, tmp_path):
        src = Sources()

        async def feed():
            await src.flows.add(_flow())

        await _record(src, tmp_path / "r", feed)
        [flow] = [e["data"] for e in _events(tmp_path / "r") if e["type"] == "flow"]
        assert flow["request"]["body"] == '{"email":"a@b.c"}'
        assert flow["request"]["headers"] == {"content-type": "application/json"}
        assert flow["response"]["body"] == '{"ok":true}'

    async def test_an_actions_started_marker_is_kept(self, tmp_path):
        """It is what shows an action that hung: the trace skips it, the
        recording must not lose it."""
        src = Sources()

        async def feed():
            await src.server.append(_action("tap", outcome="started"))

        await _record(src, tmp_path / "r", feed)
        assert [e["data"]["outcome"] for e in _events(tmp_path / "r")
                if e["type"] == "action"] == ["started"]

    async def test_unattributed_work_only_when_asked(self, tmp_path):
        src = Sources()

        async def feed():
            await src.flows.add(_flow(sim=None))
            await src.ring.append(_log(device=""))

        _, rec = await _record(src, tmp_path / "a", feed)
        assert rec.counts["flow"] == 0 and rec.counts["log"] == 0
        _, rec = await _record(src, tmp_path / "b", feed,
                               Filters(include_unattributed=True))
        assert rec.counts["flow"] == 1 and rec.counts["log"] == 1

    async def test_a_physical_device_by_its_recorded_address(self, tmp_path):
        """The trace's own rule: `device_of` through the ip map."""
        src = Sources()

        async def feed():
            await src.flows.add(_flow(sim=None, ip="10.0.0.5"))
            await src.flows.add(_flow(sim=None, ip="10.0.0.9"))

        _, rec = await _record(src, tmp_path / "r", feed,
                               ip_map={"10.0.0.5": (SIM, True), "10.0.0.9": (OTHER, True)})
        assert rec.counts["flow"] == 1

    async def test_host_filters_take_subdomains_and_nothing_else(self, tmp_path):
        src = Sources()

        async def feed():
            for host in ("api.example.com", "example.com", "badexample.com",
                         "t.analytics.example.net"):
                await src.flows.add(_flow(host=host))

        await _record(src, tmp_path / "r", feed,
                      Filters(hosts=["example.com", "analytics.example.net"],
                              exclude_hosts=["analytics.example.net"]))
        hosts = [e["data"]["request"]["host"] for e in _events(tmp_path / "r")
                 if e["type"] == "flow"]
        assert hosts == ["api.example.com", "example.com"]

    async def test_network_calls_only(self, tmp_path):
        src = Sources()

        async def feed():
            await src.ring.append(_log())
            await src.crash.append(_log(source=LogSource.CRASH))
            await src.flows.add(_flow())

        _, rec = await _record(src, tmp_path / "r", feed, Filters(kinds=("flows",)))
        assert rec.counts["log"] == 0 and rec.counts["crash"] == 0 and rec.counts["flow"] == 1

    async def test_every_line_carries_both_clocks(self, tmp_path):
        """`monotonic` is the clock video frames are stamped with (#290)."""
        src = Sources()

        async def feed():
            await src.flows.add(_flow())

        await _record(src, tmp_path / "r", feed)
        events = _events(tmp_path / "r")
        assert all(isinstance(e["monotonic"], float) and e["at"] for e in events)
        assert events[0]["type"] == "started" and set(events[0]["clock_anchor"]) == {
            "wall", "monotonic"}


# ── completeness ─────────────────────────────────────────────────────────────


class TestCompleteness:
    async def test_a_clean_stop_is_complete(self, tmp_path):
        src = Sources()

        async def feed():
            await src.flows.add(_flow())

        _, rec = await _record(src, tmp_path / "r", feed)
        manifest = json.loads((tmp_path / "r" / "manifest.json").read_text())
        assert manifest["complete"] is True and manifest["state"] == "stopped"
        assert _events(tmp_path / "r")[-1]["type"] == "stopped"

    async def test_a_writer_that_fell_behind_says_where(self, tmp_path):
        """The subscription holds 1,000: 1,200 at once without yielding drops 200."""
        src = Sources()
        manager = src.manager()
        rec = await manager.start(SIM, str(tmp_path / "r"), Filters())
        for _ in range(1200):
            src.flows._fanout.publish(_flow())
        await manager.stop(rec.id)
        [dropped] = [e for e in _events(tmp_path / "r") if e["type"] == "dropped"]
        assert dropped["what"] == "flow" and dropped["count"] == 200
        assert dropped["first"] and dropped["last"]
        assert rec.counts["flow"] == 1000 and rec.dropped == {"flow": 200}
        assert rec.complete is False
        manifest = json.loads((tmp_path / "r" / "manifest.json").read_text())
        assert manifest["complete"] is False and manifest["dropped"] == {"flow": 200}

    async def test_a_write_that_fails_ends_the_recording_and_says_so(self, tmp_path,
                                                                     monkeypatch):
        src = Sources()
        manager = src.manager()
        rec = await manager.start(SIM, str(tmp_path / "r"), Filters())

        def full(*a, **k):
            raise OSError(28, "No space left on device")
        monkeypatch.setattr(RecordingManager, "_append", staticmethod(full))
        await src.flows.add(_flow())
        await _settle()
        await manager._flush(rec)
        assert rec.state == "failed" and "No space left" in rec.error
        assert rec.complete is False
        manifest = json.loads((tmp_path / "r" / "manifest.json").read_text())
        assert manifest["state"] == "failed"
        # And it no longer listens: nothing is queued for a dead recording.
        assert all(q not in src.flows._fanout for _, _, q in rec._subs)


# ── surviving a restart ──────────────────────────────────────────────────────


class TestRestarts:
    async def test_a_restart_costs_a_gap_not_the_recording(self, tmp_path):
        src = Sources()
        first = src.manager()
        rec = await first.start(SIM, str(tmp_path / "r"), Filters())
        await src.flows.add(_flow(host="before.example.com"))
        await _settle()
        await first.shutdown()

        again = Sources()                     # a new process: new buffers
        second = again.manager()
        assert await second.resume_all() == [rec.id]
        await again.flows.add(_flow(host="after.example.com"))
        await _settle()
        resumed = await second.stop(rec.id)

        events = _events(tmp_path / "r")
        kinds = [e["type"] for e in events]
        assert kinds.index("paused") < kinds.index("resumed") < kinds.index("stopped")
        hosts = [e["data"]["request"]["host"] for e in events if e["type"] == "flow"]
        assert hosts == ["before.example.com", "after.example.com"]
        [gap] = resumed.gaps
        assert gap["reason"] == "quern was not running" and gap["from"] and gap["to"]
        assert resumed.complete is False
        assert resumed.counts["flow"] == 2, "counts carry across the restart"

    async def test_a_crash_has_no_paused_line_and_the_gap_starts_at_the_last_line(
            self, tmp_path):
        src = Sources()
        first = src.manager()
        rec = await first.start(SIM, str(tmp_path / "r"), Filters())
        await src.flows.add(_flow())
        await _settle()
        await first._flush(rec)
        last = _events(tmp_path / "r")[-1]["at"]
        with open(tmp_path / "r" / "events.jsonl", "a") as f:
            f.write('{"type": "flow", "at": "torn')          # killed mid-write

        second = Sources().manager()
        await second.resume_all()
        [gap] = second.get(rec.id).gaps
        assert gap["from"] == last
        assert "paused" not in [e.get("type") for e in map(_safe_json, (
            tmp_path / "r" / "events.jsonl").read_text().splitlines())]

    async def test_drops_after_a_resume_are_still_said(self, tmp_path):
        """A resumed recording's subscriptions start again from zero while its
        totals carry on: a new drop must not hide under the old total."""
        src = Sources()
        first = src.manager()
        rec = await first.start(SIM, str(tmp_path / "r"), Filters())
        for _ in range(1100):
            src.flows._fanout.publish(_flow())
        await first.shutdown()

        again = Sources()
        second = again.manager()
        await second.resume_all()
        for _ in range(1050):
            again.flows._fanout.publish(_flow())
        done = await second.stop(rec.id)
        assert done.dropped == {"flow": 150}
        assert [e["count"] for e in _events(tmp_path / "r") if e["type"] == "dropped"] == [
            100, 50]

    async def test_a_recording_whose_directory_is_gone_is_not_resumed(self, tmp_path):
        import shutil
        first = Sources().manager()
        await first.start(SIM, str(tmp_path / "r"), Filters())
        await first.shutdown()
        shutil.rmtree(tmp_path / "r")
        assert await Sources().manager().resume_all() == []

    async def test_an_unreadable_state_file_resumes_nothing_and_says_so(self, tmp_path,
                                                                        caplog):
        rec_mod._state_file().parent.mkdir(parents=True)
        rec_mod._state_file().write_text("{not json")
        assert await Sources().manager().resume_all() == []
        assert "were not resumed" in caplog.text

    async def test_a_stopped_recording_is_not_resumed(self, tmp_path):
        first = Sources().manager()
        rec = await first.start(SIM, str(tmp_path / "r"), Filters())
        await first.stop(rec.id)
        assert await Sources().manager().resume_all() == []


def _safe_json(line):
    try:
        return json.loads(line)
    except ValueError:
        return {}


# ── starting ─────────────────────────────────────────────────────────────────


class TestStarting:
    async def test_a_relative_directory_is_refused(self):
        with pytest.raises(RecordingError, match="absolute"):
            await Sources().manager().start(SIM, "out/rec", Filters())

    async def test_a_directory_holding_a_recording_is_refused(self, tmp_path):
        manager = Sources().manager()
        rec = await manager.start(SIM, str(tmp_path / "r"), Filters())
        await manager.stop(rec.id)
        with pytest.raises(RecordingError, match="already exists"):
            await manager.start(SIM, str(tmp_path / "r"), Filters())

    async def test_the_default_directory_is_under_the_state_dir(self):
        manager = Sources().manager()
        rec = await manager.start(SIM, None, Filters())
        assert rec.dir.parent == config_mod.CONFIG_DIR / "recordings"
        await manager.stop(rec.id)

    async def test_two_recordings_of_one_device_each_get_everything(self, tmp_path):
        src = Sources()
        manager = src.manager()
        a = await manager.start(SIM, str(tmp_path / "a"), Filters())
        b = await manager.start(SIM, str(tmp_path / "b"), Filters())
        await src.flows.add(_flow())
        await _settle()
        await manager.stop(a.id)
        await manager.stop(b.id)
        assert a.counts["flow"] == b.counts["flow"] == 1


# ── reading back ─────────────────────────────────────────────────────────────


class TestLoading:
    async def test_a_flow_updated_in_the_store_is_kept_once(self, tmp_path):
        src = Sources()

        async def feed():
            await src.flows.add(_flow(fid="f1", status=None))
            await src.flows.add(_flow(fid="f1", status=503))

        await _record(src, tmp_path / "r", feed)
        loaded = rec_mod.load(tmp_path / "r")
        [flow] = loaded.flows
        assert flow.response.status_code == 503 and loaded.stopped

    def test_a_torn_line_is_counted_not_fatal(self, tmp_path):
        (tmp_path / "events.jsonl").write_text(
            '{"type":"started","udid":"SIM-A","at":"2026-10-01T00:00:00+00:00"}\n{"type": "fl')
        loaded = rec_mod.load(tmp_path)
        assert loaded.unreadable_lines == 1 and loaded.udid == SIM and not loaded.stopped

    def test_holes_overlap_by_kind_and_window(self):
        t = datetime(2026, 10, 1, 12, tzinfo=UTC)
        loaded = rec_mod.Loaded(actions=[], flows=[], logs=[], udid=SIM, stopped=True,
                                unreadable_lines=0, holes=[
                                    (t, t + timedelta(minutes=1), frozenset({"flow"}), "dropped"),
                                    (None, t + timedelta(hours=1), frozenset({"log"}), "gap")])
        assert rec_mod.holes_in(loaded, {"flow"}, t - timedelta(hours=1), t)
        assert not rec_mod.holes_in(loaded, {"flow"}, t + timedelta(minutes=2), None)
        assert not rec_mod.holes_in(loaded, {"action"}, None, None)
        # An unknown start reaches back as far as it must: never read as covered.
        assert rec_mod.holes_in(loaded, {"log"}, t - timedelta(days=1), t - timedelta(hours=2))


# ── the routes, the trace over a recording, and the CLI ─────────────────────


def _app(src: Sources) -> FastAPI:
    app = FastAPI()
    app.include_router(recordings_router)
    app.include_router(trace_router)
    app.state.recordings = src.manager()
    app.state.server_buffer = src.server
    app.state.ring_buffer = src.ring
    app.state.crash_buffer = src.crash
    app.state.flow_store = src.flows
    return app


class TestTheRoutes:
    def test_start_list_stop(self, tmp_path):
        src = Sources()
        with TestClient(_app(src)) as client:
            started = client.post("/api/v1/recordings", json={
                "udid": SIM, "output_dir": str(tmp_path / "r"),
                "exclude_hosts": ["analytics.example.net"]}).json()
            assert started["state"] == "recording" and started["complete"] is None
            assert "the proxy is not running" in " ".join(started["warnings"])
            assert [r["id"] for r in client.get("/api/v1/recordings").json()["recordings"]] == [
                started["id"]]
            stopped = client.post(f"/api/v1/recordings/{started['id']}/stop").json()
            assert stopped["state"] == "stopped" and stopped["complete"] is True

    def test_refusals_are_400_and_404(self, tmp_path):
        with TestClient(_app(Sources())) as client:
            r = client.post("/api/v1/recordings", json={"udid": SIM, "output_dir": "rel"})
            assert r.status_code == 400 and "absolute" in r.json()["detail"]
            assert client.post("/api/v1/recordings/rec_nope/stop").status_code == 404
            assert client.post("/api/v1/recordings", json={"udid": ""}).status_code == 422

    def test_the_trace_over_a_recording_attributes_as_live(self, tmp_path):
        """The hung-request case: a tap, the sign-up request it caused, and
        a log line -- long after the buffers would have dropped them."""
        src = Sources()
        app = _app(src)
        t = _now(-3600)
        with TestClient(app) as client:
            rid = client.post("/api/v1/recordings", json={
                "udid": SIM, "output_dir": str(tmp_path / "r")}).json()["id"]

            async def feed():
                await src.server.append(_action("tap", at=t + timedelta(seconds=1),
                                                duration_ms=1000))
                await src.flows.add(_flow(at=t + timedelta(milliseconds=500)))
                await src.ring.append(_log(at=t + timedelta(milliseconds=700)))
                await _settle()
            client.portal.call(feed)
            client.post(f"/api/v1/recordings/{rid}/stop")
            for ref in (rid, str(tmp_path / "r")):
                trace = client.get("/api/v1/trace", params={"recording": ref}).json()
                [action] = trace["actions"]
                assert action["action"] == "tap" and len(action["flows"]) == 1
                assert len(action["logs"]) == 1
                assert trace["recording"]["stopped"] is True
                assert trace["flow_window_truncated"] is False
                assert trace["proxy_running"] is None

    def test_a_hole_in_the_window_sets_the_flag(self, tmp_path):
        (tmp_path / "r").mkdir()
        t = datetime(2026, 10, 1, 12, tzinfo=UTC)
        lines = [
            {"type": "started", "at": t.isoformat(), "monotonic": 1.0, "udid": SIM},
            {"type": "dropped", "at": t.isoformat(), "monotonic": 2.0, "what": "flow",
             "count": 3, "first": t.isoformat(), "last": (t + timedelta(seconds=5)).isoformat()},
        ]
        (tmp_path / "r" / "events.jsonl").write_text("\n".join(map(json.dumps, lines)) + "\n")
        with TestClient(_app(Sources())) as client:
            trace = client.get("/api/v1/trace", params={
                "recording": str(tmp_path / "r"),
                "since": (t - timedelta(minutes=1)).isoformat()}).json()
        assert trace["flow_window_truncated"] is True and trace["log_window_truncated"] is False
        assert trace["recording"]["stopped"] is False
        assert "3 flow dropped" in trace["recording"]["holes"][0]

    def test_an_unknown_recording_is_404(self, tmp_path):
        with TestClient(_app(Sources())) as client:
            r = client.get("/api/v1/trace", params={"recording": str(tmp_path / "nope")})
        assert r.status_code == 404


class TestTheCli:
    def test_start_prints_the_id_first(self, monkeypatch, capsys):
        monkeypatch.setattr(record_cli, "_call", lambda m, p, b=None: (200, {
            "id": "rec_1", "udid": SIM, "output_dir": "/x", "warnings": ["w"]}))
        assert record_cli.main(["start", "--udid", SIM, "--out", "/x"]) == 0
        out, err = capsys.readouterr()
        assert out.splitlines()[0] == "rec_1" and "warning: w" in err

    def test_start_sends_what_was_asked(self, monkeypatch):
        sent = {}

        def call(method, path, body=None):
            sent.update(method=method, path=path, body=body)
            return 200, {"id": "rec_1", "udid": SIM, "output_dir": "/x"}
        monkeypatch.setattr(record_cli, "_call", call)
        record_cli.main(["start", "--udid", SIM, "--exclude-host", "a.com", "--exclude-host",
                         "b.com", "--kinds", "flows, logs"])
        assert sent["path"] == "/api/v1/recordings" and sent["body"]["exclude_hosts"] == [
            "a.com", "b.com"] and sent["body"]["kinds"] == ["flows", "logs"]

    @pytest.mark.parametrize("answer, code", [
        ({"id": "r", "complete": True, "state": "stopped"}, 0),
        ({"id": "r", "complete": False, "state": "stopped", "dropped": {"flow": 3}}, 3),
        ({"id": "r", "complete": False, "state": "failed"}, 3),
    ])
    def test_stop_require_complete(self, monkeypatch, answer, code):
        monkeypatch.setattr(record_cli, "_call", lambda m, p, b=None: (200, answer))
        assert record_cli.main(["stop", "r", "--require-complete"]) == code

    def test_an_incomplete_stop_without_the_flag_is_zero(self, monkeypatch):
        monkeypatch.setattr(record_cli, "_call", lambda m, p, b=None: (200, {
            "id": "r", "complete": False}))
        assert record_cli.main(["stop", "r"]) == 0

    def test_no_server_is_1_and_a_refusal_is_2(self, monkeypatch, capsys):
        def gone(*a, **k):
            raise ConnectionError("no server answering")
        monkeypatch.setattr(record_cli, "_call", gone)
        assert record_cli.main(["list"]) == 1
        monkeypatch.setattr(record_cli, "_call", lambda m, p, b=None: (400, {"detail": "no"}))
        assert record_cli.main(["start", "--udid", SIM]) == 2
        assert "no" in capsys.readouterr().err

    def test_bad_arguments_are_2(self, capsys):
        assert record_cli.main(["start"]) == 2          # --udid is required
        assert record_cli.main([]) == 2


def test_host_matching():
    assert rec_mod.host_matches("api.example.com", ["example.com"])
    assert rec_mod.host_matches("EXAMPLE.com.", [".example.com"])
    assert not rec_mod.host_matches("badexample.com", ["example.com"])
    assert not rec_mod.host_matches("example.com", [""])


# ── choosing what to collect, and reading back any combination ──────────────


class TestKinds:
    def test_unknown_or_empty_kinds_are_refused(self):
        with pytest.raises(RecordingError, match="kinds must be some of"):
            Filters(kinds=("flows", "video"))
        with pytest.raises(RecordingError):
            Filters(kinds=())

    async def test_only_what_was_chosen_is_subscribed(self, tmp_path):
        """Not filtered out after the fact: a kind not chosen takes no queue,
        so it can never be counted as dropped from this recording."""
        src = Sources()
        manager = src.manager()
        rec = await manager.start(SIM, str(tmp_path / "r"), Filters(kinds=("flows",)))
        assert [kind for kind, _, _ in rec._subs] == ["flow"]
        await manager.stop(rec.id)

    async def test_actions_only(self, tmp_path):
        src = Sources()

        async def feed():
            await src.server.append(_action())
            await src.flows.add(_flow())
            await src.ring.append(_log())

        _, rec = await _record(src, tmp_path / "r", feed, Filters(kinds=("actions",)))
        assert rec.counts == {"action": 1, "flow": 0, "log": 0, "crash": 0}

    async def test_the_kinds_survive_a_restart(self, tmp_path):
        first = Sources().manager()
        rec = await first.start(SIM, str(tmp_path / "r"), Filters(kinds=("flows", "logs")))
        await first.shutdown()
        second = Sources().manager()
        await second.resume_all()
        assert second.get(rec.id).filters.kinds == ("flows", "logs")


class TestReadingEvents:
    async def _recorded(self, tmp_path):
        src = Sources()
        t = _now(-60)

        async def feed():
            await src.server.append(_action("tap", at=t))
            for i in range(5):
                await src.flows.add(_flow(at=t + timedelta(seconds=i), host=f"h{i}.example.com"))
            await src.ring.append(_log(at=t + timedelta(seconds=2)))

        await _record(src, tmp_path / "r", feed)
        return t

    async def test_network_calls_only(self, tmp_path):
        await self._recorded(tmp_path)
        page = rec_mod.read_events(tmp_path / "r", ("flows",))
        kinds = [e["type"] for e in page["events"]]
        assert kinds.count("flow") == 5 and "action" not in kinds and "log" not in kinds
        assert {"started", "stopped"} <= set(kinds), "markers come with every page"

    async def test_any_combination(self, tmp_path):
        await self._recorded(tmp_path)
        kinds = [e["type"] for e in rec_mod.read_events(tmp_path / "r", ("actions", "logs"))[
            "events"]]
        assert kinds.count("action") == 1 and kinds.count("log") == 1 and "flow" not in kinds

    async def test_a_window(self, tmp_path):
        t = await self._recorded(tmp_path)
        page = rec_mod.read_events(tmp_path / "r", ("flows",), since=t + timedelta(seconds=1),
                                   until=t + timedelta(seconds=3))
        hosts = [e["data"]["request"]["host"] for e in page["events"] if e["type"] == "flow"]
        assert hosts == ["h1.example.com", "h2.example.com", "h3.example.com"]

    async def test_pages_join_up_with_nothing_lost_or_repeated(self, tmp_path):
        await self._recorded(tmp_path)
        seen, cursor = [], 0
        while cursor is not None:
            page = rec_mod.read_events(tmp_path / "r", ("flows",), cursor=cursor, limit=2)
            seen += [e["data"]["id"] for e in page["events"] if e["type"] == "flow"]
            cursor = page["next_cursor"]
        assert len(seen) == 5 and len(set(seen)) == 5

    async def test_a_summary_leaves_the_bodies_out(self, tmp_path):
        await self._recorded(tmp_path)
        [flow, *_] = [e["data"] for e in rec_mod.read_events(
            tmp_path / "r", ("flows",), detail="summary")["events"] if e["type"] == "flow"]
        assert set(flow) == {"id", "timestamp", "method", "url", "status", "error", "total_ms"}
        assert flow["method"] == "POST" and flow["status"] == 200

    async def test_one_flow_in_full(self, tmp_path):
        await self._recorded(tmp_path)
        some = next(e["data"]["id"] for e in rec_mod.read_events(tmp_path / "r", ("flows",))[
            "events"] if e["type"] == "flow")
        page = rec_mod.read_events(tmp_path / "r", ("flows",), flow_id=some)
        assert [e["data"]["id"] for e in page["events"]] == [some]
        assert page["events"][0]["data"]["request"]["body"] == '{"email":"a@b.c"}'

    async def test_the_route(self, tmp_path):
        src = Sources()
        with TestClient(_app(src)) as client:
            rid = client.post("/api/v1/recordings", json={
                "udid": SIM, "output_dir": str(tmp_path / "r"), "kinds": ["flows"]}).json()["id"]
            client.portal.call(lambda: src.flows.add(_flow()))
            client.portal.call(_settle)
            client.post(f"/api/v1/recordings/{rid}/stop")
            page = client.get("/api/v1/recordings/events", params={
                "recording": rid, "kinds": "flows", "detail": "summary"}).json()
            assert [e["type"] for e in page["events"]].count("flow") == 1
            assert page["stopped"] is True and page["holes"] == []
            bad = client.get("/api/v1/recordings/events", params={"recording": rid,
                                                                    "kinds": "video"})
            assert bad.status_code == 400
            assert client.post("/api/v1/recordings", json={
                "udid": SIM, "output_dir": str(tmp_path / "x"),
                "kinds": ["video"]}).status_code == 422
