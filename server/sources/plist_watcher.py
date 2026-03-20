"""Plist watcher source adapter — polls a plist file and emits changes as log entries."""

from __future__ import annotations

import asyncio
import collections
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

# Values longer than this are truncated in log messages
_MAX_VALUE_LEN = 120


class PlistWatcherAdapter(BaseSourceAdapter):
    """Polls a plist file and emits per-key change entries into the log pipeline."""

    def __init__(
        self,
        udid: str,
        bundle_id: str,
        container: str,
        plist_path: str,
        poll_interval: float = 1.0,
        ignore_prefixes: list[str] | None = None,
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
        self.ignore_prefixes = ignore_prefixes or []
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

            # Read initial snapshot
            self._previous = await read_plist(self._resolved_path)

            # Emit concise summary at INFO (visible in tail_logs level=info)
            summary = _summarize_keys(self._previous)
            if self.ignore_prefixes:
                ignore_note = f", ignoring {len(self.ignore_prefixes)} prefixes"
            else:
                ignore_note = ""
            msg = (
                f"Watching {self._subsystem()} — "
                f"{len(self._previous)} keys ({summary}){ignore_note}"
            )
            await self.emit(self._make_entry(msg))

            # Emit full snapshot at DEBUG (visible with level=debug)
            await self.emit(self._make_entry(
                f"Full snapshot:\n{json.dumps(self._previous, indent=2, default=str)}",
                LogLevel.DEBUG,
            ))

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
        # Final diff before stopping — catches writes during app termination
        if self._running:
            try:
                await self._check_for_changes()
            except Exception:
                pass

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

    def _is_ignored(self, key: str) -> bool:
        return any(key.startswith(p) for p in self.ignore_prefixes)

    async def _check_for_changes(self) -> None:
        if self._resolved_path is None or not self._resolved_path.exists():
            return

        current = await read_plist(self._resolved_path)
        if self._previous is None:
            self._previous = current
            return

        diff = diff_plists(self._previous, current)

        for key, value in diff["added"].items():
            if not self._is_ignored(key):
                await self.emit(self._make_entry(f"+ {key} = {_fmt(value)}"))

        for key, value in diff["removed"].items():
            if not self._is_ignored(key):
                await self.emit(self._make_entry(f"- {key} (was: {_fmt(value)})"))

        for key, change in diff["changed"].items():
            if not self._is_ignored(key):
                await self.emit(self._make_entry(
                    f"{key}: {_fmt(change['old'])} → {_fmt(change['new'])}"
                ))

        self._previous = current


def _fmt(value: object) -> str:
    """Format a plist value for log display, truncating large blobs."""
    if isinstance(value, str):
        if len(value) > _MAX_VALUE_LEN:
            return f"({len(value)} chars)"
        return repr(value)
    s = str(value)
    if len(s) > _MAX_VALUE_LEN:
        return f"({len(s)} chars)"
    return s


def _summarize_keys(data: dict) -> str:
    """Summarize keys by common prefix for the initial snapshot log entry."""
    prefixes: dict[str, int] = collections.Counter()
    for key in data:
        # Find a meaningful prefix: up to the first uppercase transition or underscore boundary
        # e.g., "kHasSeenTip1" → "kHasSeen", "INTERNAL_ENV" → "INTERNAL_"
        prefix = key
        for i, ch in enumerate(key):
            if i > 2 and (ch.isupper() or ch == "_"):
                candidate = key[: i + 1] if ch == "_" else key[:i]
                if len(candidate) >= 3:
                    prefix = candidate + "*"
                    break
        prefixes[prefix] += 1

    # Show prefixes with >1 key, plus count of unique keys
    groups = sorted(
        ((p, c) for p, c in prefixes.items() if c > 1),
        key=lambda x: -x[1],
    )[:5]
    unique = sum(1 for c in prefixes.values() if c == 1)

    parts = [f"{count} {prefix}" for prefix, count in groups]
    if unique:
        parts.append(f"{unique} other")
    return ", ".join(parts)
