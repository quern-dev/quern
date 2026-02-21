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
import logging
import time
from dataclasses import dataclass, field

import httpx

from server.models import DeviceError

logger = logging.getLogger("quern-debug-server.wda-client")

WDA_PORT = 8100
WDA_TIMEOUT = 10.0  # seconds for HTTP requests
SOURCE_TIMEOUT = 3.0  # seconds — most screens return /source in <2s
SNAPSHOT_MAX_DEPTH = 10  # WDA default is 50 — way too deep for MapKit etc.
FORWARD_START_PORT = 18100  # base port for usbmux forwards
IDLE_TIMEOUT = 15 * 60  # 15 minutes
IDLE_CHECK_INTERVAL = 60  # check every 60 seconds

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


@dataclass
class _WdaConnection:
    """Cached connection info for a device."""

    base_url: str
    # For usbmux-forwarded connections, track the subprocess so we can kill it
    forward_proc: asyncio.subprocess.Process | None = None
    local_port: int | None = None
    session_id: str | None = None


class WdaBackend:
    """Speaks WDA's HTTP API for UI automation on physical iOS devices."""

    def __init__(self) -> None:
        self._connections: dict[str, _WdaConnection] = {}
        self._next_port = FORWARD_START_PORT
        # os_version cache for auto-start — populated by controller
        self._device_os_versions: dict[str, str] = {}
        # Idle timeout tracking
        self._last_interaction: dict[str, float] = {}
        self._idle_task: asyncio.Task | None = None
        # Track active snapshotMaxDepth per device to avoid redundant POSTs
        self._current_depth: dict[str, int] = {}
        # Per-device lock for session creation (prevents parallel _ensure_session races)
        self._session_locks: dict[str, asyncio.Lock] = {}

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

        # Kill port-forward subprocesses
        for conn in self._connections.values():
            if conn.forward_proc and conn.forward_proc.returncode is None:
                conn.forward_proc.terminate()
                try:
                    await asyncio.wait_for(conn.forward_proc.wait(), timeout=3)
                except asyncio.TimeoutError:
                    conn.forward_proc.kill()
        self._connections.clear()
        self._last_interaction.clear()
        self._current_depth.clear()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    async def _get_base_url(self, udid: str) -> str:
        """Get (or create) the WDA base URL for a device.

        iOS 17+: tries the tunneld IPv6 address directly.
        iOS 16-: starts a usbmux port-forward subprocess.

        If WDA is not reachable and os_version is known, auto-starts the driver.
        """
        if udid in self._connections:
            conn = self._connections[udid]

            if conn.forward_proc is not None:
                # usbmux forward — check if process is still alive
                if conn.forward_proc.returncode is None:
                    return conn.base_url
                # Forward proc died — remove and reconnect
                del self._connections[udid]
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
                del self._connections[udid]

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
        from server.device.wda import start_driver

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
        from server.device.tunneld import get_tunneld_devices, resolve_tunnel_udid

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
        from server.device.tunneld import find_pymobiledevice3_binary

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

        # Give the forward a moment to establish
        await asyncio.sleep(0.5)

        if proc.returncode is not None:
            stderr = (await proc.stderr.read()).decode() if proc.stderr else ""
            raise DeviceError(
                f"usbmux forward failed for {udid[:8]}: {stderr.strip()}",
                tool="wda",
            )

        base_url = f"http://localhost:{local_port}"

        # Verify WDA is reachable
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{base_url}/status", timeout=3.0)
                if resp.status_code != 200:
                    raise DeviceError(
                        f"WDA not responding on {udid[:8]} (status {resp.status_code}). "
                        "Ensure WDA is running on the device.",
                        tool="wda",
                    )
        except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as exc:
            proc.terminate()
            raise DeviceError(
                f"Cannot connect to WDA on {udid[:8]} ({type(exc).__name__}). "
                "Ensure WDA is running: launch WebDriverAgentRunner on the device.",
                tool="wda",
            )

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
            except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as exc:
                raise DeviceError(
                    f"WDA session creation failed on {udid[:8]} ({type(exc).__name__})",
                    tool="wda",
                )

            if resp.status_code != 200:
                raise DeviceError(
                    f"WDA session creation failed (status {resp.status_code})",
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
            # snapshotMaxDepth=10 prevents the accessibility tree walk from going
            # 50 levels deep (the default), which deadlocks WDA on MapKit screens
            # with hundreds of annotations.
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
                    logger.info("WDA idle timeout for %s — deleting session (driver stays running)", udid[:8])
                    try:
                        await self.delete_session(udid)
                    except Exception:
                        pass
                    self._connections.pop(udid, None)
                    self._last_interaction.pop(udid, None)
        except asyncio.CancelledError:
            return

    async def _request(
        self, method: str, udid: str, path: str,
        use_session: bool = False, timeout: float | None = None,
        raise_on_timeout: bool = False, **kwargs,
    ) -> httpx.Response:
        """Make an HTTP request to WDA, converting transport errors to DeviceError.

        If use_session=True, prepends /session/{sessionId} to the path.
        If raise_on_timeout=True, re-raises httpx.TimeoutException directly
        instead of wrapping it in DeviceError (so callers can handle timeouts).
        """
        if use_session:
            session_id = await self._ensure_session(udid)
            base_url = self._connections[udid].base_url
            url = f"{base_url}/session/{session_id}{path}"
        else:
            base_url = await self._get_base_url(udid)
            url = f"{base_url}{path}"
        try:
            async with httpx.AsyncClient() as client:
                resp = await getattr(client, method)(
                    url, timeout=timeout or WDA_TIMEOUT, **kwargs,
                )
        except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as exc:
            if raise_on_timeout and isinstance(exc, httpx.TimeoutException):
                # Caller wants to handle timeouts — don't invalidate connection
                # (WDA may still be alive, just slow on this request)
                raise
            # Connection lost — invalidate cached connection so next call reconnects
            self._connections.pop(udid, None)
            raise DeviceError(
                f"WDA connection failed on {udid[:8]} ({type(exc).__name__}). "
                "Ensure WDA is running on the device.",
                tool="wda",
            )

        # Track interaction for idle timeout
        self._last_interaction[udid] = time.monotonic()
        self._ensure_idle_task()

        return resp

    async def _is_wda_responsive(self, udid: str) -> bool:
        """Quick /status ping to check if WDA is still alive (2s timeout)."""
        conn = self._connections.get(udid)
        if not conn:
            # No cached connection — try to resolve base URL without full reconnect
            try:
                base_url = await self._get_base_url(udid)
            except DeviceError:
                return False
        else:
            base_url = conn.base_url

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{base_url}/status", timeout=2.0)
                return resp.status_code == 200
        except Exception:
            return False

    async def _restart_wda(self, udid: str) -> None:
        """Stop and restart the WDA driver for a device, clearing cached connection."""
        from server.device.wda import start_driver, stop_driver

        # Clear cached connection
        self._connections.pop(udid, None)

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
        session_id = await self._ensure_session(udid)
        base_url = self._connections[udid].base_url

        if scope_element_id:
            url = f"{base_url}/session/{session_id}/element/{scope_element_id}/elements"
        else:
            url = f"{base_url}/session/{session_id}/elements"

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    url,
                    json={"using": using, "value": value},
                    timeout=timeout or SKELETON_QUERY_TIMEOUT,
                )
        except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException):
            logger.debug("Element query failed (%s=%s) on %s", using, value, udid[:8])
            return []

        if resp.status_code != 200:
            logger.debug("Element query non-200 (%s=%s) on %s: %d", using, value, udid[:8], resp.status_code)
            return []

        elements = resp.json().get("value", [])
        results: list[dict] = []
        for el in elements:
            # Determine type: prefer element's own 'type' field (available with compact responses off)
            raw_type = el.get("type", "") or value
            # Class chain values like "**/XCUIElementTypeTabBar" — extract just the type
            el_type = raw_type.rsplit("/", 1)[-1] if "/" in raw_type else raw_type
            mapped = _map_wda_element_from_query(el, el_type)
            if mapped:
                # Preserve WDA element UUID for scoped child queries
                wda_id = (el.get("ELEMENT")
                          or el.get("element-6066-11e4-a52e-4f735466cecf"))
                if wda_id:
                    mapped["_wda_element_id"] = wda_id
                results.append(mapped)

        # Track interaction for idle timeout
        self._last_interaction[udid] = time.monotonic()
        self._ensure_idle_task()
        return results

    async def build_screen_skeleton(self, udid: str) -> list[dict]:
        """Build a lightweight screen description using class chain queries.

        Two-phase parallel approach:
        1. Query all container types (TabBar, NavBar, Toolbar, Alert, Sheet) in parallel
        2. Query direct children (Button, Other) scoped to each container in parallel

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

        # Phase 2: query direct children for each container using class name.
        # class name finds immediate children only — reliable on all WDA versions.
        _CHILD_TYPES = [
            "XCUIElementTypeButton",
            "XCUIElementTypeOther",
        ]
        child_tasks = [
            self.find_elements_by_query(
                udid, "class name", child_type,
                scope_element_id=c["_wda_element_id"],
            )
            for c in containers
            for child_type in _CHILD_TYPES
        ]
        child_results = await asyncio.gather(*child_tasks, return_exceptions=True) if child_tasks else []

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

    async def describe_all(self, udid: str, *, snapshot_depth: int | None = None) -> list[dict]:
        """Get all UI elements as flat dicts in idb format.

        Fetches WDA's /source?format=json, flattens the nested tree,
        and converts field names to match idb's describe-all output.

        If /source times out (common on complex screens like MapKit),
        falls back to targeted element queries by class name.

        Args:
            snapshot_depth: WDA accessibility tree depth (1-50). If provided
                and different from current, updates WDA settings before fetching.
        """
        if snapshot_depth is not None:
            await self._set_snapshot_depth(udid, snapshot_depth)

        start = time.perf_counter()
        try:
            resp = await self._request(
                "get", udid, "/source", params={"format": "json"},
                timeout=SOURCE_TIMEOUT, raise_on_timeout=True,
            )
        except httpx.TimeoutException:
            elapsed = (time.perf_counter() - start) * 1000
            logger.warning(
                "[PERF] wda /source timed out after %.0fms on %s — falling back to element queries",
                elapsed, udid[:8],
            )

            # Check if WDA is hung (common with MapKit/large trees)
            if not await self._is_wda_responsive(udid):
                logger.warning("WDA hung on %s, restarting driver...", udid[:8])
                await self._restart_wda(udid)

            return await self.build_screen_skeleton(udid)

        if resp.status_code != 200:
            raise DeviceError(
                f"WDA /source failed (status {resp.status_code}): {resp.text[:200]}",
                tool="wda",
            )

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

    async def describe_all_nested(self, udid: str, *, snapshot_depth: int | None = None) -> list[dict]:
        """Get UI elements with hierarchy preserved, in idb-compatible format.

        Falls back to flat element queries if /source times out.

        Args:
            snapshot_depth: WDA accessibility tree depth (1-50). If provided
                and different from current, updates WDA settings before fetching.
        """
        if snapshot_depth is not None:
            await self._set_snapshot_depth(udid, snapshot_depth)

        start = time.perf_counter()
        try:
            resp = await self._request(
                "get", udid, "/source", params={"format": "json"},
                timeout=SOURCE_TIMEOUT, raise_on_timeout=True,
            )
        except httpx.TimeoutException:
            elapsed = (time.perf_counter() - start) * 1000
            logger.warning(
                "[PERF] wda /source timed out after %.0fms on %s (nested) — falling back to element queries",
                elapsed, udid[:8],
            )

            if not await self._is_wda_responsive(udid):
                logger.warning("WDA hung on %s, restarting driver...", udid[:8])
                await self._restart_wda(udid)

            # Fallback returns flat list — no hierarchy, but better than an error
            return await self.build_screen_skeleton(udid)

        if resp.status_code != 200:
            raise DeviceError(
                f"WDA /source failed (status {resp.status_code})",
                tool="wda",
            )

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

    async def tap(self, udid: str, x: float, y: float) -> None:
        """Tap at coordinates via WDA."""
        resp = await self._request("post", udid, "/wda/tap",
                                    use_session=True, json={"x": x, "y": y})
        if resp.status_code != 200:
            raise DeviceError(
                f"WDA tap failed (status {resp.status_code}): {resp.text[:200]}",
                tool="wda",
            )

    async def swipe(
        self,
        udid: str,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
        duration: float = 0.5,
    ) -> None:
        """Swipe gesture via WDA."""
        resp = await self._request("post", udid, "/wda/dragfromtoforduration",
                                    use_session=True, json={
            "fromX": start_x,
            "fromY": start_y,
            "toX": end_x,
            "toY": end_y,
            "duration": duration,
        })
        if resp.status_code != 200:
            raise DeviceError(
                f"WDA swipe failed (status {resp.status_code}): {resp.text[:200]}",
                tool="wda",
            )

    async def type_text(self, udid: str, text: str) -> None:
        """Type text via WDA."""
        resp = await self._request("post", udid, "/wda/keys",
                                    use_session=True, json={"value": list(text)})
        if resp.status_code != 200:
            raise DeviceError(
                f"WDA type_text failed (status {resp.status_code}): {resp.text[:200]}",
                tool="wda",
            )

    async def press_button(self, udid: str, button: str) -> None:
        """Press a hardware button via WDA."""
        resp = await self._request("post", udid, "/wda/pressButton",
                                    use_session=True, json={"name": button})
        if resp.status_code != 200:
            raise DeviceError(
                f"WDA pressButton failed (status {resp.status_code}): {resp.text[:200]}",
                tool="wda",
            )

    async def select_all_and_delete(
        self, udid: str, x: float, y: float,
        element_type: str | None = None,
    ) -> None:
        """Clear text in a field via WDA's native element clear.

        Uses POST /element to find the text field by class name, then calls
        POST /element/:uuid/clear. Falls back to triple-tap + backspace
        if the native approach fails.
        """
        # Map our normalized type names to XCUIElementType class names
        class_map = {
            "SearchField": "XCUIElementTypeSearchField",
            "TextField": "XCUIElementTypeTextField",
            "SecureTextField": "XCUIElementTypeSecureTextField",
            "TextArea": "XCUIElementTypeTextView",
        }

        # Build ordered list of class names to try (preferred type first)
        class_names = []
        if element_type and element_type in class_map:
            class_names.append(class_map[element_type])
        class_names.extend(v for v in class_map.values() if v not in class_names)

        for class_name in class_names:
            try:
                resp = await self._request("post", udid, "/element",
                                           use_session=True, json={
                    "using": "class name",
                    "value": class_name,
                })
                if resp.status_code != 200:
                    continue
                value = resp.json().get("value", {})
                element_id = (value.get("ELEMENT")
                              or value.get("element-6066-11e4-a52e-4f735466cecf"))
                if not element_id:
                    continue
                clear_resp = await self._request("post", udid,
                                                  f"/element/{element_id}/clear",
                                                  use_session=True)
                if clear_resp.status_code == 200:
                    return
            except DeviceError:
                continue

        # Fallback: triple-tap + backspace (works on simulators via idb)
        for _ in range(3):
            await self.tap(udid, x, y)
        await asyncio.sleep(0.15)
        resp = await self._request("post", udid, "/wda/keys",
                                    use_session=True, json={"value": ["\b"]})
        if resp.status_code != 200:
            raise DeviceError(
                f"WDA select_all_and_delete failed: {resp.text[:200]}",
                tool="wda",
            )


# ------------------------------------------------------------------
# WDA tree → idb format conversion (pure functions, testable)
# ------------------------------------------------------------------


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

    wda_type = wda.get("type", "")
    # WDA prefixes types with "XCUIElementType" — strip it for idb compat
    if wda_type.startswith("XCUIElementType"):
        wda_type = wda_type[len("XCUIElementType"):]

    return {
        "type": wda_type,
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
    # Strip XCUIElementType prefix for the type field
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
        "AXUniqueId": identifier,
        "AXLabel": el.get("label") or "",
        "AXValue": el.get("value"),
        "frame": frame,
        "enabled": el.get("isEnabled", True),
        "role": "",
        "role_description": "",
    }


def find_element_at_point(elements: list[dict], x: float, y: float) -> dict | None:
    """Find the deepest (last in flat list) element whose frame contains (x, y).

    Since flatten_wda_tree outputs parents before children, the last match
    is the most specific (deepest) element.
    """
    best = None
    for el in elements:
        frame = el.get("frame")
        if not frame:
            continue
        fx = frame["x"]
        fy = frame["y"]
        fw = frame["width"]
        fh = frame["height"]
        if fx <= x <= fx + fw and fy <= y <= fy + fh:
            best = el
    return best
