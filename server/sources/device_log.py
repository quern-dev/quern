"""Source adapter for physical device logs via pymobiledevice3 syslog.

Spawns `pymobiledevice3 syslog live` as a subprocess with optional
process filtering, and parses the output line-by-line into LogEntry objects.

Expected line format from pymobiledevice3:
    2026-02-21 21:22:45.272141 LogTester{Foundation}[2915] <NOTICE>: message text

This differs from idevicesyslog format (used by SyslogAdapter):
- Full ISO date instead of "Mon DD HH:MM:SS"
- Curly braces for subsystem instead of parentheses
- Uppercase level names (NOTICE, ERROR, DEBUG, INFO, FAULT)

This adapter is on-demand — agents start/stop it when they want to capture
physical device app logs, similar to SimulatorLogAdapter for simulators.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime, timezone

from server.device.tunneld import find_pymobiledevice3_binary, resolve_tunnel_udid
from server.models import LogEntry, LogLevel, LogSource
from server.sources import BaseSourceAdapter, EntryCallback

logger = logging.getLogger(__name__)

# Regex to parse pymobiledevice3 syslog live output lines
# Format: "2026-02-21 21:22:45.272141 LogTester{Foundation}[2915] <NOTICE>: message"
# Groups: datetime, process, subsystem (optional), pid, level, message
PMD3_SYSLOG_PATTERN = re.compile(
    r"^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+)\s+"  # datetime: "2026-02-21 21:22:45.272141"
    r"(\S+?)"                                               # process: "LogTester"
    r"(?:\{([^}]+)\})?"                                      # subsystem (optional): "{Foundation}"
    r"\[(\d+)\]\s+"                                          # pid: "[2915]"
    r"<(\w+)>:\s*"                                           # level: "<NOTICE>"
    r"(.*)$"                                                 # message: everything else
)

PMD3_LEVEL_MAP: dict[str, LogLevel] = {
    "debug": LogLevel.DEBUG,
    "info": LogLevel.INFO,
    "notice": LogLevel.NOTICE,
    "warning": LogLevel.WARNING,
    "error": LogLevel.ERROR,
    "fault": LogLevel.FAULT,
    "default": LogLevel.NOTICE,
}


class PhysicalDeviceLogAdapter(BaseSourceAdapter):
    """Captures physical device logs via `pymobiledevice3 syslog live`."""

    def __init__(
        self,
        udid: str,
        device_id: str = "default",
        on_entry: EntryCallback | None = None,
        process_filter: str | None = None,
        match_filter: str | None = None,
    ) -> None:
        super().__init__(
            adapter_id=f"devlog-{udid[:8]}",
            adapter_type="pymobiledevice3_syslog",
            device_id=device_id,
            on_entry=on_entry,
        )
        self.udid = udid
        self.process_filter = process_filter
        self.match_filter = match_filter
        self._tunnel_udid: str | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._read_task: asyncio.Task | None = None

    async def _build_command(self) -> list[str] | None:
        """Build the pymobiledevice3 syslog live command.

        Returns None if the binary is not found or tunnel resolution fails.
        """
        binary = find_pymobiledevice3_binary()
        if not binary:
            self._error = (
                "pymobiledevice3 not found. Install it: pipx install pymobiledevice3"
            )
            logger.error(self._error)
            return None

        cmd = [str(binary), "syslog", "live"]

        # Try tunnel-first (iOS 17+), fall back to --udid (iOS 16-)
        tunnel_udid = await resolve_tunnel_udid(self.udid)
        if tunnel_udid:
            self._tunnel_udid = tunnel_udid
            cmd.extend(["--tunnel", tunnel_udid])
        else:
            cmd.extend(["--udid", self.udid])

        if self.process_filter:
            cmd.extend(["-pn", self.process_filter])
        if self.match_filter:
            cmd.extend(["-m", self.match_filter])

        return cmd

    async def start(self) -> None:
        """Spawn pymobiledevice3 syslog live and begin reading output."""
        cmd = await self._build_command()
        if cmd is None:
            return

        try:
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            self._error = (
                "pymobiledevice3 not found. Install it: pipx install pymobiledevice3"
            )
            logger.error(self._error)
            return
        except Exception as e:
            self._error = f"Failed to start pymobiledevice3 syslog: {e}"
            logger.error(self._error)
            return

        self._running = True
        self.started_at = self._now()
        self._read_task = asyncio.create_task(self._read_loop())
        logger.info(
            "PhysicalDeviceLog adapter started (udid=%s, process=%s)",
            self.udid[:8],
            self.process_filter,
        )

    async def stop(self) -> None:
        """Terminate the pymobiledevice3 subprocess and clean up."""
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
        logger.info("PhysicalDeviceLog adapter stopped (udid=%s)", self.udid[:8])

    async def _read_loop(self) -> None:
        """Read lines from pymobiledevice3 stdout and parse them."""
        assert self._process is not None
        assert self._process.stdout is not None

        try:
            async for raw_line in self._process.stdout:
                if not self._running:
                    break

                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue

                # Skip the "[connected:...]" header line
                if line.startswith("[connected:"):
                    continue

                entry = self._parse_line(line)
                if entry is not None:
                    await self.emit(entry)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if self._running:
                self._error = f"Read loop error: {e}"
                logger.exception("PhysicalDeviceLog read loop failed")
        finally:
            self._running = False

    def _parse_line(self, line: str) -> LogEntry | None:
        """Parse a single pymobiledevice3 syslog output line into a LogEntry."""
        match = PMD3_SYSLOG_PATTERN.match(line)
        if not match:
            return LogEntry(
                id=uuid.uuid4().hex[:8],
                timestamp=self._now(),
                device_id=self.device_id,
                level=LogLevel.INFO,
                message=line,
                source=LogSource.DEVICE,
                raw=line,
            )

        dt_str, process, subsystem, pid_str, level_str, message = match.groups()

        try:
            ts = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S.%f").replace(
                tzinfo=timezone.utc,
            )
        except ValueError:
            ts = self._now()

        level = PMD3_LEVEL_MAP.get(level_str.lower(), LogLevel.INFO)

        return LogEntry(
            id=uuid.uuid4().hex[:8],
            timestamp=ts,
            device_id=self.device_id,
            process=process,
            subsystem=subsystem or "",
            pid=int(pid_str) if pid_str else None,
            level=level,
            message=message,
            source=LogSource.DEVICE,
            raw=line,
        )
