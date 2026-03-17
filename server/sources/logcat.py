"""Source adapter for Android device logs via adb logcat.

Spawns `adb -s <serial> logcat -v threadtime` as a subprocess and parses
the output line-by-line into LogEntry objects.

Expected threadtime format:
    03-08 14:22:45.123  1234  5678 D MyTag  : message text

This adapter is on-demand — agents start/stop it when they want to capture
Android device logs, similar to PhysicalDeviceLogAdapter for iOS devices.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import UTC, datetime

from server.models import LogEntry, LogLevel, LogSource
from server.sources import BaseSourceAdapter, EntryCallback

logger = logging.getLogger(__name__)

# Regex to parse logcat threadtime format
# Format: "03-08 14:22:45.123  1234  5678 D MyTag  : message"
# Groups: date, time, pid, tid, level, tag, message
LOGCAT_PATTERN = re.compile(
    r"^(\d{2}-\d{2})\s+"          # date: "03-08"
    r"(\d{2}:\d{2}:\d{2}\.\d+)\s+"  # time: "14:22:45.123"
    r"(\d+)\s+"                    # pid: "1234"
    r"(\d+)\s+"                    # tid: "5678"
    r"([VDIWEFA])\s+"             # level: "D"
    r"(.+?)\s*:\s*"               # tag: "MyTag"
    r"(.*)$"                       # message: everything else
)

LOGCAT_LEVEL_MAP: dict[str, LogLevel] = {
    "V": LogLevel.DEBUG,
    "D": LogLevel.DEBUG,
    "I": LogLevel.INFO,
    "W": LogLevel.WARNING,
    "E": LogLevel.ERROR,
    "F": LogLevel.FAULT,
    "A": LogLevel.FAULT,
}


class LogcatAdapter(BaseSourceAdapter):
    """Captures Android device logs via `adb logcat -v threadtime`."""

    def __init__(
        self,
        serial: str,
        device_id: str = "default",
        on_entry: EntryCallback | None = None,
        process_filter: str | None = None,
        tag_filter: str | None = None,
    ) -> None:
        super().__init__(
            adapter_id=f"logcat-{serial[:8]}",
            adapter_type="adb_logcat",
            device_id=device_id,
            on_entry=on_entry,
        )
        self.serial = serial
        self.process_filter = process_filter
        self.tag_filter = tag_filter
        self._process: asyncio.subprocess.Process | None = None
        self._read_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Clear logcat buffer, then spawn adb logcat and begin reading output."""
        import shutil

        if not shutil.which("adb"):
            self._error = "adb not found on PATH"
            logger.error(self._error)
            return

        # Clear existing buffer so we only get new entries
        try:
            proc = await asyncio.create_subprocess_exec(
                "adb", "-s", self.serial, "logcat", "-c",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
        except Exception:
            pass  # Non-fatal — proceed even if clear fails

        cmd = ["adb", "-s", self.serial, "logcat", "-v", "threadtime"]

        # Add tag filter if specified (e.g. "MyTag:D *:S")
        if self.tag_filter:
            cmd.extend(self.tag_filter.split())

        try:
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            self._error = "adb not found on PATH"
            logger.error(self._error)
            return
        except Exception as e:
            self._error = f"Failed to start adb logcat: {e}"
            logger.error(self._error)
            return

        self._running = True
        self.started_at = self._now()
        self._read_task = asyncio.create_task(self._read_loop())
        logger.info(
            "Logcat adapter started (serial=%s, process=%s, tag=%s)",
            self.serial[:8],
            self.process_filter,
            self.tag_filter,
        )

    async def stop(self) -> None:
        """Terminate the adb logcat subprocess and clean up."""
        self._running = False

        if self._process and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except TimeoutError:
                self._process.kill()

        if self._read_task and not self._read_task.done():
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass

        self._process = None
        self._read_task = None
        logger.info("Logcat adapter stopped (serial=%s)", self.serial[:8])

    async def _read_loop(self) -> None:
        """Read lines from adb logcat stdout and parse them."""
        assert self._process is not None
        assert self._process.stdout is not None

        try:
            async for raw_line in self._process.stdout:
                if not self._running:
                    break

                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue

                # Skip logcat header lines (e.g. "--------- beginning of main")
                if line.startswith("---------"):
                    continue

                entry = self._parse_line(line)
                if entry is not None:
                    # Apply process filter (logcat doesn't support process filtering natively)
                    if (self.process_filter
                            and self.process_filter.lower() not in entry.process.lower()):
                        continue
                    await self.emit(entry)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if self._running:
                self._error = f"Read loop error: {e}"
                logger.exception("Logcat read loop failed")
        finally:
            self._running = False

    def _parse_line(self, line: str) -> LogEntry | None:
        """Parse a single logcat threadtime line into a LogEntry."""
        match = LOGCAT_PATTERN.match(line)
        if not match:
            # Continuation line or unparseable — emit as-is
            return LogEntry(
                id=uuid.uuid4().hex[:8],
                timestamp=self._now(),
                device_id=self.device_id,
                level=LogLevel.INFO,
                message=line,
                source=LogSource.LOGCAT,
                raw=line,
            )

        date_str, time_str, pid_str, tid_str, level_char, tag, message = match.groups()

        # Parse timestamp — logcat doesn't include year, use current year
        now = datetime.now(UTC)
        try:
            ts = datetime.strptime(
                f"{now.year}-{date_str} {time_str}",
                "%Y-%m-%d %H:%M:%S.%f",
            ).replace(tzinfo=UTC)
        except ValueError:
            ts = self._now()

        level = LOGCAT_LEVEL_MAP.get(level_char, LogLevel.INFO)

        return LogEntry(
            id=uuid.uuid4().hex[:8],
            timestamp=ts,
            device_id=self.device_id,
            process=tag.strip(),
            pid=int(pid_str) if pid_str else None,
            level=level,
            message=message,
            source=LogSource.LOGCAT,
            raw=line,
        )
