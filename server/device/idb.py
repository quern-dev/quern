"""IdbBackend — async wrapper around Facebook's idb CLI for UI automation."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sys
from pathlib import Path

from server.models import DeviceError

logger = logging.getLogger("quern-debug-server.idb")

# Container role_descriptions whose children are often missing from describe-all
_PROBEABLE_ROLES = frozenset({
    "Nav bar", "Tab bar", "Toolbar", "Navigation bar",
})

# Probe interval in points — smaller than iOS minimum tap target (44pt)
_PROBE_STEP = 20


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

        Runs: idb ui describe-all --udid <udid> --nested
        Uses --nested to get the full tree including children inside
        containers (nav bars, tab bars, etc.), then flattens to a list.

        Empty interactive containers (nav bars, tab bars, toolbars) are
        probed with describe-point to discover hidden child elements that
        idb's SimulatorBridge fails to enumerate.
        """
        stdout, _ = await self._run(
            "ui", "describe-all", "--udid", udid, "--nested",
        )
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

        # Find empty containers before flattening (which pops children)
        empty_containers = self._find_empty_containers(data)

        # Flatten the tree
        flat = self._flatten_nested(data)

        # Probe empty containers concurrently to discover hidden children
        if empty_containers:
            logger.debug("Probing %d empty container(s)", len(empty_containers))
            probe_tasks = [self._probe_container(udid, c) for c in empty_containers]
            probe_results = await asyncio.gather(*probe_tasks)
            probed_elements = [el for batch in probe_results for el in batch]

            # Merge probed elements, deduplicating against existing
            if probed_elements:
                existing_frames: set[tuple[int, int, int, int]] = set()
                for item in flat:
                    f = item.get("frame")
                    if f:
                        existing_frames.add((
                            int(f.get("x", 0)), int(f.get("y", 0)),
                            int(f.get("width", 0)), int(f.get("height", 0)),
                        ))
                for el in probed_elements:
                    f = el.get("frame")
                    if f:
                        key = (
                            int(f.get("x", 0)), int(f.get("y", 0)),
                            int(f.get("width", 0)), int(f.get("height", 0)),
                        )
                        if key not in existing_frames:
                            flat.append(el)
                            existing_frames.add(key)

        return flat

    async def describe_point(self, udid: str, x: float, y: float) -> dict | None:
        """Get the UI element at specific coordinates.

        Runs: idb ui describe-point <x> <y> --udid <udid>
        Returns the element dict, or None if no element at that point.
        """
        try:
            stdout, _ = await self._run(
                "ui", "describe-point",
                str(int(round(x))), str(int(round(y))),
                "--udid", udid,
            )
            data = json.loads(stdout)
            if isinstance(data, list):
                return data[0] if data else None
            if isinstance(data, dict):
                return data
            return None
        except (DeviceError, json.JSONDecodeError):
            return None

    @staticmethod
    def _is_probeable_container(item: dict) -> bool:
        """Check if an item is an interactive container with no children."""
        children = item.get("children", [])
        if children:
            return False  # Has children, no need to probe

        role_desc = item.get("role_description", "")
        if role_desc in _PROBEABLE_ROLES:
            return True

        label = item.get("AXLabel") or ""
        if item.get("type") == "Group" and "tab bar" in label.lower():
            return True

        return False

    @staticmethod
    def _find_empty_containers(items: list[dict]) -> list[dict]:
        """Walk the nested tree and find containers that need probing."""
        containers: list[dict] = []
        for item in items:
            if IdbBackend._is_probeable_container(item):
                containers.append(item)
            children = item.get("children", [])
            if children:
                containers.extend(IdbBackend._find_empty_containers(children))
        return containers

    async def _probe_container(self, udid: str, container: dict) -> list[dict]:
        """Probe across a container to discover hidden child elements.

        Sends describe-point calls at regular intervals across the container's
        width, at its vertical center. Deduplicates results by frame position
        and filters out hits that match the container itself.
        """
        frame = container.get("frame")
        if not frame:
            return []

        x_start = float(frame.get("x", 0))
        y_center = float(frame.get("y", 0)) + float(frame.get("height", 0)) / 2
        width = float(frame.get("width", 0))

        # Generate probe X positions across the container
        probe_xs: list[float] = []
        x = x_start + _PROBE_STEP / 2  # Start half a step in
        while x < x_start + width:
            probe_xs.append(x)
            x += _PROBE_STEP

        if not probe_xs:
            return []

        # Run all probes concurrently
        tasks = [self.describe_point(udid, px, y_center) for px in probe_xs]
        results = await asyncio.gather(*tasks)

        # Deduplicate by frame position
        container_frame_key = (
            int(frame.get("x", 0)), int(frame.get("y", 0)),
            int(frame.get("width", 0)), int(frame.get("height", 0)),
        )
        seen_frames: set[tuple[int, int, int, int]] = set()
        discovered: list[dict] = []

        for element in results:
            if element is None:
                continue
            el_frame = element.get("frame")
            if not el_frame:
                continue
            frame_key = (
                int(el_frame.get("x", 0)), int(el_frame.get("y", 0)),
                int(el_frame.get("width", 0)), int(el_frame.get("height", 0)),
            )
            # Skip if it's the container itself
            if frame_key == container_frame_key:
                continue
            # Skip if already seen
            if frame_key in seen_frames:
                continue
            seen_frames.add(frame_key)
            discovered.append(element)

        return discovered

    @staticmethod
    def _flatten_nested(items: list[dict]) -> list[dict]:
        """Recursively flatten a nested idb element tree into a flat list."""
        result: list[dict] = []
        for item in items:
            children = item.pop("children", [])
            result.append(item)
            if children:
                result.extend(IdbBackend._flatten_nested(children))
        return result

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
