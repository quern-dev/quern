"""A trace that lost the start of its window must say so."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from server.api.trace import get_trace
from server.models import LogEntry, LogLevel, LogSource
from server.storage.ring_buffer import RingBuffer

BASE = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)


def _entry(at_s):
    return LogEntry(
        id=uuid.uuid4().hex, timestamp=BASE + timedelta(seconds=at_s),
        device_id="SIM-A", process="MyApp", level=LogLevel.INFO,
        message="x", source=LogSource.SIMULATOR,
    )


async def _call(ring, since):
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        server_buffer=RingBuffer(max_size=10), ring_buffer=ring,
        flow_store=None, proxy_adapter=None,
    )))
    return await get_trace(request=request, since=since, udid=None, limit=100)


class TestSilentEvictionIsReported:
    """The buffer is a deque with a maxlen and eviction counts nothing, so an
    incomplete trace is indistinguishable from a quiet one. The reader
    concludes the app logged nothing, when the entries were dropped."""

    async def test_a_full_buffer_that_starts_late_is_truncated(self):
        ring = RingBuffer(max_size=3)
        for at in (10, 11, 12):          # window asks from t=0
            await ring.append(_entry(at))

        result = await _call(ring, BASE)

        assert result["log_window_truncated"] is True

    async def test_a_full_buffer_reaching_back_far_enough_is_not(self):
        """Full is not the same as truncated. If the oldest surviving entry
        predates the window, nothing inside it was lost."""
        ring = RingBuffer(max_size=3)
        for at in (10, 11, 12):
            await ring.append(_entry(at))

        result = await _call(ring, BASE + timedelta(seconds=11))

        assert result["log_window_truncated"] is False

    async def test_a_buffer_with_room_left_is_never_truncated(self):
        ring = RingBuffer(max_size=100)
        await ring.append(_entry(10))

        result = await _call(ring, BASE)

        assert result["log_window_truncated"] is False


class TestTheLimitDoesNotCrashTheEndpoint:
    """`limit` is documented up to 1000, and the handler multiplied it by ten
    to size the device-log query. `LogQueryParams` caps at 1000, so anything
    above 100 raised inside the handler -- an uncaught HTTP 500 on a
    documented input."""

    @pytest.mark.parametrize("limit", [101, 500, 1000])
    async def test_a_large_limit_is_served_not_crashed(self, limit):
        ring = RingBuffer(max_size=10)
        await ring.append(_entry(10))

        result = await _call_with_limit(ring, BASE, limit)

        assert "actions" in result

    async def test_the_limit_bounds_what_comes_back(self):
        """The slice is the only bound: `filter_entries` ignores the query
        limit, so nothing before it was ever bounding the result."""
        server = RingBuffer(max_size=100)
        for i in range(10):
            await server.append(_action_entry(i))
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            server_buffer=server, ring_buffer=RingBuffer(max_size=10),
            flow_store=None, proxy_adapter=None,
        )))
        result = await get_trace(
            request=request, since=BASE, udid=None, limit=3,
        )

        assert len(result["actions"]) == 3


def _action_entry(i):
    return LogEntry(
        id=uuid.uuid4().hex, timestamp=BASE + timedelta(seconds=i),
        device_id="server", process="server.api.actions", category="device.action",
        level=LogLevel.INFO, message="x", source=LogSource.SERVER,
        action="tap", udid="SIM-A", duration_ms=10, outcome="ok",
    )


async def _call_with_limit(ring, since, limit):
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        server_buffer=RingBuffer(max_size=10), ring_buffer=ring,
        flow_store=None, proxy_adapter=None,
    )))
    return await get_trace(request=request, since=since, udid=None, limit=limit)


class TestMoreLogsThanAskedFor:
    """`filter_entries` returns everything that matches — its docstring says
    "ALL matching entries (no pagination)" — so the query limit only stops the
    query being rejected. Bounding the result is a separate slice, and the
    loss it causes is a different one from eviction."""

    async def test_the_result_is_bounded(self):
        ring = RingBuffer(max_size=5000)
        for i in range(50):
            await ring.append(_entry(i))

        result = await _call_with_limit(ring, BASE, limit=1)

        # limit*10 == 10 device logs retained, from 50 matching.
        assert result["logs_over_limit"] is True

    async def test_a_result_within_the_limit_is_not_flagged(self):
        ring = RingBuffer(max_size=5000)
        await ring.append(_entry(1))

        result = await _call_with_limit(ring, BASE, limit=100)

        assert result["logs_over_limit"] is False

    async def test_it_is_distinct_from_eviction(self):
        """Same symptom, different cause: folding them together would send a
        reader to the wrong fix."""
        ring = RingBuffer(max_size=5000)
        for i in range(50):
            await ring.append(_entry(i))

        result = await _call_with_limit(ring, BASE, limit=1)

        assert result["logs_over_limit"] is True
        assert result["log_window_truncated"] is False


class TestEveryLossIsReported:
    """The log path reported both of its losses; the flow and action paths
    reported neither. A trace missing the requests that explain a failure,
    with nothing saying they were dropped, is the exact shape the rest of
    this file guards against — it was only guarded on one of three inputs."""

    async def test_flows_over_the_limit_are_reported(self):
        store = _FakeFlowStore([_flow(i) for i in range(50)])
        result = await _call_full(ring=RingBuffer(max_size=10), flows=store, limit=1)

        assert result["flows_over_limit"] is True

    async def test_flows_within_the_limit_are_not(self):
        store = _FakeFlowStore([_flow(1)])
        result = await _call_full(ring=RingBuffer(max_size=10), flows=store, limit=100)

        assert result["flows_over_limit"] is False

    async def test_actions_over_the_limit_are_reported(self):
        server = RingBuffer(max_size=100)
        for i in range(10):
            await server.append(_action_entry(i))

        result = await _call_full(
            ring=RingBuffer(max_size=10), flows=None, limit=3, server=server,
        )

        assert result["actions_over_limit"] is True
        assert len(result["actions"]) == 3

    async def test_a_quiet_server_is_not_reported_as_truncated(self):
        result = await _call_full(ring=RingBuffer(max_size=10), flows=None, limit=100)

        assert result["actions_over_limit"] is False
        assert result["action_window_truncated"] is False


class _FakeFlowStore:
    """Enough of FlowStore to exercise the bounding, without a proxy."""

    def __init__(self, flows):
        self._flows = flows
        self.max_size = 5000

    @property
    def size(self):
        return len(self._flows)

    async def get_since(self, since):
        return [f for f in self._flows if f.timestamp >= since]


def _flow(i):
    from server.models import FlowRecord, FlowRequest

    return FlowRecord(
        id=uuid.uuid4().hex,
        timestamp=BASE + timedelta(seconds=i),
        request=FlowRequest(method="GET", url="https://x/", host="x", path="/"),
    )


async def _call_full(*, ring, flows, limit, server=None):
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        server_buffer=server or RingBuffer(max_size=10),
        ring_buffer=ring, flow_store=flows, proxy_adapter=None,
    )))
    return await get_trace(request=request, since=BASE, udid=None, limit=limit)
