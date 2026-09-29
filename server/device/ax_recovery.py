"""Recover a simulator's accessibility bridge after XCUITest poisons it (#66).

Any XCUITest or WDA run against a simulator leaves `CoreSimulatorBridge` with a
stale mach-port cache, after which affected apps report a single bare
`Application` element. `os_log` names it directly:

    CoreSimulatorBridge [com.apple.Accessibility:AXRuntimeCommon]
        AX Lookup problem - errorCode:1102 error:Unknown service name port

The empty tree looks exactly like every landmark on every screen drifting at
once, which is a long way from the truth and sends people editing knowledge
bases that are fine.

**What gets hit is decided by process lifetime, not by which app it is.** A
process already holding a live accessibility connection when the cache goes
stale keeps reading normally; any process that connects afterwards gets the
poisoned lookup. Measured over eight runs on Xcode 27.0 / iPhone 16 Pro /
iOS 18.6 (22G86):

    Safari foregrounded before the test run    3/3 healthy,  5 elements
    Safari first launched after it             3/3 poisoned, 1 element
    Safari foregrounded before, then           2/2 poisoned, 1 element
      terminated and relaunched after

The third row is the one that settles it: same app, same boot, same wedged
bridge, opposite answers either side of a restart.

This reconciles two readings that looked contradictory. #66 reported the damage
simulator-wide, having sampled Safari after the run; a later check found
another app healthy, having sampled it before. Both measurements were correct
and neither generalises -- they differ in *when* the app they sampled was
launched, which is the variable neither controlled. It also explains #66's note
that SpringBoard alone kept reading: SpringBoard is the one process that never
restarts.

The practical consequence is not that the wedge is common but that **which side
of it you land on is invisible to the caller.** Reads here do not launch
anything -- `controller_ui.py` has no launch call -- so an agent that launches
an app and then reads it gets the poisoned side, while one that attaches to an
app already running, or reads after a person opened it by hand, gets the
healthy one. Same call, same response shape, opposite answers, decided by a
process lifetime nothing in the API reports.

None of this changes what the recovery does: it fires on the signature in the
tree it was given, whatever else is or is not affected.

There is no reload path: the port cache belongs to the AX runtime loaded into
the process, the bridge holds no handle to invalidate it, and `SIGHUP` is not
handled. Kill and respawn is the only lever, and it is enough — but **not
immediately**, which this used to claim. "The next query brings it back against
a fresh cache" is wrong: `reset_bridge` returns when the kill lands, 0.07s
measured, while the replacement process appears at +0.66s and its cache answers
at +0.80s to +1.29s. A re-read issued straight after the kill gets the *same*
poisoned tree, so recovery reported success and delivered nothing. See
`reread_after_recovery`, which watches for both. Recovery does not degrade under
a loaded pool: measured with six simulators booted at load average 672.

One bridge exists per booted simulator, so this is scoped to the simulator that
needs it and leaves the others undisturbed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

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
    # A nested read is *always* one root, so `len != 1` above can never fire on
    # one and the whole decision falls to the frame and label. A root with
    # children has told us something, whatever its own frame says -- and a
    # wedged bridge reports no children at all. Without this the nested paths
    # rest on two attributes of a single element, and a 0x0 unlabelled root
    # over real content would SIGKILL the bridge on a healthy screen
    # (review of #337).
    if el.get("children"):
        return False
    frame = el.get("frame") or {}
    if (frame.get("width") or 0) or (frame.get("height") or 0):
        return False
    return not (el.get("AXLabel") or el.get("label") or "")


async def _reap(proc) -> None:
    """Wait for a killed child, so it does not linger as a zombie.

    Shielded and bounded: this runs while a `CancelledError` is in flight, so
    an unprotected await would be cancelled before the wait completed, and an
    unbounded one would hold up the cancellation it is cleaning up after.
    Best effort by design -- failing to reap is not worth masking the cancel.
    """
    with contextlib.suppress(Exception):
        await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=1.0)


async def _run(*args: str, timeout: float = 5.0) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        return 1, ""
    except asyncio.CancelledError:
        # `reread_after_recovery` runs this under a deadline, so a cancel here
        # is routine rather than exceptional -- and a cancel left the `pgrep`
        # or `lsof` running, once per poll, on the path that fires when a
        # simulator is already unwell (review of #343).
        proc.kill()
        await _reap(proc)
        raise
    return proc.returncode or 0, out.decode(errors="replace")


#: Ceiling on the whole recovery, not a wait anyone expects to spend. Both
#: phases below watch for something real; this only bounds how long they may
#: watch for. Measured on a real wedge: the replacement process appears at
#: +0.66s and reads come good at +0.80s, +1.27s and +1.29s, so this is roughly
#: three times the slowest observed.
_RESPAWN_BUDGET = 4.0
#: Phase 1 polls `bridge_pids_for`, which runs an `lsof` per matching pid,
#: so this is a compromise rather than as-fast-as-possible: the respawn
#: takes ~0.66s, and a quarter-second poll finds it in two or three calls
#: instead of a dozen (review of #337).
_RESPAWN_POLL = 0.25


async def reread_after_recovery(
    udid: str,
    tree: list[dict],
    reread: Callable[[], Awaitable[list[dict]]],
    *,
    budget: float | None = None,
) -> list[dict]:
    """`tree`, or a fresh read taken once a reset bridge is answering again.

    The decision lives here rather than at each read, because there are five of
    them across two backends and a copied condition drifts -- which is how
    `describe_all` ended up the only path with any recovery at all (#337).

    **Re-reading immediately does not work, and used to be what happened.**
    `reset_bridge` returns as soon as the kill lands -- 0.07s measured -- while
    the bridge takes about a second to come back usable. Against a real wedge
    the immediate retry read the *same* poisoned tree and handed it back as the
    answer, so a path that looked covered healed nothing.

    Nothing here sleeps a guessed interval. Two things are watched instead:

    1. **The replacement process.** `bridge_pids_for` shows a new pid at +0.66s
       measured, without anything having read the tree -- launchd brings it
       back on its own rather than waiting to be asked.
    2. **The tree itself**, because the pid is necessary and not sufficient:
       the process is up before its cache answers, and reads came good at
       +0.80s to +1.29s. Only a read proves the recovery, so a read decides.

    `budget` is a ceiling on both phases together, not an expected wait.

    `reread` must not itself recover, or a bridge that stays wedged recurses.
    Every caller passes a read with its own `_recovered=True`.
    """
    if not looks_poisoned(tree):
        return tree

    # Read at call time rather than bound as a default, so the ceiling stays a
    # module constant one place can change.
    budget = _RESPAWN_BUDGET if budget is None else budget

    before = set(await bridge_pids_for(udid))
    if not await reset_bridge(udid):
        return tree

    # One read after the kill happens regardless: the caller's tree was read
    # from the bridge we just killed, so returning it would make the reset
    # unobservable. `budget` bounds the *waiting*, and the wall clock bounds
    # the whole thing -- checking a deadline between awaits does not bound an
    # await that hangs, and both a `bridge_pids_for` (an `lsof` per pid, ten
    # seconds each) and a tree read can outlast the budget on their own
    # (review of #337).
    best = tree
    try:
        async with asyncio.timeout(budget):
            # Phase 1: wait for the replacement, so the first re-read is not
            # spent confirming what `ps` already knows.
            while True:
                if set(await bridge_pids_for(udid)) - before:
                    break
                await asyncio.sleep(_RESPAWN_POLL)

            # Phase 2: the authoritative one.
            while True:
                best = await reread()
                if not looks_poisoned(best):
                    return best
                await asyncio.sleep(_RESPAWN_POLL)
    except TimeoutError:
        pass

    if best is tree:
        # The budget went entirely on watching, so nothing has been read since
        # the kill. One read, unbounded like any other read this backend makes,
        # rather than handing back a tree from a process that no longer exists.
        best = await reread()
    if looks_poisoned(best):
        # Still wedged after a reset and the full budget: the cause is
        # something else, and the poisoned tree is the honest answer.
        logger.warning(
            "accessibility bridge for %s still wedged %.1fs after a reset",
            udid, budget,
        )
    return best


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
