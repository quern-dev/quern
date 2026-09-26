"""In-memory ring buffer for log entry storage.

Provides fast append and query operations over a fixed-size circular buffer.
When the buffer is full, oldest entries are overwritten -- and counted, so a
reader can tell an empty answer from one whose entries were evicted (#255).

The storage interface is designed to be swappable — a SQLite implementation
can replace this later without changing the API layer.
"""

from __future__ import annotations

import asyncio
from collections import Counter, deque
from collections.abc import Callable, Iterable
from datetime import datetime

from server.models import LogEntry, LogLevel, LogQueryParams, LogSource
from server.storage.fanout import Fanout


def _source_key(source: LogSource | str) -> str:
    """`syslog`, not `LogSource.SYSLOG`.

    `LogSource` is a `(str, Enum)`, and on 3.11+ `str()` of one returns the
    qualified member name rather than its value -- so keying on `str()` would
    report `evicted_by_source` in a spelling no query parameter accepts.
    """
    return source.value if isinstance(source, LogSource) else str(source)


class RingBuffer:
    """Thread-safe ring buffer for log entries with query support."""

    def __init__(self, max_size: int = 10_000) -> None:
        self._buffer: deque[LogEntry] = deque(maxlen=max_size)
        self._lock = asyncio.Lock()
        self._fanout: Fanout[LogEntry] = Fanout(maxsize=1000)
        # Eviction bookkeeping. A `deque(maxlen=)` drops the oldest entry
        # without a word, so for as long as this buffer existed a query that
        # found nothing could not say whether nothing happened or everything
        # that happened was gone -- and at the ~2,900 entries/sec an
        # unfiltered simulator produces, 10,000 entries is about 3.5 seconds.
        self._appended = 0
        self._evicted = 0
        self._evicted_by_source: Counter[str] = Counter()
        # The newest *timestamp* among evicted entries, overall and per
        # source. Not the most recent eviction: entries do not arrive in
        # timestamp order -- a crash is stamped with the time it happened,
        # a physical device with its own clock -- so the oldest entry by
        # position is not the oldest by time. The maximum is what makes the
        # guarantee exact: nothing stamped after it was ever evicted.
        #
        # Kept per (source, level) rather than overall, because a busy buffer
        # evicts debug lines constantly: an overall mark would make every
        # `get_errors` call report "may be incomplete" when no error was ever
        # lost, and a signal that is always on carries no information.
        # A dozen sources by six levels bounds this at a few dozen keys.
        self._evicted_through: dict[tuple[str, LogLevel], datetime] = {}
        # Deliberate removals, counted apart from overflow. A purge follows a
        # filter change, and every call that purges reports the count in its
        # response (`set_log_filter`, and the two logging-start calls when a
        # preset is applied) -- that response is the only place a purge can
        # be seen, because it is not eviction and no `truncated` flag will
        # mention it. An entry the caller's own filter excluded is not a lost
        # answer; it is an answer to a different question.
        self._purged = 0

    @property
    def size(self) -> int:
        return len(self._buffer)

    @property
    def max_size(self) -> int:
        return self._buffer.maxlen  # type: ignore[return-value]

    async def append(self, entry: LogEntry) -> None:
        """Add an entry to the buffer and notify all subscribers."""
        async with self._lock:
            if len(self._buffer) == self._buffer.maxlen:
                self._record_eviction(self._buffer[0])
            self._buffer.append(entry)
            self._appended += 1

        # Notify SSE subscribers (non-blocking). A slow one loses this entry,
        # and the loss is counted -- see server/storage/fanout.py.
        self._fanout.publish(entry)

    async def purge(self, keep: Callable[[LogEntry], bool]) -> int:
        """Remove entries that don't match the predicate. Returns count removed."""
        async with self._lock:
            before = len(self._buffer)
            self._buffer = deque(
                (e for e in self._buffer if keep(e)),
                maxlen=self._buffer.maxlen,
            )
            removed = before - len(self._buffer)
            self._purged += removed
            return removed

    # --- what has been lost --------------------------------------------------

    def _record_eviction(self, entry: LogEntry) -> None:
        """Note an entry about to be overwritten. Must be called under lock."""
        self._evicted += 1
        source = _source_key(entry.source)
        self._evicted_by_source[source] += 1
        key = (source, entry.level)
        previous = self._evicted_through.get(key)
        if previous is None or entry.timestamp > previous:
            self._evicted_through[key] = entry.timestamp

    def evicted_through(
        self,
        sources: Iterable[LogSource] | None = None,
        min_level: LogLevel | None = None,
    ) -> datetime | None:
        """The newest timestamp of any entry evicted, or None if none were.

        Everything stamped *after* this is still here. `sources` and
        `min_level` narrow which evictions count, the way the same filters
        narrow a query: a search for simulator errors is not made incomplete
        by syslog having shed debug lines.
        """
        wanted_sources = (
            None if sources is None else {_source_key(s) for s in sources}
        )
        wanted_levels = (
            None if min_level is None else set(LogLevel.at_least(min_level))
        )
        stamps = [
            at for (source, level), at in self._evicted_through.items()
            if (wanted_sources is None or source in wanted_sources)
            and (wanted_levels is None or level in wanted_levels)
        ]
        return max(stamps) if stamps else None

    def is_complete_since(
        self,
        since: datetime | None,
        sources: Iterable[LogSource] | None = None,
        min_level: LogLevel | None = None,
    ) -> bool:
        """Does this buffer still hold everything stamped at or after `since`?

        Exact in one direction and conservative in the other. True is a
        guarantee: nothing from the window was evicted. False means something
        stamped inside it was, though not necessarily something the caller's
        other filters would have matched -- so it says "may be missing
        entries", never "is missing these".

        `since=None` asks about all time, which is complete only if nothing
        has ever been evicted.
        """
        through = self.evicted_through(sources, min_level)
        if through is None:
            return True
        return since is not None and since > through

    def stats(self) -> dict:
        """What this buffer holds, what it has taken in, and what it lost."""
        timestamps = [e.timestamp for e in self._buffer]
        through = self.evicted_through()
        return {
            "capacity": self.max_size,
            "size": len(self._buffer),
            "appended": self._appended,
            "evicted": self._evicted,
            "evicted_by_source": dict(self._evicted_by_source),
            "evicted_through": through.isoformat() if through else None,
            "purged": self._purged,
            # By timestamp, not position, for the reason `_evicted_through`
            # gives. This is the span a reader can actually query.
            "oldest": min(timestamps).isoformat() if timestamps else None,
            "newest": max(timestamps).isoformat() if timestamps else None,
        }

    async def query(self, params: LogQueryParams) -> tuple[list[LogEntry], int]:
        """Query the buffer with filters. Returns (entries, total_matching).

        If params.tail is True, returns the LAST N matching entries instead of
        paginating from the start.
        """
        async with self._lock:
            results = self._filter(params)
            total = len(results)
            if params.tail:
                paginated = results[-params.limit:]
            else:
                paginated = results[params.offset : params.offset + params.limit]
            return paginated, total

    async def filter_entries(self, params: LogQueryParams) -> list[LogEntry]:
        """Apply query filters and return ALL matching entries (no pagination).

        Use when merging results across buffers — caller handles pagination.
        """
        async with self._lock:
            return self._filter(params)

    async def get_since(self, since: datetime) -> list[LogEntry]:
        """Get all entries at or after a given timestamp."""
        async with self._lock:
            return [e for e in self._buffer if e.timestamp >= since]

    async def get_after(self, after: datetime) -> list[LogEntry]:
        """Get all entries strictly after a given timestamp (for cursor deltas)."""
        async with self._lock:
            return [e for e in self._buffer if e.timestamp > after]

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
        return self._fanout.subscribe()

    def unsubscribe(self, queue: asyncio.Queue[LogEntry]) -> None:
        """Remove a subscription queue."""
        self._fanout.unsubscribe(queue)

    def dropped(self, queue: asyncio.Queue[LogEntry]) -> int:
        """Entries this subscriber missed because its queue was full."""
        return self._fanout.dropped(queue)

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
            if params.category and entry.category != params.category:
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
