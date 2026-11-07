"""Why a log answer filtered to one process came back empty (#619).

An iOS app that writes with `print()`, `debugPrint()` or `dump()` writes to its
stdout, which unified logging never carries and quern never reads. So an agent
that filters `query_logs` to that app's process gets nothing, reads it as "the
app logged nothing", and debugs from a false premise. When the process is
running on a simulator whose logs quern is streaming, the empty answer says
what could explain it.

The note is decided from the app's whole history in the buffer, never from the
query's own filters: a search or a `since` that matched nothing says nothing
about whether the app logs, and blaming `print()` for it sends the reader to
the wrong place (review). And "no entries at all" counts as silence only when
the buffer has kept everything since the app started: on a busy server its
lines may simply have been evicted.

Simulators only, on purpose. A simulator's processes are the Mac's own, so
whether one is running, and since when, is a `ps` away; its app executables
sit under that simulator's `CoreSimulator/Devices/<udid>/`, which ties the
process to the device. A physical iPhone has no such cheap answer, and on
Android `print`-style output reaches logcat anyway, whose `process` is the log
tag. Where quern cannot tell, it says nothing: "could not tell" is not
"running".
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)

#: Bounded, because the note is a courtesy on an answer, never a reason to
#: hold it up. A `ps` that takes longer is a "could not tell".
PS_TIMEOUT_S = 2.0
#: An agent polling an empty answer asks again within seconds; one `ps` serves
#: them all for this long rather than one each.
PS_CACHE_S = 5.0

Ps = Callable[[], Awaitable[list[tuple[float, str]] | None]]
_cache: tuple[float, list[tuple[float, str]]] | None = None


def parse_etime(text: str) -> float | None:
    """Seconds from ps's `etime`, `[[dd-]hh:]mm:ss`; None if it isn't one."""
    days, _, clock = text.strip().rpartition("-")
    parts = clock.split(":")
    try:
        nums = [int(p) for p in parts]
        d = int(days) if days else 0
    except ValueError:
        return None
    if not 2 <= len(nums) <= 3:
        return None
    h, m, s = ([0] + nums)[-3:]
    return float(((d * 24 + h) * 60 + m) * 60 + s)


async def _ps_rows() -> list[tuple[float, str]] | None:
    """(seconds running, executable path) for every process, or None when
    `ps` could not say."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ps", "-axo", "etime=,comm=",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except OSError as e:
        logger.debug("ps could not start: %s", e)
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), PS_TIMEOUT_S)
    except TimeoutError:
        try:
            proc.kill()
        except OSError:          # it exited in the meantime
            pass
        await proc.wait()
        return None
    if proc.returncode != 0:
        return None
    rows = []
    for line in out.decode(errors="replace").splitlines():
        etime, _, path = line.strip().partition(" ")
        seconds = parse_etime(etime)
        if seconds is not None and path:
            rows.append((seconds, path.strip()))
    return rows


async def _rows(ps: Ps | None) -> list[tuple[float, str]] | None:
    global _cache
    if ps is not None:
        return await ps()
    now = time.monotonic()
    if _cache is not None and now - _cache[0] < PS_CACHE_S:
        return _cache[1]
    rows = await _ps_rows()
    if rows is not None:
        _cache = (now, rows)
    return rows


async def running_on_simulators(
    process: str, udids: Iterable[str], ps: Ps | None = None,
) -> dict[str, datetime] | None:
    """When `process` started on each simulator in `udids` it is running on,
    or None when that could not be told. An app's executable is
    `.../CoreSimulator/Devices/<udid>/data/Containers/Bundle/Application/<id>/
    <App>.app/<App>`, and its log lines carry `<App>` as their process."""
    udids = list(udids)
    if not process or not udids:
        return {}
    rows = await _rows(ps)
    if rows is None:
        return None
    now = datetime.now(UTC)
    found: dict[str, datetime] = {}
    for udid in udids:
        marker = f"/CoreSimulator/Devices/{udid}/"
        ages = [age for age, path in rows
                if marker in path and os.path.basename(path) == process]
        if ages:
            found[udid] = now - timedelta(seconds=min(ages))
    return found


def gap_note(process: str, *, filtered: list[str], below: list[str],
             silent: list[str], level: str | None) -> str | None:
    """What to say about an empty answer for `process`. A log filter that
    may be dropping it first, then lines that exist but all below the
    caller's level, then silence since it started, which is `print()`."""
    def on(udids: list[str]) -> str:
        return ("simulator " if len(udids) == 1 else "simulators ") + ", ".join(udids)
    if filtered:
        return (f"No entries from {process}, which is running on {on(filtered)}: a log "
                f"filter set with set_log_filter applies there and may be dropping its "
                f"entries. get_log_filter shows it; widen or clear it to see them.")
    if below and level:
        return (f"{process} is logging on {on(below)}, but only below level {level}: "
                f"ask without level, or with a lower one.")
    if silent:
        return (f"No entries at all from {process} since it started on {on(silent)}. "
                f"Quern sees what an iOS app logs through unified logging (os.Logger, "
                f"os_log, NSLog), not print(), debugPrint() or dump(), which go to its "
                f"stdout: if the app uses print(), its output isn't captured. See "
                f"quern://guide/logs.")
    return None
