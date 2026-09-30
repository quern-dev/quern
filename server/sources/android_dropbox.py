"""Android crash reports, from the system's DropBox (#316).

iOS crash reports arrive as files, and `get_latest_crash` has always read
them. Android has no report file a host can read without root -- tombstones
live in /data -- so on Android `get_latest_crash` returned nothing, even
straight after a crash. The logcat adapter recognises crashes as they stream
(#255), but only while capture is running, and only as a line or two.

DropBox is where Android keeps its own record of every app crash, native
crash and ANR, and `dumpsys dropbox` prints it to the shell user on an
unrooted phone. This reads it and turns each record into a CrashReport.

Three things about the format, each found on real devices rather than
assumed:

- **The tag names the kind of app, not only the kind of failure.** Settings is
  a system app, so its crashes were filed as `system_app_crash`; asking for
  `data_app_crash` alone found nothing. Both families are read.
- **The header time is device-local with no zone** (`2026-09-26 11:15:45` for a
  crash at 18:15 UTC), the trap #255 found in logcat. It is converted with the
  device's zone *name*, so the offset in force on that date applies. A native
  crash's tombstone carries its own zone-stamped time
  (`13:54:24.057854241-0700`), which is used when present.
- **Records repeat across pulls.** DropBox keeps up to 1,000 entries and prints
  all of them every time, so ids are derived from the record, not random, and
  a crash pulled twice is one report.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from typing import TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from server.models import CrashFrame, CrashImage, CrashReport
from server.sources import crash_frames

#: Every app crash, native crash and ANR, for both regular and system apps.
#: SYSTEM_TOMBSTONE is left out: it is the same native crash again, written by
#: a different component, and would count every native crash twice.
CRASH_TAGS = (
    "data_app_crash", "system_app_crash",
    "data_app_native_crash", "system_app_native_crash",
    "data_app_anr", "system_app_anr",
)

#: Tag suffix -> kind, longest first: `system_app_native_crash` also ends in
#: `_crash`, and checking that first filed every native crash as a Java one.
_KIND = (("_native_crash", "native_crash"), ("_anr", "anr"), ("_crash", "crash"))
_SEPARATOR = re.compile(r"^=+$", re.M)
_HEADER = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (\S+) \(")
_TOMBSTONE_TIME = re.compile(
    r"^Timestamp: (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(\.\d+)?([+-]\d{4})", re.M,
)
_SIGNAL = re.compile(r"^signal (\d+) \((SIG\w+)\), (.*)$", re.M)
_SUBJECT = re.compile(r"^Subject: (.*)$", re.M)
_JAVA_EXCEPTION = re.compile(r"^([\w$.]+(?:Exception|Error|Throwable)[\w$]*)(?:: (.*))?$", re.M)
_JAVA_FRAME = re.compile(r"^\s+at (.+)$", re.M)
_NATIVE_FRAME = re.compile(r"^\s+(#\d+ pc .+)$", re.M)
_MAIN_THREAD = re.compile(r'^"main" ', re.M)

TOP_FRAMES = 8
RAW_LIMIT = 4000


def device_zone(zone_name: str, offset: str) -> timezone | ZoneInfo | None:
    """The device's zone: its name if usable, else its current offset.

    The name is preferred because it knows daylight saving -- an offset read
    today is an hour wrong for a crash from the other side of a change.
    None means neither was usable, and times cannot be converted honestly.
    """
    name = zone_name.strip()
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    m = re.fullmatch(r"([+-])(\d{2})(\d{2})", offset.strip())
    if m:
        delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
        return timezone(delta if m.group(1) == "+" else -delta)
    return None


def parse_dropbox(text: str, *, serial: str, zone: timezone | ZoneInfo | None) -> list[CrashReport]:
    """Every crash record in `dumpsys dropbox --print` output, as CrashReports."""
    return _parse_records(text, serial=serial, zone=zone)[0]


def _parse_records(
    text: str, *, serial: str, zone: timezone | ZoneInfo | None,
) -> tuple[list[CrashReport], int]:
    """The reports, and how many crash records could not be dated.

    A record is undated when the device's zone is unknown and it carries no
    zone-stamped time of its own. It is dropped rather than guessed at, and
    counted, because a pull that dropped records must not read as complete.
    """
    reports, undated = [], 0
    for block in _SEPARATOR.split(text):
        report = _parse_record(block.strip("\n"), serial=serial, zone=zone)
        if report is _UNDATED:
            undated += 1
        elif report is not None:
            reports.append(report)
    return reports, undated


_UNDATED = object()


def _parse_record(
    block: str, *, serial: str, zone: timezone | ZoneInfo | None,
) -> CrashReport | object | None:
    lines = block.splitlines()
    if not lines:
        return None
    header = _HEADER.match(lines[0])
    if not header:
        return None
    local_time, tag = header.group(1), header.group(2)
    if tag not in CRASH_TAGS:
        return None
    kind = next(k for suffix, k in _KIND if tag.endswith(suffix))

    fields: dict[str, str] = {}
    body_start = len(lines)
    for i, line in enumerate(lines[1:], start=1):
        if not line.strip():
            body_start = i + 1
            break
        key, sep, value = line.partition(": ")
        if sep:
            fields[key] = value
    body = "\n".join(lines[body_start:])

    timestamp = _timestamp(local_time, body if kind == "native_crash" else "", zone)
    if timestamp is None:
        return _UNDATED
    process = fields.get("Process", "")
    pid = fields.get("PID", "")

    exception_type = exception_codes = signal = reason = ""
    frames: list[str] = []
    structured: list[CrashFrame] = []
    native: list[tuple[CrashFrame, str]] = []      # native crashes' frames, with paths
    app_frame: CrashFrame | None = None
    package, app_version, build_version = _package(fields.get("Package", ""))
    if kind == "crash":
        m = _JAVA_EXCEPTION.search(body)
        if m:
            exception_type, exception_codes = m.group(1), m.group(2) or ""
        frames = _JAVA_FRAME.findall(body)
        structured, app_frame, reason = _java_trace(body, package)
    elif kind == "native_crash":
        m = _SIGNAL.search(body)
        if m:
            signal = m.group(2)
            exception_type = f"signal {m.group(1)} ({m.group(2)})"
            exception_codes = m.group(3)
        # The crashing thread's backtrace only: the tombstone goes on to
        # every other thread's, and reading the whole body mixed them in.
        frames = _NATIVE_FRAME.findall(_crashing_backtrace(body))
        native = _safe(_native_frames, frames)
        structured = [frame for frame, _ in native]
        app_frame = crash_frames.first_app_frame(structured)     # from the whole stack
        abort = _ABORT_MESSAGE.search(body)
        reason = abort.group(1) if abort else ""
    else:  # anr
        exception_type = "ANR"
        m = _SUBJECT.search(body)
        exception_codes = m.group(1) if m else ""
        lines = _main_thread_frames(body, pid)
        frames = [line.removeprefix("at ") for line in lines]
        structured = _anr_frames(lines, package)
        app_frame = crash_frames.first_app_frame(structured)

    # Derived from the record, so a crash seen by two pulls is one report.
    # From the UTC time, not the header's: DropBox prints the header in the
    # zone in force *now*, so after the device changed zone -- travel, or a
    # manual change -- every record would have come back with a new id.
    identity = f"{serial}|{tag}|{timestamp.isoformat()}|{pid}|{process}"
    crash_id = "android-" + hashlib.sha1(identity.encode()).hexdigest()[:12]

    return CrashReport(
        crash_id=crash_id,
        timestamp=timestamp,
        device_id=serial,
        process=process,
        pid=int(pid) if pid.isdigit() else None,
        kind=kind,
        exception_type=exception_type,
        exception_codes=exception_codes,
        signal=signal,
        top_frames=[f.strip() for f in frames[:TOP_FRAMES]],
        frames=structured[:crash_frames.MAX_FRAMES],
        images=_native_images(native, app_frame),
        frames_from=_FRAMES_FROM[kind] if structured else "",
        app_frame=app_frame,
        reason=reason,
        bundle_id=package,
        app_version=app_version,
        build_version=build_version,
        file_path=f"dropbox:{tag}@{local_time}",
        raw_text=block[:RAW_LIMIT],
    )


def _timestamp(
    local_time: str, tombstone_body: str, zone: timezone | ZoneInfo | None,
) -> datetime | None:
    """UTC, from the tombstone's own zone-stamped time if it has one.

    A header time inside the hour repeated when clocks go back is ambiguous,
    and is read as the first pass through it; the header carries nothing that
    could say which.
    """
    m = _TOMBSTONE_TIME.search(tombstone_body)
    if m:
        frac = (m.group(2) or ".0")[:7]
        try:
            return datetime.strptime(
                f"{m.group(1)}{frac}{m.group(3)}", "%Y-%m-%d %H:%M:%S.%f%z",
            ).astimezone(UTC)
        except ValueError:
            pass
    if zone is None:
        return None
    naive = datetime.strptime(local_time, "%Y-%m-%d %H:%M:%S")
    return naive.replace(tzinfo=zone).astimezone(UTC)


def _crashing_backtrace(body: str) -> str:
    """The `backtrace:` block of the crashing thread, which a tombstone lists
    first; the whole body when there is none."""
    start = body.find("\nbacktrace:")
    if start == -1:
        return body
    block = body[start + len("\nbacktrace:"):]
    end = block.find("\n\n")
    return block if end == -1 else block[:end]


def _native_frames(lines: list[str]) -> list[tuple[CrashFrame, str]]:
    return [parsed for parsed in map(crash_frames.native_frame, lines) if parsed]


def _native_images(native: list[tuple[CrashFrame, str]],
                   app_frame: CrashFrame | None) -> list[CrashImage]:
    """The libraries the returned frames point into, with their BuildIds, and
    the app frame's, which may lie past the cap.

    One per path, not per name: an app's own `libcrypto.so` and the system's
    share a name, and keying by it kept only the first. A frame's `image` and
    `build_id` together say which of them it is."""
    wanted = {path for _, path in native[:crash_frames.MAX_FRAMES]}
    wanted |= {path for frame, path in native if frame is app_frame}
    images: dict[str, CrashImage] = {}
    for frame, path in native:
        if path and path in wanted and path not in images:
            images[path] = CrashImage(name=frame.image, uuid=frame.build_id, path=path)
    return list(images.values())


_PACKAGE = re.compile(r"^(\S+)(?: v(\d+))?(?: \((.+)\))?")
_ABORT_MESSAGE = re.compile(r"^Abort message: '(.*)'\s*$", re.M)
_CAUSED_BY = re.compile(r"^Caused by: (.+)$", re.M)


def _package(value: str) -> tuple[str, str, str]:
    """`com.example.app v32 (1.2.3)` -> package, version name, version code."""
    m = _PACKAGE.match(value.strip())
    if not m:
        return "", "", ""
    return m.group(1), m.group(3) or "", m.group(2) or ""


def _java_trace(body: str, package: str) -> tuple[list[CrashFrame], CrashFrame | None, str]:
    """The frames of a Java trace, where in the app it began, and its root cause.

    The frames are the outer exception's followed by each `Caused by`. Where it
    began is the first app frame of the innermost cause that reaches the app's
    code: the outer exception is often only a wrapper rethrowing it. The root
    cause's message is the reason.
    """
    blocks: list[list[str]] = [[]]
    first = _JAVA_EXCEPTION.search(body)
    headers = [first.group(0).strip() if first else ""]
    for line in body.splitlines():
        if line.startswith("Caused by: "):
            blocks.append([])
            headers.append(line.strip())
        elif _JAVA_FRAME.match(line):
            blocks[-1].append(line)
    # Decided over the whole trace, so every block agrees on which package is
    # the app's; then split back into blocks to find the innermost cause.
    lines = [line for block in blocks for line in block]
    frames = _safe(crash_frames.java_frames, lines, package)
    parsed, i = [], 0
    for block, header in zip(blocks, headers, strict=True):
        parsed.append(frames[i:i + len(block)])
        if parsed[-1] and len(frames) == len(lines):
            parsed[-1][0].thrown = header
        i += len(block)
    app_frame = root_cause_app_frame(parsed)
    causes = _CAUSED_BY.findall(body)
    # The root cause; a trace with no cause is its own.
    reason = causes[-1].strip() if causes else (first.group(0).strip() if first else "")
    return frames, app_frame, reason


def root_cause_app_frame(blocks: list[list[CrashFrame]]) -> CrashFrame | None:
    """The first app frame of the innermost cause that reaches the app's code."""
    return next((a for a in (crash_frames.first_app_frame(b) for b in reversed(blocks))
                 if a is not None), None)


def java_blocks(frames: list[CrashFrame]) -> list[list[CrashFrame]]:
    """Frames split back into the exception's blocks, by the `thrown` line on
    each block's first frame."""
    blocks: list[list[CrashFrame]] = [[]]
    for frame in frames:
        if frame.thrown and blocks[-1]:
            blocks.append([])
        blocks[-1].append(frame)
    return blocks


#: What `frames` is, per kind of record.
_FRAMES_FROM = {"crash": "exception", "native_crash": "crashing_thread", "anr": "main_thread"}


def _anr_frames(lines: list[str], package: str) -> list[CrashFrame]:
    """The ANR'd main thread, `at` and `native:` lines in order, with the Java
    frames' app flags decided over all of them together."""
    java = _safe(crash_frames.java_frames, [ln for ln in lines if ln.startswith("at ")], package)
    java_iter = iter(java)
    frames: list[CrashFrame] = []
    for line in lines:
        if line.startswith("at "):
            frame = next(java_iter, None)
            if frame is not None:
                frames.append(frame)
        else:
            frames += _safe(crash_frames.native_frames, [line.removeprefix("native: ")])
    return frames


_T = TypeVar("_T")


def _safe(parse: Callable[..., list[_T]], lines: list[str], *args: str) -> list[_T]:
    """A parse of frames that cannot fail the pull: a record with frames it
    cannot read is still a crash, just without them."""
    try:
        return parse(lines, *args)
    except (TypeError, ValueError, AttributeError):
        return []


def _main_thread_frames(body: str, pid: str) -> list[str]:
    """The ANR'd app's main thread stack: where it was stuck.

    Only from the app's own `----- pid N` section. An ANR record carries
    stack dumps for several processes, and when the app's own dump is missing
    -- measured on an emulator, for an app too wedged to answer the dump
    request -- the first `"main"` thread in the record is system_server's.
    Returning that as the app's stack would point the reader at the wrong
    process entirely; no frames is the honest answer.
    """
    if not pid:
        return []
    start = body.find(f"----- pid {pid} ")
    if start == -1:
        return []
    ends = [
        e for e in (body.find("----- end ", start), body.find("\n----- pid ", start + 1))
        if e != -1
    ]
    section = body[start:min(ends)] if ends else body[start:]
    # The thread's own header line. A bare `"main"` also matches the
    # `| group="main"` line of every thread printed before it.
    main = _MAIN_THREAD.search(section)
    if main is None:
        return []
    frames = []
    for line in section[main.start():].splitlines()[1:]:
        if not line.strip():
            break
        stripped = line.strip()
        if stripped.startswith(("at ", "native:")):
            frames.append(stripped)
    return frames


#: Marks between the parts of the one shell round trip.
_TZ = "__QUERN_TZ__"
_OFFSET = "__QUERN_OFFSET__"
_PROCS = "__QUERN_PROCS__"
_SPLIT = "__QUERN_DROPBOX__"
_TAG = "__QUERN_TAG__"
_RC = "__QUERN_RC__"
_TZ_LINE = re.compile(rf"^{_TZ} ?(.*)$", re.M)
_OFFSET_LINE = re.compile(rf"^{_OFFSET} ?(.*)$", re.M)
_TAG_LINE = re.compile(rf"^{_TAG} (\S+)$", re.M)
_RC_LINE = re.compile(rf"^{_RC} (\d+)\s*$", re.M)
#: dumpsys's own failure lines, which it prints and then exits 0 after.
#: Anchored to dumpsys's own wording: a crash record whose message merely
#: contained one of these phrases marked its tag failed on every pull.
_DUMPSYS_FAILED = re.compile(
    r"^(?:Can't find service: .*|\*\*\* SERVICE '.*' DUMP TIMEOUT .*)$", re.M,
)

_PROCESS_RECORD = re.compile(r"ProcessRecord\{\w+ \d+:([^/}\s]+)")
#: `mCrashing=true` is measured (API 32; the same ProcessErrorStateRecord dump
#: from API 31 on). Android 11 and older printed the fields without the `m`,
#: per AOSP, and that spelling has not been seen on a device here.
_CRASHING = re.compile(r"\bm?[Cc]rashing=true\b")
_NOT_RESPONDING = re.compile(r"\bm?[Nn]otResponding=true\b")
#: The process lines, and the flag line printed under a process only while one
#: of these is set -- so the whole listing stays small.
_PROCS_GREP = (
    "dumpsys activity processes"
    " | grep -E 'ProcessRecord\\{|rashing=true|otResponding=true'"
)


def parse_open_dialogs(text: str) -> dict[str, str] | None:
    """Processes showing a crash dialog, or not responding: {process: kind}.

    "anr" is set when Android notices, and the ANR dialog and DropBox record
    follow about 13 seconds later, once its threads are dumped (measured, API
    32); it clears when the dialog is answered.

    None when the listing named no process at all. A device always has
    processes, so that is a listing that failed, and reporting it as "no
    dialogs" would be the false all-clear this exists to prevent.

    From `dumpsys activity processes`. This matters because Android holds a
    process that crashed twice in quick succession behind an "app keeps
    stopping" dialog, and while that dialog is open every further crash of it
    is dropped -- no DropBox record, no logcat line, `am crash` exiting 0.
    Found live: an app's first crashes were recorded and the next ones simply
    were not, with nothing anywhere saying why. A pull that finds no new
    crashes is true and still misleading unless it says this.

    It is the process that is held, not the window: after `am force-stop` the
    dialog stayed on screen and the next crash was recorded. And no dialog is
    shown over a lock screen, so a locked phone records every crash.
    """
    dialogs: dict[str, str] = {}
    current = ""
    seen_any = False
    for line in text.splitlines():
        m = _PROCESS_RECORD.search(line)
        if m:
            current = m.group(1)
            seen_any = True
            continue
        if not current:
            continue
        if _CRASHING.search(line):
            dialogs[current] = "crash"
        elif _NOT_RESPONDING.search(line):
            dialogs[current] = "anr"
    return dialogs if seen_any else None


@dataclass
class DropboxPull:
    reports: list[CrashReport] = field(default_factory=list)
    #: {process: "crash" | "anr"} for any process showing that dialog now;
    #: None when the process listing could not be read.
    open_dialogs: dict[str, str] | None = field(default_factory=dict)
    #: Tags that could not be read in full. The records that were read are
    #: still in `reports`; the pull as a whole did not succeed.
    errors: list[str] = field(default_factory=list)
    #: Crash records dropped because nothing said what zone their time is in.
    undated: int = 0


#: How long one pull may take. DropBox prints every stored record for each tag
#: -- up to 1,000 -- so this is generous; a wedged device must still not hang
#: the `get_latest_crash` call that asked.
PULL_TIMEOUT_S = 30


class DropboxPullError(Exception):
    """The pull could not be made. Distinct from a pull that found nothing."""


async def pull_dropbox(adb_path: str | None, serial: str) -> DropboxPull:
    """Every crash record DropBox holds for `serial`, and any open crash dialogs.

    Raises DropboxPullError when the device could not be asked, so a caller
    can tell "no crashes" from "could not look" -- the two must not read
    alike.
    """
    import asyncio

    if not adb_path:
        raise DropboxPullError("adb not found")
    # Each value behind its own marker: read by position, an empty timezone
    # property shifted the offset into the zone-name slot, the zone came out
    # unknown, and every Java crash and ANR was dropped. And each tag read
    # on its own, with its exit status and its stderr: `;` passes on only the
    # last command's status, so a failure in any earlier tag read as success
    # with its records missing. The status is not enough on its own either --
    # dumpsys exits 0 after "Can't find service" (measured, API 32) -- so the
    # text is checked too.
    script = "; ".join([
        f'echo "{_TZ} $(getprop persist.sys.timezone)"',
        f'echo "{_OFFSET} $(date +%z)"',
        f"echo {_PROCS}",
        _PROCS_GREP,
        f"echo {_SPLIT}",
        *(f"echo {_TAG} {tag}; dumpsys dropbox --print {tag} 2>&1; echo {_RC} $?"
          for tag in CRASH_TAGS),
    ])
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            adb_path, "-s", serial, "shell", script,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=PULL_TIMEOUT_S)
    except TimeoutError as e:
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise DropboxPullError(f"dumpsys dropbox timed out after {PULL_TIMEOUT_S}s") from e
    except OSError as e:
        raise DropboxPullError(f"could not run adb: {e}") from e
    except asyncio.CancelledError:
        if proc is not None and proc.returncode is None:
            proc.kill()
        raise

    out = stdout.decode(errors="replace")
    if proc.returncode != 0 or _SPLIT not in out:
        said = stderr.decode(errors="replace").strip() or out.strip()[:200] or "no output"
        raise DropboxPullError(f"adb shell failed (exit {proc.returncode}): {said}")

    head, _, body = out.partition(_SPLIT)
    zone_part, _, procs = head.partition(_PROCS)
    name, offset = _TZ_LINE.search(zone_part), _OFFSET_LINE.search(zone_part)
    zone = device_zone(
        name.group(1).strip() if name else "", offset.group(1).strip() if offset else "",
    )
    # Parsed in a thread: up to DropBox's 1,000 records, which should not hold
    # the event loop (measured: 0.16s for 43 crash records among 196 entries).
    return await asyncio.to_thread(_parse_pull, body, procs, serial, zone)


def _parse_pull(
    body: str, procs: str, serial: str, zone: timezone | ZoneInfo | None,
) -> DropboxPull:
    pulled = DropboxPull(open_dialogs=parse_open_dialogs(procs))
    chunks = _TAG_LINE.split(body)[1:]     # [tag, output, tag, output, ...]
    read = set()
    for tag, chunk in zip(chunks[::2], chunks[1::2], strict=True):
        read.add(tag)
        rc = _RC_LINE.search(chunk)
        text = chunk[:rc.start()] if rc else chunk
        if rc is None or rc.group(1) != "0":
            pulled.errors.append(f"{tag}: dumpsys exited {rc.group(1) if rc else '(unknown)'}")
        elif m := _DUMPSYS_FAILED.search(text):
            pulled.errors.append(f"{tag}: {m.group(0).strip()}")
        reports, undated = _parse_records(text, serial=serial, zone=zone)
        pulled.reports.extend(reports)
        pulled.undated += undated
    pulled.errors.extend(f"{tag}: not read" for tag in CRASH_TAGS if tag not in read)
    return pulled
