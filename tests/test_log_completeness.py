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


def test_tail_logs_forwards_every_completeness_field():
    """`tail_logs` rebuilds its response in the MCP wrapper instead of passing
    it through, so a field the server adds is dropped there unless named.
    That would deliver the signal to every caller except the one that reaches
    for `tail_logs` first -- an agent over MCP. Tied to the model, so a new
    field fails here until the wrapper carries it."""
    from pathlib import Path

    from server.models import Completeness

    source = (Path(__file__).resolve().parents[1] / "mcp" / "src" / "tools" / "logs.ts").read_text()
    start = source.index('registerTool("tail_logs"')
    block = source[start:source.index("registerTool(", start + 1)]

    # The forwarding expression, not the bare name: the tool's description
    # mentions `truncated: true` too, and matching that let this pass with
    # the forwarding deleted -- found by mutating it.
    for field in Completeness.model_fields:
        assert f"{field}: data.{field}" in block, f"tail_logs drops `{field}`"
