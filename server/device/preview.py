"""Preview manager for live iOS device screen previews via CoreMediaIO."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("quern-debug-server.preview")

QUERN_BIN_DIR = Path.home() / ".quern" / "bin"
BINARY_NAME = "ios-preview"
# Source is relative to the project root (tools/ios-preview.swift)
# We resolve it at compile time from the known location.
_SOURCE_CANDIDATES = [
    Path(__file__).resolve().parent.parent.parent / "tools" / "ios-preview.swift",
]


def _find_source() -> Path | None:
    for p in _SOURCE_CANDIDATES:
        if p.exists():
            return p
    return None


class PreviewManager:
    """Manages the ios-preview binary lifecycle (compile, launch, kill)."""

    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._device_filter: str | None = None
        self._started_at: datetime | None = None
        self._binary_path = QUERN_BIN_DIR / BINARY_NAME

    async def ensure_binary(self) -> Path:
        """Lazy-compile ios-preview if needed. Returns path to binary."""
        source = _find_source()
        if source is None:
            raise RuntimeError(
                "ios-preview.swift source not found. "
                "Expected at tools/ios-preview.swift relative to the project root."
            )

        # Check if binary is up-to-date
        if self._binary_path.exists():
            src_mtime = source.stat().st_mtime
            bin_mtime = self._binary_path.stat().st_mtime
            if bin_mtime >= src_mtime:
                return self._binary_path

        # Find swiftc
        swiftc = shutil.which("swiftc")
        if swiftc is None:
            raise RuntimeError(
                "swiftc not found. Install Xcode or Xcode Command Line Tools: "
                "xcode-select --install"
            )

        # Compile
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

    async def start(self, device_name: str | None = None) -> dict:
        """Start the preview binary.

        Args:
            device_name: Optional device name substring to filter on.

        Returns:
            Status dict with state info.
        """
        # Stop existing preview if running
        if self._process is not None and self._process.returncode is None:
            await self.stop()

        binary = await self.ensure_binary()

        cmd = [str(binary)]
        if device_name:
            cmd.append(device_name)

        logger.info("Starting ios-preview: %s", " ".join(cmd))

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._device_filter = device_name
        self._started_at = datetime.now(timezone.utc)

        return {
            "status": "started",
            "pid": self._process.pid,
            "device_filter": self._device_filter,
        }

    async def stop(self) -> dict:
        """Stop the preview binary."""
        if self._process is None or self._process.returncode is not None:
            self._process = None
            self._device_filter = None
            self._started_at = None
            return {"status": "stopped"}

        pid = self._process.pid
        logger.info("Stopping ios-preview (pid %d)", pid)
        self._process.terminate()
        try:
            await asyncio.wait_for(self._process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("ios-preview didn't terminate, killing (pid %d)", pid)
            self._process.kill()
            await self._process.wait()

        self._process = None
        self._device_filter = None
        self._started_at = None
        return {"status": "stopped", "pid": pid}

    def status(self) -> dict:
        """Return current preview state."""
        if self._process is not None and self._process.returncode is None:
            return {
                "status": "running",
                "pid": self._process.pid,
                "device_filter": self._device_filter,
                "started_at": self._started_at.isoformat() if self._started_at else None,
            }
        return {"status": "stopped"}

    async def list_devices(self) -> dict:
        """Run ios-preview --list and parse output.

        This takes ~3s due to CoreMediaIO device discovery delay.
        """
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

        # Parse output like:
        #   Connected iOS screen capture devices:
        #     [0] iPhone 11  (id: abc123...)
        devices = []
        for line in stdout.decode().splitlines():
            line = line.strip()
            if line.startswith("["):
                # [0] iPhone 11  (id: abc123...)
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
