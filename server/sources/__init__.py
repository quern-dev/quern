"""Abstract base class for log source adapters.

All source adapters (idevicesyslog, oslog, crash watcher, etc.) inherit from this.
Each adapter is responsible for:
1. Spawning/connecting to its log source
2. Parsing raw output into LogEntry objects
3. Calling the on_entry callback for each parsed entry
4. Handling its own errors without crashing the server
"""

from __future__ import annotations

import abc
from collections.abc import Callable, Coroutine
from datetime import datetime, timezone
from typing import Any

from server.models import LogEntry, SourceStatus


# Type alias for the callback that source adapters use to emit log entries
EntryCallback = Callable[[LogEntry], Coroutine[Any, Any, None]]


class BaseSourceAdapter(abc.ABC):
    """Base class for all log source adapters."""

    def __init__(
        self,
        adapter_id: str,
        adapter_type: str,
        device_id: str = "default",
        on_entry: EntryCallback | None = None,
    ) -> None:
        self.adapter_id = adapter_id
        self.adapter_type = adapter_type
        self.device_id = device_id
        self.on_entry = on_entry
        self.entries_captured: int = 0
        self.started_at: datetime | None = None
        self._running: bool = False
        self._error: str | None = None

    @abc.abstractmethod
    async def start(self) -> None:
        """Start capturing logs from this source.

        Must set self._running = True on success and self.started_at.
        Must catch and store exceptions in self._error rather than raising.
        """
        ...

    @abc.abstractmethod
    async def stop(self) -> None:
        """Stop capturing logs and clean up resources.

        Must set self._running = False.
        """
        ...

    @property
    def is_running(self) -> bool:
        return self._running

    async def emit(self, entry: LogEntry) -> None:
        """Emit a parsed log entry to the processing pipeline."""
        self.entries_captured += 1
        if self.on_entry is not None:
            await self.on_entry(entry)

    def status(self) -> SourceStatus:
        """Return the current status of this adapter."""
        if self._error:
            status_str = "error"
        elif self._running:
            status_str = "streaming"
        else:
            status_str = "stopped"

        return SourceStatus(
            id=self.adapter_id,
            type=self.adapter_type,
            status=status_str,
            device_id=self.device_id,
            entries_captured=self.entries_captured,
            started_at=self.started_at,
            error=self._error,
        )

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)
