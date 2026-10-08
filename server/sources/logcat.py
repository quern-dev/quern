"""Source adapter for Android device logs via adb logcat.

Spawns `adb -s <serial> logcat -v threadtime -v UTC -v year` as a subprocess
and parses the output line-by-line into LogEntry objects.

Expected format:
    2026-09-26 18:05:07.696 +0000  1234  5678 D MyTag  : message text

**Why UTC and year.** Plain `threadtime` prints the *device's local time*
with no zone and no year, and this adapter used to stamp that as UTC. On a
device set to Pacific time every line landed seven hours in the past --
measured on an API 32 emulator, `11:05Z` for a line logged at `18:05Z` -- so
Android lines fell outside every "last N minutes" query, the summary and the
trace's action intervals, and a windowed query answered "nothing here, and
complete" after capturing thousands of lines (#255). The year also removes a
New Year's Eve bug: the old parse assumed the current year. Both modifiers
exist from Android 7 (API 24); older devices get plain `threadtime`, whose
lines are stamped on arrival (see `_parse_line`). Measured on API 32 and 34.

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
from server.sources.android_crash import AndroidCrashDetector, crash_specs_for, process_matches

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

# The format actually requested: threadtime with `-v UTC -v year`.
# Groups: datetime, zone, pid, tid, level, tag, message
LOGCAT_UTC_PATTERN = re.compile(
    r"^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+)\s+"  # "2026-09-26 18:05:07.696"
    r"([+-]\d{4})\s+"                                    # zone: "+0000"
    r"(\d+)\s+"                                          # pid
    r"(\d+)\s+"                                          # tid
    r"([VDIWEFA])\s+"                                     # level
    r"(.+?)\s*:\s*"                                      # tag
    r"(.*)$"                                               # message
)

#: `-v UTC` and `-v year` exist from Android 7.
UTC_FORMAT_MIN_API = 24

#: How long a freshly spawned logcat gets to reject its arguments.
STARTUP_GRACE_S = 0.5

#: How long to wait for the device to answer getprop before starting anyway.
GETPROP_TIMEOUT_S = 10

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
        device_id: str = "",
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
        self._crashes = AndroidCrashDetector(device_id=device_id)
        self.api_level: int | None = None

    async def start(self) -> None:
        """Spawn adb logcat from the newest line on, and begin reading output."""
        import shutil

        if not shutil.which("adb"):
            self._error = "adb not found on PATH"
            logger.error(self._error)
            return

        # Start from now without destroying what came before. This used to
        # run `logcat -c` first, which empties the *device's* log buffers --
        # history belonging to whoever else reads that device (Android
        # Studio, a bug report, another tool) and to the user, gone because
        # quern wanted a clean start. `-T 1` gets the same clean start by
        # reading from the newest line on, leaving the buffers intact; the
        # cost is that one line from before capture comes through.
        #
        # The format depends on the device. Before Android 7 logcat's `-v`
        # takes format names only and *exits* on `UTC` or `year`, so asking
        # for them unconditionally turned capture on those devices into a
        # process that died at once while the start call reported success.
        self.api_level = await self._api_level()
        cmd = ["adb", "-s", self.serial, "logcat", "-v", "threadtime"]
        if self.api_level is None or self.api_level >= UTC_FORMAT_MIN_API:
            # Unknown is treated as modern: every device measured is, and if
            # this one is not, the early-exit check below says so rather
            # than capture silently dying.
            cmd += ["-v", "UTC", "-v", "year"]
        cmd += ["-T", "1"]

        # Add tag filter if specified (e.g. "MyTag:D *:S"), plus the tags a
        # crash is recognised from. A filterspec silences everything it does
        # not name at the device, so without them a tag filter turned crash
        # detection off with nothing to say so.
        if self.tag_filter:
            cmd.extend(self.tag_filter.split())
            cmd.extend(crash_specs_for(self.tag_filter))

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

        # A logcat that rejects its arguments exits at once, and the read loop
        # would see only EOF -- so capture "started" and then stopped with no
        # error anywhere, because stderr was piped and never read. Give it a
        # moment and ask.
        try:
            await asyncio.wait_for(self._process.wait(), timeout=STARTUP_GRACE_S)
        except TimeoutError:
            pass  # still running, which is the point
        else:
            self._error = await self._exit_reason("exited at once", read_stdout=True)
            logger.error(self._error)
            self._process = None
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
        # Held before any await: an overlapping stop, or a restart meanwhile,
        # must neither re-read these after another stop cleared them nor have
        # this stop clear a newer run's.
        process, task = self._process, self._read_task

        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except TimeoutError:
                process.kill()

        # The loop runs to EOF, which terminating produces, so lines written
        # before the stop are kept rather than cancelled away.
        await self._drain(task)

        if self._process is process:
            self._process = None
        if self._read_task is task:
            self._read_task = None
        logger.info("Logcat adapter stopped (serial=%s)", self.serial[:8])

    async def _read_loop(self) -> None:
        """Read lines from adb logcat stdout and parse them."""
        assert self._process is not None
        assert self._process.stdout is not None
        process = self._process

        try:
            async for raw_line in process.stdout:
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue

                # Skip logcat header lines (e.g. "--------- beginning of main")
                if line.startswith("---------"):
                    continue

                entry = self._parse_line(line)
                if entry is not None:
                    # Crashes first, and filtered by the process that crashed
                    # rather than the line's tag. A Java crash is logged under
                    # the tag `AndroidRuntime`, so filtering it like any other
                    # line would drop exactly the crash of the app the caller
                    # asked to watch.
                    for crash in self._crashes.feed(entry):
                        if self._wanted_crash(crash):
                            await self.emit(crash)
                    if self._wanted(entry):
                        await self.emit(entry)
            # The stream ended without anyone stopping it: the device went
            # away or logcat died. That is an error to report, not "stopped".
            if self._running and self._process is process:
                self._error = await self._exit_reason("ended unexpectedly")
                logger.error(self._error)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if self._running and self._process is process:
                self._error = f"Read loop error: {e}"
            # Logged during stop's drain too, which processes what the stream
            # had already written; a failure there would otherwise leave no trace.
            logger.exception("Logcat read loop failed")
        finally:
            # Only this run's: a loop still finishing after a restart must not
            # mark the new run stopped or give it this one's error.
            if self._process is process:
                self._running = False
            # A crash whose last line never came is still a crash.
            for crash in self._crashes.flush():
                if self._wanted_crash(crash):
                    try:
                        await self.emit(crash)
                    except Exception:
                        logger.exception("Could not emit a pending Android crash")

    async def _api_level(self) -> int | None:
        """The device's SDK level, or None if it could not be asked.

        None is not an answer and is not treated as one; the caller decides
        what to do without it.
        """
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "adb", "-s", self.serial, "shell", "getprop", "ro.build.version.sdk",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=GETPROP_TIMEOUT_S)
        except (OSError, TimeoutError):
            if proc is not None and proc.returncode is None:
                proc.kill()
            return None
        try:
            return int(out.decode(errors="replace").strip())
        except ValueError:
            return None

    async def _exit_reason(self, what: str, *, read_stdout: bool = False) -> str:
        """Why logcat is gone, from its exit code and whatever it said.

        `read_stdout` is for an exit before the read loop has started. Before
        Android 7, adb has no separate stderr channel from the device: the
        device's "Invalid parameter to -v" arrives on *stdout*, and the exit
        code arrives as 0. Reading stderr alone reported "exit 0: no output"
        for exactly the case this message exists to explain.
        """
        proc = self._process
        said = ""
        streams = [getattr(proc, "stderr", None)]
        if read_stdout:
            streams.append(getattr(proc, "stdout", None))
        for stream in streams:
            if said or stream is None:
                continue
            try:
                raw = await asyncio.wait_for(stream.read(), timeout=2)
            except (OSError, TimeoutError, AttributeError):
                continue
            said = raw.decode(errors="replace").strip()[:500]
        code = proc.returncode if proc is not None else None
        return f"adb logcat {what} (exit {code}): {said or 'no output'}"

    def _wanted(self, entry: LogEntry) -> bool:
        """Logcat cannot filter by process, so the adapter does."""
        return not self.process_filter or self.process_filter.lower() in entry.process.lower()

    def _wanted_crash(self, crash: LogEntry) -> bool:
        """The same for a crash, whose name is not always the package's."""
        return not self.process_filter or process_matches(self.process_filter, crash)

    def _parse_line(self, line: str) -> LogEntry | None:
        """Parse a single logcat threadtime line into a LogEntry."""
        match = LOGCAT_UTC_PATTERN.match(line)
        if match:
            dt_str, zone, pid_str, tid_str, level_char, tag, message = match.groups()
            try:
                ts = datetime.strptime(
                    f"{dt_str} {zone}", "%Y-%m-%d %H:%M:%S.%f %z",
                ).astimezone(UTC)
            except ValueError:
                ts = self._now()
            return LogEntry(
                id=uuid.uuid4().hex[:8],
                timestamp=ts,
                device_id=self.device_id,
                process=tag.strip(),
                pid=int(pid_str),
                level=LOGCAT_LEVEL_MAP.get(level_char, LogLevel.INFO),
                message=message,
                source=LogSource.LOGCAT,
                raw=line,
            )

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

        _date, _time, pid_str, tid_str, level_char, tag, message = match.groups()

        # The legacy format: a device too old for `-v UTC -v year` (before
        # API 24) prints local time with no zone and no year. Reading that as
        # UTC was the seven-hour error; the device's zone is not in the line,
        # so the honest timestamp is arrival on the host -- late by the
        # pipe's latency, rather than wrong by the zone offset.
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
