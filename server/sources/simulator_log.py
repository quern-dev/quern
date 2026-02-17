"""Source adapter for simulator app logs via `xcrun simctl spawn <UDID> log stream`.

Captures os_log, Logger, and NSLog output from apps running inside iOS simulators.
The JSON output format is identical to macOS `log stream --style json`, so we reuse
the same parsing helpers from the OSLog adapter.

This adapter is on-demand — agents start/stop it when they want to capture
simulator app logs, unlike the always-running OSLog adapter.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

from server.models import LogEntry, LogLevel, LogSource
from server.sources import BaseSourceAdapter, EntryCallback
from server.sources.oslog import (
    OSLOG_LEVEL_MAP,
    extract_process_name,
    parse_oslog_timestamp,
)

logger = logging.getLogger(__name__)


class SimulatorLogAdapter(BaseSourceAdapter):
    """Captures simulator app logs via `xcrun simctl spawn <UDID> log stream`."""

    def __init__(
        self,
        udid: str,
        device_id: str = "default",
        on_entry: EntryCallback | None = None,
        process_filter: str | None = None,
        subsystem_filter: str | None = None,
        level: str = "debug",
    ) -> None:
        super().__init__(
            adapter_id=f"simlog-{udid[:8]}",
            adapter_type="simctl_log_stream",
            device_id=device_id,
            on_entry=on_entry,
        )
        self.udid = udid
        self.process_filter = process_filter
        self.subsystem_filter = subsystem_filter
        self.level = level
        self._process: asyncio.subprocess.Process | None = None
        self._read_task: asyncio.Task | None = None

    def _build_command(self) -> list[str]:
        """Build the simctl log stream command with filters."""
        cmd = [
            "xcrun", "simctl", "spawn", self.udid,
            "log", "stream", "--style", "json", "--level", self.level,
        ]

        predicates: list[str] = []
        if self.process_filter:
            predicates.append(f'process == "{self.process_filter}"')
        if self.subsystem_filter:
            predicates.append(f'subsystem == "{self.subsystem_filter}"')

        if predicates:
            cmd.extend(["--predicate", " AND ".join(predicates)])

        return cmd

    async def start(self) -> None:
        """Spawn simctl log stream and begin reading JSON output."""
        cmd = self._build_command()

        try:
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            self._error = "xcrun not found. Install Xcode Command Line Tools."
            logger.error(self._error)
            return
        except Exception as e:
            self._error = f"Failed to start simctl log stream: {e}"
            logger.error(self._error)
            return

        self._running = True
        self.started_at = self._now()
        self._read_task = asyncio.create_task(self._read_loop())
        logger.info(
            "SimulatorLog adapter started (udid=%s, process=%s, subsystem=%s)",
            self.udid[:8],
            self.process_filter,
            self.subsystem_filter,
        )

    async def stop(self) -> None:
        """Terminate the simctl log stream subprocess and clean up."""
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
        logger.info("SimulatorLog adapter stopped (udid=%s)", self.udid[:8])

    async def _read_loop(self) -> None:
        """Read lines from simctl log stream stdout and parse JSON objects.

        simctl spawn's log stream outputs pretty-printed JSON in an array,
        unlike host-side `log stream` which outputs compact single-line JSON.
        We accumulate characters and track brace depth (outside JSON strings)
        to detect complete objects. Handles `},{` separators correctly.
        """
        assert self._process is not None
        assert self._process.stdout is not None

        # Character-level accumulator for pretty-printed JSON
        obj_chars: list[str] = []
        brace_depth = 0
        in_string = False
        escape_next = False

        try:
            async for raw_line in self._process.stdout:
                if not self._running:
                    break

                line = raw_line.decode("utf-8", errors="replace").rstrip()

                for ch in line:
                    if escape_next:
                        escape_next = False
                        if brace_depth > 0:
                            obj_chars.append(ch)
                        continue

                    if ch == "\\" and in_string:
                        escape_next = True
                        if brace_depth > 0:
                            obj_chars.append(ch)
                        continue

                    if ch == '"' and not escape_next:
                        if brace_depth > 0:
                            in_string = not in_string
                            obj_chars.append(ch)
                        continue

                    if in_string:
                        obj_chars.append(ch)
                        continue

                    # Outside strings — track braces
                    if ch == "{":
                        brace_depth += 1
                        obj_chars.append(ch)
                    elif ch == "}":
                        brace_depth -= 1
                        obj_chars.append(ch)
                        if brace_depth == 0:
                            # Complete JSON object
                            raw = "".join(obj_chars)
                            obj_chars.clear()
                            in_string = False
                            escape_next = False

                            entry = self._parse_json_line(raw)
                            if entry is not None:
                                await self.emit(entry)
                    elif brace_depth > 0:
                        obj_chars.append(ch)
                    # else: outside object, skip (array brackets, commas, preamble)

                # Add newline to preserve multi-line structure for JSON parsing
                if brace_depth > 0:
                    obj_chars.append("\n")

        except asyncio.CancelledError:
            raise
        except Exception as e:
            if self._running:
                self._error = f"Read loop error: {e}"
                logger.exception("SimulatorLog read loop failed")
        finally:
            self._running = False

    def _parse_json_line(self, line: str) -> LogEntry | None:
        """Parse a JSON object from simctl log stream output.

        Handles both compact single-line and pretty-printed multi-line JSON.
        The underlying JSON structure is identical to macOS `log stream --style json`.
        """
        stripped = line.strip().strip(",").strip("[").strip("]").strip(",")
        if not stripped or stripped in ("{", "}"):
            return None

        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            return None

        if not isinstance(data, dict):
            return None

        event_type = data.get("eventType", "")
        if event_type and event_type != "logEvent":
            return None

        message = data.get("eventMessage", "")
        if not message and not data.get("formatString", ""):
            return None

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
            source=LogSource.SIMULATOR,
            raw=line,
        )
