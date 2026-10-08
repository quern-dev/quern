"""An empty log answer must not look like a complete one (#255).

The buffers evict their oldest entries, and until this change nothing said so:
`total: 0` meant "nothing matched" and also "everything that matched is gone".
#313 is the field report -- an agent captured a simulator unfiltered, queried
for faults it knew had fired, got nothing, and concluded quern could not
capture them. The faults had been evicted within seconds.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from server.config import ServerConfig
from server.main import buffer_for, create_app
from server.models import LogEntry, LogLevel, LogSource

HEADERS = {"Authorization": "Bearer test-key-12345"}


def _entry(message="line", *, source=LogSource.SIMULATOR, level=LogLevel.INFO, ago_s=0.0):
    return LogEntry(
        id=uuid.uuid4().hex,
        timestamp=datetime.now(UTC) - timedelta(seconds=ago_s),
        device_id="SIM-A", process="MyApp", level=level,
        message=message, source=source,
    )


@pytest.fixture
def app():
    config = ServerConfig(api_key="test-key-12345", ring_buffer_size=5)
    return create_app(config=config, enable_oslog=False, enable_crash=False, enable_proxy=False)


async def _get(app, path, **params):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(path, headers=HEADERS, params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _flood(app, count, **kwargs):
    """Append `count` entries, oldest first, ending just before now."""
    for i in range(count):
        await app.state.ring_buffer.append(_entry(ago_s=(count - i) * 0.01, **kwargs))


class TestQueryLogs:
    async def test_an_evicted_match_reads_as_truncated_not_absent(self, app):
        """The #313 case, reduced: the fault arrived, the firehose pushed it
        out, and the search found nothing."""
        await app.state.ring_buffer.append(
            _entry("Modifying state during view update", level=LogLevel.FAULT, ago_s=10),
        )
        await _flood(app, 5)

        data = await _get(app, "/api/v1/logs/query", search="Modifying state")

        assert data["total"] == 0
        assert data["truncated"] is True
        assert data["complete_after"] is not None

    async def test_a_quiet_buffer_is_a_true_negative(self, app):
        await _flood(app, 3)

        data = await _get(app, "/api/v1/logs/query", search="Modifying state")

        assert data["total"] == 0
        assert data["truncated"] is False
        assert data["complete_after"] is None

    async def test_another_sources_eviction_does_not_flag_this_one(self, app):
        await app.state.ring_buffer.append(_entry(source=LogSource.BUILD, ago_s=10))
        await _flood(app, 5)  # evicts the build line only

        data = await _get(app, "/api/v1/logs/query", source="simulator")

        assert data["truncated"] is False

    async def test_shed_debug_lines_do_not_flag_an_errors_query(self, app):
        await _flood(app, 10, level=LogLevel.DEBUG)

        errors_only = await _get(app, "/api/v1/logs/query", level="error")
        everything = await _get(app, "/api/v1/logs/query")

        assert errors_only["truncated"] is False
        assert everything["truncated"] is True

    async def test_a_window_after_every_eviction_is_whole(self, app):
        await _flood(app, 10)
        through = datetime.fromisoformat(
            (await _get(app, "/api/v1/logs/query"))["complete_after"],
        )

        data = await _get(
            app, "/api/v1/logs/query",
            since=(through + timedelta(microseconds=1)).isoformat(),
        )

        assert data["truncated"] is False


class TestTail:
    """A tail asks for the newest N, not a window."""

    async def test_a_full_tail_newer_than_every_eviction_is_whole(self, app):
        await _flood(app, 10)

        data = await _get(app, "/api/v1/logs/query", tail="true", limit=3)

        assert len(data["entries"]) == 3
        assert data["truncated"] is False, (
            "tail_logs would read 'truncated' on every call to a busy server"
        )

    async def test_a_full_tail_is_whole_even_when_timestamps_arrive_out_of_order(self, app):
        """A physical iPhone's lines arrive out of timestamp order. One buffer
        evicts in arrival order, so the newest N *arrivals* are all present
        whatever their stamps say -- but a rule comparing timestamps reported
        this full tail as truncated. Found live on an iPhone 12."""
        ring = app.state.ring_buffer
        await ring.append(_entry("late-stamped, arrived first", ago_s=0))  # evicted
        for i in range(5):
            await ring.append(_entry(f"earlier-stamped {i}", ago_s=30 - i))

        data = await _get(app, "/api/v1/logs/query", source="simulator", tail="true", limit=3)

        assert len(data["entries"]) == 3
        assert data["truncated"] is False

    async def test_a_merged_tail_tied_with_an_eviction_is_not_whole(self, app):
        """Merged across buffers the tail is ranked by timestamp, and an
        evicted entry stamped at the same instant as the oldest returned one
        could have ranked among them. Review mutation M3 (`>` to `>=`)
        survived without this."""
        at = datetime.now(UTC) - timedelta(seconds=5)
        ring = app.state.ring_buffer
        for i in range(6):   # capacity 5: the first is evicted
            entry = _entry(f"tied {i}")
            entry.timestamp = at
            await ring.append(entry)

        data = await _get(app, "/api/v1/logs/query", tail="true", limit=3)  # merged

        assert len(data["entries"]) == 3
        assert data["truncated"] is True

    async def test_a_short_tail_after_eviction_is_truncated(self, app):
        """Fewer matches than asked for means older matches may have been
        evicted -- the answer is not "only this many ever happened"."""
        await _flood(app, 10)

        data = await _get(app, "/api/v1/logs/query", tail="true", limit=50)

        assert len(data["entries"]) == 5
        assert data["truncated"] is True


class TestErrors:
    async def test_shed_debug_lines_lose_no_errors(self, app):
        await _flood(app, 10, level=LogLevel.DEBUG)

        data = await _get(app, "/api/v1/logs/errors")

        assert data["total"] == 0
        assert data["truncated"] is False

    async def test_an_evicted_error_is_reported(self, app):
        await app.state.ring_buffer.append(_entry(level=LogLevel.ERROR, ago_s=10))
        await _flood(app, 5)

        data = await _get(app, "/api/v1/logs/errors")

        assert data["total"] == 0
        assert data["truncated"] is True


    async def test_the_newest_errors_are_kept_when_there_are_more_than_asked(self, app):
        """It sliced the front of an oldest-first list, so over the limit it
        returned the stale errors and cut the one that had just happened."""
        for i in range(4):
            await app.state.ring_buffer.append(
                _entry(f"error {i}", level=LogLevel.ERROR, ago_s=10 - i),
            )

        data = await _get(app, "/api/v1/logs/errors", limit=2)

        assert data["total"] == 4
        assert [e["message"] for e in data["entries"]] == ["error 3", "error 2"]


class TestSummary:
    async def test_truncation_is_in_the_prose_as_well_as_the_field(self, app):
        """The prose is what a reader takes in first."""
        await _flood(app, 10)

        data = await _get(app, "/api/v1/logs/summary", window="5m")

        assert data["truncated"] is True
        assert data["summary"].startswith("Entries in this window were evicted")

    async def test_the_cursor_path_measures_from_the_cursor(self, app):
        """Entries evicted after the cursor are exactly what a delta summary
        is missing. Review mutation M6 -- measuring from `now` instead --
        survived the full suite."""
        await _flood(app, 3)
        cursor = (await _get(app, "/api/v1/logs/summary", window="5m"))["cursor"]
        for i in range(10):      # capacity 5: entries after the cursor are evicted
            await app.state.ring_buffer.append(_entry(f"after {i}"))

        data = await _get(app, "/api/v1/logs/summary", since_cursor=cursor)

        assert data["truncated"] is True

    async def test_a_whole_window_says_nothing_extra(self, app):
        await _flood(app, 3)

        data = await _get(app, "/api/v1/logs/summary", window="5m")

        assert data["truncated"] is False
        assert "evicted" not in data["summary"]


class TestSources:
    async def test_each_buffer_reports_what_it_lost(self, app):
        await _flood(app, 8)

        data = await _get(app, "/api/v1/logs/sources")

        assert set(data["buffers"]) == {"logs", "server", "crashes"}
        logs = data["buffers"]["logs"]
        assert logs["capacity"] == 5
        assert logs["appended"] == 8
        assert logs["evicted"] == 3
        assert logs["evicted_by_source"] == {"simulator": 3}


class TestCrashBuffer:
    """Crash reports shared a budget with the firehose, so a chatty app could
    evict the crash that explained why it died."""

    def test_crashes_are_routed_to_their_own_buffer(self, app):
        logs, crashes = app.state.ring_buffer, app.state.crash_buffer

        assert buffer_for(_entry(source=LogSource.CRASH), logs=logs, crashes=crashes) is crashes
        assert buffer_for(_entry(source=LogSource.SIMULATOR), logs=logs, crashes=crashes) is logs

    async def test_a_flood_cannot_evict_a_crash(self, app):
        crash = _entry("MyApp crashed", source=LogSource.CRASH, level=LogLevel.FAULT, ago_s=10)
        logs, crashes = app.state.ring_buffer, app.state.crash_buffer
        await buffer_for(crash, logs=logs, crashes=crashes).append(crash)
        await _flood(app, 50)

        data = await _get(app, "/api/v1/logs/errors")

        assert [e["message"] for e in data["entries"]] == ["MyApp crashed"]

    async def test_a_crash_query_reads_the_crash_buffer(self, app):
        await app.state.crash_buffer.append(
            _entry("MyApp crashed", source=LogSource.CRASH, level=LogLevel.FAULT),
        )

        by_source = await _get(app, "/api/v1/logs/query", source="crash")
        unfiltered = await _get(app, "/api/v1/logs/query")

        assert [e["message"] for e in by_source["entries"]] == ["MyApp crashed"]
        assert "MyApp crashed" in [e["message"] for e in unfiltered["entries"]]


def test_tail_logs_shapes_its_answer_with_the_tested_function():
    """Wiring only. What the shaping does is tested by running it, in
    `mcp/test/log-responses.test.mjs` (CI: `npm test`). This used to check
    the TypeScript by text instead, and passed three times with the
    forwarding it claimed to cover deleted or forced to false -- a text match
    cannot tell forwarding from its absence. What text *can* confirm is that
    `tail_logs` hands its answer to the function those tests run."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "mcp" / "src" / "tools" / "logs.ts").read_text()
    start = source.index('registerTool("tail_logs"')
    block = source[start:source.index("registerTool(", start + 1)]

    assert "JSON.stringify(tailLogsResult(data)" in block


async def test_starting_capture_with_a_preset_reports_what_it_purged(app, monkeypatch):
    """A preset purges entries from a window already captured. That is not
    eviction, so no `truncated` flag will ever mention them; the response is
    the only place it can be seen, and the count used to be discarded."""
    from types import SimpleNamespace

    from server.processing.ingestion_filter import IngestionFilter
    from server.sources.simulator_log import SimulatorLogAdapter

    async def _resolve(udid):
        return "SIM-A"

    async def _no_spawn(self):
        self._running = True

    app.state.device_controller = SimpleNamespace(resolve_udid=_resolve)
    app.state.deduplicator = SimpleNamespace(process=lambda entry: None)
    app.state.ingestion_filter = IngestionFilter()
    monkeypatch.setattr(SimulatorLogAdapter, "start", _no_spawn)

    await app.state.ring_buffer.append(_entry("HangTracer noise"))   # the preset excludes it
    await app.state.ring_buffer.append(_entry("an app line"))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/device/logging/start", headers=HEADERS,
            json={"udid": "SIM-A", "preset": "simulator-quiet"},
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["purged"] == 1


async def test_two_starts_for_one_device_launch_one_capture(app, monkeypatch):
    """Starting logcat takes at least half a second, and the endpoint checked
    for a running adapter before that and registered one after. Two calls in
    the window both launched logcat; one was orphaned. Second review,
    reproduced with a fake adb: two "started" responses, one process left
    after stopping everything registered."""
    import asyncio
    from types import SimpleNamespace

    from server.sources.logcat import LogcatAdapter

    async def _resolve(udid):
        return udid

    starts = []

    async def _slow_start(self):
        starts.append(self.serial)
        await asyncio.sleep(0.3)      # the early-exit check, and then some
        self._running = True

    app.state.device_controller = SimpleNamespace(
        resolve_udid=_resolve, _is_android=lambda u: True, _is_physical=lambda u: False,
    )
    app.state.deduplicator = SimpleNamespace(process=lambda entry: None)
    monkeypatch.setattr(LogcatAdapter, "start", _slow_start)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async def start():
            return await client.post(
                "/api/v1/device/logging/device/start", headers=HEADERS,
                json={"udid": "emulator-5554"},
            )
        # Bounded: a lock regression that deadlocks must fail, not hang.
        first, second = await asyncio.wait_for(asyncio.gather(start(), start()), timeout=10)

    assert sorted([first.json()["status"], second.json()["status"]]) == [
        "already_running", "started",
    ]
    assert starts == ["emulator-5554"], "a second logcat was launched"


async def test_starting_device_capture_with_a_preset_reports_what_it_purged(app, monkeypatch):
    """Survivor P20: only the simulator endpoint was tested."""
    from types import SimpleNamespace

    from server.processing.ingestion_filter import IngestionFilter
    from server.sources.device_log import PhysicalDeviceLogAdapter

    async def _resolve(udid):
        return "PHONE"

    async def _no_spawn(self):
        self._running = True

    app.state.device_controller = SimpleNamespace(
        resolve_udid=_resolve, _is_android=lambda u: False, _is_physical=lambda u: True,
    )
    app.state.deduplicator = SimpleNamespace(process=lambda entry: None)
    app.state.ingestion_filter = IngestionFilter()
    monkeypatch.setattr(PhysicalDeviceLogAdapter, "start", _no_spawn)

    noise = _entry("pairing chatter", source=LogSource.DEVICE)
    noise.process = "remotepairingdeviced"                 # the preset excludes it
    await app.state.ring_buffer.append(noise)
    await app.state.ring_buffer.append(_entry("an app line", source=LogSource.DEVICE))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/device/logging/device/start", headers=HEADERS,
            json={"udid": "PHONE", "preset": "device-quiet"},
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["purged"] == 1


async def test_two_starts_for_one_simulator_launch_one_capture(app, monkeypatch):
    """The simulator endpoint has the same check-then-register shape and the
    same lock; without a test the lock could go unnoticed."""
    import asyncio
    from types import SimpleNamespace

    from server.sources.simulator_log import SimulatorLogAdapter

    async def _resolve(udid):
        return udid

    starts = []

    async def _slow_start(self):
        starts.append(self.udid)
        await asyncio.sleep(0.3)
        self._running = True

    app.state.device_controller = SimpleNamespace(resolve_udid=_resolve)
    app.state.deduplicator = SimpleNamespace(process=lambda entry: None)
    monkeypatch.setattr(SimulatorLogAdapter, "start", _slow_start)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async def start():
            return await client.post(
                "/api/v1/device/logging/start", headers=HEADERS, json={"udid": "SIM-A"},
            )
        # Bounded: a lock regression that deadlocks must fail, not hang.
        first, second = await asyncio.wait_for(asyncio.gather(start(), start()), timeout=10)

    assert sorted([first.json()["status"], second.json()["status"]]) == [
        "already_running", "started",
    ]
    assert starts == ["SIM-A"]


async def test_a_start_during_a_stop_leaves_the_new_capture_registered(app, monkeypatch):
    """CodeRabbit on #319: stop() marks the adapter not running before it
    finishes, so a start in that gap registered a new capture which the stop,
    resuming, deleted from the registry -- a running logcat the API could no
    longer stop. The stop now holds the same per-device lock as the start."""
    import asyncio
    from types import SimpleNamespace

    from server.sources.logcat import LogcatAdapter

    async def _resolve(udid):
        return udid

    async def _start(self):
        self._running = True

    async def _slow_stop(self):
        self._running = False
        await asyncio.sleep(0.3)      # shutting logcat down takes a moment

    app.state.device_controller = SimpleNamespace(
        resolve_udid=_resolve, _is_android=lambda u: True, _is_physical=lambda u: False,
    )
    app.state.deduplicator = SimpleNamespace(process=lambda entry: None)
    monkeypatch.setattr(LogcatAdapter, "start", _start)
    monkeypatch.setattr(LogcatAdapter, "stop", _slow_stop)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        body = {"udid": "emulator-5554"}
        await client.post("/api/v1/device/logging/device/start", headers=HEADERS, json=body)

        async def start_soon():
            await asyncio.sleep(0.05)   # lands while the stop is shutting down
            return await client.post(
                "/api/v1/device/logging/device/start", headers=HEADERS, json=body,
            )

        stop, start = await asyncio.wait_for(asyncio.gather(
            client.post("/api/v1/device/logging/device/stop", headers=HEADERS, json=body),
            start_soon(),
        ), timeout=10)   # a lock regression that deadlocks must fail, not hang

    assert stop.json()["status"] == "stopped"
    assert start.json()["status"] == "started"
    registered = app.state.device_log_adapters.get("emulator-5554")
    assert registered is not None and registered.is_running, (
        "the new capture is running but no longer registered, so it cannot be stopped"
    )


async def _start_with(app, monkeypatch, start):
    from types import SimpleNamespace

    from server.sources.simulator_log import SimulatorLogAdapter

    async def _resolve(udid):
        return udid

    app.state.device_controller = SimpleNamespace(resolve_udid=_resolve)
    app.state.deduplicator = SimpleNamespace(process=lambda entry: None)
    monkeypatch.setattr(SimulatorLogAdapter, "start", start)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/api/v1/device/logging/start", headers=HEADERS, json={"udid": "SIM-A"},
        )


async def test_a_stream_that_dies_at_start_is_a_409_with_its_reason(app, monkeypatch):
    """A simulator that is not booted: simctl exits at once. That answered
    "started", and the error surfaced only later on the adapter's status."""
    async def _dies(self):
        self.exited_at_start = True
        self._error = ("simctl log stream exited (149): Process spawn via launchd "
                       "failed because device is not booted.")

    resp = await _start_with(app, monkeypatch, _dies)
    assert resp.status_code == 409, resp.text
    assert "not booted" in resp.json()["detail"]
    assert "SIM-A" not in app.state.sim_log_adapters, "registered a dead capture"


async def test_failing_to_spawn_stays_a_500(app, monkeypatch):
    async def _cannot_spawn(self):
        self._error = "xcrun not found. Install Xcode Command Line Tools."

    resp = await _start_with(app, monkeypatch, _cannot_spawn)
    assert resp.status_code == 500, resp.text


class _Capture:
    """A running capture whose restart does what the test says."""

    def __init__(self, restarts: bool):
        self.adapter_id = "simlog-SIM-A"
        self.is_running = True
        self._error = None
        self._restarts = restarts

    async def reconfigure(self, process_filter=None):
        if self._restarts is None:
            self.is_running = False
            raise RuntimeError("could not build the command")
        if not self._restarts:
            self.is_running = False
            self._error = "simctl log stream exited (149): device is not booted."


async def _filter(app, capture):
    from server.processing.ingestion_filter import IngestionFilter

    app.state.ingestion_filter = IngestionFilter()
    app.state.sim_log_adapters["SIM-A"] = capture
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/api/v1/logs/filter", headers=HEADERS, json={"process": "App", "source": "simulator"},
        )


async def test_a_filter_whose_capture_fails_to_restart_says_so(app):
    """The restart can now fail visibly -- the simulator shut down since. It
    answered "applied" and adapter_restarted: true over a dead capture."""
    resp = await _filter(app, _Capture(restarts=False))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "applied_capture_failed"
    assert body["adapter_restarted"] is False
    assert body["restart_errors"] == [{
        "adapter_id": "simlog-SIM-A",
        "error": "simctl log stream exited (149): device is not booted.",
    }]


async def test_a_filter_whose_capture_restarts_is_applied(app):
    resp = await _filter(app, _Capture(restarts=True))
    body = resp.json()
    assert body["status"] == "applied" and body["adapter_restarted"] is True
    assert body["restart_errors"] == []



async def test_a_restart_that_raises_is_reported_not_a_500(app):
    """A start can raise before its own handling; that is this capture's
    failure, reported with the rest, not the whole request failing."""
    resp = await _filter(app, _Capture(restarts=None))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "applied_capture_failed"
    assert "could not build the command" in body["restart_errors"][0]["error"]
