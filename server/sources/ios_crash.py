"""iPhone crash reports through pymobiledevice3: list, pull recent, clear (#322).

The pull used `idevicecrashreport -k`, which copies a phone's whole crash
history on every call. Since #316 left reports on the phone, that history
only grows, and a pull has 30 seconds: on a busy phone it could time out
before reaching the newest report -- the one an agent is asking about -- and
the copy order is not chronological.

pymobiledevice3, already a dependency, can list a phone's reports without
copying them and pull only the ones named. So a pull lists first, keeps the
reports dated within a window (3 days by default), pulls those, and says how
many older ones it left behind. Measured on an iPhone 12 (iOS 26.5.2) over
USB, no tunnel: a listing took 0.63s and a one-report pull 0.54s, most of each
being process start-up.

Every report's basename carries its capture time, device-local
(`Calculator-2026-09-27-190847.ips`); that is what the window reads.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

LIST_TIMEOUT_S = 15
PULL_TIMEOUT_S = 30
CLEAR_TIMEOUT_S = 30

DEFAULT_WINDOW_DAYS = 3

#: Only these are crash reports; the listing also holds logs and directories.
REPORT_SUFFIXES = (".ips", ".crash")

_NAME_TIME = re.compile(r"-(\d{4}-\d{2}-\d{2})-(\d{6})(?:\.[A-Za-z0-9]+)+$")


class IosCrashError(Exception):
    """pymobiledevice3 could not do what was asked. The message says why."""


@dataclass
class Selection:
    """Which of a phone's reports a pull wants, and what it leaves behind."""

    wanted: list[str] = field(default_factory=list)
    #: Reports dated before the window: on the phone, not pulled.
    older: int = 0
    #: The oldest report date on the phone, pulled or not.
    oldest: date | None = None


def find_binary() -> str | None:
    """The pymobiledevice3 CLI, or None. Separate so tests can replace it."""
    from server.device.tunneld import find_pymobiledevice3_binary

    path = find_pymobiledevice3_binary()
    return str(path) if path else None


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


async def _run(cmd: list[str], timeout: float) -> str:
    """Run a pymobiledevice3 command; its stdout, or IosCrashError saying why."""
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError as e:
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise IosCrashError(
            f"pymobiledevice3 {cmd[1]} {cmd[2]} timed out after {timeout:g}s",
        ) from e
    except OSError as e:
        raise IosCrashError(f"could not run pymobiledevice3: {e}") from e
    except asyncio.CancelledError:
        if proc is not None and proc.returncode is None:
            proc.kill()
        raise
    if proc.returncode:
        said = _last_line(stderr.decode(errors="replace")) or "no output"
        raise IosCrashError(f"pymobiledevice3 {cmd[1]} {cmd[2]} exited {proc.returncode}: {said}")
    return stdout.decode(errors="replace")


def _last_line(text: str) -> str:
    """The error, without pymobiledevice3's timestamp-and-logger prefix."""
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if not lines:
        return ""
    return re.sub(r"^\S+ \S+ \S+ [\w.]+\[\d+\] [A-Z]+ ", "", lines[-1])


def _udid_args(udid: str | None) -> list[str]:
    return ["--udid", udid] if udid else []


async def list_reports(binary: str, udid: str | None) -> list[str]:
    """Basenames of the crash reports at the top of the phone's crash directory."""
    out = await _run(
        [binary, "crash", "ls", *_udid_args(udid), "--depth", "1"], LIST_TIMEOUT_S,
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


async def pull_reports(binary: str, udid: str | None, names: list[str], out: Path) -> None:
    """Copy exactly these reports into `out`, leaving them on the phone."""
    if not names:
        return
    pattern = "^(?:" + "|".join(re.escape(n) for n in names) + ")$"
    await _run(
        [binary, "crash", "pull", *_udid_args(udid), "--match", pattern, str(out)],
        PULL_TIMEOUT_S,
    )


async def clear_reports(binary: str, udid: str | None) -> None:
    """Delete every crash report from the phone. Permanent; callers must mean it."""
    await _run([binary, "crash", "clear", *_udid_args(udid)], CLEAR_TIMEOUT_S)
