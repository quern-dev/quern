"""Source adapter for crash report watching.

Polls a directory for new .ips / .crash files, parses them into structured
CrashReport objects, and emits a LogEntry for each new crash.

Optionally runs ``idevicecrashreport -e <dir>`` to pull crash reports from a
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
from datetime import datetime, timezone
from pathlib import Path

from server.models import CrashReport, LogEntry, LogLevel, LogSource
from server.sources import BaseSourceAdapter, EntryCallback

logger = logging.getLogger(__name__)

CRASH_DIR = Path.home() / ".ios-debug-server" / "crashes"
POLL_INTERVAL = 10  # seconds
PULL_TIMEOUT = 30  # seconds


class CrashAdapter(BaseSourceAdapter):
    """Watches a directory for new crash report files."""

    def __init__(
        self,
        device_id: str = "default",
        on_entry: EntryCallback | None = None,
        watch_dir: Path | None = None,
        pull_from_device: bool = False,
        poll_interval: float = POLL_INTERVAL,
    ) -> None:
        super().__init__(
            adapter_id="crash",
            adapter_type="crash_reporter",
            device_id=device_id,
            on_entry=on_entry,
        )
        self.watch_dir = watch_dir or CRASH_DIR
        self.pull_from_device = pull_from_device
        self.poll_interval = poll_interval
        self._poll_task: asyncio.Task | None = None
        self._seen_files: set[str] = set()
        self.crash_reports: list[CrashReport] = []

    async def start(self) -> None:
        """Start the crash watcher background loop."""
        self.watch_dir.mkdir(parents=True, exist_ok=True)

        # Index existing files so we don't re-emit on restart
        for f in self.watch_dir.iterdir():
            if f.suffix in (".ips", ".crash"):
                self._seen_files.add(f.name)

        self._running = True
        self.started_at = self._now()
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info(
            "Crash adapter started (watch_dir=%s, pull=%s)",
            self.watch_dir,
            self.pull_from_device,
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
                    if self.pull_from_device:
                        await self._pull_from_device()
                    await self._scan_for_new_files()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Crash poll iteration failed")

                await asyncio.sleep(self.poll_interval)
        except asyncio.CancelledError:
            pass

    async def _pull_from_device(self) -> None:
        """Run idevicecrashreport to copy crashes from a connected device."""
        if not shutil.which("idevicecrashreport"):
            return

        try:
            proc = await asyncio.create_subprocess_exec(
                "idevicecrashreport", "-e", str(self.watch_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.wait(), timeout=PULL_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("idevicecrashreport timed out after %ds", PULL_TIMEOUT)
            if proc.returncode is None:
                proc.kill()
        except FileNotFoundError:
            pass
        except Exception:
            logger.exception("idevicecrashreport failed")

    async def _scan_for_new_files(self) -> None:
        """Scan watch directory for new crash files."""
        if not self.watch_dir.exists():
            return

        for f in sorted(self.watch_dir.iterdir(), key=lambda p: p.stat().st_mtime):
            if f.name in self._seen_files:
                continue
            if f.suffix not in (".ips", ".crash"):
                continue

            self._seen_files.add(f.name)

            try:
                content = f.read_text(errors="replace")
            except Exception:
                logger.exception("Failed to read crash file %s", f)
                continue

            report = self._parse_crash_file(f, content)
            if report:
                self.crash_reports.append(report)
                entry = LogEntry(
                    id=report.crash_id,
                    timestamp=report.timestamp,
                    device_id=self.device_id,
                    process=report.process,
                    level=LogLevel.FAULT,
                    message=self._crash_summary(report),
                    source=LogSource.CRASH,
                    raw=content[:2000],
                )
                await self.emit(entry)

    def _parse_crash_file(self, path: Path, content: str) -> CrashReport | None:
        """Parse a .ips (JSON) or .crash (text) file."""
        if path.suffix == ".ips":
            return self._parse_ips(path, content)
        elif path.suffix == ".crash":
            return self._parse_crash_text(path, content)
        return None

    def _parse_ips(self, path: Path, content: str) -> CrashReport | None:
        """Parse iOS 15+ .ips JSON crash report."""
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            # Some .ips files have a header line before JSON
            lines = content.split("\n", 1)
            if len(lines) < 2:
                logger.warning("Could not parse .ips file %s", path)
                return None
            try:
                data = json.loads(lines[1])
            except json.JSONDecodeError:
                logger.warning("Could not parse .ips file %s", path)
                return None

        crash_id = uuid.uuid4().hex[:12]
        proc_name = data.get("procName", "") or data.get("name", "") or path.stem

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
            return datetime.now(timezone.utc)

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
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except ValueError:
                continue

        return datetime.now(timezone.utc)
