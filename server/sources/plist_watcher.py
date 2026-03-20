"""Plist watcher source adapter — polls a plist file and emits changes as log entries."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

from server.device.app_state import resolve_container
from server.device.plist import diff_plists, read_plist
from server.models import LogEntry, LogLevel, LogSource
from server.sources import BaseSourceAdapter

logger = logging.getLogger("quern-debug-server.plist-watcher")


class PlistWatcherAdapter(BaseSourceAdapter):
    """Polls a plist file and emits per-key change entries into the log pipeline."""

    def __init__(
        self,
        udid: str,
        bundle_id: str,
        container: str,
        plist_path: str,
        poll_interval: float = 1.0,
        on_entry=None,
    ) -> None:
        adapter_id = f"plist-watch-{uuid.uuid4().hex[:8]}"
        super().__init__(
            adapter_id=adapter_id,
            adapter_type="plist_watcher",
            device_id=udid,
            on_entry=on_entry,
        )
        self.udid = udid
        self.bundle_id = bundle_id
        self.container = container
        self.plist_path = plist_path
        self.poll_interval = poll_interval
        self._task: asyncio.Task | None = None
        self._previous: dict | None = None
        self._resolved_path: Path | None = None

    def _subsystem(self) -> str:
        return f"{self.container}:{self.plist_path}"

    def _make_entry(self, message: str, level: LogLevel = LogLevel.INFO) -> LogEntry:
        return LogEntry(
            id=uuid.uuid4().hex[:8],
            timestamp=datetime.now(UTC),
            device_id=self.udid,
            process=self.bundle_id,
            subsystem=self._subsystem(),
            category="plist",
            pid=None,
            level=level,
            message=message,
            source=LogSource.PLIST_WATCHER,
            raw=message,
        )

    async def start(self) -> None:
        try:
            container_path = await resolve_container(self.udid, self.bundle_id, self.container)
            self._resolved_path = container_path / self.plist_path
            if not self._resolved_path.exists():
                self._error = f"Plist not found: {self._resolved_path}"
                return

            # Read and emit initial snapshot
            self._previous = await read_plist(self._resolved_path)
            snapshot_msg = (
                f"Watching {self._subsystem()} — {len(self._previous)} keys\n"
                f"{json.dumps(self._previous, indent=2, default=str)}"
            )
            await self.emit(self._make_entry(snapshot_msg, LogLevel.DEBUG))

            self._running = True
            self.started_at = self._now()
            self._task = asyncio.create_task(self._poll_loop())
            logger.info(
                "Started plist watch: %s (interval=%.1fs)",
                self._subsystem(), self.poll_interval,
            )
        except Exception as e:
            self._error = str(e)
            logger.error("Failed to start plist watch: %s", e)

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("Stopped plist watch: %s", self._subsystem())

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self.poll_interval)
                if not self._running:
                    break
                await self._check_for_changes()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Plist watch poll error: %s", e)
                self._error = str(e)

    async def _check_for_changes(self) -> None:
        if self._resolved_path is None or not self._resolved_path.exists():
            return

        current = await read_plist(self._resolved_path)
        if self._previous is None:
            self._previous = current
            return

        diff = diff_plists(self._previous, current)

        for key, value in diff["added"].items():
            await self.emit(self._make_entry(f"+ {key} = {_fmt(value)}"))

        for key, value in diff["removed"].items():
            await self.emit(self._make_entry(f"- {key} (was: {_fmt(value)})"))

        for key, change in diff["changed"].items():
            await self.emit(self._make_entry(
                f"{key}: {_fmt(change['old'])} → {_fmt(change['new'])}"
            ))

        self._previous = current


def _fmt(value: object) -> str:
    """Format a plist value for log display."""
    if isinstance(value, str):
        return repr(value)
    return str(value)
