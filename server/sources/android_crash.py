"""Recognise Android crashes in a logcat stream.

iOS crashes arrive as report files, which `CrashAdapter` parses into one
`LogSource.CRASH` entry each. Android has no equivalent file on the host: a
crash is only ever a few lines of logcat, at the same level and in the same
stream as everything else. So until this existed an Android crash was an
ordinary logcat line -- stored in the shared buffer, evictable by the
firehose in seconds, and never in the crash buffer that exists precisely so
that cannot happen (#255).

This turns the three shapes of Android failure into crash entries, alongside
the raw lines rather than instead of them:

- **Java crash** -- `AndroidRuntime`: `FATAL EXCEPTION: <thread>`, then
  `Process: <name>, PID: <pid>`, then the exception. Measured on API 32.
- **Native crash** -- `libc`: `Fatal signal 11 (SIGSEGV), ... pid 1234 (name)`.
  One line; the tombstone that follows adds detail, not the fact.
- **ANR** -- `ActivityManager`: `ANR in <name> (...)`. Not a crash in the
  strict sense, but it is the app dying from the user's point of view and it
  is as rare and as valuable, which is what the crash buffer is for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from server.models import LogEntry, LogLevel, LogSource

_FATAL_EXCEPTION = re.compile(r"^FATAL EXCEPTION:\s*(.*)$")
_PROCESS_LINE = re.compile(r"^Process:\s*([^,\s]+),\s*PID:\s*(\d+)")
# "Fatal signal 11 (SIGSEGV), code 1 (SEGV_MAPERR), fault addr 0x0 in tid
#  4321 (RenderThread), pid 1234 (com.example.app)"
_FATAL_SIGNAL = re.compile(r"^Fatal signal .*?\bpid (\d+) \(([^)]+)\)")
_ANR = re.compile(r"^ANR in (\S+)")


@dataclass
class _PendingJavaCrash:
    """A `FATAL EXCEPTION` header whose detail lines have not all arrived."""

    header: LogEntry
    thread: str
    process: str = ""
    lines: list[str] = field(default_factory=list)


class AndroidCrashDetector:
    """Feed it parsed logcat entries; it returns the crash entries they form.

    Stateful only for the Java case, which spans lines. Everything is keyed by
    pid, because two processes can crash at once and their lines interleave.
    """

    def __init__(self, device_id: str = "") -> None:
        self.device_id = device_id
        self._pending: dict[int, _PendingJavaCrash] = {}

    def feed(self, entry: LogEntry) -> list[LogEntry]:
        tag, message, pid = entry.process, entry.message, entry.pid

        if tag == "AndroidRuntime" and pid is not None:
            header = _FATAL_EXCEPTION.match(message)
            if header:
                # A second header for the same pid means the first never
                # completed; say what we have rather than lose it.
                out = self._flush(pid)
                self._pending[pid] = _PendingJavaCrash(
                    header=entry, thread=header.group(1), lines=[message],
                )
                return out
            pending = self._pending.get(pid)
            if pending is not None:
                pending.lines.append(message)
                process = _PROCESS_LINE.match(message)
                if process:
                    pending.process = process.group(1)
                    return []
                # The line after `Process:` is the exception. That is the
                # whole fact; the stack frames below it are detail.
                return self._flush(pid)
            return []

        out: list[LogEntry] = []
        # Any other line from a pid with a pending crash means its
        # AndroidRuntime block is over, however short it was.
        if pid is not None and pid in self._pending:
            out.extend(self._flush(pid))

        if tag == "libc":
            signal = _FATAL_SIGNAL.match(message)
            if signal:
                out.append(self._crash(entry, signal.group(2), int(signal.group(1)),
                                       f"{signal.group(2)} crashed: {message}", [message]))
        elif tag == "ActivityManager":
            anr = _ANR.match(message)
            if anr:
                out.append(self._crash(entry, anr.group(1), pid,
                                       f"{anr.group(1)} is not responding: {message}", [message]))
        return out

    def flush(self) -> list[LogEntry]:
        """Everything still pending, e.g. when capture stops mid-crash."""
        out: list[LogEntry] = []
        for pid in list(self._pending):
            out.extend(self._flush(pid))
        return out

    def _flush(self, pid: int) -> list[LogEntry]:
        pending = self._pending.pop(pid, None)
        if pending is None:
            return []
        process = pending.process or f"pid {pid}"
        detail = pending.lines[-1] if len(pending.lines) > 2 else ""
        message = f"{process} crashed: FATAL EXCEPTION: {pending.thread}"
        if detail:
            message += f" -- {detail}"
        return [self._crash(pending.header, process, pid, message, pending.lines)]

    def _crash(
        self, at: LogEntry, process: str, pid: int | None, message: str, lines: list[str],
    ) -> LogEntry:
        return LogEntry(
            # Tied to the line that triggered it, so the two can be matched.
            id=f"android-crash-{at.id}",
            timestamp=at.timestamp,
            device_id=self.device_id or at.device_id,
            process=process,
            pid=pid,
            level=LogLevel.FAULT,
            message=message,
            source=LogSource.CRASH,
            raw="\n".join(lines),
        )
