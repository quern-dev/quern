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
import asyncio
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
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
        device_id: str = "",
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
        self._note: str | None = None

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

    #: How long stop() lets a read loop finish what its stream already wrote.
    DRAIN_TIMEOUT = 2.0

    async def _drain(self, *tasks: asyncio.Task | None) -> None:
        """Let read tasks finish what their streams already wrote, then stop them.

        Terminating a subprocess closes its pipes, so a read loop that runs to
        EOF -- rather than breaking as soon as `_running` is cleared -- parses
        the lines written before the stop instead of dropping them. Bounded,
        so a stream that does not close cannot hold up the stop; whatever is
        still running after that is cancelled.
        """
        pending = [t for t in tasks if t is not None and not t.done()]
        if not pending:
            return
        try:
            await asyncio.wait_for(
                asyncio.shield(asyncio.gather(*pending, return_exceptions=True)),
                self.DRAIN_TIMEOUT,
            )
        except TimeoutError:
            pass
        for task in pending:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

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
            note=self._note,
        )

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)


async def describe_exit(process: asyncio.subprocess.Process, name: str) -> str:
    """What a log subprocess said as it ended its output on its own.

    For a read loop whose stream ended while nobody stopped it: reported as an
    error with the tool's own reason, rather than reading as a clean stop.
    """
    code = None
    try:
        code = await asyncio.wait_for(process.wait(), 5)
    except TimeoutError:
        pass
    detail = ""
    if process.stderr is not None:
        try:
            raw = await asyncio.wait_for(process.stderr.read(), 2)
            lines = raw.decode("utf-8", errors="replace").splitlines()
            detail = " / ".join(ln.strip() for ln in lines if ln.strip())[:300]
        except (TimeoutError, OSError):
            pass
    status = (f"exited ({code})" if code is not None
              else "closed its output but has not exited")
    return f"{name} {status}" + (f": {detail}" if detail else "")
