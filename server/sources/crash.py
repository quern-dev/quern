"""Source adapter for crash report watching.

Polls a directory for new .ips / .crash files, parses them into structured
CrashReport objects, and emits a LogEntry for each new crash.

Optionally runs ``idevicecrashreport -k -e <dir>`` to pull crash reports from a
connected device.  The command has a hard timeout because it can hang when the
device is in a bad state.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from server.config import CONFIG_DIR
from server.models import CrashReport, LogEntry, LogLevel, LogSource
from server.sources import BaseSourceAdapter, EntryCallback

logger = logging.getLogger(__name__)

CRASH_DIR = CONFIG_DIR / "crashes"
DIAGNOSTIC_REPORTS_DIR = Path.home() / "Library" / "Logs" / "DiagnosticReports"
POLL_INTERVAL = 10  # seconds
PULL_TIMEOUT = 30  # seconds


@dataclass
class PullResult:
    """What a pull from a device found, and why it failed if it did."""

    new: list[CrashReport] = field(default_factory=list)
    error: str | None = None


class CrashAdapter(BaseSourceAdapter):
    """Watches a directory for new crash report files."""

    def __init__(
        self,
        device_id: str = "",
        on_entry: EntryCallback | None = None,
        watch_dir: Path | None = None,
        poll_interval: float = POLL_INTERVAL,
        extra_watch_dirs: list[Path] | None = None,
        process_filter: str | None = None,
        on_crash_hook: str | None = None,
    ) -> None:
        super().__init__(
            adapter_id="crash",
            adapter_type="crash_reporter",
            device_id=device_id,
            on_entry=on_entry,
        )
        self.watch_dir = watch_dir or CRASH_DIR
        self.poll_interval = poll_interval
        self.extra_watch_dirs = extra_watch_dirs or []
        self.process_filter = process_filter
        self.on_crash_hook = on_crash_hook
        self._poll_task: asyncio.Task | None = None
        self._seen_files: set[str] = set()
        # One scan at a time. The poll loop scanning while idevicecrashreport
        # was still writing took the phone's files as its own, so the pull
        # neither counted nor tagged them.
        self._scan_lock = asyncio.Lock()
        self.crash_reports: list[CrashReport] = []

    async def start(self) -> None:
        """Start the crash watcher background loop."""
        self.watch_dir.mkdir(parents=True, exist_ok=True)

        # Index existing files so we don't re-emit on restart
        for d in self._all_watch_dirs():
            if not d.exists():
                continue
            for f in d.iterdir():
                if f.suffix in (".ips", ".crash"):
                    self._seen_files.add(str(f))

        self._running = True
        self.started_at = self._now()
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info(
            "Crash adapter started (watch_dir=%s, extra_dirs=%s, filter=%s)",
            self.watch_dir,
            self.extra_watch_dirs,
            self.process_filter,
        )

    async def stop(self) -> None:
        """Stop polling."""
        self._running = False
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        self._poll_task = None
        logger.info("Crash adapter stopped")

    def status(self):
        """Override status to report 'watching' instead of 'streaming'."""
        s = super().status()
        if s.status == "streaming":
            s.status = "watching"
        return s

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """Periodically check for new crash files."""
        try:
            while self._running:
                try:
                    async with self._scan_lock:
                        await self._scan_for_new_files()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Crash poll iteration failed")

                await asyncio.sleep(self.poll_interval)
        except asyncio.CancelledError:
            pass

    async def pull_from_device(
        self, libimobiledevice_udid: str | None = None, *, device_id: str = "",
    ) -> PullResult:
        """Pull crash reports from a connected device via idevicecrashreport.

        Args:
            libimobiledevice_udid: Target a specific device. If None, pulls from
                any connected device.
            device_id: The device the reports are recorded against. The files
                do not say which phone they came from, so the pull says it.

        Only files this pull wrote count as its reports: new files in the
        directory it writes to. The scan also covers the other watched
        directories, and a simulator's crash written since the last poll was
        counted as the phone's and tagged with its udid.

        Returns:
            The newly discovered reports, and why the pull failed if it did.
            A failure used to return an empty list -- the missing tool, a
            timeout, a non-zero exit, an exception -- which read exactly like
            "the device has no new crashes" (#316, aligning with Android).
        """
        if not shutil.which("idevicecrashreport"):
            return PullResult(error="idevicecrashreport not found on PATH")

        try:
            self.watch_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return PullResult(error=f"could not create {self.watch_dir}: {e}")

        # -k: copy, and leave the reports on the phone. Without it the tool
        # deletes each report after copying, so a pull took the user's crash
        # history away from Xcode, Finder and everything else that reads it --
        # the same fault as the `logcat -c` #255 removed. Keeping them means a
        # pull re-copies reports it has already seen; they land at the same
        # path, which `_seen_files` already skips, including across restarts.
        cmd = ["idevicecrashreport", "-k", "-e"]
        if libimobiledevice_udid:
            cmd.extend(["-u", libimobiledevice_udid])
        cmd.append(str(self.watch_dir))

        async with self._scan_lock:
            before = {str(f) for f in _crash_files(self.watch_dir)}
            error = await self._run_pull(cmd)
            # Scan even after a failure: a timeout can follow a partial copy,
            # and those reports are real.
            added = await self._scan_for_new_files(
                pulled=(before, device_id),
            )
        new = [r for r in added if r.file_path not in before
               and Path(r.file_path).parent == self.watch_dir]
        return PullResult(new=new, error=error)

    async def _run_pull(self, cmd: list[str]) -> str | None:
        """Run idevicecrashreport. The error, or None if it succeeded."""
        error: str | None = None
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=PULL_TIMEOUT)
            if proc.returncode:
                said = stderr.decode(errors="replace").strip() if stderr else ""
                error = f"idevicecrashreport exited {proc.returncode}: {said or 'no output'}"
        except TimeoutError:
            logger.warning("idevicecrashreport timed out after %ds", PULL_TIMEOUT)
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.wait()
            error = f"idevicecrashreport timed out after {PULL_TIMEOUT}s"
        except OSError as e:
            error = f"could not run idevicecrashreport: {e}"
        return error

    async def add_reports(
        self,
        reports: list[CrashReport],
        *,
        already_logged: Callable[[CrashReport], Awaitable[bool]] | None = None,
    ) -> list[CrashReport]:
        """Add reports from another source (Android's DropBox), once each.

        Returns the ones that were new. Each new report also becomes a crash
        log entry, like a report file does -- unless `already_logged` says the
        same crash is already in the buffer, as it is when logcat was running
        and recognised it as it happened (#255), which would otherwise put one
        crash on the timeline twice. The on-crash hook runs either way: logcat
        does not run it, so skipping it there made it fire only when capture
        happened to be off.

        A crash from before this adapter started is listed but neither logged
        nor hooked. DropBox holds days of records and this list starts empty,
        so otherwise every restart logged the whole history as arriving now
        and ran the hook once per record. It matches report files, which are
        indexed without being emitted when the adapter starts.
        """
        known = {r.crash_id for r in self.crash_reports}
        new = []
        for report in reports:
            if report.crash_id in known:
                continue
            if self.process_filter and self.process_filter not in report.process:
                continue
            known.add(report.crash_id)
            self.crash_reports.append(report)
            new.append(report)
            if self._predates_start(report):
                continue
            log = already_logged is None or not await already_logged(report)
            await self._emit_report(report, report.raw_text, log=log)
        return new

    async def _emit_report(self, report: CrashReport, raw: str, *, log: bool = True) -> None:
        if self.on_crash_hook:
            asyncio.create_task(self._run_crash_hook(report))
        if not log:
            return
        entry = LogEntry(
            id=report.crash_id,
            timestamp=report.timestamp,
            device_id=report.device_id or self.device_id,
            process=report.process,
            level=LogLevel.FAULT,
            message=self._crash_summary(report),
            source=LogSource.CRASH,
            raw=raw[:2000],
        )
        await self.emit(entry)

    def _all_watch_dirs(self) -> list[Path]:
        """Return the primary watch dir plus any extra watch dirs."""
        return [self.watch_dir] + self.extra_watch_dirs

    async def _scan_for_new_files(
        self, *, pulled: tuple[set[str], str] | None = None,
    ) -> list[CrashReport]:
        """Scan all watch directories for new crash files; the reports added.

        `pulled` is (files in watch_dir before a pull, the device pulled from):
        a file new to watch_dir came from that device and is tagged before it
        is emitted, so its log entry names the device too.
        """
        all_files: list[tuple[float, Path]] = []
        for d in self._all_watch_dirs():
            for f in _crash_files(d):
                try:
                    all_files.append((f.stat().st_mtime, f))
                except OSError:
                    continue    # gone between listing and stat

        added: list[CrashReport] = []
        for _, f in sorted(all_files, key=lambda pair: pair[0]):
            if str(f) in self._seen_files:
                continue

            self._seen_files.add(str(f))

            try:
                content = f.read_text(errors="replace")
            except Exception:
                logger.exception("Failed to read crash file %s", f)
                continue

            report = self._parse_crash_file(f, content)
            if report:
                from_pull = pulled and f.parent == self.watch_dir and str(f) not in pulled[0]
                if from_pull:
                    report.device_id = pulled[1] or report.device_id
                self.crash_reports.append(report)
                added.append(report)
                if from_pull and self._predates_start(report):
                    # The pull leaves reports on the phone (-k), so a first
                    # pull into an empty directory copies its whole history.
                    # Listed, not replayed: the same rule as add_reports.
                    continue
                await self._emit_report(report, content)
        return added

    def _predates_start(self, report: CrashReport) -> bool:
        return self.started_at is not None and report.timestamp < self.started_at

    async def _run_crash_hook(self, report: CrashReport) -> None:
        """Run the on-crash hook command with CrashReport JSON on stdin."""
        try:
            proc = await asyncio.create_subprocess_shell(
                self.on_crash_hook,  # type: ignore[arg-type]
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(input=report.model_dump_json().encode()),
                    timeout=60,
                )
            except TimeoutError:
                logger.warning("on-crash hook timed out after 60s, killing")
                proc.kill()
                await proc.wait()
                return
            if proc.returncode != 0:
                logger.warning(
                    "on-crash hook exited with code %d: %s",
                    proc.returncode,
                    stderr.decode(errors="replace")[:500],
                )
        except Exception:
            logger.exception("on-crash hook failed")

    def _parse_crash_file(self, path: Path, content: str) -> CrashReport | None:
        """Parse a .ips (JSON) or .crash (text) file."""
        if path.suffix == ".ips":
            return self._parse_ips(path, content)
        elif path.suffix == ".crash":
            return self._parse_crash_text(path, content)
        return None

    # bug_type values that represent actual crash reports (not diagnostics)
    CRASH_BUG_TYPES = {"309"}

    def _parse_ips(self, path: Path, content: str) -> CrashReport | None:
        """Parse iOS 15+ .ips JSON crash report."""
        header: dict = {}
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            # .ips files typically have a JSON header line, then the report body
            lines = content.split("\n", 1)
            if len(lines) < 2:
                logger.warning("Could not parse .ips file %s", path)
                return None
            try:
                header = json.loads(lines[0])
            except json.JSONDecodeError:
                pass
            try:
                data = json.loads(lines[1])
            except json.JSONDecodeError:
                logger.warning("Could not parse .ips file %s", path)
                return None

        # Filter out non-crash diagnostic reports (Jetsam, SFA, analytics, etc.)
        bug_type = header.get("bug_type") or data.get("bug_type")
        if bug_type and str(bug_type) not in self.CRASH_BUG_TYPES:
            logger.debug("Skipping non-crash .ips (bug_type=%s): %s", bug_type, path.name)
            return None

        crash_id = uuid.uuid4().hex[:12]
        proc_name = data.get("procName", "") or data.get("name", "") or path.stem

        if self.process_filter and self.process_filter not in proc_name:
            return None

        # Extract exception info
        exception = data.get("exception", {})
        exc_type = exception.get("type", "")
        exc_codes = exception.get("codes", "")
        signal_name = exception.get("signal", "")

        # Also check top-level for signal
        if not signal_name:
            signal_name = data.get("termination", {}).get("signal", "")

        # Extract top frames from faulting thread
        top_frames: list[str] = []
        threads = data.get("threads", [])
        faulting = data.get("faultingThread", 0)
        if isinstance(threads, list) and 0 <= faulting < len(threads):
            thread = threads[faulting]
            frames = thread.get("frames", [])
            for frame in frames[:5]:
                image = frame.get("imageOffset", "")
                symbol = frame.get("symbol", "")
                if symbol:
                    top_frames.append(symbol)
                elif image:
                    top_frames.append(str(image))

        # Timestamp
        ts_str = data.get("captureTime", "") or data.get("timestamp", "")
        ts = self._parse_timestamp(ts_str)

        return CrashReport(
            crash_id=crash_id,
            timestamp=ts,
            device_id=self.device_id,
            process=proc_name,
            exception_type=exc_type,
            exception_codes=exc_codes,
            signal=signal_name,
            top_frames=top_frames,
            file_path=str(path),
            raw_text=content[:3000],
        )

    def _parse_crash_text(self, path: Path, content: str) -> CrashReport | None:
        """Parse older-format .crash text crash report."""
        crash_id = uuid.uuid4().hex[:12]

        proc_match = re.search(r"^Process:\s+(\S+)", content, re.MULTILINE)
        exc_match = re.search(r"^Exception Type:\s+(.+)$", content, re.MULTILINE)
        codes_match = re.search(r"^Exception Codes:\s+(.+)$", content, re.MULTILINE)

        proc_name = proc_match.group(1) if proc_match else path.stem

        if self.process_filter and self.process_filter not in proc_name:
            return None

        exc_type = exc_match.group(1).strip() if exc_match else ""
        exc_codes = codes_match.group(1).strip() if codes_match else ""

        # Extract signal from exception type (e.g. "EXC_BAD_ACCESS (SIGSEGV)")
        signal_name = ""
        sig_match = re.search(r"\((\w+)\)", exc_type)
        if sig_match:
            signal_name = sig_match.group(1)

        # Extract top frames from "Thread N Crashed:" section
        top_frames: list[str] = []
        crashed_section = re.search(
            r"Thread \d+ Crashed.*?\n((?:\d+\s+.+\n){1,5})", content
        )
        if crashed_section:
            for line in crashed_section.group(1).strip().split("\n"):
                parts = line.split(None, 3)
                if len(parts) >= 4:
                    top_frames.append(parts[3].strip())
                elif len(parts) >= 3:
                    top_frames.append(parts[2].strip())

        # Timestamp
        ts_match = re.search(r"^Date/Time:\s+(.+)$", content, re.MULTILINE)
        ts = self._parse_timestamp(ts_match.group(1).strip() if ts_match else "")

        return CrashReport(
            crash_id=crash_id,
            timestamp=ts,
            device_id=self.device_id,
            process=proc_name,
            exception_type=exc_type,
            exception_codes=exc_codes,
            signal=signal_name,
            top_frames=top_frames,
            file_path=str(path),
            raw_text=content[:3000],
        )

    @staticmethod
    def _crash_summary(report: CrashReport) -> str:
        """Build a one-line summary of a crash for the LogEntry message."""
        parts = [f"CRASH: {report.process}"]
        if report.exception_type:
            parts.append(report.exception_type)
        if report.signal:
            parts.append(f"({report.signal})")
        if report.top_frames:
            parts.append(f"@ {report.top_frames[0]}")
        return " ".join(parts)

    @staticmethod
    def _parse_timestamp(ts_str: str) -> datetime:
        """Best-effort timestamp parsing from crash report."""
        if not ts_str:
            return datetime.now(UTC)

        # ISO 8601
        for fmt in (
            "%Y-%m-%d %H:%M:%S.%f %z",
            "%Y-%m-%d %H:%M:%S %z",
            "%Y-%m-%dT%H:%M:%S.%fZ",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%d %H:%M:%S",
        ):
            try:
                dt = datetime.strptime(ts_str.strip(), fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                return dt
            except ValueError:
                continue

        return datetime.now(UTC)


def _crash_files(d: Path) -> list[Path]:
    """The crash report files in `d`; none if it is missing or unreadable."""
    try:
        return [f for f in d.iterdir() if f.suffix in (".ips", ".crash")]
    except OSError:
        return []
