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
    """What a subscriber lost: how many, and the span of their timestamps.

    The span is what makes a gap fillable. "34 entries missed" says a query
    is needed; "stamped between 18:00:11 and 18:00:14" says which one.
    """

    count: int = 0
    first: datetime | None = None
    last: datetime | None = None

    def add(self, item: object) -> None:
        self.count += 1
        at = getattr(item, "timestamp", None)
        if isinstance(at, datetime):
            self.first = at if self.first is None or at < self.first else self.first
            self.last = at if self.last is None or at > self.last else self.last

    def merged(self, *others: Missed) -> Missed:
        out = Missed(self.count, self.first, self.last)
        for other in others:
            out.count += other.count
            for at in (other.first, other.last):
                if at is not None:
                    out.first = at if out.first is None or at < out.first else out.first
                    out.last = at if out.last is None or at > out.last else out.last
        return out


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
    the span of what was missed, so coalescing loses nothing.
    """

    def __init__(self, interval: float = 1.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._interval = interval
        self._clock = clock
        self._reported = 0
        self._last: float | None = None

    def due(self, missed: Missed | int) -> dict | None:
        """The notice to send now, or None if there is nothing new or it is too soon."""
        if isinstance(missed, int):
            missed = Missed(count=missed)
        total = missed.count
        if total <= self._reported:
            return None
        now = self._clock()
        if self._last is not None and now - self._last < self._interval:
            return None
        notice: dict = {"dropped": total - self._reported, "total_dropped": total}
        if missed.first is not None:
            # The whole span missed since the stream began, so a client can
            # backfill it with one `query_logs(since=..., until=...)`.
            notice["missed_from"] = missed.first.isoformat()
            notice["missed_to"] = missed.last.isoformat() if missed.last else None
        self._reported, self._last = total, now
        return notice
