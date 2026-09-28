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

#: Under the watch dir, one directory per phone a pull has read, named by
#: its device id. A report file does not say which phone it came from, so
#: the directory says it -- which is what lets a restart list a phone's
#: reports against the right device instead of losing them.
DEVICES_SUBDIR = "devices"
_SAFE_DEVICE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


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
        # Files that could not be parsed, by path, with the size they had.
        # Retried only when it changes. Marking them seen instead lost a report
        # for good: a pull that timed out mid-copy left a partial file, and -k
        # re-copies the complete one to the same path -- always larger. Not
        # the mtime: idevicecrashreport rewrites every file it copies (measured,
        # 1.4.0), so every pull would re-read every non-crash report a phone
        # holds, JetsamEvent and the like, which never parse.
        self._unparsed: dict[str, int] = {}
        # One scan at a time. The poll loop scanning while idevicecrashreport
        # was still writing took the phone's files as its own, so the pull
        # neither counted nor tagged them.
        self._scan_lock = asyncio.Lock()
        self.crash_reports: list[CrashReport] = []

    async def start(self) -> None:
        """Start the crash watcher background loop."""
        self.watch_dir.mkdir(parents=True, exist_ok=True)

        # Index existing files so we don't re-emit on restart. A phone's
        # reports are listed too, without being emitted: Android's DropBox
        # history is listed again after a restart, and an iPhone's reports
        # were marked seen and never listed, so the phone showed none -- and
        # since the pull leaves them on the phone and re-copies them to the
        # same paths, it never would again. Loose files in the watch dir, from
        # before pulls had a directory per phone, name no device and stay
        # unlisted: listed, they would appear under every device's udid.
        for d in self._all_watch_dirs():
            for f in _crash_files(d):
                device = self._device_of(f)
                if not device:
                    self._seen_files.add(str(f))
                    continue
                read = self._read_report(f)
                if read is None:
                    self._mark_unparsed(f)
                    continue
                self._seen_files.add(str(f))
                read[0].device_id = device
                self.crash_reports.append(read[0])

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
                    # Skip a turn while a pull holds the lock rather than wait
                    # up to its 30s timeout: the pull's own scan covers every
                    # directory, simulator crashes included.
                    if not self._scan_lock.locked():
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

        target = self._device_dir(device_id) or self.watch_dir
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return PullResult(error=f"could not create {target}: {e}")

        # -k: copy, and leave the reports on the phone. Without it the tool
        # deletes each report after copying, so a pull took the user's crash
        # history away from Xcode, Finder and everything else that reads it --
        # the same fault as the `logcat -c` #255 removed. Keeping them means a
        # pull re-copies reports it has already seen; they land at the same
        # path, which `_seen_files` already skips, including across restarts.
        cmd = ["idevicecrashreport", "-k", "-e"]
        if libimobiledevice_udid:
            cmd.extend(["-u", libimobiledevice_udid])
        cmd.append(str(target))

        async with self._scan_lock:
            # Files already read. Not merely present: a partial copy left by a
            # timed-out pull is present, and its completed re-copy is this
            # pull's report.
            before = {str(f) for f in _crash_files(target) if str(f) in self._seen_files}
            error = await self._run_pull(cmd)
            # Scan even after a failure: a timeout can follow a partial copy,
            # and those reports are real.
            added = await self._scan_for_new_files(
                pulled=(target, before, device_id),
            )
        new = [r for r in added if r.file_path not in before
               and Path(r.file_path).parent == target]
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
        except asyncio.CancelledError:
            # A cancelled request or a shutdown: do not leave it writing into
            # the phone's directory with the lock released.
            if proc is not None and proc.returncode is None:
                proc.kill()
            raise
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
        """The primary watch dir, each phone's directory under it, and the extras."""
        return [self.watch_dir, *self._device_dirs(), *self.extra_watch_dirs]

    def _device_dirs(self) -> list[Path]:
        try:
            return sorted(d for d in (self.watch_dir / DEVICES_SUBDIR).iterdir() if d.is_dir())
        except OSError:
            return []

    def _device_dir(self, device_id: str) -> Path | None:
        """Where a pull from this device writes; None without a usable id.

        None falls back to the watch dir itself, where the pull still tags
        what it wrote for this run but the device is not remembered.
        """
        if device_id and _SAFE_DEVICE_ID.match(device_id):
            return self.watch_dir / DEVICES_SUBDIR / device_id
        return None

    def _device_of(self, f: Path) -> str:
        """The device a report file belongs to, from its directory; "" if none."""
        return f.parent.name if f.parent.parent == self.watch_dir / DEVICES_SUBDIR else ""

    def _read_report(self, f: Path) -> tuple[CrashReport, str] | None:
        """The report in `f`, or None if it cannot be read or parsed.

        Never raises. The parsers catch malformed JSON but not a well-formed
        body of the wrong shape (`{"faultingThread": null}` raised TypeError),
        and since start() reads a phone's reports, one such file on disk
        stopped the server booting, every time.
        """
        try:
            content = f.read_text(errors="replace")
            report = self._parse_crash_file(f, content)
        except Exception:
            logger.exception("Failed to read crash file %s", f)
            return None
        return (report, content) if report else None

    def _mark_unparsed(self, f: Path) -> None:
        try:
            self._unparsed[str(f)] = f.stat().st_size
        except OSError:
            return

    async def _scan_for_new_files(
        self, *, pulled: tuple[Path, set[str], str] | None = None,
    ) -> list[CrashReport]:
        """Scan all watch directories for new crash files; the reports added.

        A file in a phone's directory is that phone's. `pulled` is (the
        directory a pull wrote to, the files in it before, the device pulled
        from): a file new to it came from that device. Either way the report is
        tagged before it is emitted, so its log entry names the device too.
        """
        all_files: list[tuple[float, Path, int]] = []
        for d in self._all_watch_dirs():
            for f in _crash_files(d):
                try:
                    st = f.stat()
                except OSError:
                    continue    # gone between listing and stat
                all_files.append((st.st_mtime, f, st.st_size))

        added: list[CrashReport] = []
        for _, f, signature in sorted(all_files, key=lambda t: t[0]):
            if str(f) in self._seen_files or self._unparsed.get(str(f)) == signature:
                continue

            read = self._read_report(f)
            if not read:
                self._unparsed[str(f)] = signature
                continue
            self._seen_files.add(str(f))
            self._unparsed.pop(str(f), None)
            report, content = read
            from_pull = bool(pulled) and f.parent == pulled[0] and str(f) not in pulled[1]
            device = self._device_of(f) or (pulled[2] if from_pull else "")
            if device:
                report.device_id = device
            self.crash_reports.append(report)
            added.append(report)
            if (from_pull or self._device_of(f)) and self._predates_start(report):
                # The pull leaves reports on the phone (-k), so a first
                # pull into an empty directory copies its whole history.
                # Listed, not replayed: the same rule as add_reports. Any
                # file in a phone's directory, not only this pull's: one
                # the poll loop reaches first -- after a pull was cancelled,
                # say -- was replayed with a hook run apiece.
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
        ts = self._parse_timestamp(ts_str, fallback=_mtime(path))

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

        # Every crash report states its exception type. A file without one is
        # not a report yet: an empty or cut-short copy from a pull that timed
        # out "parsed" as a crash named after the file, was logged and hooked,
        # and marked seen, so the complete copy was never read. Unparsed, it is
        # retried when its size changes. A copy cut off further in still
        # parses, as a report with fewer frames.
        if exc_match is None:
            return None

        proc_name = proc_match.group(1) if proc_match else path.stem

        if self.process_filter and self.process_filter not in proc_name:
            return None

        exc_type = exc_match.group(1).strip()
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
        ts = self._parse_timestamp(
            ts_match.group(1).strip() if ts_match else "", fallback=_mtime(path),
        )

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
    def _parse_timestamp(ts_str: str, fallback: datetime | None = None) -> datetime:
        """Best-effort timestamp parsing from crash report.

        Unreadable, it is `fallback` -- the file's modification time, where the
        caller has one -- and only then now. That helps a report written on
        this Mac (a simulator's, in DiagnosticReports), whose mtime is when it
        was written. It does not help a pulled one: idevicecrashreport rewrites
        each file it copies, so the mtime is the copy time. A crash report
        carries its own time (`captureTime`, `Date/Time`), so this is rare.
        """
        if not ts_str:
            return fallback or datetime.now(UTC)

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

        return fallback or datetime.now(UTC)


def _crash_files(d: Path) -> list[Path]:
    """The crash report files in `d`; none if it is missing or unreadable."""
    try:
        return [f for f in d.iterdir() if f.suffix in (".ips", ".crash")]
    except OSError:
        return []


def _mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, UTC)
    except OSError:
        return None
