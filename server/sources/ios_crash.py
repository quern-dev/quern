"""iPhone crash reports through pymobiledevice3: list, pull recent, delete (#322).

The pull used `idevicecrashreport -k`, which copies a phone's whole crash
history on every call. Since #316 left reports on the phone, that history
only grows, and a pull has 30 seconds: on a busy phone it could time out
before reaching the newest report -- the one an agent is asking about -- and
the copy order is not chronological.

pymobiledevice3 can list a phone's reports without copying them and pull only
the ones named. So a pull lists first, keeps the reports dated within a window
(3 days by default), pulls those, and says how many older ones it left behind.
Measured on an iPhone 12 (iOS 26.5.2) over USB, no tunnel: a listing of six
reports took 0.63s and a one-report pull 0.54s, most of each being process
start-up. A listing is one stat per entry, so a phone with a long history will
take longer; that has not been measured.

It is run as `python -m pymobiledevice3` with quern's own interpreter, so the
CLI is the library quern depends on, at the version quern installed -- not
whichever copy is first on PATH, which may be missing or older.

Every report's basename carries its capture time, device-local
(`Calculator-2026-09-27-190847.ips`); that is what the window reads.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

LIST_TIMEOUT_S = 15
PULL_TIMEOUT_S = 30
REMOVE_TIMEOUT_S = 30

DEFAULT_WINDOW_DAYS = 3

#: Only these are crash reports. The crash directory also holds logs, and
#: directories -- DiagnosticLogs, where sysdiagnose archives live, and
#: Retired -- which a pull ignores and a delete must never touch.
REPORT_SUFFIXES = (".ips", ".crash")

_NAME_TIME = re.compile(r"-(\d{4}-\d{2}-\d{2})-(\d{6})(?:\.[A-Za-z0-9]+)+$")
_LOG_PREFIX = re.compile(r"^\S+ \S+ \S+ [\w.]+\[\d+\] [A-Z]+ ")
_BOX = "│╭╰╮╯─ "

#: Deletes exactly the named reports, one REMOVE_PATH each. The CLI's
#: `crash clear` removes every child of the crash directory recursively --
#: DiagnosticLogs and its sysdiagnose archives included -- so it is not used.
_REMOVE_SCRIPT = """
import asyncio, json, sys
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.crash_reports import CrashReportsManager

async def main(udid, names):
    lockdown = await create_using_usbmux(serial=udid)
    removed, failed = [], []
    async with CrashReportsManager(lockdown) as manager:
        for name in names:
            ok = await manager.afc.rm_single("/" + name, force=True)
            (removed if ok else failed).append(name)
    print(json.dumps({"removed": removed, "failed": failed}))

asyncio.run(main(sys.argv[1], sys.argv[2:]))
"""


class IosCrashError(Exception):
    """pymobiledevice3 could not do what was asked. The message says why.

    `copied` is what a failed pull did copy before it stopped.
    """

    def __init__(self, message: str, copied: list[str] | None = None) -> None:
        super().__init__(message)
        self.copied = copied or []


@dataclass
class Selection:
    """Which of a phone's reports a pull wants, and what it leaves behind."""

    wanted: list[str] = field(default_factory=list)
    #: Reports dated before the window: on the phone, not pulled.
    older: int = 0
    #: The oldest report date on the phone, pulled or not.
    oldest: date | None = None


def command() -> list[str] | None:
    """How to run pymobiledevice3: quern's interpreter with its own copy.

    Falls back to a standalone CLI only if the library is somehow absent.
    Separate so tests can replace it.
    """
    if importlib.util.find_spec("pymobiledevice3") is not None:
        return [sys.executable, "-m", "pymobiledevice3"]
    from server.device.ios.tunneld import find_pymobiledevice3_binary

    path = find_pymobiledevice3_binary()
    return [str(path)] if path else None


def report_time(name: str) -> datetime | None:
    """The device-local capture time in a report's basename, if it has one."""
    m = _NAME_TIME.search(name)
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H%M%S")
    except ValueError:
        return None


def select_recent(names: list[str], days: int, now: datetime) -> Selection:
    """The reports dated within `days` of `now`, plus undated ones.

    The window gets an extra day of margin: a name's time is the device's
    local time and `now` is the Mac's, and the two can sit in different zones.
    A report with no date in its name is pulled -- in practice iOS dates every
    report, and over-pulling one beats hiding a crash.
    """
    cutoff = now - timedelta(days=days + 1)
    selection = Selection()
    for name in names:
        when = report_time(name)
        if when is not None and (selection.oldest is None or when.date() < selection.oldest):
            selection.oldest = when.date()
        if when is None or when >= cutoff:
            selection.wanted.append(name)
        else:
            selection.older += 1
    return selection


async def _run(argv: list[str], what: str, timeout: float) -> tuple[str, str]:
    """Run a command; (stdout, stderr), or IosCrashError saying why it failed."""
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError as e:
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise IosCrashError(f"pymobiledevice3 {what} timed out after {timeout:g}s") from e
    except OSError as e:
        raise IosCrashError(f"could not run pymobiledevice3: {e}") from e
    except asyncio.CancelledError:
        if proc is not None and proc.returncode is None:
            proc.kill()
        raise
    err = stderr.decode(errors="replace")
    if proc.returncode:
        said = _last_line(err) or "no output"
        raise IosCrashError(f"pymobiledevice3 {what} exited {proc.returncode}: {said}")
    return stdout.decode(errors="replace"), err


def _last_line(text: str) -> str:
    """The error, without the log prefix or the frame a usage error is drawn in."""
    lines = []
    for raw in text.splitlines():
        line = _LOG_PREFIX.sub("", raw.strip()).strip(_BOX)
        if line:
            lines.append(line)
    if not lines:
        return ""
    # A usage error is a box: "Error" in the border, the reason inside it.
    if len(lines) > 1 and lines[0].lower() == "error":
        return " ".join(lines[1:])
    return lines[-1]


def _require(udid: str | None) -> str:
    """Every call names its phone. Without --udid, pymobiledevice3 picks the
    first USB device -- the wrong one when two are plugged in."""
    if not udid:
        raise IosCrashError("no device udid: refusing to act on whichever phone is first")
    return udid


async def list_reports(cmd: list[str], udid: str | None) -> list[str]:
    """Basenames of the crash reports at the top of the phone's crash directory."""
    out, _ = await _run(
        [*cmd, "crash", "ls", "--udid", _require(udid), "--depth", "1"],
        "crash ls", LIST_TIMEOUT_S,
    )
    names = []
    for line in out.splitlines():
        path = line.strip()
        if not path.startswith("/") or path.count("/") != 1:
            continue
        name = path[1:]
        if name.endswith(REPORT_SUFFIXES):
            names.append(name)
    return names


async def pull_reports(cmd: list[str], udid: str | None, names: list[str], out: Path) -> list[str]:
    """Copy exactly these reports into `out`, leaving them on the phone.

    Returns the names copied. Raises IosCrashError naming the ones that were
    not, after keeping the ones that were.

    Through a staging directory, moved into `out` only when complete: the
    pull skips a file it cannot read ("(Ignoring) Error", still exit 0) after
    creating it, so pulling straight into `out` left an empty file -- and
    truncated a good copy from an earlier pull to nothing.
    """
    if not names:
        return []
    udid = _require(udid)
    staging = out / ".incoming"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    pattern = "^(?:" + "|".join(re.escape(n) for n in names) + ")$"
    try:
        try:
            _, err = await _run(
                [*cmd, "crash", "pull", "--udid", udid, "--match", pattern, str(staging)],
                "crash pull", PULL_TIMEOUT_S,
            )
        except IosCrashError as e:
            copied = _keep_complete(staging, out, names)
            raise IosCrashError(f"{e}; copied {len(copied)} of {len(names)}", copied) from e
        copied = _keep_complete(staging, out, names)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    missing = [n for n in names if n not in copied]
    if missing:
        said = _last_line("\n".join(ln for ln in err.splitlines() if "(Ignoring)" in ln))
        raise IosCrashError(
            f"pymobiledevice3 crash pull did not copy {len(missing)} of {len(names)} "
            f"report(s) ({', '.join(missing[:3])}{', ...' if len(missing) > 3 else ''})"
            + (f": {said}" if said else ""),
            copied,
        )
    return copied


def _keep_complete(staging: Path, out: Path, names: list[str]) -> list[str]:
    copied = []
    for name in names:
        src = staging / name
        try:
            if src.stat().st_size == 0:
                continue
            os.replace(src, out / name)
        except OSError:
            continue
        copied.append(name)
    return copied


async def remove_reports(udid: str | None, names: list[str]) -> tuple[list[str], list[str]]:
    """Delete exactly these reports from the phone: (removed, failed). Permanent.

    Needs the library, run with quern's interpreter; the CLI has no per-file
    delete.
    """
    if not names:
        return [], []
    udid = _require(udid)
    if importlib.util.find_spec("pymobiledevice3") is None:
        raise IosCrashError("deleting single reports needs the pymobiledevice3 library")
    out, _ = await _run(
        [sys.executable, "-c", _REMOVE_SCRIPT, udid, *names], "crash delete", REMOVE_TIMEOUT_S,
    )
    try:
        result = json.loads(out.strip().splitlines()[-1])
        return list(result["removed"]), list(result["failed"])
    except (IndexError, KeyError, TypeError, ValueError) as e:
        raise IosCrashError(f"pymobiledevice3 crash delete gave no result: {out[-200:]!r}") from e
