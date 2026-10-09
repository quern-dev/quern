"""WdaBackend — HTTP client for WebDriverAgent on physical iOS devices.

Provides the same interface as IdbBackend (describe_all, tap, swipe, etc.)
but communicates with WDA's HTTP API instead of the idb CLI.

Connection strategy:
- iOS 17+ (tunneld devices): Connect directly via tunnel IPv6 address on port 8100
- iOS 16- (usbmuxd devices): Use pymobiledevice3 usbmux forward to create
  a local port → device:8100 tunnel, then connect to localhost:PORT
"""

from __future__ import annotations

import asyncio
import contextvars
import enum
import logging
import time
from dataclasses import dataclass

import httpx

from server.device.gestures import Plan, check_edge_start, w3c_actions
from server.device.ios.wda_selector import ElementSelector
from server.models import (
    DeviceError,
    InvalidDeviceRequestError,
    WdaAppCrashedError,
    WdaElementNotFoundError,
    WdaElementNotInteractableError,
    WdaError,
    WdaInvalidSessionError,
    WdaKeyboardNotPresentError,
    WdaStaleElementError,
)

logger = logging.getLogger(__name__)

WDA_PORT = 8100
WDA_TIMEOUT = 10.0  # seconds for HTTP requests
# seconds for tap/swipe/type — WDA serializes requests,
# so actions queue behind slow queries
ACTION_TIMEOUT = 25.0
#: XCUIApplication.launch waits for the app to start, and a cold start of a
#: large app on an older phone is measured in seconds, not milliseconds.
LAUNCH_TIMEOUT = 60.0
#: A front-app read is a status check made while waiting on an open; a
#: healthy WDA answers it in about 0.1s (measured on an iPhone 12).
ACTIVE_APP_TIMEOUT = 3.0
# seconds, for everything newer than A13. Measured on an iPhone 15 Pro (A17 Pro,
# iOS 26), WDA built with Xcode 27: 5.15-5.34s across four samples on the home
# screen -- a *denser* tree than the iPhone 11's, 714KB against 448KB, in half
# the time -- and 4.37-4.58s across eight on a 150KB screen.
#
# So the 5.0 this replaces did not fail outright on a modern device; it
# straddled one. Which screen you were on decided whether you got a tree, and
# `element_count: 0` on the busiest screens was the normal outcome. Confirmed by
# asking for the old budget explicitly on the dense screen and getting an empty
# tree, then 641 elements at this one.
#
# Note what those two ranges say about the cost: 4.75x less markup bought 15%
# less time. /source is dominated by a fixed serialization cost on the device,
# so tree size moves it only at the margin -- and transport not at all. The same
# device answered in 4.37s mean over a USB forward and 4.51s over Wi-Fi,
# interleaved against one screen, which is noise.
SOURCE_TIMEOUT = 10.0
# seconds, for A13 and older devices.
#
# The budget is not a latency knob. A timeout here is treated as evidence that
# WDA is hung, and the response is to restart the driver, which reinstalls the
# runner -- so a value set too low does not produce a slow call, it produces an
# empty tree with no error, on every call, permanently. See #170; the rest of
# that cascade is still open and this only stops it firing on ordinary hardware.
#
# Both values have now been wrong twice, and the second time is the interesting
# one. 0.18.0 raised them to 5/10 from a measured 7.52s on an iPhone 11 (iOS
# 26.6.2) on its home screen. Rebuilding WDA under Xcode 27 -- same device, same
# screen, same quern -- moved that to 10.19-10.35s across three samples, so the
# 10.0 floor was under water again within a day. The *toolchain* changed, not
# the device or the tree.
#
# Doubled rather than nudged past the new measurement, because a margin sized to
# the last observation is what produced this situation twice. The real fix is to
# derive the budget from the first successful /source per device and delete
# these constants, which is the open half of #170.
SOURCE_TIMEOUT_SLOW = 20.0

#: How many times to ask `/status` after a `/source` timeout. Not what
#: decides hung-versus-busy -- `WdaLiveness` does that -- but enough that a
#: runner recovering from a restart, which can refuse one ping and answer
#: the next, is not judged on a single sample.
SOURCE_TIMEOUT_PING_ATTEMPTS = 4

#: Seconds between those pings. A constant so a test can drive the retry
#: without paying for it.
SOURCE_TIMEOUT_PING_GAP = 1.0

class WdaLiveness(enum.Enum):
    """What a liveness probe established about a runner.

    Three states rather than a boolean, because the restart decision needs
    to tell "did not answer" from "is not there". WDA serialises, so a
    `/source` big enough to outlast the probe window queues every `/status`
    behind it -- a runner that answers nothing can be perfectly healthy.

    Returned rather than stored. An earlier version kept the evidence in a
    `dict[udid, bool]` on the client and review found three faults in it at
    once: an early return skipped the per-call reset so a later probe
    inherited an older one's verdict, nothing ever cleared it -- not
    `close()`, not `_drop_connection`, not `_restart_wda` -- and the name
    said "last ping" while the value meant "any of four". None of those are
    possible for a value that is computed and handed back.
    """

    #: Answered 200. Healthy.
    ALIVE = "alive"
    #: Accepted the connection but did not finish in time. Busy, not dead --
    #: and busy is what a long tree read looks like from outside.
    BUSY = "busy"
    #: Nothing usable is listening: refused, reset, never completed a
    #: handshake, or answering something other than 200 every time.
    GONE = "gone"


#: `(udid, seconds)` for the tree read on this task, or None if it succeeded.
#:
#: Per-task rather than per-device: two callers can read one device at once,
#: and a device-keyed record belongs to whichever finished last. See
#: `WdaBackend._note_source_timeout`.
_LAST_SOURCE_READ: contextvars.ContextVar[tuple[str, float] | None] = (
    contextvars.ContextVar("quern_last_source_read", default=None)
)
# WDA default is 50 — 25 resolves most screens;
# skeleton fallback handles dense maps
SNAPSHOT_MAX_DEPTH = 25

#: The depth reads made on a caller's behalf use -- tap_element, get_element,
#: wait_for_element -- unless they ask for more. Measured on an iPhone 11 on a
#: 200-row table: 12 reads in 3.9s and holds the tab bar, the controls and
#: every row's identifier; past it the read walks each cell's insides, 1,055
#: elements in 34.5s, and the next tap times out (F35). 11 loses the rows and
#: 10 the tab bar. Deeper nesting is the caller's to ask for.
ACTION_SNAPSHOT_DEPTH = 12
FORWARD_START_PORT = 18100  # base port for usbmux forwards
FORWARD_KILL_GRACE = 3  # seconds to wait for SIGTERM before SIGKILL
IDLE_TIMEOUT = 15 * 60  # 15 minutes
IDLE_CHECK_INTERVAL = 60  # check every 60 seconds


async def _kill_forward(proc: asyncio.subprocess.Process | None) -> None:
    """Stop a usbmux forward subprocess, escalating to SIGKILL.

    SIGTERM alone is not enough and cannot be relied on: measured on eleven
    leaked forwards, every one survived `kill` and needed `kill -9` (#296).
    So a bare `terminate()` is a cleanup that reports success and leaves the
    process running -- this repo's recurring shape, in a teardown path.

    One definition, used by both the failure path in `_start_usbmux_forward`
    and by `close()`, because the two drifted once already: the second had
    the escalation and the first did not.
    """
    if proc is None or proc.returncode is not None:
        return
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=FORWARD_KILL_GRACE)
        return
    except TimeoutError:
        pass
    except BaseException:
        # Cancelled while waiting out the grace period. SIGTERM has been
        # sent and these children ignore it, so leaving now strands exactly
        # the orphan this function exists to prevent. Escalate first, then
        # let the cancellation through. Reachable on a second Ctrl-C, or
        # from a TaskGroup cancelled while a sibling's cleanup runs.
        proc.kill()
        raise
    proc.kill()
    # Reap it, so the child does not sit as a zombie for the server's life.
    # Bounded: a process that ignores SIGKILL is not ours to fix, and
    # blocking teardown on it would be worse than the leak.
    try:
        await asyncio.wait_for(proc.wait(), timeout=FORWARD_KILL_GRACE)
    except TimeoutError:
        logger.warning("usbmux forward %s survived SIGKILL", proc.pid)

# Class chain queries for the skeleton fallback (when /source times out).
# These use XCTest's native lazy query API and bypass WDA's snapshot mechanism,
# making them safe on screens with 300+ map pins where /source hangs.
_SKELETON_CONTAINER_TYPES = [
    "**/XCUIElementTypeTabBar",
    "**/XCUIElementTypeNavigationBar",
    "**/XCUIElementTypeToolbar",
    "**/XCUIElementTypeAlert",
    "**/XCUIElementTypeSheet",
]
SKELETON_QUERY_TIMEOUT = 8.0  # seconds — busy map: container ~1.6s + children ~4.7s
_ELEMENT_RESPONSE_ATTRIBUTES = "type,label,name,rect,enabled,value"

# iPhone models with A13 chip or older (slower WDA /source).
# A14+ (iPhone 12 and later) are fast enough for the default timeout.
_SLOW_DEVICE_PREFIXES = (
    "iPhone 11", "iPhone XS", "iPhone XR", "iPhone X ",  # trailing space to avoid "XS"/"XR"
    "iPhone SE",  # SE 1st/2nd gen both have A13 or older
    "iPhone 8", "iPhone 7", "iPhone 6",
    "iPad Air 2", "iPad Air (3", "iPad Air (4",  # A14 is iPad Air 4th gen, but borderline
    "iPad mini", "iPad (", "iPad Pro (9", "iPad Pro (10", "iPad Pro (11",
    "iPod",
)


def _is_slow_device(name: str) -> bool:
    """Check if a device name corresponds to an A13 or older chip."""
    # iPhone X (no suffix) needs special handling — "iPhone X" without S/R
    if name == "iPhone X":
        return True
    return name.startswith(_SLOW_DEVICE_PREFIXES)


@dataclass
class _WdaConnection:
    """Cached connection info for a device."""

    base_url: str
    # For usbmux-forwarded connections, track the subprocess so we can kill it
    forward_proc: asyncio.subprocess.Process | None = None
    local_port: int | None = None
    session_id: str | None = None


# W3C error code → WdaError subclass mapping
_W3C_ERROR_MAP: dict[str, type[WdaError]] = {
    "invalid session id": WdaInvalidSessionError,
    "no such element": WdaElementNotFoundError,
    "stale element reference": WdaStaleElementError,
    "element not interactable": WdaElementNotInteractableError,
}


def _parse_wda_error(resp: httpx.Response, udid: str) -> WdaError | None:
    """Inspect an httpx.Response and return a WdaError subclass, or None for 200."""
    if resp.status_code == 200:
        return None

    try:
        body = resp.json()
    except Exception:
        return WdaError(
            f"WDA request failed on {udid[:8]} (HTTP {resp.status_code}): {resp.text[:200]}",
        )

    value = body.get("value", {})
    if isinstance(value, str):
        # Some WDA responses have value as a plain string
        return WdaError(
            f"WDA error on {udid[:8]}: {value[:200]}",
            wda_error="unknown",
            wda_message=value,
        )

    wda_error = value.get("error", "")
    wda_message = value.get("message", "")
    error_lower = wda_error.lower()
    message_lower = wda_message.lower()

    # Direct mapping for known W3C error codes
    for code, cls in _W3C_ERROR_MAP.items():
        if code in error_lower:
            return cls(
                f"WDA error on {udid[:8]}: {wda_message[:200]}",
                wda_error=wda_error,
                wda_message=wda_message,
            )

    # Special cases for "invalid element state"
    if "invalid element state" in error_lower:
        if "keyboard" in message_lower:
            return WdaKeyboardNotPresentError(
                f"WDA error on {udid[:8]}: {wda_message[:200]}",
                wda_error=wda_error,
                wda_message=wda_message,
            )
        return WdaElementNotInteractableError(
            f"WDA error on {udid[:8]}: {wda_message[:200]}",
            wda_error=wda_error,
            wda_message=wda_message,
        )

    # Crash detection
    if "unknown error" in error_lower and "crash" in message_lower:
        return WdaAppCrashedError(
            f"WDA error on {udid[:8]}: {wda_message[:200]}",
            wda_error=wda_error,
            wda_message=wda_message,
        )

    # Anything else non-200
    return WdaError(
        f"WDA error on {udid[:8]} ({wda_error}): {wda_message[:200]}",
        wda_error=wda_error,
        wda_message=wda_message,
    )


class WdaBackend:
    """Speaks WDA's HTTP API for UI automation on physical iOS devices."""

    #: What this backend calls itself in an error. The dispatcher reads it
    #: off whichever backend it selected, so an error can no longer name a
    #: tool that was never involved (#186).
    TOOL_NAME = "wda"

    #: A WDA swipe returns once the app is idle, so a read straight after it
    #: is at rest. Measured on an iPhone 11, including at the end of a list.
    swipe_returns_at_rest = True

    #: W3C pointer actions carry one touch source per finger (#252).
    multitouch = True

    #: A swipe from the screen edge is the system's on a real device (#251).
    edge_swipes = True

    def __init__(self) -> None:
        self._connections: dict[str, _WdaConnection] = {}
        #: Simulators whose UI this backend serves, by the port their WDA
        #: listens on (#336). A simulator shares the Mac's network, so it is
        #: reached on 127.0.0.1 -- none of the tunnel, usbmux or auto-start
        #: machinery below applies.
        self._simulator_ports: dict[str, int] = {}
        self._next_port = FORWARD_START_PORT
        # os_version cache for auto-start — populated by controller
        self._device_os_versions: dict[str, str] = {}
        # device name cache for timeout tuning — populated by controller
        self._device_names: dict[str, str] = {}
        # Idle timeout tracking
        self._last_interaction: dict[str, float] = {}
        self._idle_task: asyncio.Task | None = None
        # Track active snapshotMaxDepth per device to avoid redundant POSTs
        self._current_depth: dict[str, int] = {}
        # Per-device lock for session creation (prevents parallel _ensure_session races)
        self._session_locks: dict[str, asyncio.Lock] = {}
        #: Seconds the last `/source` ran before timing out, per device, or
        #: absent once a read succeeds. Read by the summary so a fallback is
        #: reported rather than passed off as the screen -- see
        #: `_note_source_timeout`.

    def _source_timeout(self, udid: str) -> float:
        """Return the /source timeout for a device, extended for slower chips.

        Was a doubling of the default; it is now set from measurement instead,
        because the observed value on an A13 exceeded the doubled one.
        """
        name = self._device_names.get(udid, "")
        if name and _is_slow_device(name):
            return SOURCE_TIMEOUT_SLOW
        return SOURCE_TIMEOUT

    async def _drop_connection(
        self, udid: str, expected: _WdaConnection | None = None,
    ) -> _WdaConnection | None:
        """Forget a device's connection, killing its forward if it had one.

        `expected` guards against dropping a *newer* connection than the one
        the caller was using. Two requests can be in flight on one device:
        if A fails and reconnects, and B then fails on the old URL, B's drop
        would otherwise remove A's fresh connection and kill the forward A
        is about to use -- turning what used to be a leak into a failed
        request. Pass the connection you actually used, and the drop becomes
        a no-op once it has been replaced.

        **Returns the connection that superseded yours, or None.** That is
        not a convenience: a caller that no-ops and then carries on to build
        its own connection *overwrites* the replacement, orphaning its
        forward -- the same leak this method exists to prevent, one level up.
        Returning the winner makes "someone else already reconnected" a value
        the caller has to handle rather than a case it has to remember.

        **Every** site that drops a `_WdaConnection` goes through here. A
        connection is the only record of its forward -- `close()` reaps what
        is in `self._connections` and nothing else -- so a bare
        `self._connections.pop()` orphans the subprocess immediately and
        permanently.

        That was not a hypothetical: four sites popped without killing, and
        the one that fires on an ordinary transport error (`_request`'s
        reconnect) then calls `_get_base_url`, which takes the next port.
        Measured in the wild: eleven forwards on the unbroken run
        18100-18110, none with a live connection. See #296.

        Kept as one method rather than a rule to remember, because the rule
        was already not being remembered.
        """
        current = self._connections.get(udid)
        if expected is not None and current is not expected:
            # Already replaced by a newer connection; not ours to drop. Hand
            # the caller the winner so it uses that rather than replacing it.
            return current
        conn = self._connections.pop(udid, None)
        self._last_interaction.pop(udid, None)
        if conn is None:
            return None
        try:
            await _kill_forward(conn.forward_proc)
        except Exception:
            logger.warning(
                "Could not stop the usbmux forward for %s", udid[:8],
                exc_info=True,
            )
        return None

    async def close(self) -> None:
        """Shutdown: cancel idle task, delete sessions, kill port-forwards."""
        # Cancel idle timeout task
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
            try:
                await self._idle_task
            except asyncio.CancelledError:
                pass
            self._idle_task = None

        # Delete active sessions
        for udid, conn in list(self._connections.items()):
            if conn.session_id:
                try:
                    await self.delete_session(udid)
                except Exception:
                    pass

        # Kill port-forward subprocesses. Guarded per connection: one that
        # refuses to die must not strand the rest, nor skip the clear() below.
        for udid, conn in list(self._connections.items()):
            try:
                await _kill_forward(conn.forward_proc)
            except Exception:
                logger.warning(
                    "Could not stop the usbmux forward for %s", udid[:8],
                    exc_info=True,
                )
        self._connections.clear()
        self._last_interaction.clear()
        self._current_depth.clear()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def register_simulator(self, udid: str, port: int) -> None:
        """Serve this simulator through WDA on `port` until unregistered."""
        self._simulator_ports[udid] = port
        self._connections.pop(udid, None)

    def unregister_simulator(self, udid: str) -> None:
        self._simulator_ports.pop(udid, None)
        self._connections.pop(udid, None)
        # Per-session state that a later session must not inherit: a stale
        # depth would skip the depth POST on the new session.
        for attr in ("_current_depth", "_last_interaction"):
            store = getattr(self, attr, None)
            if isinstance(store, dict):
                store.pop(udid, None)

    def serves_simulator(self, udid: str) -> bool:
        return udid in self._simulator_ports

    def _wda_mode_hint(self, udid: str) -> str:
        """What a failure means for a simulator in WDA mode, and the way out.

        Its runner can die on its own -- the simulator shut down or rebooted,
        xcodebuild exited -- and nothing unregisters it, so every read then
        fails. Not unregistered here: switching it back to sim-bridge silently
        would change the vocabulary under a caller writing XCUITest selectors.
        So the error says what happened and names both ways forward.
        """
        port = self._simulator_ports.get(udid)
        if port is None:
            return ""
        return (
            f". This simulator is in WDA mode and its WDA (port {port}) is not "
            "answering -- start_driver to restart it, or stop_driver to return "
            "the simulator to the default backend"
        )

    async def _get_base_url(self, udid: str) -> str:
        """Get (or create) the WDA base URL for a device.

        iOS 17+: tries the tunneld IPv6 address directly.
        iOS 16-: starts a usbmux port-forward subprocess.

        If WDA is not reachable and os_version is known, auto-starts the driver.
        A simulator in WDA mode is reached on its own port and never
        auto-started: its runner is started and stopped deliberately, and a
        silent restart would hide that it had died.
        """
        port = self._simulator_ports.get(udid)
        if port is not None:
            # A real connection entry, as for a phone: the session and snapshot
            # depth live on it, and several methods read it directly. Returning
            # the URL alone left those reading a key that was never written --
            # found by the first live read, which raised KeyError.
            base_url = f"http://127.0.0.1:{port}"
            conn = self._connections.get(udid)
            if conn is None or conn.base_url != base_url:
                self._connections[udid] = _WdaConnection(base_url=base_url)
            return base_url
        if udid in self._connections:
            conn = self._connections[udid]

            if conn.forward_proc is not None:
                # usbmux forward — check if process is still alive
                if conn.forward_proc.returncode is None:
                    return conn.base_url
                # Forward proc died — remove and reconnect
                winner = await self._drop_connection(udid, expected=conn)
                if winner is not None:
                    # Someone reconnected while we were checking. Use theirs;
                    # building our own would overwrite it and orphan its
                    # forward. Both branches, so this does not depend on
                    # which of them happens to await today.
                    return winner.base_url
            else:
                # tunneld connection — verify WDA is still reachable
                try:
                    async with httpx.AsyncClient() as client:
                        resp = await client.get(
                            f"{conn.base_url}/status", timeout=2.0,
                        )
                        if resp.status_code == 200:
                            return conn.base_url
                except Exception:
                    pass
                logger.info(
                    "Cached WDA tunnel stale for %s, reconnecting...", udid[:8],
                )
                winner = await self._drop_connection(udid, expected=conn)
                if winner is not None:
                    # Someone reconnected while we were checking. Use theirs;
                    # building our own would overwrite it and orphan its
                    # forward. Both branches, so this does not depend on
                    # which of them happens to await today.
                    return winner.base_url

        # Try tunneld first (iOS 17+)
        base_url = await self._try_tunneld_connection(udid)
        if base_url:
            self._connections[udid] = _WdaConnection(base_url=base_url)
            return base_url

        # Try usbmux forward (iOS 16-)
        try:
            base_url, proc, port = await self._start_usbmux_forward(udid)
            self._connections[udid] = _WdaConnection(
                base_url=base_url, forward_proc=proc, local_port=port,
            )
            return base_url
        except DeviceError:
            pass

        # WDA not reachable — try auto-start if we know the os_version
        os_version = self._device_os_versions.get(udid)
        if not os_version:
            raise DeviceError(
                f"WDA not reachable on {udid[:8]} and os_version unknown — "
                "cannot auto-start. Ensure WDA is running on the device.",
                tool="wda",
            )

        logger.info("WDA not reachable on %s, auto-starting driver...", udid[:8])
        from server.device.ios.wda import start_driver

        result = await start_driver(udid, os_version)
        if not result.get("ready"):
            raise DeviceError(
                f"Auto-started WDA driver on {udid[:8]} but it did not become responsive. "
                f"Check log: ~/.quern/wda/runner-{udid[:8]}.log",
                tool="wda",
            )

        # Retry connection after auto-start
        base_url = await self._try_tunneld_connection(udid)
        if base_url:
            self._connections[udid] = _WdaConnection(base_url=base_url)
            return base_url

        # Try usbmux again
        base_url, proc, port = await self._start_usbmux_forward(udid)
        self._connections[udid] = _WdaConnection(
            base_url=base_url, forward_proc=proc, local_port=port,
        )
        return base_url

    async def _try_tunneld_connection(self, udid: str) -> str | None:
        """Try to connect to WDA via the tunneld tunnel address."""
        from server.device.ios.tunneld import get_tunneld_devices, resolve_tunnel_udid

        tunnel_udid = await resolve_tunnel_udid(udid)
        if not tunnel_udid:
            return None

        devices = await get_tunneld_devices()
        tunnels = devices.get(tunnel_udid, [])
        if not tunnels:
            return None

        tunnel_addr = tunnels[0].get("tunnel-address")
        if not tunnel_addr:
            return None

        # IPv6 addresses need brackets in URLs
        base_url = f"http://[{tunnel_addr}]:{WDA_PORT}"

        # Verify WDA is reachable
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{base_url}/status", timeout=3.0)
                if resp.status_code == 200:
                    logger.info("WDA reachable via tunnel at %s", base_url)
                    return base_url
        except Exception:
            logger.debug("WDA not reachable via tunnel address %s", base_url)

        return None

    async def _start_usbmux_forward(
        self, udid: str,
    ) -> tuple[str, asyncio.subprocess.Process, int]:
        """Start a pymobiledevice3 usbmux forward for a pre-iOS 17 device."""
        from server.device.ios.tunneld import find_pymobiledevice3_binary

        binary = find_pymobiledevice3_binary()
        if not binary:
            raise DeviceError(
                "pymobiledevice3 not found — needed for USB port forwarding to WDA. "
                "Install: pipx install pymobiledevice3",
                tool="wda",
            )

        local_port = self._next_port
        self._next_port += 1

        proc = await asyncio.create_subprocess_exec(
            str(binary), "usbmux", "forward",
            str(local_port), str(WDA_PORT),
            "--udid", udid,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # From here to the `return`, every exit that is not a success has to
        # kill `proc`. Nothing else will: `close()` reaps the forwards in
        # `self._connections`, and this one is not in there until the caller
        # records it, which only happens if we return.
        #
        # The try starts on the line after the spawn deliberately. It used to
        # start below the sleep, which left a 0.5s window on *every* forward
        # start where a cancellation orphaned the child -- and uvicorn cancels
        # the request task when a client disconnects, so it was reachable
        # rather than theoretical.
        #
        # It also used to catch httpx errors alone, while the non-200 raise
        # sat inside the same try. DeviceError is not an httpx error, so it
        # travelled straight past the cleanup: a device whose WDA answered
        # 500 leaked a forward on every attempt, and the caller swallows the
        # error and retries on the next port, so it also incremented.
        # Measured: eleven orphans on 18100-18110, oldest 6d23h, none with a
        # live connection. See #296.
        try:
            # Give the forward a moment to establish
            await asyncio.sleep(0.5)

            if proc.returncode is not None:
                stderr = (await proc.stderr.read()).decode() if proc.stderr else ""
                raise DeviceError(
                    f"usbmux forward failed for {udid[:8]}: {stderr.strip()}",
                    tool="wda",
                )

            base_url = f"http://localhost:{local_port}"
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{base_url}/status", timeout=3.0)
            if resp.status_code != 200:
                raise DeviceError(
                    f"WDA not responding on {udid[:8]} (status {resp.status_code}). "
                    "Ensure WDA is running on the device.",
                    tool="wda",
                )
        except httpx.HTTPError as exc:
            # The base class, not the three subclasses that had been seen:
            # a ProxyError or a ProtocolError is just as fatal here, and
            # naming them one at a time is how this list got short.
            await _kill_forward(proc)
            raise DeviceError(
                f"Cannot connect to WDA on {udid[:8]} ({type(exc).__name__}). "
                "Ensure WDA is running: launch WebDriverAgentRunner on the device.",
                tool="wda",
            ) from exc
        except BaseException:
            # The non-200 DeviceError above, and anything else including
            # cancellation. Re-raised unchanged; this clause exists only so
            # that no path leaves the subprocess behind.
            await _kill_forward(proc)
            raise

        logger.info(
            "WDA reachable via usbmux forward at %s (device %s)",
            base_url, udid[:8],
        )
        return base_url, proc, local_port

    # ------------------------------------------------------------------
    # UI automation methods (matching IdbBackend interface)
    # ------------------------------------------------------------------

    async def _ensure_session(self, udid: str) -> str:
        """Create or return a cached WDA session for this device."""
        # Fast path: session already exists (no lock needed)
        conn = self._connections.get(udid)
        if conn and conn.session_id:
            return conn.session_id

        # Serialize session creation per device to prevent parallel races
        # (e.g. build_screen_skeleton fires 5 concurrent find_elements_by_query)
        if udid not in self._session_locks:
            self._session_locks[udid] = asyncio.Lock()
        async with self._session_locks[udid]:
            # Re-check after acquiring lock (another coroutine may have created it)
            conn = self._connections.get(udid)
            if conn and conn.session_id:
                return conn.session_id

            base_url = await self._get_base_url(udid)
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.post(
                        f"{base_url}/session",
                        json={"capabilities": {}},
                        timeout=WDA_TIMEOUT,
                    )
            except httpx.HTTPError as exc:  # base class: a ProxyError is as fatal (#296)
                raise DeviceError(
                    f"WDA session creation failed on {udid[:8]} ({type(exc).__name__})"
                    + self._wda_mode_hint(udid),
                    tool="wda",
                )

            error = _parse_wda_error(resp, udid)
            if error is not None:
                raise DeviceError(
                    f"WDA session creation failed (status {resp.status_code})"
                    + self._wda_mode_hint(udid),
                    tool="wda",
                )

            session_id = resp.json().get("sessionId", "")
            if not session_id:
                session_id = resp.json().get("value", {}).get("sessionId", "")

            conn = self._connections.get(udid)
            if conn:
                conn.session_id = session_id
            logger.info("WDA session created for %s: %s", udid[:8], session_id[:8])

            # Configure WDA settings for better performance on complex screens.
            # snapshotMaxDepth prevents the accessibility tree walk from going
            # 50 levels deep (the WDA default), which deadlocks WDA on MapKit
            # screens with hundreds of annotations.
            # shouldUseCompactResponses=False + elementResponseAttributes ensures
            # element query responses include rect, name, value, enabled — not just
            # type and label.
            try:
                async with httpx.AsyncClient() as client:
                    await client.post(
                        f"{base_url}/session/{session_id}/appium/settings",
                        json={"settings": {
                            "snapshotMaxDepth": SNAPSHOT_MAX_DEPTH,
                            "shouldUseCompactResponses": False,
                            "elementResponseAttributes": _ELEMENT_RESPONSE_ATTRIBUTES,
                        }},
                        timeout=WDA_TIMEOUT,
                    )
                self._current_depth[udid] = SNAPSHOT_MAX_DEPTH
            except Exception:
                logger.debug("Failed to configure WDA settings for %s", udid[:8])

            return session_id

    async def _set_snapshot_depth(self, udid: str, depth: int) -> None:
        """Update WDA snapshotMaxDepth if it differs from the current value."""
        if self._current_depth.get(udid) == depth:
            return

        session_id = await self._ensure_session(udid)
        base_url = self._connections[udid].base_url
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{base_url}/session/{session_id}/appium/settings",
                    json={"settings": {"snapshotMaxDepth": depth}},
                    timeout=WDA_TIMEOUT,
                )
            self._current_depth[udid] = depth
            logger.info("WDA snapshotMaxDepth set to %d for %s", depth, udid[:8])
        except Exception:
            logger.debug("Failed to set snapshotMaxDepth=%d for %s", depth, udid[:8])

    async def delete_session(self, udid: str) -> None:
        """Delete the active WDA session for a device. No-op if no session."""
        conn = self._connections.get(udid)
        if not conn or not conn.session_id:
            return

        session_id = conn.session_id
        try:
            async with httpx.AsyncClient() as client:
                await client.delete(
                    f"{conn.base_url}/session/{session_id}",
                    timeout=WDA_TIMEOUT,
                )
        except Exception:
            logger.debug("Failed to delete WDA session %s on %s", session_id[:8], udid[:8])

        conn.session_id = None
        self._current_depth.pop(udid, None)
        logger.info("WDA session deleted for %s", udid[:8])

    def _ensure_idle_task(self) -> None:
        """Start the idle checker background task if not already running."""
        if self._idle_task is None or self._idle_task.done():
            self._idle_task = asyncio.create_task(self._idle_checker())

    async def _idle_checker(self) -> None:
        """Background task: clean up idle sessions.

        Deletes the WDA session and clears cached connections, but leaves
        the xcodebuild process running so the next interaction can reconnect
        without a costly reinstall.
        """
        try:
            while True:
                await asyncio.sleep(IDLE_CHECK_INTERVAL)
                now = time.monotonic()
                idle_udids = [
                    udid for udid, last in self._last_interaction.items()
                    if now - last > IDLE_TIMEOUT
                ]
                for udid in idle_udids:
                    logger.info(
                        "WDA idle timeout for %s — deleting session "
                        "(driver stays running)", udid[:8],
                    )
                    try:
                        await self.delete_session(udid)
                    except Exception:
                        pass
                    await self._drop_connection(udid)
        except asyncio.CancelledError:
            return

    async def _request(
        self, method: str, udid: str, path: str,
        use_session: bool = False, timeout: float | None = None,
        raise_on_timeout: bool = False, raise_if_maybe_delivered: bool = False,
        _is_retry: bool = False,
        _is_connection_retry: bool = False,
        **kwargs,
    ) -> httpx.Response:
        """Make an HTTP request to WDA, converting transport errors to DeviceError.

        If use_session=True, prepends /session/{sessionId} to the path.
        If raise_on_timeout=True, re-raises httpx.TimeoutException directly
        instead of wrapping it in DeviceError (so callers can handle timeouts).
        If raise_if_maybe_delivered=True, any transport error that may have
        come after WDA had the request -- everything but a refused connection
        -- is re-raised as the httpx exception and never re-sent: for a write
        whose second answer would differ from its first (CodeRabbit on #393).

        On WdaInvalidSessionError with use_session=True, automatically clears
        the stale session, creates a new one, and retries once.

        On ConnectError/ReadError, automatically clears the stale connection,
        reconnects via _get_base_url(), and retries once.
        """
        if use_session:
            session_id = await self._ensure_session(udid)
            base_url = self._connections[udid].base_url
            url = f"{base_url}/session/{session_id}{path}"
        else:
            base_url = await self._get_base_url(udid)
            url = f"{base_url}{path}"
        # Captured before the request so a failure drops *this* connection
        # and not a newer one another request has since established.
        conn_used = self._connections.get(udid)
        try:
            async with httpx.AsyncClient() as client:
                resp = await getattr(client, method)(
                    url, timeout=timeout or WDA_TIMEOUT, **kwargs,
                )
        except httpx.HTTPError as exc:  # base class: a ProxyError is as fatal (#296)
            if raise_on_timeout and isinstance(exc, httpx.TimeoutException):
                # Caller wants to handle timeouts — don't invalidate connection
                # (WDA may still be alive, just slow on this request)
                raise
            if raise_if_maybe_delivered and not isinstance(exc, httpx.ConnectError):
                if not isinstance(exc, httpx.TimeoutException):
                    # The connection itself failed; a slow one is kept.
                    await self._drop_connection(udid, expected=conn_used)
                raise
            # Connection lost — invalidate cached connection, and kill
            # its forward: this fires on ordinary transport errors and
            # the reconnect below takes the next port (#296).
            await self._drop_connection(udid, expected=conn_used)

            # Cleanup widened to every HTTPError; the *retry* deliberately
            # did not. RemoteProtocolError ("server disconnected without
            # sending a response") and DecodingError arrive *after* WDA has
            # the request, so re-sending is re-executing: type_text types
            # twice, a tap taps twice. tap/swipe/type/press all reach here
            # with raise_on_timeout=False. The pre-existing set is already
            # ambiguous for writes (#74); this must not add to it.
            retryable = isinstance(
                exc, (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException),
            )
            if retryable and not _is_connection_retry:
                self._current_depth.pop(udid, None)
                logger.info(
                    "WDA transport error on %s (%s), reconnecting",
                    udid[:8], type(exc).__name__,
                )
                return await self._request(
                    method, udid, path, use_session=use_session,
                    timeout=timeout, raise_on_timeout=raise_on_timeout,
                    raise_if_maybe_delivered=raise_if_maybe_delivered,
                    _is_retry=_is_retry,
                    _is_connection_retry=True,
                    **kwargs,
                )

            if not retryable:
                # Not retried on purpose: this error arrived after WDA had
                # the request, so re-sending could re-execute it (#74).
                raise DeviceError(
                    f"WDA connection failed on {udid[:8]} "
                    f"({type(exc).__name__}). The request may already have "
                    "run on the device, so it was not retried. Ensure WDA is "
                    "running and re-issue it yourself if it is safe to repeat.",
                    tool="wda",
                ) from exc
            raise DeviceError(
                f"WDA connection failed on {udid[:8]} ({type(exc).__name__}) "
                "after reconnect attempt. Ensure WDA is running on the device.",
                tool="wda",
            ) from exc

        # Track interaction for idle timeout
        self._last_interaction[udid] = time.monotonic()
        self._ensure_idle_task()

        error = _parse_wda_error(resp, udid)
        if error is not None:
            # Session recovery: if the session went stale, clear it and retry once
            if (
                isinstance(error, WdaInvalidSessionError)
                and use_session
                and not _is_retry
            ):
                conn = self._connections.get(udid)
                if conn:
                    conn.session_id = None
                self._current_depth.pop(udid, None)
                logger.info("WDA session invalid on %s, recovering", udid[:8])
                return await self._request(
                    method, udid, path, use_session=True,
                    timeout=timeout, raise_on_timeout=raise_on_timeout,
                    raise_if_maybe_delivered=raise_if_maybe_delivered,
                    _is_retry=True,
                    _is_connection_retry=_is_connection_retry,
                    **kwargs,
                )
            raise error

        return resp

    async def _write(
        self, udid: str, path: str, *, action: str, json: dict,
        timeout: float = 0.0,
    ) -> httpx.Response:
        """POST a write that must not run twice, and say so if it may have.

        `_request` re-sends a request once after a timeout or a read error, on
        a fresh connection -- right for a read, wrong for a write. Both errors
        can arrive *after* WDA has the request, so re-sending runs it again: a
        tap lands twice, text is typed twice, a second Home press opens the app
        switcher (#407; #74 is the same ambiguity on sim-bridge). Only a
        refused connection, which never reached WDA, is still retried.

        Anything else is a `DeviceError` saying the action may already have
        been performed. Re-issuing it is the caller's call, made after looking
        at the screen, because only the caller can tell whether a second one is
        safe.
        """
        try:
            return await self._request(
                "post", udid, path, use_session=True,
                timeout=timeout or ACTION_TIMEOUT,
                raise_if_maybe_delivered=True, json=json,
            )
        except httpx.HTTPError as exc:
            raise DeviceError(
                f"WDA did not answer the {action} on {udid[:8]} ({type(exc).__name__}). "
                "It may already have been performed, so it was not sent again; check "
                "the screen.",
                tool="wda",
            ) from exc

    def _note_source_timeout(self, udid: str, seconds: float) -> None:
        """Record that *this* tree read timed out and fell back.

        The fallback returns a container skeleton, which is frequently empty
        -- and an empty result is exactly what a genuinely blank screen
        returns. `element_count: 0` with no error is the reason #170 took a
        long time to diagnose: every symptom said the device was fine and
        the screen was empty, when the read had simply not finished.

        Scoped to the read rather than the device. It began as a
        `dict[udid, seconds]`, which is wrong under concurrency and was
        caught in review: two callers can read one device at once, and the
        device-wide entry belongs to whichever finished last. A timed-out
        read whose neighbour then succeeded returned a fallback with no
        `degraded` at all, and a successful read could be labelled degraded
        by its neighbour's failure. Both are worse than the bug this field
        exists to report, because they are wrong rather than merely silent.

        A `ContextVar` is the right scope: asyncio copies the context per
        task, so each request carries its own answer and no lock is needed.
        The udid travels with it so a value set for one device cannot be
        read back for another.
        """
        _LAST_SOURCE_READ.set((udid, seconds))

    def _clear_source_timeout(self, udid: str) -> None:
        """Record that this read succeeded, so nothing reports it degraded."""
        _LAST_SOURCE_READ.set(None)

    def source_timed_out(self, udid: str) -> float | None:
        """Seconds *this caller's* tree read burned before falling back."""
        seen = _LAST_SOURCE_READ.get()
        if seen is None:
            return None
        seen_udid, seconds = seen
        return seconds if seen_udid == udid else None

    async def probe_wda(
        self, udid: str, *, attempts: int = 1, timeout: float = 2.0,
        gap: float | None = None,
    ) -> WdaLiveness:
        """What `/status` says about this runner.

        One 2s ping by default, which is the right question for "is this
        thing alive at all".

        `attempts` exists for the caller that has just had a *different*
        endpoint time out. A runner part-way through building a large
        accessibility tree can miss a 2s ping while being perfectly healthy
        -- `/source` on an iPhone 11 measures 10.19-10.35s under Xcode 27 --
        and the old single ping declared that runner hung. The cost of being
        wrong is not a retry: `_restart_wda` reinstalls the runner through
        `xcodebuild`, so a slow read destroyed the device's automation
        rather than degrading it (#170).

        Asking more than once is not what distinguishes busy from dead --
        review established that no window can, because WDA serialises and a
        long enough tree queues every ping behind it. The verdict does that.
        The extra attempts buy something narrower: a runner recovering from
        a restart can refuse one ping and answer the next, and one ping
        would have called that gone.

        Cost: nothing when the runner answers, since the first attempt does
        not sleep. About nine seconds otherwise -- and that is now paid on
        every timed-out read that will *not* restart, which is the common
        case, so it is a real cost rather than a prelude to a reinstall.
        """
        conn = self._connections.get(udid)
        if not conn:
            try:
                base_url = await self._get_base_url(udid)
            except DeviceError:
                # Not even addressable. The strongest evidence available that
                # there is nothing to talk to -- and the old code returned a
                # bare False here, which the caller could not tell from "did
                # not answer in time".
                return WdaLiveness.GONE
        else:
            base_url = conn.base_url

        pause = SOURCE_TIMEOUT_PING_GAP if gap is None else gap
        saw_busy = False
        for attempt in range(max(1, attempts)):
            if attempt:
                await asyncio.sleep(pause)
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(f"{base_url}/status", timeout=timeout)
                    if resp.status_code == 200:
                        return WdaLiveness.ALIVE
                    # Answering, but not with anything usable. Persistent
                    # non-200 is a state this file has met before: a runner
                    # returning 500 leaked eleven port-forwards (#296), and
                    # the two other reachability probes here both require a
                    # 200. A single bad status is not enough, which is why
                    # this does not set `saw_busy` and does not return.
            except httpx.ConnectTimeout:
                # A handshake that never completed. Nothing accepted the
                # connection, which is this criterion's own definition of
                # gone, even though httpx files it under TimeoutException.
                pass
            except httpx.TimeoutException:
                # Read/write/pool: something accepted the connection and is
                # taking its time. That is exactly what a runner serialising
                # a large /source behind this ping looks like.
                saw_busy = True
            except httpx.TransportError:
                # Refused, reset, protocol error: nothing usable is there.
                pass
            except Exception:  # noqa: BLE001
                # An unknown failure is not evidence of death, and the cost
                # of being wrong here is a reinstall.
                saw_busy = True

        # Any evidence that something accepted a connection outweighs the
        # rest. A runner that refuses one ping mid-restart and then answers
        # slowly is recovering, not gone -- restarting it there is the bug
        # this whole change exists to remove.
        return WdaLiveness.BUSY if saw_busy else WdaLiveness.GONE

    async def _is_wda_responsive(
        self, udid: str, *, attempts: int = 1, timeout: float = 2.0,
        gap: float | None = None,
    ) -> bool:
        """Whether WDA answered `/status`. See `probe_wda` for the detail."""
        probe = await self.probe_wda(
            udid, attempts=attempts, timeout=timeout, gap=gap,
        )
        return probe is WdaLiveness.ALIVE

    async def _restart_wda(self, udid: str) -> None:
        """Stop and restart the WDA driver for a device, clearing cached connection."""
        from server.device.ios.wda import start_driver, stop_driver

        # Clear cached connection
        await self._drop_connection(udid)

        os_version = self._device_os_versions.get(udid)
        if not os_version:
            logger.warning("Cannot restart WDA on %s — os_version unknown", udid[:8])
            return

        try:
            await stop_driver(udid)
        except Exception:
            logger.debug("stop_driver failed for %s (may already be dead)", udid[:8])

        result = await start_driver(udid, os_version)
        if not result.get("ready"):
            logger.warning(
                "WDA restart on %s: driver started but not responsive", udid[:8],
            )

    async def find_elements_by_query(
        self, udid: str, using: str, value: str,
        *, scope_element_id: str | None = None, timeout: float | None = None,
    ) -> list[dict]:
        """Query WDA for elements using a locator strategy.

        Wraps POST /session/{id}/elements (or /element/{id}/elements for scoped).
        Supports: 'class chain', 'class name', 'accessibility id', 'predicate string'.

        Returns idb-format dicts with _wda_element_id preserved for scoped child queries.
        Timeout/non-200 returns [] — graceful degradation.
        """
        if scope_element_id:
            path = f"/element/{scope_element_id}/elements"
        else:
            path = "/elements"

        try:
            resp = await self._request(
                "post", udid, path, use_session=True,
                timeout=timeout or SKELETON_QUERY_TIMEOUT,
                json={"using": using, "value": value},
            )
        except (DeviceError, WdaError):
            logger.debug("Element query failed (%s=%s) on %s", using, value, udid[:8])
            return []

        elements = resp.json().get("value", [])
        results: list[dict] = []
        for el in elements:
            # Determine type: prefer element's own 'type' field
            # (available with compact responses off)
            raw_type = el.get("type", "") or value
            # Class chain values like "**/XCUIElementTypeTabBar" — extract just the type
            el_type = raw_type.rsplit("/", 1)[-1] if "/" in raw_type else raw_type
            mapped = _map_wda_element_from_query(el, el_type)
            if mapped:
                # If found via 'accessibility id', the identifier IS the query value.
                # WDA often echoes the class name in 'name' instead of the real
                # accessibilityIdentifier, causing the mapper to discard it.
                if using == "accessibility id" and not mapped.get("AXUniqueId"):
                    mapped["AXUniqueId"] = value
                # Preserve WDA element UUID for scoped child queries
                wda_id = (el.get("ELEMENT")
                          or el.get("element-6066-11e4-a52e-4f735466cecf"))
                if wda_id:
                    mapped["_wda_element_id"] = wda_id
                results.append(mapped)

        return results

    async def build_screen_skeleton(self, udid: str) -> list[dict]:
        """Build a lightweight screen description using class chain queries.

        Two-phase approach:
        1. Query all container types (TabBar, NavBar, Toolbar, Alert, Sheet) in parallel
        2. Query descendant buttons scoped to each container type sequentially

        Returns flat idb-format list. Gracefully handles missing containers
        (Alert/Sheet usually absent). Strips _wda_element_id before returning.
        """
        start = time.perf_counter()

        # Phase 1: find containers in parallel
        container_tasks = [
            self.find_elements_by_query(udid, "class chain", chain)
            for chain in _SKELETON_CONTAINER_TYPES
        ]
        container_results = await asyncio.gather(*container_tasks, return_exceptions=True)

        # Collect containers with their WDA element IDs
        containers: list[dict] = []
        for result in container_results:
            if isinstance(result, Exception):
                continue
            for el in result:
                if el.get("_wda_element_id"):
                    containers.append(el)

        # Phase 2: find children using unscoped class chain queries.
        # Must bump snapshotMaxDepth — at depth=10, TabBar buttons are invisible
        # because some apps nest them inside Other wrappers.
        # Run sequentially: WDA serializes queries internally, so parallel
        # queries share the same timeout budget and the second one times out.
        await self._set_snapshot_depth(udid, 50)
        try:
            container_types = list(dict.fromkeys(
                c["type"].replace("XCUIElementType", "")
                for c in containers if c.get("type")
            ))
            child_results: list[list[dict] | Exception] = []
            for c_type in container_types:
                try:
                    result = await self.find_elements_by_query(
                        udid, "class chain",
                        f"**/XCUIElementType{c_type}/**/XCUIElementTypeButton",
                    )
                    child_results.append(result)
                except Exception as exc:
                    child_results.append(exc)
        finally:
            await self._set_snapshot_depth(udid, SNAPSHOT_MAX_DEPTH)

        # Dedupe children by WDA element ID (multiple type queries may return same element)
        seen_ids: set[str] = set()
        all_children: list[dict] = []
        for result in child_results:
            if isinstance(result, Exception):
                continue
            for child in result:
                wda_id = child.get("_wda_element_id", "")
                if wda_id and wda_id in seen_ids:
                    continue
                if wda_id:
                    seen_ids.add(wda_id)
                all_children.append(child)

        # Build flat result list: containers + their children
        flat: list[dict] = []
        for container in containers:
            c = {k: v for k, v in container.items() if k != "_wda_element_id"}
            flat.append(c)

        for child in all_children:
            c = {k: v for k, v in child.items() if k != "_wda_element_id"}
            flat.append(c)

        elapsed = (time.perf_counter() - start) * 1000
        logger.info(
            "[PERF] wda.build_screen_skeleton: %d elements (%d containers) in %.1fms (device %s)",
            len(flat), len(containers), elapsed, udid[:8],
        )
        return flat

    async def describe_all(
        self, udid: str, *,
        snapshot_depth: int | None = None,
        source_timeout: float | None = None,
        # Accepted for interface parity with SimBridgeBackend and IdbBackend,
        # and ignored: XCUITest's /source enumerates container children, so there is
        # nothing to probe and nothing for the caller to switch off.
        probe: bool = True,
    ) -> list[dict]:
        """Get all UI elements as flat dicts in idb format.

        Fetches WDA's /source?format=json, flattens the nested tree,
        and converts field names to match idb's describe-all output.

        If /source times out (common on complex screens like MapKit),
        falls back to targeted element queries by class name.

        Args:
            snapshot_depth: WDA accessibility tree depth (1-50). If provided
                and different from current, updates WDA settings before fetching.
        """
        # Always ensure depth is set — covers stale sessions where the
        # settings POST in _ensure_session failed silently.
        target_depth = snapshot_depth if snapshot_depth is not None else SNAPSHOT_MAX_DEPTH
        await self._set_snapshot_depth(udid, target_depth)

        effective_timeout = (
            source_timeout if source_timeout is not None
            else self._source_timeout(udid)
        )
        start = time.perf_counter()
        try:
            resp = await self._request(
                "get", udid, "/source", params={"format": "json"},
                timeout=effective_timeout, raise_on_timeout=True,
            )
        except httpx.TimeoutException:
            elapsed = (time.perf_counter() - start) * 1000
            logger.warning(
                "[PERF] wda /source timed out after %.0fms on %s — falling back to element queries",
                elapsed, udid[:8],
            )

            # A slow /source is not evidence of a hung runner, and the
            # recovery is expensive enough that guessing wrong is worse than
            # the fault: `_restart_wda` reinstalls through xcodebuild. So the
            # runner gets several chances to answer, spread over a window
            # wider than the read that just timed out -- it may still be
            # finishing that very tree.
            self._note_source_timeout(udid, elapsed / 1000)
            # GONE, not merely un-ALIVE. WDA serialises, so a tree that
            # outlasts the probe window queues every ping behind it -- and a
            # rule that restarts once a clock runs out reinstalls a healthy
            # runner no matter how long the clock is.
            liveness = await self.probe_wda(
                udid, attempts=SOURCE_TIMEOUT_PING_ATTEMPTS,
            )
            if liveness is WdaLiveness.GONE:
                logger.warning("WDA hung on %s, restarting driver...", udid[:8])
                await self._restart_wda(udid)
            else:
                logger.warning(
                    "wda /source timed out on %s but the runner is %s, not "
                    "gone; leaving it alone and falling back to element "
                    "queries",
                    udid[:8], liveness.value,
                )

            return await self.build_screen_skeleton(udid)

        self._clear_source_timeout(udid)
        data = resp.json()
        # WDA returns {"value": {...tree...}, "sessionId": ...}
        tree = data.get("value", data)

        flat = flatten_wda_tree(tree)
        elapsed = (time.perf_counter() - start) * 1000
        logger.info(
            "[PERF] wda.describe_all: %d elements in %.1fms (device %s)",
            len(flat), elapsed, udid[:8],
        )
        return flat

    async def describe_all_nested(
        self, udid: str, *,
        snapshot_depth: int | None = None,
        source_timeout: float | None = None,
    ) -> list[dict]:
        """Get UI elements with hierarchy preserved, in idb-compatible format.

        Falls back to flat element queries if /source times out.

        Args:
            snapshot_depth: WDA accessibility tree depth (1-50). If provided
                and different from current, updates WDA settings before fetching.
            source_timeout: Override /source timeout in seconds (default: auto per device).
        """
        target_depth = snapshot_depth if snapshot_depth is not None else SNAPSHOT_MAX_DEPTH
        await self._set_snapshot_depth(udid, target_depth)

        effective_timeout = (
            source_timeout if source_timeout is not None
            else self._source_timeout(udid)
        )
        start = time.perf_counter()
        try:
            resp = await self._request(
                "get", udid, "/source", params={"format": "json"},
                timeout=effective_timeout, raise_on_timeout=True,
            )
        except httpx.TimeoutException:
            elapsed = (time.perf_counter() - start) * 1000
            logger.warning(
                "[PERF] wda /source timed out after %.0fms on %s "
                "(nested) — falling back to element queries",
                elapsed, udid[:8],
            )

            # Identical reasoning to `describe_all` above, and this route had
            # none of it until a review asked which call site the fix forgot.
            # `get_ui_tree(children_of=...)` reaches only here, so a nested
            # read on a slow-but-healthy runner reinstalled it exactly as the
            # flat read used to.
            self._note_source_timeout(udid, elapsed / 1000)
            # Same criterion as the flat read above.
            nested_liveness = await self.probe_wda(
                udid, attempts=SOURCE_TIMEOUT_PING_ATTEMPTS,
            )
            if nested_liveness is WdaLiveness.GONE:
                logger.warning("WDA hung on %s, restarting driver...", udid[:8])
                await self._restart_wda(udid)
            else:
                logger.warning(
                    "wda /source timed out on %s (nested) but the runner is "
                    "%s, not gone; leaving it alone",
                    udid[:8], nested_liveness.value,
                )

            # Fallback returns flat list — no hierarchy, but better than an error
            return await self.build_screen_skeleton(udid)

        self._clear_source_timeout(udid)
        data = resp.json()
        tree = data.get("value", data)

        # Convert to idb-format but keep children nested
        return convert_wda_tree_nested(tree)

    async def describe_point(self, udid: str, x: float, y: float) -> dict | None:
        """Get the UI element at specific coordinates.

        WDA has no direct describe-point. We fetch the full tree and find
        the deepest element whose frame contains (x, y).
        """
        try:
            elements = await self.describe_all(udid)
        except DeviceError:
            return None

        return find_element_at_point(elements, x, y)

    async def tap(self, udid: str, x: float, y: float, hold: float | None = None) -> None:
        """Tap at coordinates via WDA; with `hold`, a long press (#251).

        A long press is XCUITest's press-for-duration, and is never re-sent: one
        that WDA may already have performed would open a context menu twice,
        or open it and then select from it (#407).
        """
        if hold is None:
            await self._write(udid, "/wda/tap", action="tap", json={"x": x, "y": y})
            return
        await self._write(udid, "/wda/touchAndHold", action="long press",
                          json={"x": x, "y": y, "duration": float(hold)},
                          timeout=ACTION_TIMEOUT + float(hold))

    async def swipe(
        self,
        udid: str,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
        duration: float = 0.5,
        hold: float = 0.0,
        edge: str | None = None,
    ) -> None:
        """Swipe gesture via WDA.

        `hold` is accepted for parity with sim-bridge and ignored. WDA's drag is
        XCUITest's press-then-drag, not a flick: measured on an iPhone 11, a
        358pt drag moved the list 349pt, and nothing was moving once the call
        returned, including at the end of a list.

        An `edge` swipe needs nothing more than its start at that edge: the
        device decides from there that it is the system's. Measured on an
        iPhone 12: a drag from x=1 popped a navigation stack (#251).
        """
        if edge is not None:
            width, height = await self._window_size(udid)
            check_edge_start(edge, start_x, start_y, width, height, tool="wda")
        await self._write(udid, "/wda/dragfromtoforduration", action="swipe", json={
            "fromX": start_x,
            "fromY": start_y,
            "toX": end_x,
            "toY": end_y,
            "duration": duration,
        })

    async def perform_gesture(self, udid: str, plan: Plan) -> None:
        """A gesture `server.device.gestures` laid out, as W3C pointer actions.

        One touch source per finger, synthesised by XCUITest as one event
        record, so the fingers move in step (#252). Points off the screen are
        refused, as sim-bridge refuses them, rather than left to XCUITest.

        Never re-sent: a pinch WDA may already have performed would be
        performed twice, and a second rotation turns twice as far (#74, #407).
        """
        width, height = await self._window_size(udid)
        points = [p for path in plan.paths or [] for p in path] + list(plan.points or [])
        for x, y in points:
            if not (0 <= x <= width and 0 <= y <= height):
                raise InvalidDeviceRequestError(
                    f"point ({x:.0f}, {y:.0f}) is off the {width:.0f}x{height:.0f} screen",
                    tool="wda")
        await self._write(udid, "/actions", action=plan.kind,
                          json={"actions": w3c_actions(plan)},
                          # The device's own time on top of the usual
                          # allowance, so a long gesture is not cut off.
                          timeout=ACTION_TIMEOUT + plan.seconds)

    async def _window_size(self, udid: str) -> tuple[float, float]:
        """The screen in points, as WDA's coordinates are."""
        resp = await self._request("get", udid, "/window/size", use_session=True,
                                   timeout=ACTION_TIMEOUT)
        try:
            value = resp.json()["value"]
            return float(value["width"]), float(value["height"])
        except (ValueError, KeyError, TypeError) as exc:
            raise DeviceError(f"WDA window/size on {udid[:8]} answered something unreadable",
                              tool="wda") from exc

    async def type_text(self, udid: str, text: str) -> None:
        """Type text via WDA. Never re-sent: it would type the text twice."""
        await self._write(udid, "/wda/keys", action="typing", json={"value": list(text)})

    async def press_button(self, udid: str, button: str) -> None:
        """Press a hardware button via WDA. Never re-sent: a second Home press
        opens the app switcher."""
        await self._write(udid, "/wda/pressButton", action=f"{button} button press",
                          json={"name": button})

    async def app_state(self, udid: str, bundle_id: str) -> int:
        """XCUIApplication's state: 1 not running, 2 suspended, 3 running in
        the background, 4 in the foreground, 0 unknown."""
        resp = await self._request("post", udid, "/wda/apps/state",
                                   use_session=True, timeout=ACTION_TIMEOUT,
                                   json={"bundleId": bundle_id})
        try:
            value = resp.json().get("value")
        except (ValueError, AttributeError) as exc:
            raise DeviceError(f"WDA apps/state on {udid[:8]} answered something unreadable",
                              tool="wda") from exc
        if not isinstance(value, int):
            raise DeviceError(f"WDA apps/state on {udid[:8]} answered {value!r}", tool="wda")
        return value

    async def launch_app(
        self, udid: str, bundle_id: str, environment: dict[str, str],
    ) -> None:
        """Start an app with `environment`, through XCUIApplication.

        Measured on an iPhone 12: testmanagerd's launch request carried the
        variables (`_XCT_launchApplicationWithBundleID:...environment:`).
        WDA applies them only to an app that is not running -- a running one
        is just activated, keeping the environment it started with -- so the
        caller terminates it first when the variables must apply.

        Never re-sent once WDA may have it -- a timeout, or a connection lost
        while the answer was coming back -- since a launch WDA may already
        have made would be made twice (#74, #407). Only the timeout was
        covered before; a lost connection re-sent the launch.
        """
        try:
            await self._request("post", udid, "/wda/apps/launch",
                                use_session=True, timeout=LAUNCH_TIMEOUT,
                                raise_if_maybe_delivered=True,
                                json={"bundleId": bundle_id, "environment": environment})
        except httpx.TimeoutException as exc:
            raise DeviceError(
                f"WDA did not finish launching {bundle_id} on {udid[:8]} within "
                f"{LAUNCH_TIMEOUT:.0f}s. It may still be starting, so it was not "
                "launched again; check the screen.",
                tool="wda",
            ) from exc
        except httpx.HTTPError as exc:
            raise DeviceError(
                f"WDA did not answer launching {bundle_id} on {udid[:8]} "
                f"({type(exc).__name__}). It may already have launched, so it was "
                "not launched again; check the screen.",
                tool="wda",
            ) from exc

    async def activate_app(self, udid: str, bundle_id: str) -> None:
        """Activate (bring to foreground) an app via WDA."""
        await self._request("post", udid, "/wda/apps/activate",
                            use_session=True, timeout=ACTION_TIMEOUT,
                            json={"bundleId": bundle_id})

    async def open_url(self, udid: str, url: str) -> None:
        """Open a URL the way another app on the device would.

        No `bundleId`, deliberately. Without one WDA asks the system to open
        the URL with its default handler, which for an https link is the
        universal-link routing -- the app's associated domains checked against
        the domain's apple-app-site-association. With one it hands the URL to
        that app directly, skipping the check that deep-link testing exists to
        exercise: measured on an iPhone 12 (iOS 26.5), a `/dl/` link the app
        handles only as a universal link reached it that way and did nothing,
        as did `devicectl --payload-url`, while the same link without the
        bundle id navigated. Needs iOS 16.4; WDA falls back to Siri before it.
        """
        try:
            # Never re-sent once WDA may have it: opening the URL twice is a
            # different test (#74). A lost connection re-sent it until #407;
            # only the timeout was covered.
            await self._request("post", udid, "/url",
                                use_session=True, timeout=ACTION_TIMEOUT,
                                raise_if_maybe_delivered=True, json={"url": url})
        except httpx.TimeoutException as exc:
            raise DeviceError(
                f"WDA did not answer opening {url} on {udid[:8]} within "
                f"{ACTION_TIMEOUT:.0f}s. It may have opened anyway, so it was not "
                "re-sent; check the screen before opening it again.",
                tool="wda",
            ) from exc
        except httpx.HTTPError as exc:
            raise DeviceError(
                f"WDA did not answer opening {url} on {udid[:8]} "
                f"({type(exc).__name__}). It may have opened anyway, so it was not "
                "re-sent; check the screen before opening it again.",
                tool="wda",
            ) from exc

    async def active_app(self, udid: str) -> str | None:
        """The bundle id of the application in front, or None when WDA
        answered without naming one. Raises DeviceError when it could not be
        asked -- "could not ask" is not "nothing is in front".

        Bounded short, and a timeout is not treated as a lost connection: it
        is a status read made while waiting on something else, and must
        neither hold that up nor tear down the connection it depends on."""
        try:
            resp = await self._request("get", udid, "/wda/activeAppInfo",
                                       timeout=ACTIVE_APP_TIMEOUT, raise_on_timeout=True)
        except httpx.TimeoutException as exc:
            raise DeviceError(
                f"WDA activeAppInfo on {udid[:8]} did not answer within "
                f"{ACTIVE_APP_TIMEOUT:.0f}s", tool="wda") from exc
        try:
            value = resp.json().get("value")
        except (ValueError, AttributeError) as exc:
            raise DeviceError(
                f"WDA activeAppInfo on {udid[:8]} answered something unreadable",
                tool="wda",
            ) from exc
        bundle = value.get("bundleId") if isinstance(value, dict) else None
        return bundle if isinstance(bundle, str) and bundle else None

    async def terminate_app(self, udid: str, bundle_id: str) -> bool | None:
        """Terminate an app via WDA. Returns whether it was running and was
        terminated -- WDA answers true only then -- or None when that cannot
        be told: the answer was not a boolean, or the request timed out.

        Never re-sent once WDA may have it -- a timeout, or a connection lost
        while the answer was coming back. WDA may already have terminated the
        app, and a second request would find it stopped and answer false,
        which reads as "it was never running" (CodeRabbit on #393). The app's
        state is read instead, which is safe to repeat: stopped (1) is a
        success whose answer was lost; anything else -- still running, or
        WDA's "unknown" (0) -- is a failure, since a stop cannot be confirmed."""
        try:
            resp = await self._request("post", udid, "/wda/apps/terminate",
                                       use_session=True, raise_if_maybe_delivered=True,
                                       json={"bundleId": bundle_id})
        except httpx.HTTPError as exc:
            state = await self.app_state(udid, bundle_id)
            if state != 1:
                raise DeviceError(
                    f"WDA did not answer terminating {bundle_id} on {udid[:8]} "
                    f"({type(exc).__name__}), and it cannot be confirmed stopped "
                    f"(XCUIApplication state {state})",
                    tool="wda",
                ) from exc
            return None
        try:
            value = resp.json().get("value")
        except (ValueError, AttributeError):
            return None
        return value if isinstance(value, bool) else None

    def element(
        self,
        udid: str,
        *,
        name: str | None = None,
        label: str | None = None,
        type: str | None = None,
        predicate: str | None = None,
        class_chain: str | None = None,
    ) -> ElementSelector:
        """Create a chainable element selector for this device.

        Returns a lazy query builder — no WDA call is made until a terminal
        operation (find, get, wait, tap, clear) is awaited.

        Args:
            udid: Device UDID.
            name: Accessibility identifier (uses 'accessibility id' strategy).
            label: Display label (case-insensitive predicate match).
            type: Element type without XCUIElementType prefix (e.g. "Button").
            predicate: Raw NSPredicate string (advanced).
            class_chain: Raw class chain expression (advanced).
        """
        return ElementSelector(
            self, udid,
            name=name, label=label, type=type,
            predicate=predicate, class_chain=class_chain,
        )

    async def element_attribute(
        self, udid: str, name: str, *, identifier: str | None, label: str | None,
        center: tuple[float, float],
    ):
        """One XCUITest attribute of the element at `center`, or None if unknown.

        The element is found again by identifier (or label), and the candidate
        whose frame is centred at `center` is the one asked about, because a
        query can return several -- a label is often repeated. None means the
        question could not be put (no selector, no candidate there, WDA error);
        the caller must not read that as any particular answer.
        """
        if identifier:
            using, value = "accessibility id", identifier
        elif label:
            escaped = label.replace("\\", "\\\\").replace("'", "\\'")
            using, value = "predicate string", f"label == '{escaped}'"
        else:
            return None
        candidates = await self.find_elements_by_query(udid, using, value)
        cx, cy = center

        def _distance(el: dict) -> float:
            f = el.get("frame") or {}
            if not f:
                return float("inf")
            return abs(f["x"] + f["width"] / 2 - cx) + abs(f["y"] + f["height"] / 2 - cy)

        best = min(candidates, key=_distance, default=None)
        if best is None or _distance(best) > 2.0 or not best.get("_wda_element_id"):
            return None
        try:
            resp = await self._request(
                "get", udid, f"/element/{best['_wda_element_id']}/attribute/{name}",
                use_session=True, timeout=SKELETON_QUERY_TIMEOUT,
            )
        except (DeviceError, WdaError):
            return None
        return resp.json().get("value")

    async def is_hittable(
        self, udid: str, *, identifier: str | None, label: str | None,
        center: tuple[float, float],
    ) -> bool | None:
        """XCUITest's `isHittable` for the element at `center`, or None if unknown."""
        value = await self.element_attribute(
            udid, "hittable", identifier=identifier, label=label, center=center,
        )
        if isinstance(value, str):
            value = value.strip().lower() in ("true", "1")
        return value if isinstance(value, bool) else None

    async def element_value(
        self, udid: str, *, identifier: str | None, label: str | None,
        center: tuple[float, float],
    ) -> str | None:
        """The element's `value` as a string, or None if it could not be read.

        Needed because WDA's element query does not return values -- measured:
        a switch came back with type, rect, label and enabled, and no value --
        so a value-aware tap that trusted the query always saw "unknown" and
        toggled (F36).
        """
        value = await self.element_attribute(
            udid, "value", identifier=identifier, label=label, center=center,
        )
        if value is None:
            return None
        if isinstance(value, bool):
            return "1" if value else "0"
        return str(value)

    async def select_all_and_delete(
        self, udid: str, x: float, y: float,
        element_type: str | None = None,
        identifier: str | None = None,
    ) -> None:
        """Clear the text field at (x, y) via WDA's native element clear.

        The field is found by identifier when it has one, otherwise as the
        text field whose frame is centred at (x, y). It used to be the *first*
        element of the field's class, with the coordinates unused, so clearing
        `field_email` emptied `field_default` -- reproduced under WDA on a
        simulator and on an iPhone 11 (F33). Falls back to triple-tap +
        backspace at (x, y) when no element can be pinned down.
        """
        # Map our normalized type names to XCUIElementType class names
        class_map = {
            "SearchField": "XCUIElementTypeSearchField",
            "TextField": "XCUIElementTypeTextField",
            "SecureTextField": "XCUIElementTypeSecureTextField",
            "TextArea": "XCUIElementTypeTextView",
            "TextView": "XCUIElementTypeTextView",
        }

        def _centred_on_point(candidates: list[dict]) -> str | None:
            for el in candidates:
                f = el.get("frame") or {}
                if f and abs(f["x"] + f["width"] / 2 - x) <= 2 and \
                        abs(f["y"] + f["height"] / 2 - y) <= 2:
                    return el.get("_wda_element_id")
            return None

        element_id = None
        if identifier:
            element_id = _centred_on_point(
                await self.find_elements_by_query(udid, "accessibility id", identifier),
            )
        if element_id is None:
            class_names = []
            if element_type and element_type in class_map:
                class_names.append(class_map[element_type])
            class_names.extend(v for v in class_map.values() if v not in class_names)
            for class_name in class_names:
                element_id = _centred_on_point(
                    await self.find_elements_by_query(udid, "class name", class_name),
                )
                if element_id:
                    break

        if element_id:
            try:
                await self._request("post", udid, f"/element/{element_id}/clear",
                                    use_session=True)
                return
            except WdaError:
                pass

        # Fallback: triple-tap + backspace (works on simulators via idb)
        for _ in range(3):
            await self.tap(udid, x, y)
        await asyncio.sleep(0.15)
        await self._write(udid, "/wda/keys", action="backspace", json={"value": ["\b"]})


# ------------------------------------------------------------------
# WDA tree → idb format conversion (pure functions, testable)
# ------------------------------------------------------------------


_XCUI_PREFIX = "XCUIElementType"


def _xcui_type(name: str | None) -> str | None:
    """XCUITest's full type name, whichever form WDA sent (#336).

    WDA's JSON ``/source`` -- the main read path -- sends the *short* name
    (``Button``, ``TabBar``); only element queries send the class name
    (``XCUIElementTypeButton``). Measured against a running WDA: not one type
    in a JSON source carried the prefix. So the old "strip it for idb compat"
    was a no-op on the path that matters, and keeping the original would have
    kept the short name. Normalised here to the class name an XCUITest
    selector's documentation uses.
    """
    if not name:
        return None
    return name if name.startswith(_XCUI_PREFIX) else _XCUI_PREFIX + name


def _map_wda_element(wda: dict) -> dict:
    """Convert a single WDA element dict to idb-compatible format.

    WDA keys: type, rawIdentifier, name, value, label, rect, isEnabled,
              elementType, role
    idb keys: type, AXUniqueId, AXLabel, AXValue, frame, enabled,
              role, role_description
    """
    # WDA rect is {x, y, width, height} — same layout as idb frame
    rect = wda.get("rect", {})
    frame = None
    if rect and all(k in rect for k in ("x", "y", "width", "height")):
        frame = {
            "x": rect["x"],
            "y": rect["y"],
            "width": rect["width"],
            "height": rect["height"],
        }

    raw_type = wda.get("type", "")
    wda_type = raw_type
    # Stripped for idb compat when present -- JSON /source does not send it.
    if wda_type.startswith(_XCUI_PREFIX):
        wda_type = wda_type[len(_XCUI_PREFIX):]

    return {
        "type": wda_type,
        "xcui_type": _xcui_type(raw_type),
        "AXUniqueId": wda.get("rawIdentifier") or wda.get("name") or "",
        "AXLabel": wda.get("label") or "",
        "AXValue": wda.get("value"),
        "frame": frame,
        "enabled": wda.get("isEnabled", True),
        "role": "",
        "role_description": "",
    }


def flatten_wda_tree(node: dict) -> list[dict]:
    """Recursively flatten a WDA source tree into a flat list of idb-format dicts."""
    result: list[dict] = []
    converted = _map_wda_element(node)
    result.append(converted)

    for child in node.get("children", []):
        result.extend(flatten_wda_tree(child))

    return result


def convert_wda_tree_nested(node: dict) -> list[dict]:
    """Convert a WDA tree to idb format, keeping children nested.

    Returns a list (like idb's describe-all --nested) where each element
    has a 'children' key with its converted child elements.
    """
    converted = _map_wda_element(node)
    children = node.get("children", [])
    if children:
        converted["children"] = []
        for child in children:
            # convert_wda_tree_nested returns a list, but each child is one node
            child_converted = convert_wda_tree_nested(child)
            converted["children"].extend(child_converted)
    return [converted]


def _map_wda_element_from_query(el: dict, class_name: str) -> dict | None:
    """Convert an element from POST /session/{id}/elements to idb-format dict.

    The /elements endpoint returns less data than /source — typically just
    the element reference and a few attributes. We extract what we can.
    """
    # Strip XCUIElementType prefix for the type field; keep the original as
    # xcui_type (#336).
    el_type = class_name
    if el_type.startswith("XCUIElementType"):
        el_type = el_type[len("XCUIElementType"):]

    # WDA /elements response has label, name, rect, isEnabled, etc. inline
    rect = el.get("rect", {})
    frame = None
    if rect and all(k in rect for k in ("x", "y", "width", "height")):
        frame = {
            "x": rect["x"],
            "y": rect["y"],
            "width": rect["width"],
            "height": rect["height"],
        }

    # WDA echoes the class name (e.g. "XCUIElementTypeButton") in the name field
    # when there's no accessibility identifier — filter those out
    raw_name = el.get("name") or ""
    identifier = raw_name if raw_name and not raw_name.startswith("XCUIElementType") else ""
    if not identifier:
        identifier = el.get("rawIdentifier") or ""

    return {
        "type": el_type,
        "xcui_type": _xcui_type(class_name),
        "AXUniqueId": identifier,
        "AXLabel": el.get("label") or "",
        "AXValue": el.get("value"),
        "frame": frame,
        "enabled": el.get("isEnabled", True),
        "role": "",
        "role_description": "",
    }


def find_element_at_point(elements: list[dict], x: float, y: float) -> dict | None:
    """Find the smallest element whose frame contains (x, y).

    Smallest, not last. flatten_wda_tree emits parents before children, so the
    last match used to stand in for the deepest -- but a sibling that comes
    *after* the content is also last, however large it is. iOS 26 Settings has
    several full-screen `Other` views after its rows, so every point on the
    screen resolved to one of them. Their frame never moves, which made the
    scroll sweep's progress check conclude that nothing scrolled and give up
    after one swipe on a list it could have scrolled.

    Ties go to the later element, which keeps the deeper of a parent and a
    child that share a frame.
    """
    best = None
    best_area = float("inf")
    for el in elements:
        frame = el.get("frame")
        if not frame:
            continue
        fx = frame["x"]
        fy = frame["y"]
        fw = frame["width"]
        fh = frame["height"]
        if fx <= x <= fx + fw and fy <= y <= fy + fh:
            area = fw * fh
            if area <= best_area:
                best, best_area = el, area
    return best
