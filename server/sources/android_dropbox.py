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
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from server.models import CrashReport

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


def _parse_records(text: str, *, serial: str, zone) -> tuple[list[CrashReport], int]:
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


def _parse_record(block: str, *, serial: str, zone):
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

    exception_type = exception_codes = signal = ""
    frames: list[str] = []
    if kind == "crash":
        m = _JAVA_EXCEPTION.search(body)
        if m:
            exception_type, exception_codes = m.group(1), m.group(2) or ""
        frames = _JAVA_FRAME.findall(body)
    elif kind == "native_crash":
        m = _SIGNAL.search(body)
        if m:
            signal = m.group(2)
            exception_type = f"signal {m.group(1)} ({m.group(2)})"
            exception_codes = m.group(3)
        frames = _NATIVE_FRAME.findall(body)
    else:  # anr
        exception_type = "ANR"
        m = _SUBJECT.search(body)
        exception_codes = m.group(1) if m else ""
        frames = _main_thread_frames(body, pid)

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
        file_path=f"dropbox:{tag}@{local_time}",
        raw_text=block[:RAW_LIMIT],
    )


def _timestamp(local_time: str, tombstone_body: str, zone) -> datetime | None:
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
            frames.append(stripped.removeprefix("at "))
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
