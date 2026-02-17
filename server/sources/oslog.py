"""Source adapter for macOS `log stream` (OSLog / Unified Logging).

Spawns `log stream --style json` as a subprocess and parses JSON output
line-by-line into structured LogEntry objects.

OSLog messageType mapping:
    Default → INFO, Info → INFO, Debug → DEBUG, Error → ERROR, Fault → FAULT

Dependencies:
    - macOS only (the `log` command is not available on Linux)
    - A USB-connected iOS device (or simulator)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone

from server.models import LogEntry, LogLevel, LogSource
from server.sources import BaseSourceAdapter, EntryCallback

logger = logging.getLogger(__name__)

# Map OSLog messageType values to our LogLevel enum
OSLOG_LEVEL_MAP: dict[str, LogLevel] = {
    "default": LogLevel.INFO,
    "info": LogLevel.INFO,
    "debug": LogLevel.DEBUG,
    "error": LogLevel.ERROR,
    "fault": LogLevel.FAULT,
}

# Regex for the OSLog timestamp format: "2026-02-07 14:23:01.234567-0800"
OSLOG_TIMESTAMP_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2}\.\d+)([+-]\d{4})"
)


def parse_oslog_timestamp(ts_str: str) -> datetime:
    """Parse an OSLog timestamp string into a UTC datetime."""
    match = OSLOG_TIMESTAMP_RE.match(ts_str)
    if not match:
        return datetime.now(timezone.utc)

    date_part, time_part, tz_offset = match.groups()
    # Truncate microseconds to 6 digits if longer
    time_parts = time_part.split(".")
    if len(time_parts) == 2 and len(time_parts[1]) > 6:
        time_part = f"{time_parts[0]}.{time_parts[1][:6]}"

    iso_str = f"{date_part}T{time_part}{tz_offset[:3]}:{tz_offset[3:]}"
    try:
        return datetime.fromisoformat(iso_str).astimezone(timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


def extract_process_name(image_path: str) -> str:
    """Extract the process name from a processImagePath."""
    if not image_path:
        return ""
    # /private/var/.../MyApp → MyApp
    return image_path.rsplit("/", 1)[-1]


class OslogAdapter(BaseSourceAdapter):
    """Captures logs from macOS `log stream --style json` subprocess."""

    def __init__(
        self,
        device_id: str = "default",
        on_entry: EntryCallback | None = None,
        subsystem_filter: str | None = None,
        process_filter: str | None = None,
    ) -> None:
        super().__init__(
            adapter_id="oslog",
            adapter_type="log_stream",
            device_id=device_id,
            on_entry=on_entry,
        )
        self.subsystem_filter = subsystem_filter
        self.process_filter = process_filter
        self._process: asyncio.subprocess.Process | None = None
        self._read_task: asyncio.Task | None = None

    def _build_command(self) -> list[str]:
        """Build the log stream command with appropriate filters."""
        cmd = ["log", "stream", "--style", "json"]

        predicates: list[str] = []
        if self.subsystem_filter:
            predicates.append(f'subsystem == "{self.subsystem_filter}"')
        if self.process_filter:
            predicates.append(f'processImagePath ENDSWITH "{self.process_filter}"')

        if predicates:
            cmd.extend(["--predicate", " AND ".join(predicates)])

        return cmd

    async def start(self) -> None:
        """Spawn `log stream` and begin reading its JSON output."""
        cmd = self._build_command()

        try:
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            self._error = "log command not found. This adapter requires macOS."
            logger.error(self._error)
            return
        except Exception as e:
            self._error = f"Failed to start log stream: {e}"
            logger.error(self._error)
            return

        self._running = True
        self.started_at = self._now()
        self._read_task = asyncio.create_task(self._read_loop())
        logger.info(
            "OSLog adapter started (subsystem=%s, process=%s)",
            self.subsystem_filter,
            self.process_filter,
        )

    async def stop(self) -> None:
        """Terminate the log stream subprocess and clean up."""
        self._running = False

        if self._process and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._process.kill()

        if self._read_task and not self._read_task.done():
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass

        self._process = None
        self._read_task = None
        logger.info("OSLog adapter stopped")

    async def _read_loop(self) -> None:
        """Read lines from log stream stdout and parse JSON objects."""
        assert self._process is not None
        assert self._process.stdout is not None

        try:
            async for raw_line in self._process.stdout:
                if not self._running:
                    break

                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue

                entry = self._parse_json_line(line)
                if entry is not None:
                    await self.emit(entry)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if self._running:
                self._error = f"Read loop error: {e}"
                logger.exception("OSLog read loop failed")
        finally:
            self._running = False

    def _parse_json_line(self, line: str) -> LogEntry | None:
        """Parse a single JSON line from `log stream --style json` output.

        Lines that aren't valid JSON (e.g., the opening/closing brackets of
        the JSON array, or comma separators) are silently skipped.
        """
        # log stream wraps output in a JSON array — strip leading commas/brackets
        stripped = line.strip().strip(",").strip("[").strip("]").strip(",")
        if not stripped or stripped in ("{", "}"):
            return None

        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            return None

        if not isinstance(data, dict):
            return None

        # Skip non-log events (e.g., activity events)
        event_type = data.get("eventType", "")
        if event_type and event_type != "logEvent":
            return None

        message = data.get("eventMessage", "")
        if not message and not data.get("formatString", ""):
            return None

        # Use eventMessage, fallback to formatString
        if not message:
            message = data.get("formatString", "")

        message_type = data.get("messageType", "Default").lower()
        level = OSLOG_LEVEL_MAP.get(message_type, LogLevel.INFO)

        timestamp_str = data.get("timestamp", "")
        timestamp = parse_oslog_timestamp(timestamp_str) if timestamp_str else self._now()

        process_path = data.get("processImagePath", "")
        process_name = extract_process_name(process_path)

        return LogEntry(
            id=uuid.uuid4().hex[:8],
            timestamp=timestamp,
            device_id=self.device_id,
            process=process_name,
            subsystem=data.get("subsystem", ""),
            category=data.get("category", ""),
            pid=data.get("processID"),
            level=level,
            message=message,
            source=LogSource.OSLOG,
            raw=line,
        )
