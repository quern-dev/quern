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
from typing import Generic, TypeVar

T = TypeVar("T")


class Fanout(Generic[T]):
    def __init__(self, maxsize: int = 1000) -> None:
        self._maxsize = maxsize
        # Queue -> items dropped for it because it was full.
        self._dropped: dict[asyncio.Queue[T], int] = {}

    def subscribe(self) -> asyncio.Queue[T]:
        queue: asyncio.Queue[T] = asyncio.Queue(maxsize=self._maxsize)
        self._dropped[queue] = 0
        return queue

    def unsubscribe(self, queue: asyncio.Queue[T]) -> None:
        self._dropped.pop(queue, None)

    def publish(self, item: T) -> None:
        for queue in self._dropped:
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                self._dropped[queue] += 1

    def dropped(self, queue: asyncio.Queue[T]) -> int:
        """How many items this subscriber missed because it fell behind."""
        return self._dropped.get(queue, 0)

    def __contains__(self, queue: object) -> bool:
        return queue in self._dropped


class DropNotice:
    """When a stream should tell its client how much it has missed.

    At most once per `interval` seconds. During a sustained overflow the count
    rises on almost every loop, and a notice each time buried the stream in
    them -- 855 in one measured run. Each notice carries the running total, so
    coalescing loses nothing.
    """

    def __init__(self, interval: float = 1.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._interval = interval
        self._clock = clock
        self._reported = 0
        self._last: float | None = None

    def due(self, total: int) -> dict[str, int] | None:
        """The notice to send now, or None if there is nothing new or it is too soon."""
        if total <= self._reported:
            return None
        now = self._clock()
        if self._last is not None and now - self._last < self._interval:
            return None
        notice = {"dropped": total - self._reported, "total_dropped": total}
        self._reported, self._last = total, now
        return notice
