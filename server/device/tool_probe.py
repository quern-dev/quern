"""Bounded liveness probes for the external tools quern shells out to.

Two failures this exists to prevent, both of which reached users.

**A probe that never returns.** `check_tools()` awaited `xcrun simctl help`
with no timeout, and `xcrun simctl help` *hangs* — not errors, hangs — while
Xcode's first-launch tasks run, which is the normal state of a machine for some
minutes after an Xcode upgrade. `/tools` awaits `check_tools()`, so
`quern doctor` did not answer at all (#180).

**A probe that proves nothing.** Several checks asked only whether a path
existed, which cannot distinguish a working install from a corrupt one and
reports the second as healthy (#181, #190). `CONTRIBUTING.md` states the rule:
a failed check must never read as a passing one.

Two things are easy to get wrong here, and both were:

* **Cancelling the wait does not kill the process.** `asyncio.wait_for` on
  `proc.communicate()` raises and moves on while the child keeps running. A
  probe that gives up on a wedged tool and leaks it is how a stray
  `usbmux forward` came to squat a port for a week (#160). The child is killed
  and reaped on every exit path.
* **A probe has to launch the tool the way the runtime launches it.** The
  patched `idb_companion` needs `DYLD_FRAMEWORK_PATH` pointing at the
  frameworks beside it; run bare it dies in dyld, so the obvious
  `--version`-and-check-rc probe reports a *working* install as broken. That
  trades a false pass for a false failure, which is the same defect wearing the
  other face. Callers pass the runtime `env`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

logger = logging.getLogger(__name__)

#: Seconds any single tool probe may take before it is treated as wedged.
#:
#: Measured warm on an M-series Mac, three samples each: adb 0.01s, devicectl
#: 0.05s, simctl 0.09-0.10s, idb 0.10-0.11s, pymobiledevice3 0.22-0.23s. So this
#: is roughly 40x the slowest probe on a healthy machine.
#:
#: Deliberately generous rather than tight. What it bounds is an *indefinite*
#: hang, not a slow answer, so the only property that matters is that it never
#: fires on a machine that works — a probe that gives up early reports a healthy
#: tool as missing, which is the failure this module exists to prevent. Being
#: slow to report a genuinely wedged tool costs nothing by comparison, and
#: `check_tools()` runs its probes concurrently so this is the worst case for
#: the whole set rather than per tool.
TOOL_PROBE_TIMEOUT = 10.0


async def probe_command(
    *argv: str,
    env: dict[str, str] | None = None,
    timeout: float = TOOL_PROBE_TIMEOUT,
    tool: str | None = None,
) -> bool:
    """Run ``argv`` and report whether it answered successfully, within budget.

    Returns True only for a process that exited 0 on its own. A timeout, a
    non-zero exit, and a failure to launch are all False — the caller has one
    bit to report, so this cannot distinguish them, and that limit is the whole
    subject of #181. Until then a timeout is logged at warning rather than
    passing silently as "not installed", because those are very different
    states to be standing in front of.

    ``env`` replaces the child's environment wholesale when given, so callers
    that need a runtime fix-up (``DYLD_FRAMEWORK_PATH``, ``DEVELOPER_DIR``)
    should build it from ``os.environ``.
    """
    name = tool or argv[0]
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode == 0
    except TimeoutError:
        logger.warning(
            "%s did not answer within %.1fs and is being reported as "
            "unavailable; it is installed but not responding, which is a "
            "different problem from a missing tool",
            name, timeout,
        )
        return False
    except (OSError, ValueError):
        # Not installed, not executable, or a bad argv. Ordinary and expected.
        return False
    finally:
        # The wait was cancelled; the child was not. Leaving a wedged probe
        # running is its own bug (#160), so kill and reap it on every path.
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()


async def probe_stdout(
    *argv: str,
    env: dict[str, str] | None = None,
    timeout: float = TOOL_PROBE_TIMEOUT,
    tool: str | None = None,
) -> str | None:
    """Like `probe_command`, but returns the command's stdout when it succeeds.

    None means "could not ask" — timed out, failed to launch, or exited
    non-zero. Distinct from `""`, which means it answered with nothing; the
    caller usually has to treat those differently, and collapsing them is the
    false all-clear this module is about.
    """
    name = tool or argv[0]
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode != 0:
            return None
        return stdout.decode(errors="replace")
    except TimeoutError:
        logger.warning("%s did not answer within %.1fs", name, timeout)
        return None
    except (OSError, ValueError):
        return None
    finally:
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
