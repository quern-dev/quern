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
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path

from server.device import probing
from server.models import SimBridgeSaturatedError

logger = logging.getLogger("quern-debug-server.sim-bridge")

QUERN_BIN_DIR = Path.home() / ".quern" / "bin"
BINARY_NAME = "sim-bridge"
_SOURCE_CANDIDATES = [
    Path(__file__).resolve().parent.parent.parent / "tools" / "sim-bridge.swift",
]

# Max length of one stdout JSON line from the subprocess. asyncio's default
# StreamReader limit is 64 KiB; a describe-ui response for a dense screen
# (e.g. a map with hundreds of annotations) easily exceeds that, which made
# readline() raise LimitOverrunError and killed the reader task.
STREAM_LIMIT = 32 * 1024 * 1024

# sim-bridge serialises every command through a single lock, and an abandoned
# HTTP request is not cancelled by uvicorn — it keeps its slot and runs to
# completion. Without a bound a retry loop becomes a multi-minute outage (#68).
#
# The bound is on concurrent *operations*, not commands. One describe_all can
# legitimately fan out into a describe-ui plus one probe-point per empty
# container, dispatched concurrently via asyncio.gather — bounding commands
# rejects a request's own continuation work, which is both wrong and confusing.
MAX_CONCURRENT_OPERATIONS = 6

# Backstop for the few-but-slow case: a caller that has waited this long behind
# other commands would receive a tree describing a screen that has since moved.
LOCK_WAIT_TIMEOUT = 20.0

# Marks that we are already inside an admitted operation, so follow-on commands
# are not re-admitted. Task contexts inherit this, which is what makes the
# gather-based probe fan-out exempt.
_in_operation: ContextVar[bool] = ContextVar("sim_bridge_in_operation", default=False)


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
        self._stderr_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._binary_path = QUERN_BIN_DIR / BINARY_NAME
        self._pending_response: asyncio.Future | None = None
        self._operations = 0

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
            limit=STREAM_LIMIT,
        )
        self._reader_task = asyncio.create_task(self._stdout_reader())
        self._stderr_task = asyncio.create_task(self._stderr_reader())

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
            # The subprocess itself is usually still healthy when the reader
            # dies (e.g. an oversized line). Kill it before dropping our
            # reference — otherwise each respawn leaks an orphaned binary.
            await self._kill_process()
            self._cleanup_state()

    async def _stderr_reader(self) -> None:
        """Background task: drains subprocess stderr into the debug log.

        The Swift helper writes [PERF]/[ax]/[hid] diagnostics to stderr; if
        nobody reads the pipe, the 64 KiB buffer eventually fills and the
        subprocess blocks mid-write, hanging every subsequent command.
        """
        proc = self._process
        if proc is None or proc.stderr is None:
            return

        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                text = line.decode(errors="replace").rstrip()
                if text.startswith("[sim-bridge] "):
                    text = text[len("[sim-bridge] "):]
                logger.debug("sim-bridge stderr: %s", text)
        except Exception:
            logger.debug("sim-bridge stderr reader stopped", exc_info=True)

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

    @asynccontextmanager
    async def admit(self):
        """Admission gate for one logical operation.

        Re-entrant by design: an operation that fans out into further commands
        (describe_all probing empty containers) is admitted once, at the top.
        Bounding individual commands instead would reject a request's own
        continuation work — observed live, where one describe_all's probe
        fan-out filled an 8-command queue by itself.
        """
        if _in_operation.get():
            yield
            return

        if self._operations >= MAX_CONCURRENT_OPERATIONS:
            raise SimBridgeSaturatedError(
                f"sim-bridge is saturated: {self._operations} operations already in "
                f"flight (limit {MAX_CONCURRENT_OPERATIONS}). The device is responsive — "
                f"there is simply more queued than is useful. Stop retrying and let it drain.",
                queued=self._operations,
            )

        self._operations += 1
        token = _in_operation.set(True)
        try:
            yield
        finally:
            self._operations -= 1
            _in_operation.reset(token)

    async def send(self, cmd: dict) -> dict:
        """Send a command and wait for the response. Serialised via lock.

        The lock is load bearing rather than merely tidy: the wire protocol
        carries no request IDs, so two in-flight commands would resolve each
        other's futures. It cannot be removed without changing both sides.
        """
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=LOCK_WAIT_TIMEOUT)
        except TimeoutError:
            raise SimBridgeSaturatedError(
                f"sim-bridge did not become available within {LOCK_WAIT_TIMEOUT:.0f}s. "
                f"Any result would describe a screen that has since changed.",
                queued=self._operations,
            ) from None
        try:
            return await self._send_locked(cmd)
        finally:
            self._lock.release()

    async def _send_locked(self, cmd: dict) -> dict:
        """Body of send(), with the lock already held."""
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
        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
        self._stderr_task = None
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
        async with self._mgr.admit():
            return await self._send_admitted(cmd)

    async def _send_admitted(self, cmd: dict) -> dict:
        result = await self._mgr.send(cmd)
        if not result.get("ok", False):
            error = result.get("error", "unknown error")
            raise RuntimeError(f"sim-bridge: {error}")
        return result

    async def describe_all(
        self, udid: str, *, snapshot_depth: int | None = None,
        source_timeout: float | None = None,
    ) -> list[dict]:
        """Return flat list of UI elements.

        Fetches the nested tree, finds containers that the static walk
        reports as childless (tab bars, nav bars, toolbars with hidden
        SwiftUI subviews), probes each via server-side AXPTranslator
        hit-tests, then flattens and merges. Mirrors the behavior of
        `IdbBackend.describe_all` so downstream consumers don't need to
        care which backend is active.
        """
        async with self._mgr.admit():
            start = time.perf_counter()

            nested = await self._fetch_nested(udid)
            empty_containers = probing.find_empty_containers(nested)
            flat = probing.flatten_nested(nested)

            if empty_containers:
                logger.info(
                    "[PERF] sim-bridge.describe_all: probing %d empty containers",
                    len(empty_containers),
                )
                probe_tasks = [
                    probing.probe_container(udid, c, self.describe_point)
                    for c in empty_containers
                ]
                probe_results = await asyncio.gather(*probe_tasks)
                probed = [el for batch in probe_results for el in batch]
                probing.merge_probed_into_flat(flat, probed)

            logger.info(
                "[PERF] sim-bridge.describe_all COMPLETE: total=%.1fms elements=%d",
                (time.perf_counter() - start) * 1000, len(flat),
            )
            return flat

    async def describe_all_nested(
        self, udid: str, *, snapshot_depth: int | None = None,
    ) -> list[dict]:
        """Return nested tree with children arrays preserved (no probing)."""
        async with self._mgr.admit():
            return await self._fetch_nested(udid)

    async def _fetch_nested(self, udid: str) -> list[dict]:
        result = await self._send({
            "cmd": "describe-ui", "udid": udid, "nested": True,
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
        """Same as describe_all — sim-bridge has only one tree-fetch path."""
        async with self._mgr.admit():
            return await self.describe_all(udid, snapshot_depth=snapshot_depth,
                                           source_timeout=source_timeout)

    async def describe_point(
        self, udid: str, x: float, y: float,
    ) -> dict | None:
        """Server-side hit-test via AXPTranslator's objectAtPoint.

        Returns the deepest accessibility element at the given device-point
        coordinate. Unlike a client-side tree hit-test, this can return
        elements that the tree walk missed (the SwiftUI tab-bar case), so
        it's the foundation for `probing.probe_container`.

        Misses (no element under the point) return None rather than raising.
        """
        result = await self._mgr.send({
            "cmd": "probe-point", "udid": udid,
            "x": float(x), "y": float(y), "nested": False,
        })
        if not result.get("ok"):
            return None
        tree = result.get("tree")
        # nested=false yields a flat list whose first entry is the hit element.
        if isinstance(tree, list):
            return tree[0] if tree else None
        if isinstance(tree, dict):
            return tree
        return None

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

    async def set_hardware_keyboard(self, udid: str, enabled: bool) -> None:
        await self._send({
            "cmd": "set-hardware-keyboard", "udid": udid, "enabled": enabled,
        })

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
