"""Source adapter that bridges Python's logging module into the log ring buffer.

This makes the server's own logs (startup, errors, warnings) queryable through
the same ``/api/v1/logs/query?source=server`` endpoint used for device logs.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from server.models import LogEntry, LogLevel, LogSource
from server.sources import BaseSourceAdapter, EntryCallback

# Map Python log levels → our LogLevel enum
_LEVEL_MAP: dict[int, LogLevel] = {
    logging.DEBUG: LogLevel.DEBUG,
    logging.INFO: LogLevel.INFO,
    logging.WARNING: LogLevel.WARNING,
    logging.ERROR: LogLevel.ERROR,
    logging.CRITICAL: LogLevel.FAULT,
}


def _map_level(levelno: int) -> LogLevel:
    """Map a Python logging level number to a LogLevel enum value."""
    if levelno <= logging.DEBUG:
        return LogLevel.DEBUG
    if levelno <= logging.INFO:
        return LogLevel.INFO
    if levelno <= logging.WARNING:
        return LogLevel.WARNING
    if levelno <= logging.ERROR:
        return LogLevel.ERROR
    return LogLevel.FAULT


class _BufferHandler(logging.Handler):
    """A logging.Handler that converts LogRecords into LogEntry objects."""

    def __init__(self, adapter: ServerLogAdapter, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__()
        self.adapter = adapter
        self.loop = loop
        self.formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        # Must hold strong references to fire-and-forget tasks to prevent GC
        self._pending_tasks: set[asyncio.Task] = set()

    def emit(self, record: logging.LogRecord) -> None:
        # Avoid recursion: skip log records produced by our own emit path
        if getattr(record, "_from_server_log_adapter", False):
            return

        entry = LogEntry(
            id=uuid.uuid4().hex,
            timestamp=datetime.fromtimestamp(record.created, tz=timezone.utc),
            device_id="server",
            process=record.name,
            level=_map_level(record.levelno),
            message=record.getMessage(),
            source=LogSource.SERVER,
            raw=self.format(record),
        )

        # Schedule the async emit on the event loop (thread-safe)
        try:
            self.loop.call_soon_threadsafe(self._create_task, entry)
        except RuntimeError:
            # Loop is closed during shutdown — silently drop
            pass

    def _create_task(self, entry: LogEntry) -> None:
        """Create a task on the event loop and prevent it from being GC'd."""
        task = asyncio.ensure_future(self.adapter.emit(entry))
        self._pending_tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task) -> None:
        """Clean up completed task and log any errors to stderr (not logging, to avoid recursion)."""
        self._pending_tasks.discard(task)
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                import sys
                print(
                    f"[ServerLogAdapter] emit task failed: {type(exc).__name__}: {exc}",
                    file=sys.stderr, flush=True,
                )


class ServerLogAdapter(BaseSourceAdapter):
    """Bridges Python logging into the log ring buffer.

    Unlike other adapters that spawn subprocesses, this one installs a
    ``logging.Handler`` on the root logger and converts ``LogRecord`` objects
    into ``LogEntry`` items.
    """

    def __init__(self, on_entry: EntryCallback | None = None) -> None:
        super().__init__(
            adapter_id="server-log",
            adapter_type="server",
            device_id="server",
            on_entry=on_entry,
        )
        self._handler: _BufferHandler | None = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._handler = _BufferHandler(self, loop)
        logging.getLogger().addHandler(self._handler)
        self._running = True
        self.started_at = self._now()

    async def stop(self) -> None:
        if self._handler is not None:
            logging.getLogger().removeHandler(self._handler)
            self._handler = None
        self._running = False
