"""IdbBackend — async wrapper around Facebook's idb CLI for UI automation."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sys
from pathlib import Path

from server.models import DeviceError

logger = logging.getLogger("ios-debug-server.idb")


class IdbBackend:
    """Manages UI inspection and interaction via idb subprocess calls."""

    def __init__(self) -> None:
        self._binary: str | None = None

    @staticmethod
    def _find_idb() -> str | None:
        """Locate the idb binary, preferring the active venv."""
        # Check next to the running Python (same venv)
        venv_bin = Path(sys.executable).parent / "idb"
        if venv_bin.is_file():
            return str(venv_bin)
        # Fall back to system PATH
        return shutil.which("idb")

    def _resolve_binary(self) -> str:
        """Find the idb binary. Cached after first lookup."""
        if self._binary is not None:
            return self._binary
        path = self._find_idb()
        if path is None:
            raise DeviceError(
                "idb not found. Install with: pip install fb-idb "
                "(also requires: brew install idb-companion)",
                tool="idb",
            )
        self._binary = path
        return path

    async def is_available(self) -> bool:
        """Check if idb CLI is available."""
        return self._find_idb() is not None

    async def _run(self, *args: str) -> tuple[str, str]:
        """Run an idb command and return (stdout, stderr).

        Raises DeviceError on non-zero exit code.
        """
        binary = self._resolve_binary()
        proc = await asyncio.create_subprocess_exec(
            binary, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            cmd = args[0] if args else "unknown"
            raise DeviceError(
                f"idb {cmd} failed: {stderr.decode().strip()}",
                tool="idb",
            )
        return stdout.decode(), stderr.decode()

    async def describe_all(self, udid: str) -> list[dict]:
        """Get all UI accessibility elements as raw dicts.

        Runs: idb ui describe-all --udid <udid>
        Returns the parsed JSON array from idb output.
        """
        stdout, _ = await self._run("ui", "describe-all", "--udid", udid)
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise DeviceError(
                f"Failed to parse idb describe-all output: {exc}",
                tool="idb",
            )
        if not isinstance(data, list):
            raise DeviceError(
                f"Expected JSON array from describe-all, got {type(data).__name__}",
                tool="idb",
            )
        return data

    async def tap(self, udid: str, x: float, y: float) -> None:
        """Tap at coordinates. Runs: idb ui tap <x> <y> --udid <udid>

        Coordinates are rounded to integers as idb expects int values.
        """
        await self._run("ui", "tap", str(int(round(x))), str(int(round(y))), "--udid", udid)

    async def swipe(
        self,
        udid: str,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
        duration: float = 0.5,
    ) -> None:
        """Swipe gesture. Runs: idb ui swipe <x1> <y1> <x2> <y2> --udid <udid> --duration <d>"""
        await self._run(
            "ui", "swipe",
            str(int(round(start_x))), str(int(round(start_y))),
            str(int(round(end_x))), str(int(round(end_y))),
            "--udid", udid,
            "--duration", str(duration),
        )

    async def type_text(self, udid: str, text: str) -> None:
        """Type text into focused field. Runs: idb ui text <text> --udid <udid>"""
        await self._run("ui", "text", text, "--udid", udid)

    async def press_button(self, udid: str, button: str) -> None:
        """Press a hardware button. Runs: idb ui button <BUTTON> --udid <udid>"""
        await self._run("ui", "button", button, "--udid", udid)
