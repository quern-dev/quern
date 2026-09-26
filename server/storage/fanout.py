"""Deliver each new item to every live subscriber, and count what does not fit.

Shared by the log buffer and the flow store, which each had their own copy of
the same loop and the same defect: when a subscriber's queue was full they did
not drop the item for that subscriber, as the comment in one of them said;
they removed the *subscriber*, and nothing told the SSE handler holding the
queue. What that cost differed by stream, which is worth being exact about:

- `/proxy/flows/stream` reads the store's queue directly, so a client that
  stalled filled it and was unsubscribed -- after which its connection stayed
  open and its heartbeats kept coming, a healthy-looking stream gone quiet for
  good.
- `/logs/stream` puts a forwarder task between the buffer and the client, and
  the forwarder keeps the buffer's queue drained, so that path is rarely
  reached. Its loss is one step later: the handler's own merge queue dropped
  on overflow with a bare `pass`. Measured with a client that stalled during a
  simulator flood: 1,538 entries delivered, thousands lost, and not one word
  about it.

Now a slow subscriber stays subscribed, loses only what did not fit, and can
ask how much that was -- so the stream can say so (#255).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass
class Missed:
    """What a subscriber lost: how many, and when those entries were stamped.

    Two spans, for two readers. `first`/`last` cover everything missed since
    the subscription began. `pending_first`/`pending_last` cover only what
    was missed since the last notice took them, which is what a notice must
    report: a span that kept growing re-covered earlier gaps and every entry
    delivered between them, so a client backfilling it fetched duplicates and
    could not find the new gap (second review).
    """

    count: int = 0
    first: datetime | None = None
    last: datetime | None = None
    pending_first: datetime | None = None
    pending_last: datetime | None = None

    def add(self, item: object) -> None:
        self.count += 1
        at = getattr(item, "timestamp", None)
        if isinstance(at, datetime):
            self.first = at if self.first is None or at < self.first else self.first
            self.last = at if self.last is None or at > self.last else self.last
            if self.pending_first is None or at < self.pending_first:
                self.pending_first = at
            if self.pending_last is None or at > self.pending_last:
                self.pending_last = at

    def take_pending(self) -> tuple[datetime | None, datetime | None]:
        """The span missed since the last call, and start a new one."""
        span = (self.pending_first, self.pending_last)
        self.pending_first = self.pending_last = None
        return span


@dataclass
class _Subscription:
    accept: Callable[[object], bool] | None
    missed: Missed = field(default_factory=Missed)


class Fanout(Generic[T]):
    def __init__(self, maxsize: int = 1000) -> None:
        self._maxsize = maxsize
        self._subs: dict[asyncio.Queue[T], _Subscription] = {}

    def subscribe(self, accept: Callable[[T], bool] | None = None) -> asyncio.Queue[T]:
        """A queue of new items -- only those `accept` passes, if given.

        Filtering here rather than in the reader means an item the client
        does not want never takes a slot in its queue, so it overflows later,
        and what it does lose is counted against what it asked for rather
        than against everything published.
        """
        queue: asyncio.Queue[T] = asyncio.Queue(maxsize=self._maxsize)
        self._subs[queue] = _Subscription(accept=accept)  # type: ignore[arg-type]
        return queue

    def unsubscribe(self, queue: asyncio.Queue[T]) -> None:
        self._subs.pop(queue, None)

    def publish(self, item: T) -> None:
        for queue, sub in self._subs.items():
            if sub.accept is not None and not sub.accept(item):
                continue
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                sub.missed.add(item)

    def dropped(self, queue: asyncio.Queue[T]) -> int:
        """How many items this subscriber missed because it fell behind."""
        return self.missed(queue).count

    def missed(self, queue: asyncio.Queue[T]) -> Missed:
        sub = self._subs.get(queue)
        return sub.missed if sub is not None else Missed()

    def __contains__(self, queue: object) -> bool:
        return queue in self._subs


class DropNotice:
    """When a stream should tell its client how much it has missed.

    At most once per `interval` seconds. During a sustained overflow the count
    rises on almost every loop, and a notice each time buried the stream in
    them -- 855 in one measured run. Each notice carries the running total and
    the span missed since the previous notice, so coalescing loses nothing
    and consecutive notices' spans do not overlap.
    """

    def __init__(self, interval: float = 1.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._interval = interval
        self._clock = clock
        self._reported = 0
        self._last: float | None = None

    def due(self, *sources: Missed) -> dict | None:
        """The notice to send now, or None if nothing is new or it is too soon.

        `sources` are the live records for everything this stream can lose
        at -- a subscription, and for the log stream its merge queue too. The
        pending spans are taken from them when a notice is sent.
        """
        total = sum(m.count for m in sources)
        if total <= self._reported:
            return None
        now = self._clock()
        if self._last is not None and now - self._last < self._interval:
            return None
        notice: dict = {"dropped": total - self._reported, "total_dropped": total}
        spans = [m.take_pending() for m in sources]
        starts = [a for a, _ in spans if a is not None]
        ends = [b for _, b in spans if b is not None]
        if starts:
            # Since the previous notice only, so one query for this span --
            # `query_logs` on the log stream, `query_flows` on the flow
            # stream -- fetches this gap and nothing already delivered.
            notice["missed_from"] = min(starts).isoformat()
            notice["missed_to"] = max(ends).isoformat()
        self._reported, self._last = total, now
        return notice
