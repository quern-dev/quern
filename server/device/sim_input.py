"""Whether a simulator's legacy HID services can still be driven, and repair.

Xcode 27 ships a guest HID daemon, `dtuhidd`, which its Device Hub attaches to
every booted simulator. The daemon publishes the Darwin notify state
`com.apple.coredevice.dtuhidd.active = 1`, and backboardd answers by
*disconnecting* the legacy Indigo services -- `ScreenTouchService`,
`MainScreenButtonsService`, `ExternalKeyboardService`. Those three are what
sim-bridge drives, and the guest never gives them back: it re-adds an
already-invalidated service reference, records it as connected, and never
retries.

What that looks like from here: every tap, swipe, keystroke and button press is
accepted and discarded. Reads, screenshots, `open_url` and app launches keep
working, so the device looks healthy and the screen simply never changes.
Keyboard is lost whenever the daemon has attached; touch and buttons depend on
whether backboardd initialised before or after the state flipped, which makes
them a boot-order race -- more likely to be lost on a busy machine.

Two projects found this independently in September 2026, and the repair below
is theirs: tddworks/baguette#78 ("reclaim the input surface from Xcode 27's
Device Hub") and facebook/idb's "Detect a crashed dtuhidd instead of
discarding HID events", which also documents a second way to lose input --
`dtuhidd` aborting during boot -- and puts the handover at CoreSimulator
1155.4.

The real answer is to drive the DTUHID transport, as idb now does. This module
is the stopgap: repair it where that is free (a boot quern performed itself),
and offer the repair everywhere else.
"""

from __future__ import annotations

import asyncio
import logging

from server.models import DeviceError

logger = logging.getLogger("quern-debug-server.sim-input")

#: The guest notify state Device Hub's daemon publishes when it holds the
#: input services.
DTUHID_ACTIVE_KEY = "com.apple.coredevice.dtuhidd.active"

#: Long enough for SpringBoard to come back after backboardd is restarted,
#: measured at about four seconds.
_BACKBOARDD_RESTART_S = 6.0

_SPAWN_TIMEOUT_S = 15.0

#: Returned by `_spawn` when the command had to be killed. Not a plain
#: non-zero: the command may have taken effect before it was.
_TIMED_OUT = -1

#: One repair at a time per simulator. Two overlapping repairs interleave
#: their clear, restart and rollback steps, and can leave the state set by the
#: loser after the winner has reported success.
_REPAIR_LOCKS: dict[str, asyncio.Lock] = {}


async def _spawn(udid: str, *argv: str) -> tuple[int, str]:
    """Run a command inside the simulator.

    Returns (returncode, output) -- stdout when the command worked, stderr when
    it did not. simctl puts the useful part on stderr: a shut-down device
    answers "Unable to spawn: device is not booted", and dropping that reported
    every failure as though Device Hub were holding the services.
    """
    proc = await asyncio.create_subprocess_exec(
        "xcrun", "simctl", "spawn", udid, *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=_SPAWN_TIMEOUT_S,
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        # Distinct from a failure, because a timeout does not say the command
        # did nothing: `notifyutil -s` may have written the state before the
        # wait expired, and a caller that treats that as "no change" leaves
        # the state cleared while the services are still disconnected.
        return _TIMED_OUT, f"timed out after {_SPAWN_TIMEOUT_S:.0f}s"
    returncode = proc.returncode or 0
    if returncode != 0:
        return returncode, stderr.decode(errors="replace").strip()
    return returncode, stdout.decode(errors="replace").strip()


async def legacy_input_is_suppressed(udid: str) -> bool | None:
    """Has the guest handed its legacy input services to `dtuhidd`?

    None means the question could not be asked: a shut-down device exits
    non-zero with nothing on stdout. A caller must not read that as "fine" --
    it is the difference between asking and getting nothing back.

    A runtime that has never heard of the key is *not* in that case: measured,
    `notifyutil -g <unknown key>` exits 0 and prints the key with 0, which is
    indistinguishable from a healthy simulator. That is the right answer for
    an older runtime, which has no Device Hub to worry about, and the wrong
    one for the daemon crashing at boot (idb's case) -- which this cannot see
    at all.
    """
    returncode, output = await _spawn(udid, "notifyutil", "-g", DTUHID_ACTIVE_KEY)
    if returncode != 0 or not output:
        return None
    # `notifyutil -g <key>` prints "<key> <value>".
    fields = output.split()
    if len(fields) < 2 or fields[0] != DTUHID_ACTIVE_KEY:
        return None
    return fields[-1] == "1"


def _repair_lock(udid: str) -> asyncio.Lock:
    lock = _REPAIR_LOCKS.get(udid)
    if lock is None:
        lock = _REPAIR_LOCKS[udid] = asyncio.Lock()
    return lock


async def restore_legacy_input(udid: str) -> None:
    """Hand the legacy input services back to the guest.

    Clearing the state and then restarting backboardd, in that order. The new
    backboardd initialises reading "no Device Hub" and connects every legacy
    service; the daemon re-registers its own alongside, and both work. The
    order is load-bearing: clearing the state *after* the restart hands the new
    backboardd the active-to-inactive edge that causes the disconnect, and
    clearing it alone reconnects nothing.

    **This restarts SpringBoard, so apps running on the simulator are killed.**
    It is safe on a device that has just booted and disruptive on one that is
    in use, which is why nothing here calls it behind a caller's back except
    immediately after a boot quern performed itself.
    """
    async with _repair_lock(udid):
        returncode, stderr = await _spawn(
            udid, "notifyutil", "-s", DTUHID_ACTIVE_KEY, "0",
        )
        if returncode != 0:
            if returncode == _TIMED_OUT:
                # The write may have landed before the kill. Put the state
                # back rather than leave a cleared state over disconnected
                # services, which is the one combination nothing can detect.
                await _spawn(udid, "notifyutil", "-s", DTUHID_ACTIVE_KEY, "1")
            raise DeviceError(
                f"Could not clear {DTUHID_ACTIVE_KEY} on {udid[:8]}: "
                f"{stderr or 'no output'}. Simulator input cannot be restored "
                "while Device Hub holds it.",
                tool="simctl",
            )
        returncode, stderr = await _spawn(
            udid, "launchctl", "kickstart", "-k", "system/com.apple.backboardd",
        )
        if returncode != 0:
            # The state is cleared and the services are still disconnected,
            # which is the combination nothing can detect: every later read
            # says "not suppressed" while no input lands. Put the state back,
            # so the device goes on reporting what is actually true. Best
            # effort -- if this fails too, the error below is still raised.
            await _spawn(udid, "notifyutil", "-s", DTUHID_ACTIVE_KEY, "1")
            raise DeviceError(
                f"Could not restart backboardd on {udid[:8]}: "
                f"{stderr or 'no output'}. Simulator input cannot be restored "
                "while Device Hub holds it.",
                tool="simctl",
            )
        await asyncio.sleep(_BACKBOARDD_RESTART_S)
        logger.info(
            "Restored the legacy input services on %s (SpringBoard was "
            "restarted, so any running app was killed)", udid[:8],
        )


async def device_hub_is_running() -> bool:
    """Is Xcode's Device Hub up? It is what attaches the daemon.

    Asked so that a boot on a machine without it costs nothing: with no Device
    Hub there is nothing to wait for, and waiting anyway would add seconds to
    every boot for a state that never arrives.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "pgrep", "-f", "DeviceHub.app/Contents/MacOS/DeviceHub",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            stdin=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return False
    try:
        return await asyncio.wait_for(proc.wait(), timeout=5.0) == 0
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return False


async def wait_for_device_hub_to_attach(
    udid: str, timeout: float = 8.0, interval: float = 1.0,
) -> bool | None:
    """Wait for the daemon to take the services, and report whether it did.

    Device Hub attaches a few seconds *after* a simulator finishes booting, so
    a repair applied the moment boot returns is undone by an attachment that
    has not happened yet -- measured: the repair ran, the state went back to 1,
    and input was dead. Repairing after the attach holds; the daemon
    re-registers alongside the legacy services and both work.

    The timeout is short because the attachment is prompt -- measured within
    four seconds of boot, twice -- and because waiting is not free: it is
    spent on every boot where Device Hub is running, including the ones where
    the daemon crashed on startup and will never attach at all (idb's case,
    which this cannot detect). A caller that times out is told, rather than
    left to read the silence as health.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    answer: bool | None = None
    while True:
        answer = await legacy_input_is_suppressed(udid)
        if answer:
            return True
        if asyncio.get_running_loop().time() >= deadline:
            return answer
        await asyncio.sleep(interval)


def suppressed_input_warning(udid: str) -> str:
    """What to say when the state says the services were taken.

    A warning rather than a refusal, because the state is not the whole
    answer: measured on 2026-09-18, a simulator booted while Device Hub was
    *not* running kept working after Device Hub was launched, notify state 1
    and all. What decides it is the state when backboardd initialises, which
    nothing can read afterwards. So this says what is probably wrong and how
    to fix it, and lets the caller judge.
    """
    return (
        f"Simulator {udid[:8]} reports {DTUHID_ACTIVE_KEY}=1: Xcode 27's Device "
        "Hub has claimed its touch, button and keyboard services. If taps and "
        "keystrokes are being accepted but nothing on screen changes, that is "
        "why. POST /api/v1/device/ui/restore-input to take them back (this "
        "restarts SpringBoard and kills running apps)."
    )
