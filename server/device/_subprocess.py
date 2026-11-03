"""Waiting on a device tool's subprocess, and not outliving the wait."""

from __future__ import annotations

import asyncio
import contextlib


async def communicate(proc: asyncio.subprocess.Process) -> tuple[bytes, bytes]:
    """`proc.communicate()`, killing and reaping the child if the wait ends
    early: cancelled, or a caller's `wait_for` running out.

    A bare `communicate()` returns the caller and leaves the command running.
    A release's clean-up bounds its erase, uninstall and shutdown with
    `wait_for` and then frees the device -- and the command went on under
    whoever reserved it next (#598). The reap is bounded and best effort: it
    runs while a cancellation is in flight, which it must not hold up.
    """
    try:
        return await proc.communicate()
    except BaseException:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(proc.wait()), 1.0)
        raise
