"""Source adapter for crash report watching.

Polls a directory for new .ips / .crash files, parses them into structured
CrashReport objects, and emits a LogEntry for each new crash.

Optionally pulls crash reports from a connected iPhone with pymobiledevice3:
the recent ones only, left on the phone (see server/sources/ios_crash.py).
Every call has a hard timeout because it can hang when the device is in a bad
state.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from server.config import CONFIG_DIR
from server.models import CrashReport, LogEntry, LogLevel, LogSource
from server.sources import BaseSourceAdapter, EntryCallback, crash_frames, ios_crash

logger = logging.getLogger(__name__)

CRASH_DIR = CONFIG_DIR / "crashes"
DIAGNOSTIC_REPORTS_DIR = Path.home() / "Library" / "Logs" / "DiagnosticReports"
POLL_INTERVAL = 10  # seconds

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
    #: How far back the pull reached, in days, when it listed the phone.
    window_days: int | None = None
    #: Reports older than the window, left on the phone and not pulled.
    older_on_device: int | None = None
    #: The oldest report date on the phone.
    oldest_on_device: date | None = None


@dataclass
class ClearResult:
    """What a clear or a retention pass removed from the Mac."""

    files_removed: int = 0
    reports_removed: int = 0
    errors: list[str] = field(default_factory=list)


#: Pulled reports older than this are removed from the Mac (#322). The files'
#: mtime is when they were last copied (`_stamp_copied`), and a pull re-copies
#: only reports within its window, so a file this old has been outside every
#: window since -- including a wider one someone asked for with `days`.
DEFAULT_RETENTION_DAYS = 30
RETENTION_INTERVAL_S = 3600


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
        retention_days: int = DEFAULT_RETENTION_DAYS,
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
        # for good: a pull that timed out mid-copy left a partial file, and the
        # next pull re-copies the complete one to the same path -- always
        # larger. Not the mtime: each pull stamps what it copied with the copy
        # time (`_stamp_copied`), so every pull would re-read every report that
        # never parses.
        self._unparsed: dict[str, int] = {}
        # One scan at a time. The poll loop scanning while a pull
        # was still writing took the phone's files as its own, so the pull
        # neither counted nor tagged them.
        self._scan_lock = asyncio.Lock()
        # What has been logged (and hooked), by file path or DropBox record.
        # A report cleared from the list and read again -- re-copied from the
        # phone, re-pulled from DropBox -- is listed again but not logged as
        # a new crash a second time.
        self._emitted: set[str] = set()
        #: 0 turns automatic removal off.
        self.retention_days = retention_days
        self._last_prune: float | None = None
        self.crash_reports: list[CrashReport] = []

    async def start(self) -> None:
        """Start the crash watcher background loop."""
        self.watch_dir.mkdir(parents=True, exist_ok=True)
        self.prune()

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
                            if self._prune_due():
                                self.prune()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Crash poll iteration failed")

                await asyncio.sleep(self.poll_interval)
        except asyncio.CancelledError:
            pass

    async def pull_from_device(
        self, libimobiledevice_udid: str | None = None, *, device_id: str = "",
        days: int = ios_crash.DEFAULT_WINDOW_DAYS,
    ) -> PullResult:
        """Pull a connected iPhone's recent crash reports with pymobiledevice3.

        Args:
            libimobiledevice_udid: Target a specific device. If None, the first
                connected device.
            device_id: The device the reports are recorded against. The files
                do not say which phone they came from, so the pull says it.
            days: How far back to reach. Older reports stay on the phone and
                are counted in `older_on_device`, never silently dropped.

        Reports are left on the phone: the pull copies, it never deletes. The
        old tool deleted each report it copied unless told not to, taking the
        history away from Xcode, Finder and anything else reading it (#316).

        Only files this pull wrote count as its reports: new files in the
        directory it writes to. The scan also covers the other watched
        directories, and a simulator's crash written since the last poll was
        counted as the phone's and tagged with its udid.

        Returns:
            The newly discovered reports, what the pull left on the phone, and
            why it failed if it did. A failure used to return an empty list,
            which read exactly like "the device has no new crashes".
        """
        cmd = ios_crash.command()
        if not cmd:
            return PullResult(error="pymobiledevice3 not found")
        if not libimobiledevice_udid:
            # Without one, pymobiledevice3 picks the first USB phone -- the
            # wrong one when two are plugged in, filed under this device.
            return PullResult(error="no USB udid for this device")

        target = self._device_dir(device_id) or self.watch_dir
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return PullResult(error=f"could not create {target}: {e}")

        selection: ios_crash.Selection | None = None
        async with self._scan_lock:
            # Files already read. Not merely present: a partial copy left by a
            # timed-out pull is present, and its completed re-copy is this
            # pull's report.
            before = {str(f) for f in _crash_files(target) if str(f) in self._seen_files}
            error = None
            copied: list[str] = []
            try:
                names = await ios_crash.list_reports(cmd, libimobiledevice_udid)
                selection = ios_crash.select_recent(names, days, datetime.now())
                copied = await ios_crash.pull_reports(
                    cmd, libimobiledevice_udid, selection.wanted, target,
                )
            except ios_crash.IosCrashError as e:
                error = str(e)
                copied = e.copied
            _stamp_copied(target, copied)
            # Scan even after a failure: a timeout can follow a partial copy,
            # and those reports are real.
            added = await self._scan_for_new_files(
                pulled=(target, before, device_id),
            )
        new = [r for r in added if r.file_path not in before
               and Path(r.file_path).parent == target]
        return PullResult(
            new=new, error=error,
            window_days=days if selection else None,
            older_on_device=selection.older if selection else None,
            oldest_on_device=selection.oldest if selection else None,
        )

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
        # A file by its path; a DropBox record by its id, which hashes serial,
        # tag, time, pid and process. Its file_path is only tag@second: two
        # crashes in one second, on two emulators or two processes, shared it,
        # and the second was listed but never logged.
        key = report.crash_id if report.file_path.startswith("dropbox:") else (
            report.file_path or report.crash_id
        )
        if key in self._emitted:
            return
        self._emitted.add(key)
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

    async def clear(self, device_id: str | None = None) -> ClearResult:
        """Forget stored crash reports: one device's, or all of quern's.

        Deletes only the files quern's own pulls wrote -- each phone's
        directory under `devices/` -- and drops the reports from the list,
        Android's included. Nothing else in the watch dir is deleted: it is
        whatever `--crash-dir` names, which may be a shared directory, even
        ~/Library/Logs/DiagnosticReports, and a loose file there may be one
        someone put there. Reports from those are only dropped from the list.

        Under the scan lock, so it cannot delete files under a pull in progress.

        Clearing the Mac does not clear the device. A later pull lists again
        whatever the phone or DropBox still holds within its window -- without
        logging it as a new crash a second time.
        """
        async with self._scan_lock:
            return self._clear(device_id)

    def _clear(self, device_id: str | None) -> ClearResult:
        result = ClearResult()
        if device_id:
            directory = self._device_dir(device_id)
            dirs = [directory] if directory else []
        else:
            dirs = self._device_dirs()
        removed = self._remove_files(
            [f for d in dirs for f in _crash_files(d)], result,
        )
        kept = []
        for report in self.crash_reports:
            belongs = device_id is None or report.device_id == device_id
            if belongs or report.file_path in removed:
                result.reports_removed += 1
            else:
                kept.append(report)
        self.crash_reports = kept
        self._remove_empty_device_dirs()
        return result

    def prune(self, now: datetime | None = None) -> ClearResult:
        """Remove pulled report files not copied for `retention_days` (#322).

        A pull re-copies only reports within its window, so a file's mtime is
        when it was last inside one; past the retention age it has been
        outside every window since, or no pull has run. Only the files quern's
        own pulls wrote, under `devices/`: never a loose file in the watch dir,
        which `--crash-dir` may point anywhere, and never DiagnosticReports.
        """
        result = ClearResult()
        self._last_prune = time.monotonic()
        if self.retention_days <= 0:
            return result
        cutoff = (now or datetime.now(UTC)) - timedelta(days=self.retention_days)
        stale = []
        for d in self._device_dirs():
            for f in _crash_files(d):
                when = _mtime(f)
                if when is not None and when < cutoff:
                    stale.append(f)
        removed = self._remove_files(stale, result)
        before = len(self.crash_reports)
        self.crash_reports = [r for r in self.crash_reports if r.file_path not in removed]
        result.reports_removed = before - len(self.crash_reports)
        self._remove_empty_device_dirs()
        if result.files_removed or result.errors:
            logger.info("Crash retention removed %d file(s) older than %d days%s",
                        result.files_removed, self.retention_days,
                        f"; {len(result.errors)} could not be removed" if result.errors else "")
        return result

    def _prune_due(self) -> bool:
        if self._last_prune is None:
            return True
        return time.monotonic() - self._last_prune >= RETENTION_INTERVAL_S

    def _remove_files(self, files: list[Path], result: ClearResult) -> set[str]:
        """Delete these files; the paths removed. Failures are counted, not raised."""
        removed = set()
        for f in files:
            try:
                f.unlink()
            except FileNotFoundError:
                pass
            except OSError as e:
                result.errors.append(f"{f.name}: {e.strerror or e}")
                continue
            removed.add(str(f))
            self._seen_files.discard(str(f))
            self._unparsed.pop(str(f), None)
        result.files_removed += len(removed)
        return removed

    def _remove_empty_device_dirs(self) -> None:
        for d in self._device_dirs():
            try:
                d.rmdir()               # only succeeds when empty
            except OSError:
                pass

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
        proc_name = _text(data.get("procName"), data.get("name")) or path.stem

        if self.process_filter and self.process_filter not in proc_name:
            return None

        # Extract exception info. Each field as text or absent: a report of an
        # odd shape still says it crashed.
        exception = data.get("exception") if isinstance(data.get("exception"), dict) else {}
        exc_type = _text(exception.get("type"))
        exc_codes = _text(exception.get("codes"))
        signal_name = _text(exception.get("signal"))

        # Also check top-level for signal
        termination = data.get("termination") if isinstance(data.get("termination"), dict) else {}
        if not signal_name:
            signal_name = _text(termination.get("signal"))

        # Where it happened, with each frame's image, offset and source line,
        # and the images' UUIDs and load addresses (#326). A shape this cannot
        # read costs the frames, never the report: it parsed before them.
        try:
            frames, images, from_exception = crash_frames.ips_frames(data)
        except (TypeError, ValueError, AttributeError, KeyError):
            logger.warning("Could not read the frames of %s", path, exc_info=True)
            frames, images, from_exception = [], [], False
        bundle = data.get("bundleInfo") if isinstance(data.get("bundleInfo"), dict) else {}
        killed_by = crash_frames.ips_killed_by(data)
        # From the whole stack, before it is capped for the response.
        app_frame = None if killed_by else crash_frames.first_app_frame(frames)

        # Timestamp
        ts_str = _text(data.get("captureTime"), data.get("timestamp"))
        ts = self._parse_timestamp(ts_str, fallback=_mtime(path))

        return CrashReport(
            crash_id=crash_id,
            timestamp=ts,
            device_id=crash_frames.simulator_udid(data.get("procPath")) or self.device_id,
            process=proc_name,
            exception_type=exc_type,
            exception_codes=exc_codes,
            signal=signal_name,
            top_frames=[crash_frames.format_frame(f) for f in frames[:crash_frames.TOP_FRAMES]],
            frames=frames[:crash_frames.MAX_FRAMES],
            images=_images_for(images, frames[:crash_frames.MAX_FRAMES], app_frame),
            frames_from=("exception" if from_exception else "crashing_thread") if frames else "",
            app_frame=app_frame,
            reason=crash_frames.ips_reason(data),
            killed_by=killed_by,
            bundle_id=_text(header.get("bundleID"), bundle.get("CFBundleIdentifier")),
            app_version=_text(header.get("app_version"), bundle.get("CFBundleShortVersionString")),
            build_version=_text(header.get("build_version"), bundle.get("CFBundleVersion")),
            file_path=str(path),
            raw_text=content[:3000],
        )

    def _parse_crash_text(self, path: Path, content: str) -> CrashReport | None:
        """Parse older-format .crash text crash report."""
        crash_id = uuid.uuid4().hex[:12]

        proc_match = crash_frames._TEXT_PROCESS.search(content)
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

        # The crashed thread, and the Binary Images the frames point into.
        # Only the part after the address was kept, which dropped the image.
        try:
            frames, images = crash_frames.crash_text_frames(content)
        except (TypeError, ValueError, AttributeError):
            logger.warning("Could not read the frames of %s", path, exc_info=True)
            frames, images = [], []
        identifier = re.search(r"^Identifier:\s+(\S+)", content, re.MULTILINE)
        exe_path = re.search(r"^Path:\s+(\S.*)$", content, re.MULTILINE)
        version = re.search(r"^Version:\s+(\S+)(?:\s+\((\S+)\))?", content, re.MULTILINE)

        # Timestamp
        ts_match = re.search(r"^Date/Time:\s+(.+)$", content, re.MULTILINE)
        ts = self._parse_timestamp(
            ts_match.group(1).strip() if ts_match else "", fallback=_mtime(path),
        )

        return CrashReport(
            crash_id=crash_id,
            timestamp=ts,
            device_id=(crash_frames.simulator_udid(exe_path.group(1)) if exe_path else "")
            or self.device_id,
            process=proc_name,
            exception_type=exc_type,
            exception_codes=exc_codes,
            signal=signal_name,
            top_frames=[crash_frames.format_frame(f) for f in frames[:crash_frames.TOP_FRAMES]],
            frames=frames,
            images=images,
            frames_from="crashing_thread" if frames else "",
            app_frame=crash_frames.first_app_frame(frames),
            bundle_id=identifier.group(1) if identifier else "",
            app_version=version.group(1) if version else "",
            build_version=(version.group(2) or "") if version else "",
            file_path=str(path),
            raw_text=content[:3000],
        )

    @staticmethod
    def _crash_summary(report: CrashReport) -> str:
        """Build a one-line summary of a crash for the LogEntry message."""
        parts = [f"CRASH: {report.process}"]
        if report.exception_type:
            parts.append(report.exception_type)
        if report.signal and f"({report.signal})" not in report.exception_type:
            # An Android native crash's type already names it: "signal 11 (SIGSEGV)".
            parts.append(f"({report.signal})")
        # Where in the app's code, when the report can say; else the top frame.
        if report.killed_by:
            parts.append(f"(killed by {report.killed_by})")
        elif report.app_frame is not None:
            parts.append(f"in {crash_frames.format_frame(report.app_frame)}")
        elif report.top_frames:
            parts.append(f"@ {report.top_frames[0]}")
        return " ".join(parts)

    @staticmethod
    def _parse_timestamp(ts_str: str, fallback: datetime | None = None) -> datetime:
        """Best-effort timestamp parsing from crash report.

        Unreadable, it is `fallback` -- the file's modification time, where the
        caller has one -- and only then now. That helps a report written on
        this Mac (a simulator's, in DiagnosticReports), whose mtime is when it
        was written. It does not help a pulled one, whose mtime is when it was
        last copied (`_stamp_copied`). A crash report carries its own time
        (`captureTime`, `Date/Time`), so this is rare.
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


def _stamp_copied(target: Path, names: list[str]) -> None:
    """Set each file just pulled to the copy time.

    pymobiledevice3 keeps the device's time as the file's mtime (measured: a
    report copied twice kept 19:08:47, its crash time). Retention reads the
    mtime as "last copied", and by crash age it would delete a report someone
    had just pulled on purpose with a wider `days` -- then copy it back on the
    next pull.
    """
    for name in names:
        try:
            os.utime(target / name)     # never creates: a report not copied stays absent
        except OSError:
            continue


def _text(*values) -> str:
    """The first value that is non-empty text; report fields can be anything."""
    return next((v for v in values if isinstance(v, str) and v), "")


def _images_for(images, frames, app_frame):
    """The images the returned frames point into, and the app frame's --
    which may lie past the cap, and is the one most worth symbolicating."""
    names = {f.image for f in frames} | ({app_frame.image} if app_frame else set())
    return [i for i in images if i.name in names]
