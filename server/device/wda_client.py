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
FORWARD_START_PORT = 18100  # base port for usbmux forwards
IDLE_TIMEOUT = 15 * 60  # 15 minutes
IDLE_CHECK_INTERVAL = 60  # check every 60 seconds


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
            # Verify the connection is still alive
            if conn.forward_proc is None or conn.forward_proc.returncode is None:
                return conn.base_url

            # Forward proc died — remove and reconnect
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

        if conn:
            conn.session_id = session_id
        logger.info("WDA session created for %s: %s", udid[:8], session_id[:8])
        return session_id

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
        use_session: bool = False, **kwargs,
    ) -> httpx.Response:
        """Make an HTTP request to WDA, converting transport errors to DeviceError.

        If use_session=True, prepends /session/{sessionId} to the path.
        """
        base_url = await self._get_base_url(udid)
        if use_session:
            session_id = await self._ensure_session(udid)
            url = f"{base_url}/session/{session_id}{path}"
        else:
            url = f"{base_url}{path}"
        try:
            async with httpx.AsyncClient() as client:
                resp = await getattr(client, method)(
                    url, timeout=WDA_TIMEOUT, **kwargs,
                )
        except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as exc:
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

    async def describe_all(self, udid: str) -> list[dict]:
        """Get all UI elements as flat dicts in idb format.

        Fetches WDA's /source?format=json, flattens the nested tree,
        and converts field names to match idb's describe-all output.
        """
        start = time.perf_counter()
        resp = await self._request("get", udid, "/source", params={"format": "json"})
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

    async def describe_all_nested(self, udid: str) -> list[dict]:
        """Get UI elements with hierarchy preserved, in idb-compatible format."""
        resp = await self._request("get", udid, "/source", params={"format": "json"})
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
