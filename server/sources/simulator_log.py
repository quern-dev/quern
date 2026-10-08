"""Source adapter for simulator app logs via `xcrun simctl spawn <UDID> log stream`.

Captures os_log, Logger, and NSLog output from apps running inside iOS simulators.
The JSON output format is identical to macOS `log stream --style json`, so we reuse
the same parsing helpers from the OSLog adapter.

This adapter is on-demand — agents start/stop it when they want to capture
simulator app logs, unlike the always-running OSLog adapter.
"""

from __future__ import annotations

import asyncio
import codecs
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

_UNCHANGED = object()  # Sentinel for reconfigure() defaults


class SimulatorLogAdapter(BaseSourceAdapter):
    """Captures simulator app logs via `xcrun simctl spawn <UDID> log stream`."""

    def __init__(
        self,
        udid: str,
        device_id: str = "",
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

    async def reconfigure(
        self,
        process_filter: str | None = _UNCHANGED,  # type: ignore[assignment]
        subsystem_filter: str | None = _UNCHANGED,  # type: ignore[assignment]
        level: str | None = _UNCHANGED,  # type: ignore[assignment]
    ) -> None:
        """Stop subprocess, update filters, restart. Preserves on_entry callback."""
        was_running = self._running
        if was_running:
            await self.stop()
        if process_filter is not _UNCHANGED:
            self.process_filter = process_filter
        if subsystem_filter is not _UNCHANGED:
            self.subsystem_filter = subsystem_filter
        if level is not _UNCHANGED:
            self.level = level or "debug"
        self.entries_captured = 0
        self._error = None
        if was_running:
            await self.start()

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
        # Both held from the start, before any await: an overlapping stop()
        # clears the attributes while this one waits (re-reading them raised
        # AttributeError), and a reconfigure can start a new run meanwhile,
        # whose handles this stop must not touch.
        process = self._process
        task = self._read_task

        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except TimeoutError:
                process.kill()

        if task and not task.done():
            # Terminating the stream closes its stdout, so the read loop reaches
            # EOF on its own after parsing what was already written -- an entry
            # logged just before stop is kept, not cancelled away. Bounded, so
            # a stream that does not close cannot hold up the stop.
            try:
                await asyncio.wait_for(asyncio.shield(task), self._DRAIN_TIMEOUT)
            except TimeoutError:
                pass
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Only if they are still this run's: clearing a newer run's handles
        # left its subprocess with nothing able to stop it.
        if self._process is process:
            self._process = None
        if self._read_task is task:
            self._read_task = None
        logger.info("SimulatorLog adapter stopped (udid=%s)", self.udid[:8])

    #: Bytes per read. Any size works; a read returns whatever is waiting.
    _READ_SIZE = 65536
    #: How long stop() lets the read loop finish what the stream already wrote.
    _DRAIN_TIMEOUT = 2.0

    async def _read_loop(self) -> None:
        """Read simctl log stream stdout and parse JSON objects as they close.

        simctl spawn's log stream outputs pretty-printed JSON in an array,
        unlike host-side `log stream` which outputs compact single-line JSON.
        We accumulate characters and track brace depth (outside JSON strings)
        to detect complete objects. Handles `},{` separators correctly.

        Read in chunks, not lines. Each object's closing `}` is written with
        no newline after it -- the `,` and newline come with the *next* entry
        -- so a line reader held the newest entry until another one arrived,
        and for good when the app went quiet (measured on iOS 26.5: every
        write ends in `\n}`). An entry is emitted at its `}`, whatever follows.

        The loop ends at EOF rather than when `stop()` clears `_running`, so
        whatever the stream wrote before it was terminated is still parsed.
        """
        assert self._process is not None
        assert self._process.stdout is not None
        # Bound once: after a cancelled stop and a restart, re-reading the
        # attribute would hand this loop the new process's output.
        process = self._process
        stdout = process.stdout

        # A UTF-8 character can be split across two reads.
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

        # Character-level accumulator for pretty-printed JSON
        obj_chars: list[str] = []
        brace_depth = 0
        in_string = False
        escape_next = False

        try:
            while True:
                chunk = await stdout.read(self._READ_SIZE)
                if not chunk:
                    break

                for ch in decoder.decode(chunk):
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

            if self._running:
                # The output ended while nobody asked it to: simctl exited --
                # a simulator that is not booted, a bad predicate. Say why,
                # rather than reading as a clean stop.
                self._error = await self._exit_reason(process)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if self._running:
                self._error = f"Read loop error: {e}"
            # Logged during stop's drain too, which is a processing phase now:
            # an entry that fails there would otherwise vanish without a trace.
            logger.exception("SimulatorLog read loop failed")
        finally:
            self._running = False

    @staticmethod
    async def _exit_reason(process: asyncio.subprocess.Process) -> str:
        """What simctl said as it ended its output on its own."""
        code = None
        try:
            code = await asyncio.wait_for(process.wait(), 5)
        except TimeoutError:
            pass
        tail = ""
        if process.stderr is not None:
            try:
                raw = await asyncio.wait_for(process.stderr.read(), 2)
                # The start says what went wrong ("device is not booted");
                # the end is an underlying-error dump. One line, the start.
                lines = raw.decode("utf-8", errors="replace").splitlines()
                tail = " / ".join(ln.strip() for ln in lines if ln.strip())[:300]
            except (TimeoutError, OSError):
                pass
        status = (f"exited ({code})" if code is not None
                  else "closed its output but has not exited")
        return f"simctl log stream {status}" + (f": {tail}" if tail else "")

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
            sender=extract_process_name(data.get("senderImagePath", "")),
            category=data.get("category", ""),
            pid=data.get("processID"),
            level=level,
            message=message,
            source=LogSource.SIMULATOR,
            raw=line,
        )
