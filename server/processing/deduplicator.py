"""Log entry deduplicator.

Suppresses repeated identical messages within a sliding time window.
When duplicates are detected, a counter is incremented. After the quiet
period expires, a single summary entry ("repeated N times") is emitted.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from collections.abc import Callable, Coroutine
from datetime import datetime, timezone
from typing import Any

from server.models import LogEntry

logger = logging.getLogger(__name__)


EntryCallback = Callable[[LogEntry], Coroutine[Any, Any, None]]


class _DedupBucket:
    """Tracks repeat count for a specific message pattern."""

    __slots__ = ("first_entry", "count", "last_seen")

    def __init__(self, entry: LogEntry) -> None:
        self.first_entry = entry
        self.count = 1
        self.last_seen = entry.timestamp


class Deduplicator:
    """Suppresses repeated identical log messages within a time window.

    Args:
        on_entry: Callback to emit deduplicated entries.
        window_seconds: How long to track duplicates (default 5s).
        max_suppressed: After this many suppressed duplicates, force-emit
            a summary even if the window hasn't expired (default 100).
    """

    def __init__(
        self,
        on_entry: EntryCallback | None = None,
        window_seconds: float = 5.0,
        max_suppressed: int = 100,
        flush_interval: float = 2.0,
    ) -> None:
        self.on_entry = on_entry
        self.window_seconds = window_seconds
        self.max_suppressed = max_suppressed
        self._flush_interval = flush_interval
        self._buckets: dict[str, _DedupBucket] = {}
        self._flush_task: asyncio.Task | None = None

    def start(self) -> None:
        """Start the background flush timer.

        Call this during server startup so expired buckets are flushed
        even when no new entries arrive.
        """
        if self._flush_task is None:
            self._flush_task = asyncio.create_task(self._flush_loop())

    async def stop(self) -> None:
        """Stop the background flush timer and flush remaining buckets."""
        if self._flush_task is not None:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None
        await self.flush_all()

    async def _flush_loop(self) -> None:
        """Periodically flush expired dedup buckets."""
        try:
            while True:
                await asyncio.sleep(self._flush_interval)
                now = datetime.now(timezone.utc)
                await self._flush_expired(now)
        except asyncio.CancelledError:
            return

    @staticmethod
    def _make_key(entry: LogEntry) -> str:
        """Create a dedup key from process + message content."""
        raw = f"{entry.process}:{entry.message}"
        return hashlib.md5(raw.encode(), usedforsecurity=False).hexdigest()

    async def process(self, entry: LogEntry) -> None:
        """Process an incoming entry — emit or suppress as appropriate."""
        now = entry.timestamp
        key = self._make_key(entry)

        # Flush any expired buckets first
        await self._flush_expired(now)

        if key in self._buckets:
            bucket = self._buckets[key]
            bucket.count += 1
            bucket.last_seen = now

            # Force-emit if we've suppressed too many
            if bucket.count >= self.max_suppressed:
                await self._emit_summary(bucket)
                del self._buckets[key]
        else:
            # New message — emit it immediately and start tracking
            self._buckets[key] = _DedupBucket(entry)
            if self.on_entry:
                await self.on_entry(entry)

    async def _flush_expired(self, now: datetime) -> None:
        """Flush buckets whose window has expired."""
        expired_keys: list[str] = []
        for key, bucket in self._buckets.items():
            elapsed = (now - bucket.last_seen).total_seconds()
            if elapsed >= self.window_seconds:
                expired_keys.append(key)

        for key in expired_keys:
            bucket = self._buckets.pop(key)
            if bucket.count > 1:
                await self._emit_summary(bucket)

    async def flush_all(self) -> None:
        """Flush all pending buckets. Call on shutdown."""
        keys = list(self._buckets.keys())
        for key in keys:
            bucket = self._buckets.pop(key)
            if bucket.count > 1:
                await self._emit_summary(bucket)

    async def _emit_summary(self, bucket: _DedupBucket) -> None:
        """Emit a summary entry for suppressed duplicates.

        The summary carries repeat_count = (number of suppressed copies) so
        downstream consumers like the summarizer can weight it properly
        without double-counting.  The original message is preserved as-is.
        """
        if self.on_entry is None:
            return

        # repeat_count = suppressed copies (first occurrence was already emitted)
        suppressed = bucket.count - 1
        original = bucket.first_entry

        summary = LogEntry(
            id=uuid.uuid4().hex[:8],
            timestamp=bucket.last_seen,
            device_id=original.device_id,
            process=original.process,
            subsystem=original.subsystem,
            category=original.category,
            pid=original.pid,
            level=original.level,
            message=original.message,
            source=original.source,
            repeat_count=suppressed,
        )
        await self.on_entry(summary)
