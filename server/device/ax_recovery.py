"""Recover a simulator's accessibility bridge after XCUITest poisons it (#66).

Any XCUITest or WDA run against a simulator leaves `CoreSimulatorBridge` with a
stale mach-port cache, after which every foregrounded app reports a single bare
`Application` element. The app under test is not special — Safari breaks too,
and only SpringBoard keeps reading normally. `os_log` names it directly:

    CoreSimulatorBridge [com.apple.Accessibility:AXRuntimeCommon]
        AX Lookup problem - errorCode:1102 error:Unknown service name port

The empty tree looks exactly like every landmark on every screen drifting at
once, which is a long way from the truth and sends people editing knowledge
bases that are fine.

There is no reload path: the port cache belongs to the AX runtime loaded into
the process, the bridge holds no handle to invalidate it, and `SIGHUP` is not
handled. Kill and respawn is the only lever — and it is enough, because the
bridge is launchd-on-demand, so the next query brings it back against a fresh
cache. Measured at ~1s, including with six simulators booted at load average
672, so recovery does not degrade under a loaded pool.

One bridge exists per booted simulator, so this is scoped to the simulator that
needs it and leaves the others undisturbed.
"""

from __future__ import annotations

import asyncio
import logging
import re

logger = logging.getLogger("quern-debug-server.device")

# simctl UDIDs are canonical uppercase UUIDs. Anything else is refused rather
# than matched loosely: an empty string is a substring of every lsof line, so
# `udid in files` would match every bridge and SIGKILL the lot. A short or
# partial identifier has the same shape of problem against a neighbouring
# simulator.
_SIM_UDID = re.compile(r"^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$")


def looks_poisoned(elements: list[dict]) -> bool:
    """Whether a tree carries the signature of a wedged accessibility bridge.

    The signature is narrow on purpose. An app mid-launch legitimately reports
    a single `Application` element, so the zero frame and absent label are what
    separate "nothing has rendered yet" from "the bridge cannot see anything at
    all" — and a false positive here costs a needless kill.
    """
    if len(elements) != 1:
        return False
    el = elements[0]
    if el.get("type") != "Application":
        return False
    frame = el.get("frame") or {}
    if (frame.get("width") or 0) or (frame.get("height") or 0):
        return False
    return not (el.get("AXLabel") or el.get("label") or "")


async def _run(*args: str, timeout: float = 5.0) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        return 1, ""
    return proc.returncode or 0, out.decode(errors="replace")


async def bridge_pids_for(udid: str) -> list[int]:
    """PIDs of the CoreSimulatorBridge serving this simulator.

    `pgrep -x` rather than a `ps | grep`: a loose match also matches the shell
    running the grep, and killing the first hit kills the caller.
    """
    if not _SIM_UDID.match(udid or ""):
        logger.warning(
            "refusing to look for an accessibility bridge for %r: not a canonical "
            "simulator UDID, and a loose match here kills unrelated simulators",
            udid,
        )
        return []

    rc, out = await _run("pgrep", "-x", "CoreSimulatorBridge")
    if rc != 0:
        return []

    pids: list[int] = []
    for line in out.split():
        try:
            pid = int(line)
        except ValueError:
            continue
        # One bridge per simulator; lsof maps it to the UDID whose data
        # directory it holds open. Killing every match would disturb every
        # other booted simulator for no reason.
        _, files = await _run("lsof", "-p", str(pid), timeout=10.0)
        # As a path component, not a bare substring: the UDID appears in the
        # bridge's open data directory, and anchoring on the separators keeps a
        # partial overlap with another simulator's UDID from matching.
        if f"/{udid}/" in files or files.rstrip().endswith(f"/{udid}"):
            pids.append(pid)
    return pids


async def reset_bridge(udid: str) -> bool:
    """Kill this simulator's accessibility bridge. Returns whether one was killed.

    Deliberately does not wait or poll afterwards. The bridge is respawned on
    demand by the next query, and hammering it with retries is worse than
    useless: sim-bridge serialises commands and does not cancel abandoned ones,
    so a retry storm turns into a multi-minute drain that looks like a hang.
    """
    pids = await bridge_pids_for(udid)
    if not pids:
        logger.warning(
            "accessibility bridge looks wedged for %s but no CoreSimulatorBridge "
            "process could be matched to it — leaving it alone", udid[:8],
        )
        return False

    for pid in pids:
        await _run("kill", "-9", str(pid))
    logger.info(
        "reset the accessibility bridge for %s (killed %s) — an XCUITest or WDA "
        "run leaves it with a stale port cache; the next query respawns it",
        udid[:8], ", ".join(str(p) for p in pids),
    )
    return True
