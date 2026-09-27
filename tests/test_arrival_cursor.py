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


def _flow_on(flow_id, udid):
    flow = _flow(flow_id)
    flow.simulator_udid = udid
    return flow


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

    async def test_losing_the_entry_the_cursor_points_at_is_not_truncation(self, app):
        """That entry was in the previous summary. Only arrivals *after* the
        cursor count; the boundary is exact, and `>=` there would flag every
        delta whose first eviction was the last thing already seen."""
        ring = app.state.ring_buffer
        for i in range(50):                           # capacity 50: seqs 1..50
            await ring.append(_entry(f"a{i}"))
        cursor = (await _log_summary(app))["cursor"]
        for i in range(50):                           # evicts 1..50, exactly up to the cursor
            await ring.append(_entry(f"b{i}"))

        delta = await _log_summary(app, cursor)

        assert delta["total_count"] == 50
        assert delta["truncated"] is False

    async def test_a_purge_between_summaries_keeps_the_delta_exact(self, app):
        """A filter change purges the buffer. The arrival numbers must be
        filtered with the entries, or they fall out of step."""
        ring = app.state.ring_buffer
        await ring.append(_entry("noise before"))
        cursor = (await _log_summary(app))["cursor"]
        await ring.append(_entry("noise after"))
        await ring.append(_entry("kept after"))
        await ring.purge(lambda e: not e.message.startswith("noise"))

        delta = await _log_summary(app, cursor)

        assert delta["total_count"] == 1

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

    async def test_a_server_log_after_the_cursor_is_in_the_delta(self, app):
        """The server buffer shares the log clock. On a clock of its own its
        numbers start low, fall behind the cursor, and every server entry
        after it is skipped (review: M1 survived the suite)."""
        for i in range(10):
            await app.state.ring_buffer.append(_entry(f"app{i}"))
        cursor = (await _log_summary(app))["cursor"]
        await app.state.server_buffer.append(_entry("quern said", source=LogSource.SERVER))

        delta = await _log_summary(app, cursor)

        assert delta["total_count"] == 1

    async def test_a_cursor_ahead_of_the_clock_is_reset_not_read_as_nothing(self, app):
        """Only a mangled or invented cursor can be ahead of the clock; read
        as-is it answered "nothing new" with every flag clean (review)."""
        from server.storage.arrival import make_arrival_cursor

        for i in range(3):
            await app.state.ring_buffer.append(_entry(f"e{i}", level=LogLevel.ERROR))
        ahead = make_arrival_cursor(app.state.ring_buffer.clock, 10**12)

        delta = await _log_summary(app, ahead)

        assert delta["cursor_reset"] is True
        assert delta["error_count"] == 3                # the window, not "nothing"

    async def test_a_delta_that_lost_entries_says_so_in_delta_terms(self, app):
        cursor = (await _log_summary(app))["cursor"]
        for i in range(60):
            await app.state.ring_buffer.append(_entry(f"e{i}"))

        delta = await _log_summary(app, cursor)

        assert delta["summary"].startswith(
            "Entries that arrived since the last summary were evicted",
        )

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

    async def test_another_devices_eviction_does_not_flag_a_filtered_delta(self, app):
        """The review's regression: the delta check used the store-wide
        number, so an agent polling one simulator while another device
        flooded the store got `truncated` on every delta -- the always-on
        flag #318 removed from the window path."""
        store = FlowStore(max_size=5)
        app.state.flow_store = store
        for i in range(5):
            await store.add(_flow_on(f"b{i}", "SIM-B"))
        cursor = (await _get(app, "/api/v1/proxy/flows/summary",
                             simulator_udid="SIM-A")).json()["cursor"]
        for i in range(6):                            # evicts SIM-B flows from after the cursor
            await store.add(_flow_on(f"b2-{i}", "SIM-B"))
        await store.add(_flow_on("mine", "SIM-A"))

        mine = (await _get(app, "/api/v1/proxy/flows/summary",
                           simulator_udid="SIM-A", since_cursor=cursor)).json()
        theirs = (await _get(app, "/api/v1/proxy/flows/summary",
                             simulator_udid="SIM-B", since_cursor=cursor)).json()

        assert mine["total_flows"] == 1 and mine["truncated"] is False
        assert theirs["truncated"] is True

    async def test_losing_the_flow_the_cursor_points_at_is_not_truncation(self, app):
        """The flow half of the boundary test (review: M4/M15 survived)."""
        store = FlowStore(max_size=5)
        app.state.flow_store = store
        for i in range(5):
            await store.add(_flow(f"a{i}"))
        cursor = (await _flow_summary(app)).json()["cursor"]
        for i in range(5):                            # evicts exactly up to the cursor
            await store.add(_flow(f"b{i}"))

        delta = (await _flow_summary(app, cursor)).json()

        assert delta["total_flows"] == 5 and delta["truncated"] is False

    async def test_a_cursor_ahead_of_the_clock_is_reset(self, app):
        from server.storage.arrival import make_arrival_cursor

        await app.state.flow_store.add(_flow("f"))
        ahead = make_arrival_cursor(app.state.flow_store.clock, 10**12)

        delta = (await _flow_summary(app, ahead)).json()

        assert delta["cursor_reset"] is True and delta["total_flows"] == 1

    async def test_an_old_timestamp_cursor_still_works(self, app):
        """The flow half of the compatibility claim (review: M19 survived)."""
        store = app.state.flow_store
        await store.add(_flow("old", ago_s=10))
        old_cursor = make_cursor(datetime.now(UTC) - timedelta(seconds=5))
        await store.add(_flow("new"))

        delta = (await _flow_summary(app, old_cursor)).json()

        assert delta["total_flows"] == 1
        assert delta["cursor"].startswith("a_")

    async def test_a_junk_cursor_is_still_refused(self, app):
        resp = await _flow_summary(app, "not-a-cursor")
        assert resp.status_code == 400


class TestInterleaving:
    """No store holds its lock across an await today, so an append cannot land
    mid-summary on its own; these tests force it. They are what keeps "no
    gaps, no repeats" true if a lock is ever held across an await -- the
    snapshot taken before reading is otherwise invisible (review of #317:
    removing it survived the suite)."""

    @staticmethod
    def _append_during(read_from, write_to, entry):
        """Make `read_from.entries_between` append `entry` to `write_to` after
        it has read, the way a concurrent arrival would."""
        original = read_from.entries_between
        fired = []

        async def interleaved(after, upto):
            result = await original(after, upto)
            if not fired:
                fired.append(True)
                await write_to.append(entry)
            return result

        read_from.entries_between = interleaved

    async def test_an_arrival_into_a_buffer_not_yet_read_is_not_counted_twice(self, app):
        """Logs are read before crashes. A crash arriving between the two reads
        would, without the snapshot, be in this delta and the next."""
        cursor = (await _log_summary(app))["cursor"]
        self._append_during(
            app.state.ring_buffer, app.state.crash_buffer,
            _entry("crashed mid-read", level=LogLevel.FAULT, source=LogSource.CRASH),
        )

        first = await _log_summary(app, cursor)
        second = await _log_summary(app, first["cursor"])

        assert first["total_count"] + second["total_count"] == 1, "counted twice"
        assert second["total_count"] == 1                # it belongs to the next delta

    async def test_an_arrival_into_a_buffer_already_read_is_not_skipped(self, app):
        """Crashes are read last. A log line arriving then has missed this
        delta's read of its buffer; a cursor taken after reading would put it
        behind the cursor, and no delta would ever return it."""
        cursor = (await _log_summary(app))["cursor"]
        self._append_during(
            app.state.crash_buffer, app.state.ring_buffer, _entry("arrived mid-read"),
        )

        first = await _log_summary(app, cursor)
        second = await _log_summary(app, first["cursor"])

        assert first["total_count"] + second["total_count"] == 1, "skipped"

    async def test_a_flow_finishing_mid_read_is_in_exactly_one_delta(self, app):
        store = app.state.flow_store
        cursor = (await _flow_summary(app)).json()["cursor"]
        original = store.flows_between

        async def interleaved(after, upto):
            store.flows_between = original              # once
            await store.add(_flow("finished mid-read"))
            return await original(after, upto)

        store.flows_between = interleaved

        first = (await _flow_summary(app, cursor)).json()
        second = (await _flow_summary(app, first["cursor"])).json()

        assert first["total_flows"] + second["total_flows"] == 1


async def test_clearing_a_buffer_between_summaries_keeps_the_delta_exact(app):
    """`clear()` has no production caller, so nothing exercised it: a clear
    that forgot the arrival numbers would put them out of step with the
    entries and break the next read (review of #317)."""
    ring = app.state.ring_buffer
    await ring.append(_entry("before"))
    cursor = (await _log_summary(app))["cursor"]
    await ring.append(_entry("after, then cleared"))
    await ring.clear()
    await ring.append(_entry("after the clear"))

    delta = await _log_summary(app, cursor)

    assert delta["total_count"] == 1
