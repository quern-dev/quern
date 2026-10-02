"""Source adapter for physical device logs via pymobiledevice3 syslog.

Spawns `pymobiledevice3 syslog live` as a subprocess with optional
process filtering, and parses the output line-by-line into LogEntry objects.

Expected line format from pymobiledevice3:
    2026-02-21 21:22:45.272141 LogTester{Foundation}[2915] <NOTICE>: message text

This differs from idevicesyslog format (used by SyslogAdapter):
- Full ISO date instead of "Mon DD HH:MM:SS"
- Curly braces for subsystem instead of parentheses
- Uppercase level names (NOTICE, ERROR, DEBUG, INFO, FAULT)

With a pymobiledevice3 that has `--format json`, each line is a JSON object
instead, and carries what the text form leaves out: the os_log subsystem and
category (`label`) alongside the sending library (`image_name`). The text form
puts the library in braces where a subsystem would go, so `subsystem` used to
hold "CFNetwork" on a device and "com.apple.CFNetwork" on a simulator, and a
filter written for one did nothing on the other: `device-quiet`'s
`com.apple.network` exclude never matched a device line. JSON is used when the
installed pymobiledevice3 offers it; the text form remains the fallback.

This adapter is on-demand — agents start/stop it when they want to capture
physical device app logs, similar to SimulatorLogAdapter for simulators.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import posixpath
import re
import uuid
from datetime import UTC, datetime

from server.device.tunneld import find_pymobiledevice3_binary, resolve_tunnel_udid
from server.models import LogEntry, LogLevel, LogSource
from server.sources import BaseSourceAdapter, EntryCallback

logger = logging.getLogger(__name__)

_UNCHANGED = object()  # Sentinel for reconfigure() defaults

#: A line can carry a long message; asyncio's default 64 KiB line limit ends
#: the read loop on the first one that is longer.
_LINE_LIMIT = 4 * 1024 * 1024

#: (binary, its modification time) -> whether its `syslog live` takes
#: `--format json`. The time is in the key so an upgrade or a downgrade of
#: pymobiledevice3 -- which quern offers -- is asked again, not answered from
#: before it.
_JSON_SUPPORT: dict[tuple[str, int], bool] = {}

#: The help text, plainly: rich colours it under FORCE_COLOR and wraps it to
#: COLUMNS, and either split `--format` so the probe missed it (review).
#: How long the help text may take before JSON is given up on for this start.
PROBE_TIMEOUT = 30.0  # s

_PLAIN_ENV = {"NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "200"}
_ANSI = re.compile(rb"\x1b\[[0-9;]*m")

TEXT_MODE_WARNING = (
    "this pymobiledevice3 has no `syslog live --format json`, so device lines "
    "carry the sending library and no os_log subsystem: subsystem filters on "
    "com.apple.* -- device-quiet's included -- match nothing. "
    "`pipx upgrade pymobiledevice3` fixes it."
)


def _probe_key(binary: str) -> tuple[str, int]:
    try:
        return binary, os.stat(binary).st_mtime_ns
    except OSError:
        return binary, 0


async def _supports_json(binary: str) -> bool:
    """Whether this pymobiledevice3's `syslog live` has `--format json`.

    Asked once per binary until it changes. A help text that cannot be read
    -- no binary, a timeout, a non-zero exit -- counts as no for this start
    and is not kept: the text form works everywhere, only with less in it,
    and the next start asks again."""
    key = _probe_key(binary)
    if key not in _JSON_SUPPORT:
        try:
            proc = await asyncio.create_subprocess_exec(
                binary, "syslog", "live", "--help",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                env={**os.environ, **_PLAIN_ENV})
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), PROBE_TIMEOUT)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                raise
            if proc.returncode != 0:
                raise OSError(f"`syslog live --help` exited {proc.returncode}: "
                              f"{out.decode(errors='replace').strip()[-200:]}")
            out = _ANSI.sub(b"", out)
            _JSON_SUPPORT[key] = b"--format" in out and b"json" in out
        except (OSError, TimeoutError) as e:
            logger.warning("Could not read pymobiledevice3's syslog options (%s); "
                           "device logs will carry no os_log subsystem", e)
            return False
    return _JSON_SUPPORT[key]

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

def host_local_to_utc(naive: datetime) -> datetime:
    """A `pymobiledevice3` timestamp, which is host-local time, as UTC.

    `pymobiledevice3` builds each line's time with `datetime.fromtimestamp()`
    and no zone (`services/os_trace.py`, 11.19.1), which is naive *local*
    time on the machine running it -- this one. Stamping that as UTC put
    every physical-device line off by the host's UTC offset, seven hours on a
    Mac in Pacific time: outside every "last N minutes" query and every trace
    interval, while a windowed query reported itself complete (#255).

    `astimezone()` on a naive value assumes system local time and applies the
    offset in force on that date, so lines from summer and winter both
    convert correctly. One hour a year cannot: when clocks fall back, the
    repeated hour's local times occur twice, and a naive value does not say
    which pass it came from. Python takes the first, so lines from the
    second pass land an hour early. Nothing in the line can recover it --
    pymobiledevice3 dropped the information when it formatted the time.
    """
    return naive.astimezone(UTC)


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
        device_id: str = "",
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
        #: "json" or "text": what this capture asked pymobiledevice3 for. Only
        #: a JSON capture's lines are read as JSON -- a text capture's
        #: continuation line can be a JSON body of its own (review).
        self.output_format: str | None = None
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
        if await _supports_json(str(binary)):
            cmd.extend(["--format", "json"])
            self.output_format = "json"
            self._note = None
        else:
            self.output_format = "text"
            self._note = TEXT_MODE_WARNING

        return cmd

    async def reconfigure(
        self,
        process_filter: str | None = _UNCHANGED,  # type: ignore[assignment]
        match_filter: str | None = _UNCHANGED,  # type: ignore[assignment]
    ) -> None:
        """Stop subprocess, update filters, restart. Preserves on_entry callback."""
        was_running = self._running
        if was_running:
            await self.stop()
        if process_filter is not _UNCHANGED:
            self.process_filter = process_filter
        if match_filter is not _UNCHANGED:
            self.match_filter = match_filter
        self.entries_captured = 0
        self._error = None
        if was_running:
            await self.start()

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
                limit=_LINE_LIMIT,
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
            if self._running:
                # The output ended while nobody asked it to: pymobiledevice3
                # exited. Say why -- a downgraded one rejects `--format` at
                # once, and the capture would otherwise just stop (review).
                self._error = await self._exit_reason()
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
        if self.output_format == "json" and line.startswith("{"):
            entry = self._parse_json(line)
            if entry is not None:
                return entry
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
            ts = host_local_to_utc(datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S.%f"))
        except ValueError:
            ts = self._now()

        level = PMD3_LEVEL_MAP.get(level_str.lower(), LogLevel.INFO)

        return LogEntry(
            id=uuid.uuid4().hex[:8],
            timestamp=ts,
            device_id=self.device_id,
            process=process,
            # The text form's braces hold the sending library, not an os_log
            # subsystem; it goes where that belongs.
            sender=subsystem or "",
            pid=int(pid_str) if pid_str else None,
            level=level,
            message=message,
            source=LogSource.DEVICE,
            raw=line,
        )

    def _parse_json(self, line: str) -> LogEntry | None:
        """One `--format json` line, or None if it is not one -- then it is
        parsed as text, and kept raw at worst, never dropped."""
        try:
            d = json.loads(line)
            # pymobiledevice3's own keys, not just any object with a message.
            if not isinstance(d, dict) or not {"pid", "timestamp", "level", "message"} <= d.keys():
                return None
            label = d.get("label") or {}
            try:
                ts = host_local_to_utc(datetime.fromisoformat(d["timestamp"]))
            except (KeyError, TypeError, ValueError):
                ts = self._now()
            process = posixpath.basename(d.get("filename") or "")
            subsystem = label.get("subsystem") or ""
            category = label.get("category") or ""
            sender = posixpath.basename(d.get("image_name") or "")
            pid = d.get("pid") if isinstance(d.get("pid"), int) else None
            level_name = str(d.get("level") or "")
            message = str(d.get("message") or "")
            return LogEntry(
                id=uuid.uuid4().hex[:8],
                timestamp=ts,
                device_id=self.device_id,
                process=process,
                subsystem=subsystem,
                category=category,
                sender=sender,
                pid=pid,
                level=PMD3_LEVEL_MAP.get(level_name.lower(), LogLevel.INFO),
                message=message,
                source=LogSource.DEVICE,
                # The text form, label included -- not the JSON object, whose
                # UUIDs, offsets and container paths doubled what every log
                # query returned per entry (review).
                raw=(f"{d.get('timestamp')} {process}{{{sender}}}[{pid}] <{level_name}>: "
                     f"{message}" + (f" [{subsystem}][{category}]" if subsystem else "")),
            )
        except (ValueError, TypeError, AttributeError):
            return None

    async def _exit_reason(self) -> str:
        """What pymobiledevice3 said as it exited on its own."""
        proc = self._process
        code = None
        detail = b""
        if proc is not None:
            try:
                code = await asyncio.wait_for(proc.wait(), 5)
                if proc.stderr is not None:
                    detail = await asyncio.wait_for(proc.stderr.read(4096), 2)
            except (TimeoutError, OSError, ValueError):
                pass
        text = detail.decode(errors="replace").strip()[-300:]
        return f"pymobiledevice3 syslog exited ({code})" + (f": {text}" if text else "")

