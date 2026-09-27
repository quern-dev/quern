"""Summary cursors follow arrival order, not timestamps (#317).

A summary's cursor was the timestamp of the newest entry it had seen, and the
next delta read entries stamped after it. Entries do not arrive in timestamp
order, so anything that arrived late with an earlier stamp fell behind the
cursor and no delta ever returned it:

- a device clock running ahead moved the cursor past everything stamped
  correctly afterwards (reproduced in the review of #255);
- a crash report is stamped when the crash happened and arrives later;
- a flow is stamped when its request *started* and stored when it finished,
  so every request still running when a summary was taken was skipped.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from server.config import ServerConfig
from server.main import create_app
from server.models import LogEntry, LogLevel, LogSource
from server.processing.summarizer import make_cursor
from server.proxy.flow_store import FlowStore
from tests.test_flow_store import _make_flow

HEADERS = {"Authorization": "Bearer test-key-12345"}


def _entry(message, *, ago_s=0.0, level=LogLevel.INFO, source=LogSource.SIMULATOR):
    return LogEntry(
        id=uuid.uuid4().hex, timestamp=datetime.now(UTC) - timedelta(seconds=ago_s),
        process="MyApp", level=level, message=message, source=source,
    )


def _flow(flow_id, *, ago_s=0.0):
    return _make_flow(flow_id=flow_id, timestamp=datetime.now(UTC) - timedelta(seconds=ago_s))


@pytest.fixture
def app():
    app = create_app(
        config=ServerConfig(api_key="test-key-12345", ring_buffer_size=50),
        enable_oslog=False, enable_crash=False, enable_proxy=False,
    )
    app.state.flow_store = FlowStore(max_size=50)
    return app


async def _get(app, path, **params):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, headers=HEADERS, params=params)


async def _log_summary(app, cursor=None):
    params = {"since_cursor": cursor} if cursor else {}
    resp = await _get(app, "/api/v1/logs/summary", **params)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _flow_summary(app, cursor=None):
    params = {"since_cursor": cursor} if cursor else {}
    return await _get(app, "/api/v1/proxy/flows/summary", **params)


class TestLogSummary:
    async def test_a_future_stamped_entry_does_not_hide_what_comes_after(self, app):
        """The #317 repro: one entry from a clock two minutes ahead, then a
        correctly stamped error. The timestamp cursor answered "no entries"."""
        ring = app.state.ring_buffer
        await ring.append(_entry("from a clock ahead", ago_s=-120))
        cursor = (await _log_summary(app))["cursor"]
        await ring.append(_entry("the error", level=LogLevel.ERROR))

        delta = await _log_summary(app, cursor)

        assert delta["total_count"] == 1
        assert delta["error_count"] == 1
        assert delta["cursor_reset"] is False

    async def test_a_late_crash_report_is_in_the_next_delta(self, app):
        """Stamped when the crash happened, arriving after the summary."""
        await app.state.ring_buffer.append(_entry("before"))
        cursor = (await _log_summary(app))["cursor"]
        await app.state.crash_buffer.append(
            _entry("MyApp crashed", ago_s=30, level=LogLevel.FAULT, source=LogSource.CRASH),
        )

        delta = await _log_summary(app, cursor)

        assert delta["total_count"] == 1 and delta["error_count"] == 1

    async def test_consecutive_deltas_neither_skip_nor_repeat(self, app):
        ring = app.state.ring_buffer
        seen = 0
        cursor = (await _log_summary(app))["cursor"]
        for batch in (3, 1, 4):
            for i in range(batch):
                await ring.append(_entry(f"b{batch}-{i}", ago_s=(i % 2) * 60))  # out of order
            delta = await _log_summary(app, cursor)
            assert delta["total_count"] == batch
            seen += delta["total_count"]
            cursor = delta["cursor"]
        assert seen == 8
        assert (await _log_summary(app, cursor))["total_count"] == 0

    async def test_an_eviction_after_the_cursor_is_truncation(self, app):
        cursor = (await _log_summary(app))["cursor"]
        for i in range(60):                           # capacity 50
            await app.state.ring_buffer.append(_entry(f"e{i}"))

        delta = await _log_summary(app, cursor)

        assert delta["truncated"] is True

    async def test_an_eviction_before_the_cursor_is_not(self, app):
        for i in range(60):
            await app.state.ring_buffer.append(_entry(f"e{i}"))
        cursor = (await _log_summary(app))["cursor"]
        await app.state.ring_buffer.append(_entry("new"))   # evicts one from before the cursor

        delta = await _log_summary(app, cursor)

        assert delta["total_count"] == 1
        assert delta["truncated"] is False

    async def test_a_cursor_from_before_a_restart_is_not_read_as_nothing_new(self, app):
        """Arrival numbers restart with the server; a cursor carrying a
        number from a previous run must not be compared against this one's."""
        other = create_app(
            config=ServerConfig(api_key="test-key-12345"),
            enable_oslog=False, enable_crash=False, enable_proxy=False,
        )
        for i in range(5):
            await other.state.ring_buffer.append(_entry(f"old{i}"))
        stale = (await _log_summary(other))["cursor"]
        await app.state.ring_buffer.append(_entry("after the restart"))

        delta = await _log_summary(app, stale)

        assert delta["cursor_reset"] is True
        assert delta["total_count"] == 1              # the window, not "nothing new"

    async def test_a_junk_cursor_says_so(self, app):
        await app.state.ring_buffer.append(_entry("x"))
        delta = await _log_summary(app, "not-a-cursor")
        assert delta["cursor_reset"] is True

    async def test_an_old_timestamp_cursor_still_works_and_hands_back_a_new_one(self, app):
        ring = app.state.ring_buffer
        await ring.append(_entry("old", ago_s=10))
        old_cursor = make_cursor(datetime.now(UTC) - timedelta(seconds=5))
        await ring.append(_entry("new"))

        delta = await _log_summary(app, old_cursor)

        assert delta["total_count"] == 1
        assert delta["cursor_reset"] is False
        assert delta["cursor"].startswith("a_")


class TestFlowSummary:
    async def test_a_request_that_finishes_after_the_summary_is_in_the_next_delta(self, app):
        """Stamped when it started -- before the summary -- and stored when it
        finished, after. The timestamp cursor skipped it for good."""
        store = app.state.flow_store
        await store.add(_flow("quick"))
        cursor = (await _flow_summary(app)).json()["cursor"]
        await store.add(_flow("long", ago_s=20))      # started 20s ago, finished now

        delta = (await _flow_summary(app, cursor)).json()

        assert delta["total_flows"] == 1

    async def test_an_update_to_a_flow_is_a_new_arrival(self, app):
        store = app.state.flow_store
        await store.add(_flow("f1"))
        cursor = (await _flow_summary(app)).json()["cursor"]
        await store.add(_flow("f1"))                  # the response completed

        delta = (await _flow_summary(app, cursor)).json()

        assert delta["total_flows"] == 1

    async def test_an_eviction_after_the_cursor_is_truncation(self, app):
        cursor = (await _flow_summary(app)).json()["cursor"]
        for i in range(60):
            await app.state.flow_store.add(_flow(f"f{i}"))

        delta = (await _flow_summary(app, cursor)).json()

        assert delta["truncated"] is True

    async def test_an_eviction_before_the_cursor_is_not(self, app):
        for i in range(60):
            await app.state.flow_store.add(_flow(f"f{i}"))
        cursor = (await _flow_summary(app)).json()["cursor"]
        await app.state.flow_store.add(_flow("new"))

        delta = (await _flow_summary(app, cursor)).json()

        assert delta["total_flows"] == 1 and delta["truncated"] is False

    async def test_a_cursor_from_before_a_restart_is_reset(self, app):
        other = FlowStore(max_size=50)
        await other.add(_flow("old"))
        from server.storage.arrival import make_arrival_cursor
        stale = make_arrival_cursor(other.clock, other.clock.now)
        await app.state.flow_store.add(_flow("after"))

        delta = (await _flow_summary(app, stale)).json()

        assert delta["cursor_reset"] is True and delta["total_flows"] == 1

    async def test_a_junk_cursor_is_still_refused(self, app):
        resp = await _flow_summary(app, "not-a-cursor")
        assert resp.status_code == 400
