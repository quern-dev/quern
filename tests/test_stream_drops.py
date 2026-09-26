"""A client that falls behind a live stream is told what it missed (#255).

Both the log buffer and the flow store handled a full subscriber queue by
removing the subscriber, not by dropping the item for it. The SSE handler
holding that queue never noticed: its connection stayed open and its
heartbeats kept arriving, so the client saw a healthy stream that had gone
quiet for good.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from server.models import LogEntry, LogLevel, LogSource
from server.proxy.flow_store import FlowStore
from server.storage.fanout import DropNotice, Fanout, Missed
from server.storage.ring_buffer import RingBuffer
from tests.test_flow_store import _make_flow


def _entry(i=0):
    return LogEntry(
        id=uuid.uuid4().hex, timestamp=datetime.now(UTC), process="MyApp",
        level=LogLevel.INFO, message=f"line {i}", source=LogSource.SIMULATOR,
    )


class TestFanout:
    def test_a_slow_subscriber_loses_items_not_its_subscription(self):
        fanout: Fanout[int] = Fanout(maxsize=2)
        queue = fanout.subscribe()

        for i in range(5):
            fanout.publish(i)

        assert queue.qsize() == 2
        assert fanout.dropped(queue) == 3
        assert queue in fanout

    def test_once_it_catches_up_it_receives_again(self):
        """The old behaviour's real cost: after one overflow the subscriber
        received nothing, ever again."""
        fanout: Fanout[int] = Fanout(maxsize=1)
        queue = fanout.subscribe()
        fanout.publish(1)
        fanout.publish(2)  # dropped
        queue.get_nowait()

        fanout.publish(3)

        assert queue.get_nowait() == 3

    def test_items_the_subscriber_did_not_ask_for_take_no_slot(self):
        """Counting everything published overstated the gap, and queueing it
        made a filtered client overflow on traffic it would have discarded."""
        fanout: Fanout[int] = Fanout(maxsize=2)
        queue = fanout.subscribe(accept=lambda n: n % 2 == 0)

        for i in range(10):
            fanout.publish(i)

        assert queue.qsize() == 2            # 0 and 2
        assert fanout.dropped(queue) == 3    # 4, 6, 8 -- odd numbers never counted

    def test_the_span_of_what_was_missed_is_kept(self):
        fanout: Fanout = Fanout(maxsize=1)
        queue = fanout.subscribe()
        for i in (5, 9, 2):
            fanout.publish(SimpleNamespace(timestamp=datetime(2026, 1, 1, 0, 0, i, tzinfo=UTC)))

        missed = fanout.missed(queue)
        assert missed.count == 2
        assert missed.first.second == 2 and missed.last.second == 9

    def test_unsubscribing_forgets_the_count(self):
        fanout: Fanout[int] = Fanout(maxsize=1)
        queue = fanout.subscribe()
        fanout.unsubscribe(queue)
        fanout.publish(1)
        assert queue.empty() and fanout.dropped(queue) == 0


class TestDropNotice:
    """During a sustained overflow the count rises on nearly every loop; a
    notice each time buried the stream -- 855 in one measured run."""

    def _notice(self):
        self.now = 100.0
        return DropNotice(interval=1.0, clock=lambda: self.now)

    @staticmethod
    def _lose(missed, *seconds):
        for sec in seconds:
            missed.add(SimpleNamespace(timestamp=datetime(2026, 9, 26, 18, 0, sec, tzinfo=UTC)))

    def test_the_first_loss_is_reported_at_once(self):
        missed = Missed()
        self._lose(missed, 1, 2, 3)
        assert self._notice().due(missed)["total_dropped"] == 3

    def test_nothing_new_means_no_notice(self):
        notice, missed = self._notice(), Missed()
        self._lose(missed, 1)
        notice.due(missed)
        self.now += 5
        assert notice.due(missed) is None

    def test_losses_inside_the_interval_are_coalesced_into_the_next(self):
        notice, missed = self._notice(), Missed()
        self._lose(missed, 1)
        notice.due(missed)
        self.now += 0.5
        self._lose(missed, 2, 3)
        assert notice.due(missed) is None
        self.now += 0.6
        due = notice.due(missed)
        assert (due["dropped"], due["total_dropped"]) == (2, 3)

    def test_a_notice_says_when_the_missed_entries_were_stamped(self):
        """So the gap can be backfilled with one query."""
        missed = Missed()
        self._lose(missed, 11, 14, 12)
        due = self._notice().due(missed)
        assert due["missed_from"].endswith("18:00:11+00:00")
        assert due["missed_to"].endswith("18:00:14+00:00")

    def test_consecutive_notices_cover_separate_gaps(self):
        """A span covering everything since the stream began re-covered the
        first gap and every entry delivered after it, so a backfill fetched
        duplicates and could not find the new gap (second review)."""
        notice, missed = self._notice(), Missed()
        self._lose(missed, 1, 2)
        notice.due(missed)
        self.now += 2
        self._lose(missed, 40, 41)

        due = notice.due(missed)

        assert due["missed_from"].endswith("18:00:40+00:00")
        assert due["missed_to"].endswith("18:00:41+00:00")

    def test_spans_from_every_source_are_combined(self):
        """The log stream loses at its subscriptions and its merge queue."""
        sub, merge = Missed(), Missed()
        self._lose(sub, 5)
        self._lose(merge, 9)
        due = self._notice().due(sub, merge)
        assert due["total_dropped"] == 2
        assert due["missed_from"].endswith(":05+00:00") and due["missed_to"].endswith(":09+00:00")


async def test_the_log_buffer_keeps_a_slow_subscriber():
    buffer = RingBuffer(max_size=10)
    queue = buffer.subscribe()
    for i in range(1005):          # the subscriber queue holds 1000
        await buffer.append(_entry(i))
    while not queue.empty():
        queue.get_nowait()

    await buffer.append(_entry(9999))

    assert buffer.dropped(queue) == 5
    assert queue.get_nowait().message == "line 9999"


async def test_the_flow_store_keeps_a_slow_subscriber():
    store = FlowStore(max_size=10)
    queue = store.subscribe()
    for i in range(1003):
        await store.add(_make_flow(flow_id=f"f{i}"))
    while not queue.empty():
        queue.get_nowait()

    await store.add(_make_flow(flow_id="after"))

    assert store.dropped(queue) == 3
    assert queue.get_nowait().id == "after"


class _Request:
    """Enough of a Starlette request for an SSE handler to run against."""

    def __init__(self, **state):
        self.app = SimpleNamespace(state=SimpleNamespace(**state))
        self.polls = 0

    async def is_disconnected(self):
        self.polls += 1
        return self.polls > 5


#: Captured at import, because the heartbeat tests replace asyncio.wait_for.
_real_wait_for = asyncio.wait_for

#: No single event should take longer than this. A handler that stops
#: emitting what a test waits for must fail the test, not hang it for the
#: handler's 15s heartbeat interval per remaining iteration (CodeRabbit, #319).
EVENT_TIMEOUT_S = 5.0


async def _next(events):
    return await _real_wait_for(events.__anext__(), EVENT_TIMEOUT_S)


async def _events(response, until, *, limit=5000):
    seen = []
    events = response.body_iterator
    for _ in range(limit):
        try:
            event = await _next(events)
        except StopAsyncIteration:
            break
        seen.append(event)
        if event["event"] == until:
            break
    return seen


async def test_the_log_stream_says_what_a_slow_client_missed():
    from server.api.logs import stream_logs

    ring = RingBuffer(max_size=5000)
    request = _Request(
        ring_buffer=ring, server_buffer=RingBuffer(max_size=10),
        crash_buffer=RingBuffer(max_size=10),
    )
    response = await stream_logs(
        request=request, level=None, process=None, subsystem=None, category=None,
        source=LogSource.SIMULATOR, match=None, exclude=None, device_id=None,
    )
    events = response.body_iterator

    # Start the handler so it subscribes, then flood faster than it reads.
    first = events.__anext__()
    task = asyncio.ensure_future(first)
    await asyncio.sleep(0)
    for i in range(1200):
        await ring.append(_entry(i))
    await _real_wait_for(task, EVENT_TIMEOUT_S)

    seen = await _events(response, until="dropped")

    dropped = [e for e in seen if e["event"] == "dropped"]
    assert dropped, "the stream never said entries were missed"
    data = json.loads(dropped[0]["data"])
    assert data["total_dropped"] >= 200
    # From the real handler, not only DropNotice: survivor P17 passed the
    # handler a bare count, which dropped the span from every notice sent.
    assert data["missed_from"] and data["missed_to"]


async def test_losses_at_the_streams_own_merge_queue_are_reported_too():
    """The handler merges its buffers into one queue, which dropped on
    overflow with a bare `pass`. Fed in chunks the buffer's queue can hold,
    every loss happens there -- and must still be counted."""
    from server.api.logs import stream_logs

    ring = RingBuffer(max_size=5000)
    request = _Request(
        ring_buffer=ring, server_buffer=RingBuffer(max_size=10),
        crash_buffer=RingBuffer(max_size=10),
    )
    response = await stream_logs(
        request=request, level=None, process=None, subsystem=None, category=None,
        source=LogSource.SIMULATOR, match=None, exclude=None, device_id=None,
    )
    task = asyncio.ensure_future(response.body_iterator.__anext__())
    await asyncio.sleep(0)
    await ring.append(_entry())
    await _real_wait_for(task, EVENT_TIMEOUT_S)
    for _chunk in range(3):
        for i in range(900):
            await ring.append(_entry(i))
        for _ in range(5):
            await asyncio.sleep(0)   # let the forwarder drain into the merge queue

    seen = await _events(response, until="dropped")

    dropped = [e for e in seen if e["event"] == "dropped"]
    assert dropped, "losses at the merge queue went unreported"
    assert json.loads(dropped[0]["data"])["total_dropped"] >= 1000


async def test_the_flow_stream_says_what_a_slow_client_missed():
    from server.api.proxy import stream_flows

    store = FlowStore(max_size=5000)
    request = _Request(flow_store=store)
    response = await stream_flows(
        request=request, host=None, method=None, device_id=None, simulator_udid=None,
    )

    task = asyncio.ensure_future(response.body_iterator.__anext__())
    await asyncio.sleep(0)
    for i in range(1100):
        await store.add(_make_flow(flow_id=f"f{i}"))
    await _real_wait_for(task, EVENT_TIMEOUT_S)

    seen = await _events(response, until="dropped")

    dropped = [e for e in seen if e["event"] == "dropped"]
    assert dropped, "the stream never said flows were missed"
    data = json.loads(dropped[0]["data"])
    assert data["total_dropped"] >= 100
    assert data["missed_from"] and data["missed_to"]   # survivor P18


async def test_the_log_stream_subscribes_with_the_clients_filter():
    """Entries the client filtered out must not fill its queue: a stream for
    one process fell behind on every other process's traffic."""
    from server.api.logs import stream_logs

    ring = RingBuffer(max_size=5000)
    request = _Request(
        ring_buffer=ring, server_buffer=RingBuffer(max_size=10),
        crash_buffer=RingBuffer(max_size=10),
    )
    response = await stream_logs(
        request=request, level=None, process="MyApp", subsystem=None, category=None,
        source=LogSource.SIMULATOR, match=None, exclude=None, device_id=None,
    )
    task = asyncio.ensure_future(response.body_iterator.__anext__())
    await asyncio.sleep(0)
    await ring.append(_entry())
    await _real_wait_for(task, EVENT_TIMEOUT_S)

    for i in range(1500):
        noise = _entry(i)
        noise.process = "SpringBoard"
        await ring.append(noise)

    [sub] = ring._fanout._subs.values()
    assert sub.missed.count == 0, "entries the client filtered out were queued and lost"


async def test_the_heartbeat_carries_the_running_total(monkeypatch):
    """Review mutations M19/M20 removed `total_dropped` from the heartbeat
    and nothing failed."""
    from server.api.logs import stream_logs

    async def _time_out(awaitable, timeout):
        awaitable.close()
        raise TimeoutError

    ring = RingBuffer(max_size=5000)
    request = _Request(
        ring_buffer=ring, server_buffer=RingBuffer(max_size=10),
        crash_buffer=RingBuffer(max_size=10),
    )
    request.polls = -100  # stay connected for the whole test
    response = await stream_logs(
        request=request, level=None, process=None, subsystem=None, category=None,
        source=LogSource.SIMULATOR, match=None, exclude=None, device_id=None,
    )
    monkeypatch.setattr(asyncio, "wait_for", _time_out)
    events = response.body_iterator

    first = await _next(events)                # subscribes; reads nothing
    assert first["event"] == "heartbeat"
    for i in range(1100):                      # 100 past the subscriber queue
        await ring.append(_entry(i))

    seen = [await _next(events) for _ in range(2)]

    [heartbeat] = [e for e in seen if e["event"] == "heartbeat"]
    assert json.loads(heartbeat["data"])["total_dropped"] >= 100


async def test_the_flow_stream_subscribes_with_the_clients_filter():
    """Survivor P13: the flow stream subscribing unfiltered failed nothing."""
    from server.api.proxy import stream_flows

    store = FlowStore(max_size=5000)
    request = _Request(flow_store=store)
    response = await stream_flows(
        request=request, host="api.wanted.com", method=None, device_id=None,
        simulator_udid=None,
    )
    task = asyncio.ensure_future(response.body_iterator.__anext__())
    await asyncio.sleep(0)
    await store.add(_make_flow(flow_id="wanted", host="api.wanted.com"))
    await _real_wait_for(task, EVENT_TIMEOUT_S)

    for i in range(1500):
        await store.add(_make_flow(flow_id=f"noise{i}", host="cdn.other.com"))

    [sub] = store._fanout._subs.values()
    assert sub.missed.count == 0, "flows the client filtered out were queued and lost"


async def test_the_flow_heartbeat_carries_the_running_total(monkeypatch):
    """Survivor P19: removing it from the flow stream's heartbeat failed
    nothing; the heartbeat test covered /logs/stream only."""
    from server.api.proxy import stream_flows

    async def _time_out(awaitable, timeout):
        awaitable.close()
        raise TimeoutError

    store = FlowStore(max_size=5000)
    request = _Request(flow_store=store)
    request.polls = -100
    response = await stream_flows(
        request=request, host=None, method=None, device_id=None, simulator_udid=None,
    )
    monkeypatch.setattr(asyncio, "wait_for", _time_out)
    events = response.body_iterator

    assert (await _next(events))["event"] == "heartbeat"
    for i in range(1100):
        await store.add(_make_flow(flow_id=f"f{i}"))

    seen = [await _next(events) for _ in range(2)]

    [heartbeat] = [e for e in seen if e["event"] == "heartbeat"]
    assert json.loads(heartbeat["data"])["total_dropped"] >= 100
