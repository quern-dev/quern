"""In-memory ring buffer for log entry storage.

Provides fast append and query operations over a fixed-size circular buffer.
When the buffer is full, oldest entries are overwritten.

The storage interface is designed to be swappable — a SQLite implementation
can replace this later without changing the API layer.
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from datetime import datetime

from server.models import LogEntry, LogLevel, LogQueryParams, LogSource


class RingBuffer:
    """Thread-safe ring buffer for log entries with query support."""

    def __init__(self, max_size: int = 10_000) -> None:
        self._buffer: deque[LogEntry] = deque(maxlen=max_size)
        self._lock = asyncio.Lock()
        self._subscribers: list[asyncio.Queue[LogEntry]] = []

    @property
    def size(self) -> int:
        return len(self._buffer)

    @property
    def max_size(self) -> int:
        return self._buffer.maxlen  # type: ignore[return-value]

    async def append(self, entry: LogEntry) -> None:
        """Add an entry to the buffer and notify all subscribers."""
        async with self._lock:
            self._buffer.append(entry)

        # Notify SSE subscribers (non-blocking)
        dead_subs: list[asyncio.Queue[LogEntry]] = []
        for queue in self._subscribers:
            try:
                queue.put_nowait(entry)
            except asyncio.QueueFull:
                # Subscriber is too slow — drop the entry for them
                dead_subs.append(queue)

        for dead in dead_subs:
            self._subscribers.remove(dead)

    async def query(self, params: LogQueryParams) -> tuple[list[LogEntry], int]:
        """Query the buffer with filters. Returns (entries, total_matching)."""
        async with self._lock:
            results = self._filter(params)
            total = len(results)
            # Apply pagination
            paginated = results[params.offset : params.offset + params.limit]
            return paginated, total

    async def get_since(self, since: datetime) -> list[LogEntry]:
        """Get all entries since a given timestamp. Used by the summary cursor system."""
        async with self._lock:
            return [e for e in self._buffer if e.timestamp >= since]

    async def get_recent(self, count: int = 100) -> list[LogEntry]:
        """Get the N most recent entries."""
        async with self._lock:
            items = list(self._buffer)
            return items[-count:]

    def subscribe(self) -> asyncio.Queue[LogEntry]:
        """Create a subscription queue for real-time SSE streaming.

        Returns a queue that will receive new entries as they arrive.
        Caller must call unsubscribe() when done.
        """
        queue: asyncio.Queue[LogEntry] = asyncio.Queue(maxsize=1000)
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[LogEntry]) -> None:
        """Remove a subscription queue."""
        try:
            self._subscribers.remove(queue)
        except ValueError:
            pass

    def _filter(self, params: LogQueryParams) -> list[LogEntry]:
        """Apply query filters to the buffer. Must be called under lock."""
        results: list[LogEntry] = []

        min_levels: set[LogLevel] | None = None
        if params.level is not None:
            min_levels = set(LogLevel.at_least(params.level))

        for entry in self._buffer:
            if params.device_id and entry.device_id != params.device_id:
                continue
            if params.since and entry.timestamp < params.since:
                continue
            if params.until and entry.timestamp > params.until:
                continue
            if min_levels and entry.level not in min_levels:
                continue
            if params.process and entry.process != params.process:
                continue
            if params.source and entry.source != params.source:
                continue
            if params.search and params.search.lower() not in entry.message.lower():
                continue
            results.append(entry)

        return results

    async def clear(self) -> None:
        """Clear all entries from the buffer."""
        async with self._lock:
            self._buffer.clear()
