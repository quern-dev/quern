"""Preview manager for live iOS device screen previews via CoreMediaIO.

Uses a long-running ios-preview subprocess in --interactive mode,
communicating via a JSON Lines protocol on stdin/stdout. Supports
independent per-device preview control.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("quern-debug-server.preview")

QUERN_BIN_DIR = Path.home() / ".quern" / "bin"
BINARY_NAME = "ios-preview"
_SOURCE_CANDIDATES = [
    Path(__file__).resolve().parent.parent.parent / "tools" / "ios-preview.swift",
]


def _find_source() -> Path | None:
    for p in _SOURCE_CANDIDATES:
        if p.exists():
            return p
    return None


@dataclass
class PreviewDeviceInfo:
    name: str
    cmio_id: str


@dataclass
class ActivePreview:
    name: str
    position: int
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class PreviewManager:
    """Manages per-device preview sessions via a long-running ios-preview process."""

    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._active: dict[str, ActivePreview] = {}
        self._available: list[PreviewDeviceInfo] = []
        self._ready = asyncio.Event()
        self._reader_task: asyncio.Task | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._positions: set[int] = set()
        self._stagger_lock = asyncio.Lock()
        self._binary_path = QUERN_BIN_DIR / BINARY_NAME

    async def ensure_binary(self) -> Path:
        """Lazy-compile ios-preview if needed. Returns path to binary."""
        source = _find_source()
        if source is None:
            raise RuntimeError(
                "ios-preview.swift source not found. "
                "Expected at tools/ios-preview.swift relative to the project root."
            )

        if self._binary_path.exists():
            src_mtime = source.stat().st_mtime
            bin_mtime = self._binary_path.stat().st_mtime
            if bin_mtime >= src_mtime:
                return self._binary_path

        swiftc = shutil.which("swiftc")
        if swiftc is None:
            raise RuntimeError(
                "swiftc not found. Install Xcode or Xcode Command Line Tools: "
                "xcode-select --install"
            )

        QUERN_BIN_DIR.mkdir(parents=True, exist_ok=True)
        logger.info("Compiling ios-preview: %s → %s", source, self._binary_path)

        proc = await asyncio.create_subprocess_exec(
            swiftc,
            "-o", str(self._binary_path),
            str(source),
            "-framework", "AVFoundation",
            "-framework", "CoreMediaIO",
            "-framework", "AppKit",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            err = stderr.decode().strip() or stdout.decode().strip()
            raise RuntimeError(f"Failed to compile ios-preview:\n{err}")

        logger.info("ios-preview compiled successfully")
        return self._binary_path

    # ------------------------------------------------------------------
    # Process lifecycle
    # ------------------------------------------------------------------

    async def _ensure_process(self) -> None:
        """Launch the interactive subprocess if not running."""
        if self._process is not None and self._process.returncode is None:
            return

        # Clean up stale state
        self._cleanup_state()

        binary = await self.ensure_binary()
        logger.info("Starting ios-preview --interactive")

        self._ready.clear()
        self._process = await asyncio.create_subprocess_exec(
            str(binary), "--interactive",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._reader_task = asyncio.create_task(self._stdout_reader())

        try:
            await asyncio.wait_for(self._ready.wait(), timeout=15.0)
        except asyncio.TimeoutError:
            logger.error("ios-preview --interactive did not become ready in 15s")
            await self._kill_process()
            raise RuntimeError("Preview process failed to start (timeout waiting for ready)")

    async def _kill_process(self) -> None:
        """Forcefully terminate the subprocess."""
        if self._process is None:
            return
        if self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()
        self._cleanup_state()

    def _cleanup_state(self) -> None:
        """Reset all internal state."""
        self._process = None
        self._available = []
        self._active.clear()
        self._positions.clear()
        self._ready.clear()
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        self._reader_task = None
        # Reject all pending futures
        for name, fut in self._pending.items():
            if not fut.done():
                fut.set_exception(RuntimeError("Preview process exited"))
        self._pending.clear()

    # ------------------------------------------------------------------
    # Stdout reader
    # ------------------------------------------------------------------

    async def _stdout_reader(self) -> None:
        """Read JSON Lines from the subprocess stdout."""
        assert self._process is not None and self._process.stdout is not None
        try:
            while True:
                line = await self._process.stdout.readline()
                if not line:
                    break  # EOF
                try:
                    event = json.loads(line.decode().strip())
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue

                self._dispatch_event(event)
        except asyncio.CancelledError:
            return
        finally:
            # Process died or stdout closed
            logger.info("ios-preview stdout closed")
            self._cleanup_state()

    def _dispatch_event(self, event: dict) -> None:
        """Handle a single event from the subprocess."""
        evt_type = event.get("event")
        name = event.get("name", "")

        if evt_type == "ready":
            devices = event.get("devices", [])
            self._available = [
                PreviewDeviceInfo(name=d["name"], cmio_id=d.get("id", ""))
                for d in devices
            ]
            self._ready.set()
            logger.info(
                "Preview ready: %d device(s) available",
                len(self._available),
            )

        elif evt_type == "added":
            fut = self._pending.pop(name, None)
            if fut and not fut.done():
                fut.set_result(True)

        elif evt_type == "add_failed":
            error = event.get("error", "Unknown error")
            fut = self._pending.pop(name, None)
            if fut and not fut.done():
                fut.set_exception(RuntimeError(f"Failed to add preview for {name}: {error}"))

        elif evt_type == "removed":
            fut = self._pending.pop(name, None)
            if fut and not fut.done():
                fut.set_result(True)

        elif evt_type == "window_closed":
            # User closed the window manually
            if name in self._active:
                preview = self._active.pop(name)
                self._positions.discard(preview.position)
                logger.info("Preview window closed by user: %s", name)

        elif evt_type == "devices":
            devices = event.get("devices", [])
            self._available = [
                PreviewDeviceInfo(name=d["name"], cmio_id=d.get("id", ""))
                for d in devices
            ]

        elif evt_type == "error":
            logger.warning("Preview process error: %s", event.get("message", ""))

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def _send(self, cmd: dict) -> None:
        """Send a JSON command to the subprocess stdin."""
        if self._process is None or self._process.stdin is None:
            raise RuntimeError("Preview process not running")
        line = json.dumps(cmd) + "\n"
        self._process.stdin.write(line.encode())
        await self._process.stdin.drain()

    def _next_position(self) -> int:
        """Find the lowest unoccupied window position."""
        pos = 0
        while pos in self._positions:
            pos += 1
        return pos

    async def add(self, name: str) -> ActivePreview:
        """Add a preview for a device by name.

        Args:
            name: Device name (must match a name from _available).

        Returns:
            ActivePreview record.

        Raises:
            RuntimeError: If device not found or add fails.
        """
        await self._ensure_process()

        # Check if already active
        if name in self._active:
            return self._active[name]

        # Validate name
        available_names = [d.name for d in self._available]
        if name not in available_names:
            raise RuntimeError(
                f"Device '{name}' not found in CoreMediaIO devices. "
                f"Available: {available_names}"
            )

        async with self._stagger_lock:
            position = self._next_position()

            loop = asyncio.get_event_loop()
            fut: asyncio.Future = loop.create_future()
            self._pending[name] = fut

            await self._send({"cmd": "add", "name": name, "position": position})

            try:
                await asyncio.wait_for(fut, timeout=10.0)
            except asyncio.TimeoutError:
                self._pending.pop(name, None)
                raise RuntimeError(f"Timeout adding preview for {name}")

            preview = ActivePreview(name=name, position=position)
            self._active[name] = preview
            self._positions.add(position)

            # Stagger: wait 1s before allowing the next add
            await asyncio.sleep(1.0)

            return preview

    async def remove(self, name: str) -> None:
        """Remove a preview for a device by name."""
        if self._process is None or self._process.returncode is not None:
            self._active.pop(name, None)
            return

        if name not in self._active:
            return

        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[name] = fut

        await self._send({"cmd": "remove", "name": name})

        try:
            await asyncio.wait_for(fut, timeout=5.0)
        except asyncio.TimeoutError:
            self._pending.pop(name, None)
            logger.warning("Timeout removing preview for %s", name)

        preview = self._active.pop(name, None)
        if preview:
            self._positions.discard(preview.position)

    async def stop(self) -> dict:
        """Stop all previews and kill the process."""
        if self._process is None or self._process.returncode is not None:
            self._cleanup_state()
            return {"status": "stopped"}

        pid = self._process.pid
        try:
            await self._send({"cmd": "quit"})
            await asyncio.wait_for(self._process.wait(), timeout=5.0)
        except (asyncio.TimeoutError, RuntimeError):
            await self._kill_process()

        self._cleanup_state()
        return {"status": "stopped", "pid": pid}

    def status(self) -> dict:
        """Return current preview state."""
        running = self._process is not None and self._process.returncode is None

        active = {
            name: {
                "position": p.position,
                "started_at": p.started_at.isoformat(),
            }
            for name, p in self._active.items()
        }

        available = [
            {"name": d.name, "id": d.cmio_id}
            for d in self._available
        ]

        result: dict = {
            "status": "running" if running else "stopped",
            "active": active,
            "active_count": len(active),
            "available": available,
            "available_count": len(available),
        }
        if running and self._process:
            result["pid"] = self._process.pid
        return result

    async def list_devices(self) -> dict:
        """Get available CoreMediaIO devices.

        If the process is running, returns the cached list.
        Otherwise, runs ios-preview --list as a one-shot.
        """
        if self._process is not None and self._process.returncode is None:
            return {
                "devices": [
                    {"name": d.name, "id": d.cmio_id}
                    for d in self._available
                ]
            }

        # Fall back to --list mode
        binary = await self.ensure_binary()
        proc = await asyncio.create_subprocess_exec(
            str(binary), "--list",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            err_text = stderr.decode().strip()
            if "No iOS devices found" in err_text:
                return {"devices": []}
            raise RuntimeError(f"ios-preview --list failed: {err_text}")

        devices = []
        for line in stdout.decode().splitlines():
            line = line.strip()
            if line.startswith("["):
                try:
                    bracket_end = line.index("]")
                    idx = int(line[1:bracket_end])
                    rest = line[bracket_end + 1:].strip()
                    if "(id:" in rest:
                        name_part, id_part = rest.rsplit("(id:", 1)
                        name = name_part.strip()
                        device_id = id_part.rstrip(")").strip()
                    else:
                        name = rest
                        device_id = ""
                    devices.append({
                        "index": idx,
                        "name": name,
                        "id": device_id,
                    })
                except (ValueError, IndexError):
                    continue

        return {"devices": devices}
