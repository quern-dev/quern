"""Why a log answer filtered to one process came back empty (#619).

An iOS app that writes with `print()`, `debugPrint()` or `dump()` writes to its
stdout, which unified logging never carries and quern never reads. So an agent
that filters `query_logs` to that app's process gets nothing, reads it as "the
app logged nothing", and debugs from a false premise. When the process is
running on a simulator whose logs quern is streaming, the empty answer says so,
and says what could explain the gap.

Simulators only, on purpose. A simulator's processes are the Mac's own, so
whether one is running is a `ps` away; its app executables sit under that
simulator's `CoreSimulator/Devices/<udid>/`, which ties the process to the
device. A physical iPhone has no such cheap answer, and on Android `print`-style
output reaches logcat anyway, whose `process` is the log tag. Where quern cannot
tell, it says nothing: "could not tell" is not "running".
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Iterable

logger = logging.getLogger(__name__)

#: Bounded, because the note is a courtesy on an answer, never a reason to
#: hold it up. A `ps` that takes longer is a "could not tell".
PS_TIMEOUT_S = 2.0


async def _ps_paths() -> list[str] | None:
    """Every process's executable path, or None when `ps` could not say."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ps", "-axo", "comm=",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except OSError as e:
        logger.debug("ps could not start: %s", e)
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), PS_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    if proc.returncode != 0:
        return None
    return out.decode(errors="replace").splitlines()


async def running_on_simulators(
    process: str, udids: Iterable[str],
    ps: Callable[[], Awaitable[list[str] | None]] | None = None,
) -> list[str] | None:
    """The simulators in `udids` where `process` is running, or None when
    that could not be told. An app's executable is
    `.../CoreSimulator/Devices/<udid>/data/Containers/Bundle/Application/<id>/
    <App>.app/<App>`, and its log lines carry `<App>` as their process."""
    udids = list(udids)
    if not process or not udids:
        return []
    paths = await (ps or _ps_paths)()
    if paths is None:
        return None
    found = []
    for udid in udids:
        marker = f"/CoreSimulator/Devices/{udid}/"
        if any(marker in path and os.path.basename(path.strip()) == process for path in paths):
            found.append(udid)
    return found


def gap_note(process: str, running: list[str], *, level: str | None,
             filtered_out: list[str]) -> str | None:
    """What to say about an empty answer for `process`, running on the
    simulators in `running`. The likeliest explanation first: a log filter
    that drops the process, then the caller's own level, then `print()`."""
    if not running:
        return None
    where = ", ".join(running)
    sims = "simulator" if len(running) == 1 else "simulators"
    if filtered_out:
        return (f"No entries from {process}, which is running on {sims} {where}: "
                f"the log filter set with set_log_filter drops its entries on "
                f"{', '.join(filtered_out)}. Widen or clear that filter to see them.")
    stdout = ("Quern sees what an iOS app logs through unified logging (os.Logger, os_log, "
              "NSLog), not print(), debugPrint() or dump(), which go to its stdout: if the "
              "app uses print(), its output isn't captured. See quern://guide/logs.")
    if level:
        return (f"No entries from {process} at level {level} or above, though it is running "
                f"on {sims} {where}. It may log only below {level}: ask without level. If "
                f"there are still none: {stdout}")
    return f"No entries from {process}, though it is running on {sims} {where}. {stdout}"
