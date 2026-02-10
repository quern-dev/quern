"""Source adapter for idevicesyslog (libimobiledevice).

Spawns `idevicesyslog` as a subprocess and parses its stdout line-by-line
into structured LogEntry objects.

Expected line formats (device name may or may not be present):
    Feb  7 14:23:01 iPhone MyApp(CoreFoundation)[1234] <Notice>: message text
    Feb  7 14:23:01 MyApp[1234] <Error>: message text

Dependencies:
    - libimobiledevice must be installed (`brew install libimobiledevice`)
    - A USB-connected iOS device
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime, timezone

from server.models import LogEntry, LogLevel, LogSource
from server.sources import BaseSourceAdapter, EntryCallback

logger = logging.getLogger(__name__)

# Regex to parse idevicesyslog output lines
# Groups: date, time, device (optional), process, subsystem (optional), pid, level, message
# Device name is optional — some idevicesyslog versions omit it.
SYSLOG_PATTERN = re.compile(
    r"^(\w+\s+\d+)\s+"          # date: "Feb  7"
    r"(\d{2}:\d{2}:\d{2})\s+"   # time: "14:23:01"
    r"(?:(\S+)\s+)?"             # device (optional): "iPhone"
    r"(\S+?)"                    # process: "MyApp"
    r"(?:\(([^)]+)\))?"          # subsystem (optional): "(CoreFoundation)"
    r"\[(\d+)\]\s+"              # pid: "[1234]"
    r"<(\w+)>:\s*"               # level: "<Notice>"
    r"(.*)$"                     # message: everything else
)

LEVEL_MAP: dict[str, LogLevel] = {
    "debug": LogLevel.DEBUG,
    "info": LogLevel.INFO,
    "notice": LogLevel.NOTICE,
    "warning": LogLevel.WARNING,
    "error": LogLevel.ERROR,
    "fault": LogLevel.FAULT,
    # idevicesyslog sometimes uses abbreviated forms
    "d": LogLevel.DEBUG,
    "i": LogLevel.INFO,
    "n": LogLevel.NOTICE,
    "w": LogLevel.WARNING,
    "e": LogLevel.ERROR,
    "f": LogLevel.FAULT,
}


class SyslogAdapter(BaseSourceAdapter):
    """Captures logs from idevicesyslog subprocess."""

    def __init__(
        self,
        device_id: str = "default",
        on_entry: EntryCallback | None = None,
        process_filter: str | None = None,
        udid: str | None = None,
    ) -> None:
        super().__init__(
            adapter_id="syslog",
            adapter_type="idevicesyslog",
            device_id=device_id,
            on_entry=on_entry,
        )
        self.process_filter = process_filter
        self.udid = udid
        self._process: asyncio.subprocess.Process | None = None
        self._read_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Spawn idevicesyslog and begin reading its output."""
        cmd = ["idevicesyslog"]

        if self.udid:
            cmd.extend(["-u", self.udid])
        if self.process_filter:
            cmd.extend(["-p", self.process_filter])

        try:
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            self._error = (
                "idevicesyslog not found. Install libimobiledevice: "
                "brew install libimobiledevice"
            )
            logger.error(self._error)
            return
        except Exception as e:
            self._error = f"Failed to start idevicesyslog: {e}"
            logger.error(self._error)
            return

        self._running = True
        self.started_at = self._now()
        self._read_task = asyncio.create_task(self._read_loop())
        logger.info("idevicesyslog adapter started (process_filter=%s)", self.process_filter)

    async def stop(self) -> None:
        """Terminate the idevicesyslog subprocess and clean up."""
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
        logger.info("idevicesyslog adapter stopped")

    async def _read_loop(self) -> None:
        """Read lines from idevicesyslog stdout and parse them."""
        assert self._process is not None
        assert self._process.stdout is not None

        try:
            async for raw_line in self._process.stdout:
                if not self._running:
                    break

                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue

                entry = self._parse_line(line)
                if entry is not None:
                    await self.emit(entry)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if self._running:
                self._error = f"Read loop error: {e}"
                logger.exception("idevicesyslog read loop failed")
        finally:
            self._running = False

    def _parse_line(self, line: str) -> LogEntry | None:
        """Parse a single idevicesyslog output line into a LogEntry."""
        match = SYSLOG_PATTERN.match(line)
        if not match:
            # Unparseable line — still capture it as raw info
            return LogEntry(
                id=uuid.uuid4().hex[:8],
                timestamp=self._now(),
                device_id=self.device_id,
                level=LogLevel.INFO,
                message=line,
                source=LogSource.SYSLOG,
                raw=line,
            )

        date_str, time_str, device, process, subsystem, pid_str, level_str, message = (
            match.groups()
        )

        # Parse the timestamp (idevicesyslog doesn't include year)
        now = datetime.now(timezone.utc)
        try:
            ts = datetime.strptime(
                f"{now.year} {date_str} {time_str}", "%Y %b %d %H:%M:%S"
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            ts = now

        level = LEVEL_MAP.get(level_str.lower(), LogLevel.INFO)

        return LogEntry(
            id=uuid.uuid4().hex[:8],
            timestamp=ts,
            device_id=self.device_id,
            process=process,
            subsystem=subsystem or "",
            pid=int(pid_str) if pid_str else None,
            level=level,
            message=message,
            source=LogSource.SYSLOG,
            raw=line,
        )
