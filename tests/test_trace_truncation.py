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


def _entry(at_s, source=LogSource.SIMULATOR):
    return LogEntry(
        id=uuid.uuid4().hex, timestamp=BASE + timedelta(seconds=at_s),
        device_id="SIM-A", process="MyApp", level=LogLevel.INFO,
        message="x", source=source,
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


def _action_entry(i, duration_ms=10):
    return LogEntry(
        id=uuid.uuid4().hex, timestamp=BASE + timedelta(seconds=i),
        device_id="server", process="server.api.actions", category="device.action",
        level=LogLevel.INFO, message="x", source=LogSource.SERVER,
        action="tap", udid="SIM-A", duration_ms=duration_ms, outcome="ok",
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


def _flow(i, **kw):
    from server.models import FlowRecord, FlowRequest

    return FlowRecord(
        id=uuid.uuid4().hex,
        timestamp=BASE + timedelta(seconds=i),
        request=FlowRequest(method="GET", url="https://x/", host="x", path="/"),
        **kw,
    )


async def _call_full(*, ring, flows, limit, server=None, udid=None):
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        server_buffer=server or RingBuffer(max_size=10),
        ring_buffer=ring, flow_store=flows, proxy_adapter=None,
    )))
    return await get_trace(request=request, since=BASE, udid=udid, limit=limit)


class TestTheBoundIsSpentOnUsableLogs:
    """`build_trace` keeps only APP_LOG_SOURCES, so slicing before that
    filter spent the bound on entries that were then discarded — newer build,
    proxy and server records pushing out older app logs and crash reports.
    The trace then looked empty for a reason that had nothing to do with the
    app."""

    async def test_noise_does_not_push_out_app_logs(self):
        from server.models import LogSource

        ring = RingBuffer(max_size=5000)
        # One app line, then a flood of entries the trace will discard.
        await ring.append(_entry(1))
        for i in range(2, 60):
            await ring.append(_entry(i, source=LogSource.BUILD))

        # With an action to attribute to, so the assertion can be about the
        # line surviving rather than about a flag. The previous version
        # asserted only `logs_over_limit is False`, which stayed true when the
        # whole source filter was deleted -- it guarded the flag, not the
        # behaviour its name describes.
        # An action whose window actually spans the app line at t=1, so the
        # assertion is about the line surviving the bound rather than about
        # the fixture.
        server = RingBuffer(max_size=100)
        await server.append(_action_entry(2, duration_ms=3000))

        result = await _call_full(
            ring=ring, flows=None, limit=1, server=server,
        )

        [attributed] = result["actions"]
        assert attributed["logs"], "the app line was crowded out by discarded noise"
        assert result["logs_over_limit"] is False

    async def test_the_flag_counts_usable_logs_only(self):
        from server.models import LogSource

        ring = RingBuffer(max_size=5000)
        for i in range(60):
            await ring.append(_entry(i, source=LogSource.BUILD))

        result = await _call_with_limit(ring, BASE, limit=1)

        assert result["logs_over_limit"] is False, (
            "discarded sources were counted against the caller's limit"
        )


class TestTheTruncationProbesFireWhenTheyShould:
    """Two signals were asserted only in the `False` direction, so hardwiring
    them off passed the entire 3474-test suite. A loss signal that can never
    be true is worse than none — it reads as a guarantee."""

    async def test_actions_lost_from_the_server_buffer_are_reported(self):
        server = RingBuffer(max_size=3)
        # More than fits, all after the requested window start.
        for i in range(10, 14):
            await server.append(_action_entry(i))

        result = await _call_full(
            ring=RingBuffer(max_size=10), flows=None, limit=100, server=server,
        )

        assert result["action_window_truncated"] is True

    async def test_flows_lost_from_the_store_are_reported(self):
        store = _FakeFlowStore([_flow(i) for i in range(10, 14)])
        store.max_size = 4  # full

        result = await _call_full(ring=RingBuffer(max_size=10), flows=store, limit=100)

        assert result["flow_window_truncated"] is True

    async def test_a_store_reaching_back_past_the_window_is_not(self):
        """Full is not truncated. The probe has to find the true oldest, and
        the store is in completion order rather than timestamp order — so
        reading its first entry claimed truncation that had not happened."""
        store = _FakeFlowStore([_flow(10), _flow(-5), _flow(11)])
        store.max_size = 3

        result = await _call_full(ring=RingBuffer(max_size=10), flows=store, limit=100)

        assert result["flow_window_truncated"] is False


class TestTheDeviceFilterKeepsTheCallersLogs:
    """The `udid` pre-filter on device logs had no coverage. It is also the
    mechanism behind the `device_id` sentinel bug: it discarded every log
    line, because `"default"` is truthy and never equal to a udid.

    As with the flow filter, the assertion has to be about the *bound*.
    Attribution rejects a foreign line on its own, so a test that only counts
    attributed logs stays green with the filter deleted -- measured."""

    async def test_a_neighbour_cannot_crowd_out_the_callers_own_logs(self):
        from server.models import LogSource

        ring = RingBuffer(max_size=100)
        await ring.append(_entry(1))  # device_id="SIM-A", oldest
        for i in range(15):           # another device, newer
            ring_entry = LogEntry(
                id=uuid.uuid4().hex, timestamp=BASE + timedelta(seconds=2 + i),
                device_id="SIM-B", process="MyApp", level=LogLevel.INFO,
                message="theirs", source=LogSource.SIMULATOR,
            )
            await ring.append(ring_entry)
        server = RingBuffer(max_size=100)
        await server.append(_action_entry(2, duration_ms=4000))

        # limit=1 bounds device logs at 10, fewer than the neighbour sent.
        result = await _call_full(
            ring=ring, flows=None, limit=1, server=server, udid="SIM-A",
        )

        [attributed] = result["actions"]
        assert attributed["logs"], "the caller's own log line was crowded out"

    async def test_a_line_with_no_device_is_kept(self):
        """Unknown is not foreign, here too."""
        from server.models import LogSource

        ring = RingBuffer(max_size=100)
        await ring.append(LogEntry(
            id=uuid.uuid4().hex, timestamp=BASE + timedelta(seconds=1),
            device_id="", process="MyApp", level=LogLevel.INFO,
            message="unscoped", source=LogSource.SIMULATOR,
        ))
        server = RingBuffer(max_size=100)
        await server.append(_action_entry(2, duration_ms=3000))

        result = await _call_full(
            ring=ring, flows=None, limit=100, server=server, udid="SIM-A",
        )

        [attributed] = result["actions"]
        assert attributed["logs"]


class TestTheFlowFilterSeesPhysicalDevices:
    """The pre-filter exists to spend the bound on the caller's own flows. It
    read `simulator_udid`, which only a simulator has -- so every
    physical-device flow read as unidentified and survived it, including
    another device's. On a Wi-Fi-proxied device the filter did nothing at all.

    Note what these assert on. Attribution drops a foreign flow anyway, so a
    test counting attributed flows is green with the filter deleted. The only
    observable effect is the *bound*: a busy neighbour's flows, kept through
    the filter, fill the slice and push this caller's own flow out of its own
    trace before attribution ever sees it."""

    @pytest.fixture
    def ip_map(self, monkeypatch):
        mapping = {}
        monkeypatch.setattr("server.api.trace._ip_map", lambda: mapping)
        return mapping

    async def _trace_of(self, store, udid):
        server = RingBuffer(max_size=100)
        await server.append(_action_entry(2, duration_ms=4000))
        result = await _call_full(
            ring=RingBuffer(max_size=10), flows=store, limit=1,
            server=server, udid=udid,
        )
        [action] = result["actions"]
        return action["flows"]

    async def test_a_neighbour_cannot_crowd_out_the_callers_own_flow(self, ip_map):
        ip_map["10.0.0.1"] = ("SIM-A", True)
        ip_map["10.0.0.9"] = ("PHONE-B", True)
        # limit=1 bounds flows at 10. The caller's own is the oldest, so
        # without the filter the slice keeps ten of the neighbour's instead.
        store = _FakeFlowStore(
            [_flow(1, client_ip="10.0.0.1")]
            + [_flow(2 + i, client_ip="10.0.0.9") for i in range(15)],
        )

        flows = await self._trace_of(store, "SIM-A")

        assert len(flows) == 1, "the caller's own flow was crowded out"

    async def test_an_unidentifiable_flow_still_reaches_attribution(self, ip_map):
        """Unknown is not foreign. Attribution can claim it on time alone,
        with a caveat, so the filter must not decide for it."""
        store = _FakeFlowStore([_flow(1)])

        flows = await self._trace_of(store, "SIM-A")

        assert len(flows) == 1

    async def test_a_simulator_flow_is_still_matched(self, ip_map):
        """`device_of` prefers `simulator_udid`; the old path must keep
        working."""
        store = _FakeFlowStore(
            [_flow(1, simulator_udid="SIM-A")]
            + [_flow(2 + i, simulator_udid="SIM-B") for i in range(15)],
        )

        flows = await self._trace_of(store, "SIM-A")

        assert len(flows) == 1


class TestTheTimelineIsInOrder:
    """The store is an OrderedDict in *completion* order while `timestamp` is
    when the request started, so overlapping requests -- the normal case --
    come back shuffled. An endpoint whose premise is one timeline has to sort
    them, and the slice below it has to bound by time rather than by whichever
    finished last."""

    async def _flows_of(self, store, limit=100):
        server = RingBuffer(max_size=100)
        await server.append(_action_entry(20, duration_ms=30000))
        result = await _call_full(
            ring=RingBuffer(max_size=10), flows=store, limit=limit, server=server,
        )
        [action] = result["actions"]
        return action["flows"]

    async def test_flows_come_back_chronological(self):
        store = _FakeFlowStore([_flow(5), _flow(1), _flow(9), _flow(3)])

        flows = await self._flows_of(store)

        stamps = [f["timestamp"] for f in flows]
        assert stamps == sorted(stamps)

    async def test_the_bound_keeps_the_newest_not_the_last_completed(self):
        """A slow request that started first but finished last is at the end
        of the store. Unsorted, the slice keeps it and discards a newer one."""
        store = _FakeFlowStore([_flow(i) for i in range(2, 14)] + [_flow(1)])

        flows = await self._flows_of(store, limit=1)   # bounds flows at 10

        stamps = [f["timestamp"] for f in flows]
        assert len(stamps) == 10
        oldest_kept = min(stamps)
        assert oldest_kept > (BASE + timedelta(seconds=1)).isoformat(), (
            "the oldest flow survived the bound while a newer one was dropped"
        )


class TestANaiveSinceIsServed:
    """`?since=2026-09-21T12:00:00` -- no offset -- is valid ISO 8601 and
    FastAPI hands it over naive. Comparing it against the UTC-aware timestamps
    everything else uses raised TypeError, so a well-formed request returned
    HTTP 500 and read as quern being broken."""

    async def test_it_does_not_raise(self):
        ring = RingBuffer(max_size=10)
        await ring.append(_entry(1))
        server = RingBuffer(max_size=10)
        await server.append(_action_entry(2, duration_ms=3000))

        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            server_buffer=server, ring_buffer=ring,
            flow_store=_FakeFlowStore([_flow(1)]), proxy_adapter=None,
        )))
        result = await get_trace(
            request=request, since=BASE.replace(tzinfo=None), udid=None, limit=10,
        )

        assert result["since"] == BASE.isoformat()

    async def test_it_is_read_as_utc(self, monkeypatch):
        """Not as local time. A window silently shifted by the server's offset
        returns the wrong entries and says nothing about it.

        The timezone is pinned rather than inherited, and it is pinned to one
        *ahead* of UTC. Reading a naive value as local shifts it by the
        server's offset, so under `TZ=UTC` the two readings are identical and
        this test is green against the bug -- measured, and CI runs in UTC.
        A test that only fails in some timezones is the shape this file
        exists to avoid."""
        import time

        monkeypatch.setenv("TZ", "Asia/Tokyo")
        time.tzset()
        ring = RingBuffer(max_size=3)
        for at in (10, 11, 12):
            await ring.append(_entry(at))

        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            server_buffer=RingBuffer(max_size=10), ring_buffer=ring,
            flow_store=None, proxy_adapter=None,
        )))
        result = await get_trace(
            request=request,
            since=(BASE + timedelta(seconds=11)).replace(tzinfo=None),
            udid=None, limit=10,
        )

        assert result["log_window_truncated"] is False


class TestIdentifiedByReachesTheResponse:
    """The helper being right is not the same as the response carrying it.

    Both halves need a test: the unit tests in test_trace.py pass whether or
    not `_serialise` ever calls the helper, which is the shape that has
    already caught this branch twice."""

    @pytest.fixture
    def ip_map(self, monkeypatch):
        mapping = {}
        monkeypatch.setattr("server.api.trace._ip_map", lambda: mapping)
        return mapping

    async def _trace(self, *, store=None, ring=None):
        server = RingBuffer(max_size=100)
        await server.append(_action_entry(2, duration_ms=4000))
        result = await _call_full(
            ring=ring or RingBuffer(max_size=10), flows=store, limit=100,
            server=server, udid="SIM-A",
        )
        [action] = result["actions"]
        return action

    async def test_a_simulator_flow_is_marked_process(self, ip_map):
        action = await self._trace(store=_FakeFlowStore([_flow(1, simulator_udid="SIM-A")]))

        assert action["flows"][0]["identified_by"] == "process"

    async def test_an_ip_mapped_flow_is_marked_client_ip(self, ip_map):
        ip_map["10.0.0.1"] = ("SIM-A", True)
        action = await self._trace(store=_FakeFlowStore([_flow(1, client_ip="10.0.0.1")]))

        assert action["flows"][0]["identified_by"] == "client_ip"

    async def test_a_stale_mapping_is_marked_expired(self, ip_map):
        ip_map["10.0.0.1"] = ("SIM-A", False)
        action = await self._trace(store=_FakeFlowStore([_flow(1, client_ip="10.0.0.1")]))

        assert action["flows"][0]["identified_by"] == "client_ip_expired"

    async def test_a_flow_with_no_device_is_marked_unidentified(self, ip_map):
        action = await self._trace(store=_FakeFlowStore([_flow(1)]))

        assert action["flows"][0]["identified_by"] == "unidentified"

    async def test_log_lines_carry_it_too(self, ip_map):
        ring = RingBuffer(max_size=100)
        await ring.append(_entry(1))          # device_id="SIM-A"
        action = await self._trace(ring=ring)

        assert action["logs"][0]["identified_by"] == "adapter"
