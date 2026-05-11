"""SimBridge — native simulator control via Apple private frameworks.

Uses a long-running sim-bridge Swift subprocess communicating via JSON
Lines on stdin/stdout. Replaces idb for simulator UI automation:
accessibility tree queries, tap/swipe/type gestures, button presses,
and IOSurface screenshots.

Follows the same subprocess management pattern as preview.py.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import shutil
from pathlib import Path

logger = logging.getLogger("quern-debug-server.sim-bridge")

QUERN_BIN_DIR = Path.home() / ".quern" / "bin"
BINARY_NAME = "sim-bridge"
_SOURCE_CANDIDATES = [
    Path(__file__).resolve().parent.parent.parent / "tools" / "sim-bridge.swift",
]


def _find_source() -> Path | None:
    for p in _SOURCE_CANDIDATES:
        if p.exists():
            return p
    return None


class SimBridgeManager:
    """Manages the sim-bridge subprocess lifecycle and communication."""

    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._ready = asyncio.Event()
        self._reader_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._binary_path = QUERN_BIN_DIR / BINARY_NAME
        self._pending_response: asyncio.Future | None = None

    async def is_available(self) -> bool:
        """Check if sim-bridge can be compiled and used."""
        if shutil.which("swiftc") is None:
            return False
        if _find_source() is None:
            return False
        # Check for Xcode (need private frameworks)
        try:
            proc = await asyncio.create_subprocess_exec(
                "xcode-select", "-p",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate()
            dev_dir = stdout.decode().strip()
            if not dev_dir:
                return False
            sim_kit = Path(dev_dir) / "Library" / "PrivateFrameworks" / "SimulatorKit.framework"
            return sim_kit.exists()
        except Exception:
            return False

    async def ensure_binary(self) -> Path:
        """Lazy-compile sim-bridge if needed. Returns path to binary."""
        source = _find_source()
        if source is None:
            raise RuntimeError(
                "sim-bridge.swift source not found. "
                "Expected at tools/sim-bridge.swift relative to the project root."
            )

        if self._binary_path.exists():
            src_mtime = source.stat().st_mtime
            bin_mtime = self._binary_path.stat().st_mtime
            if bin_mtime >= src_mtime:
                return self._binary_path

        swiftc = shutil.which("swiftc")
        if swiftc is None:
            raise RuntimeError(
                "swiftc not found. Install Xcode: xcode-select --install"
            )

        QUERN_BIN_DIR.mkdir(parents=True, exist_ok=True)
        logger.info("Compiling sim-bridge: %s → %s", source, self._binary_path)

        proc = await asyncio.create_subprocess_exec(
            swiftc,
            "-o", str(self._binary_path),
            str(source),
            "-framework", "Foundation",
            "-framework", "IOSurface",
            "-framework", "CoreGraphics",
            "-framework", "ImageIO",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            err = stderr.decode().strip() or stdout.decode().strip()
            raise RuntimeError(f"Failed to compile sim-bridge:\n{err}")

        logger.info("sim-bridge compiled successfully")
        return self._binary_path

    # ------------------------------------------------------------------
    # Process lifecycle
    # ------------------------------------------------------------------

    async def _ensure_process(self) -> None:
        """Launch the subprocess if not running."""
        if self._process is not None and self._process.returncode is None:
            return

        self._cleanup_state()

        binary = await self.ensure_binary()
        logger.info("Starting sim-bridge")

        self._ready.clear()
        self._process = await asyncio.create_subprocess_exec(
            str(binary),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader_task = asyncio.create_task(self._stdout_reader())

        try:
            await asyncio.wait_for(self._ready.wait(), timeout=15.0)
        except TimeoutError:
            logger.error("sim-bridge did not become ready in 15s")
            await self._kill_process()
            raise RuntimeError("sim-bridge failed to start (timeout)")

    async def _stdout_reader(self) -> None:
        """Background task: reads JSON lines from subprocess stdout."""
        proc = self._process
        if proc is None or proc.stdout is None:
            return

        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("sim-bridge: non-JSON output: %s", line[:200])
                    continue

                self._dispatch(msg)
        except Exception:
            logger.exception("sim-bridge stdout reader error")
        finally:
            self._cleanup_state()

    def _dispatch(self, msg: dict) -> None:
        """Route a parsed JSON message from the subprocess."""
        if "event" in msg:
            event = msg["event"]
            if event == "ready":
                logger.info("sim-bridge ready")
                self._ready.set()
            return

        # It's a command response — resolve the pending future
        if self._pending_response is not None and not self._pending_response.done():
            self._pending_response.set_result(msg)

    async def send(self, cmd: dict) -> dict:
        """Send a command and wait for the response. Thread-safe via lock."""
        async with self._lock:
            await self._ensure_process()

            if self._process is None or self._process.stdin is None:
                raise RuntimeError("sim-bridge process not available")

            loop = asyncio.get_event_loop()
            self._pending_response = loop.create_future()

            line = json.dumps(cmd, separators=(",", ":")) + "\n"
            self._process.stdin.write(line.encode())
            await self._process.stdin.drain()

            try:
                result = await asyncio.wait_for(self._pending_response, timeout=30.0)
            except TimeoutError:
                raise RuntimeError(f"sim-bridge command timed out: {cmd.get('cmd')}")

            return result

    async def stop(self) -> None:
        """Stop the subprocess."""
        if self._process is None:
            return
        if self._process.returncode is None:
            if self._process.stdin:
                try:
                    self._process.stdin.close()
                except Exception:
                    pass
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except TimeoutError:
                await self._kill_process()
        self._cleanup_state()

    async def _kill_process(self) -> None:
        if self._process is None:
            return
        if self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=3.0)
            except TimeoutError:
                self._process.kill()

    def _cleanup_state(self) -> None:
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        self._reader_task = None
        self._process = None
        self._ready.clear()
        if self._pending_response and not self._pending_response.done():
            self._pending_response.cancel()
        self._pending_response = None


class SimBridgeBackend:
    """Simulator UI backend using the sim-bridge Swift helper.

    Implements the same interface as IdbBackend for use by controller_ui.py.
    """

    def __init__(self, manager: SimBridgeManager) -> None:
        self._mgr = manager

    async def _send(self, cmd: dict) -> dict:
        result = await self._mgr.send(cmd)
        if not result.get("ok", False):
            error = result.get("error", "unknown error")
            raise RuntimeError(f"sim-bridge: {error}")
        return result

    async def describe_all(
        self, udid: str, *, snapshot_depth: int | None = None,
        source_timeout: float | None = None,
    ) -> list[dict]:
        """Return flat list of UI elements (no children key)."""
        result = await self._send({
            "cmd": "describe-ui",
            "udid": udid,
            "nested": False,
        })
        tree = result.get("tree", [])
        if isinstance(tree, list):
            return tree
        # If it's a single dict (shouldn't happen for non-nested), wrap it
        return [tree]

    async def describe_all_nested(
        self, udid: str, *, snapshot_depth: int | None = None,
    ) -> list[dict]:
        """Return nested tree with children arrays preserved."""
        result = await self._send({
            "cmd": "describe-ui",
            "udid": udid,
            "nested": True,
        })
        tree = result.get("tree")
        if isinstance(tree, dict):
            return [tree]
        if isinstance(tree, list):
            return tree
        return []

    async def describe_all_flat(
        self, udid: str, *, snapshot_depth: int | None = None,
        source_timeout: float | None = None,
    ) -> list[dict]:
        """Same as describe_all — sim-bridge always returns flat for non-nested."""
        return await self.describe_all(udid, snapshot_depth=snapshot_depth,
                                       source_timeout=source_timeout)

    async def describe_point(
        self, udid: str, x: float, y: float,
    ) -> dict | None:
        """Return the element at a specific point."""
        result = await self._send({
            "cmd": "describe-ui",
            "udid": udid,
            "x": x,
            "y": y,
        })
        return result.get("element")

    async def tap(self, udid: str, x: float, y: float) -> None:
        await self._send({"cmd": "tap", "udid": udid, "x": x, "y": y})

    async def swipe(
        self, udid: str,
        start_x: float, start_y: float,
        end_x: float, end_y: float,
        duration: float = 0.3,
    ) -> None:
        await self._send({
            "cmd": "swipe", "udid": udid,
            "x1": start_x, "y1": start_y,
            "x2": end_x, "y2": end_y,
            "duration": duration,
        })

    async def type_text(self, udid: str, text: str) -> None:
        await self._send({"cmd": "type", "udid": udid, "text": text})

    async def press_button(self, udid: str, button: str) -> None:
        await self._send({"cmd": "button", "udid": udid, "name": button})

    async def select_all_and_delete(
        self, udid: str, x: float, y: float,
        element_type: str | None = None,
    ) -> None:
        """Select all text and delete it. Triple-tap to select, then backspace."""
        # Triple-tap to select all
        for _ in range(3):
            await self._send({"cmd": "tap", "udid": udid, "x": x, "y": y})
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.1)
        # Send backspace (HID usage 0x2A on page 7)
        await self._send({"cmd": "type", "udid": udid, "text": "\x08"})

    async def screenshot(
        self, udid: str, quality: float = 0.8, scale: int = 1,
    ) -> bytes:
        """Capture a screenshot via IOSurface. Returns raw JPEG bytes."""
        result = await self._send({
            "cmd": "screenshot", "udid": udid,
            "quality": quality, "scale": scale,
        })
        b64 = result.get("data", "")
        return base64.b64decode(b64)
