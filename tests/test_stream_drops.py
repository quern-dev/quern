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
from server.storage.fanout import DropNotice, Fanout
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

    def test_the_first_loss_is_reported_at_once(self):
        assert self._notice().due(3) == {"dropped": 3, "total_dropped": 3}

    def test_nothing_new_means_no_notice(self):
        notice = self._notice()
        notice.due(3)
        self.now += 5
        assert notice.due(3) is None

    def test_losses_inside_the_interval_are_coalesced_into_the_next(self):
        notice = self._notice()
        notice.due(3)
        self.now += 0.5
        assert notice.due(10) is None
        self.now += 0.6
        assert notice.due(12) == {"dropped": 9, "total_dropped": 12}


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


async def _events(response, until):
    seen = []
    async for event in response.body_iterator:
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
    await task

    seen = await _events(response, until="dropped")

    dropped = [e for e in seen if e["event"] == "dropped"]
    assert dropped, "the stream never said entries were missed"
    assert json.loads(dropped[0]["data"])["total_dropped"] >= 200


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
    await task
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
    await task

    seen = await _events(response, until="dropped")

    dropped = [e for e in seen if e["event"] == "dropped"]
    assert dropped, "the stream never said flows were missed"
    assert json.loads(dropped[0]["data"])["total_dropped"] >= 100
