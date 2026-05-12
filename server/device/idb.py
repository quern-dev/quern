"""IdbBackend — async wrapper around Facebook's idb CLI for UI automation."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
from pathlib import Path

from server.device import probing
from server.models import DeviceError

logger = logging.getLogger("quern-debug-server.idb")


class IdbBackend:
    """Manages UI inspection and interaction via idb subprocess calls."""

    _QUERN_COMPANION = Path.home() / ".quern" / "bin" / "idb_companion"

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
                "(also requires: ./quern setup to install idb_companion)",
                tool="idb",
            )
        self._binary = path
        return path

    def _companion_path(self) -> Path | None:
        """Return path to the patched companion if installed."""
        return self._QUERN_COMPANION if self._QUERN_COMPANION.is_file() else None

    def _companion_env(self) -> dict[str, str]:
        """Build env with DYLD_FRAMEWORK_PATH for the patched companion."""
        bin_dir = self._QUERN_COMPANION.parent
        fw_dir = bin_dir / "Frameworks"
        env = os.environ.copy()
        env["DYLD_FRAMEWORK_PATH"] = f"{fw_dir}:{fw_dir / 'PackageFrameworks'}"
        return env

    async def is_available(self) -> bool:
        """Check if idb CLI is available."""
        return self._find_idb() is not None

    async def _run(self, *args: str) -> tuple[str, str]:
        """Run an idb command and return (stdout, stderr).

        Raises DeviceError on non-zero exit code.
        """
        import time
        binary = self._resolve_binary()
        companion = self._companion_path()

        cmd = [binary]
        if companion:
            cmd.extend(["--companion-path", str(companion)])
        cmd.extend(args)

        cmd_str = ' '.join(args[:3])  # First 3 args for logging
        start = time.perf_counter()
        logger.info(f"[PERF IDB] subprocess START: idb {cmd_str}")

        # Time the process creation
        t1 = time.perf_counter()
        kwargs: dict = dict(
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if companion:
            kwargs["env"] = self._companion_env()
        proc = await asyncio.create_subprocess_exec(*cmd, **kwargs)
        t2 = time.perf_counter()
        logger.info(f"[PERF IDB] subprocess spawned: {(t2-t1)*1000:.1f}ms")

        # Time the communication (waiting for output)
        t3 = time.perf_counter()
        stdout, stderr = await proc.communicate()
        t4 = time.perf_counter()
        logger.info(
            f"[PERF IDB] subprocess communicate: "
            f"{(t4-t3)*1000:.1f}ms, stdout={len(stdout)} bytes"
        )

        end = time.perf_counter()
        logger.info(
            f"[PERF IDB] subprocess COMPLETE: "
            f"total={(end-start)*1000:.1f}ms, "
            f"returncode={proc.returncode}"
        )

        if proc.returncode != 0:
            error_msg = stderr.decode().strip()
            # Auto-reconnect if companion is not running
            if "Connection refused" in error_msg and not getattr(self, "_reconnecting", False):
                self._reconnecting = True
                try:
                    # Extract UDID from --udid flag in args
                    udid = None
                    arg_list = list(args)
                    for i, a in enumerate(arg_list):
                        if a == "--udid" and i + 1 < len(arg_list):
                            udid = arg_list[i + 1]
                            break
                    if udid:
                        logger.info("Companion not connected — running idb connect %s", udid)
                        await self._run("connect", udid)
                        return await self._run(*args)
                finally:
                    self._reconnecting = False
            cmd = args[0] if args else "unknown"
            raise DeviceError(
                f"idb {cmd} failed: {error_msg}",
                tool="idb",
            )
        return stdout.decode(), stderr.decode()

    async def describe_all(
        self, udid: str, *,
        snapshot_depth: int | None = None,
        source_timeout: float | None = None,
    ) -> list[dict]:
        """Get all UI accessibility elements as raw dicts.

        Args:
            snapshot_depth: Ignored for idb (no depth control). Accepted for
                interface compatibility with WdaBackend.

        Runs: idb ui describe-all --udid <udid> --nested
        Uses --nested to get the full tree including children inside
        containers (nav bars, tab bars, etc.), then flattens to a list.

        Empty interactive containers (nav bars, tab bars, toolbars) are
        probed with describe-point to discover hidden child elements that
        idb's SimulatorBridge fails to enumerate.
        """
        import time
        start = time.perf_counter()
        logger.info(f"[PERF] idb.describe_all START (udid={udid[:8]})")

        # Before subprocess
        t1 = time.perf_counter()
        stdout, _ = await self._run(
            "ui", "describe-all", "--udid", udid, "--nested",
        )
        t2 = time.perf_counter()
        logger.info(f"[PERF] idb.describe_all: subprocess returned (+{(t2-t1)*1000:.1f}ms)")

        # Before JSON parse
        t3 = time.perf_counter()
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise DeviceError(
                f"Failed to parse idb describe-all output: {exc}",
                tool="idb",
            )
        t4 = time.perf_counter()
        item_count = len(data) if isinstance(data, list) else '?'
        logger.info(
            f"[PERF] idb.describe_all: JSON parsed {item_count} "
            f"items (+{(t4-t3)*1000:.1f}ms)"
        )

        if not isinstance(data, list):
            raise DeviceError(
                f"Expected JSON array from describe-all, got {type(data).__name__}",
                tool="idb",
            )

        # Find empty containers before flattening (which pops children)
        empty_containers = probing.find_empty_containers(data)

        # Before flatten
        t5 = time.perf_counter()
        flat = probing.flatten_nested(data)
        t6 = time.perf_counter()
        logger.info(
            f"[PERF] idb.describe_all: flattened to "
            f"{len(flat)} elements (+{(t6-t5)*1000:.1f}ms)"
        )

        # Probe empty containers concurrently to discover hidden children
        if empty_containers:
            t7 = time.perf_counter()
            logger.info(
                f"[PERF] idb.describe_all: probing "
                f"{len(empty_containers)} containers "
                f"(+{(t7-t6)*1000:.1f}ms)"
            )

            probe_tasks = [
                probing.probe_container(udid, c, self.describe_point)
                for c in empty_containers
            ]
            probe_results = await asyncio.gather(*probe_tasks)
            probed_elements = [el for batch in probe_results for el in batch]

            t8 = time.perf_counter()
            logger.info(
                f"[PERF] idb.describe_all: probing complete, "
                f"found {len(probed_elements)} elements "
                f"(+{(t8-t7)*1000:.1f}ms)"
            )

            probing.merge_probed_into_flat(flat, probed_elements)

        end = time.perf_counter()
        logger.info(
            f"[PERF] idb.describe_all COMPLETE: "
            f"total={(end-start)*1000:.1f}ms, elements={len(flat)}"
        )

        return flat

    async def describe_all_nested(
        self, udid: str, *, snapshot_depth: int | None = None,
    ) -> list[dict]:
        """Get all UI accessibility elements with hierarchy preserved.

        Same subprocess call as describe_all (--nested), but skips flattening
        and container probing. Returns raw nested dicts with `children` arrays
        intact.

        Args:
            snapshot_depth: Ignored for idb (no depth control). Accepted for
                interface compatibility with WdaBackend.
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
        return data

    async def describe_all_flat(
        self, udid: str, *,
        snapshot_depth: int | None = None,
        source_timeout: float | None = None,
    ) -> list[dict]:
        """Get UI elements using flat mode — designed for the custom companion.

        Uses flat format (no --nested) so the custom companion's Group-children
        fix works correctly. Deduplicates by element identity (type + label +
        identifier + frame) to handle idb's flat-mode duplicate emission.
        No probing — the companion enumerates Group children directly.

        Returns the same flat list[dict] interface as describe_all.
        """
        import time
        start = time.perf_counter()
        logger.info(f"[PERF] idb.describe_all_flat START (udid={udid[:8]})")

        stdout, _ = await self._run(
            "ui", "describe-all", "--udid", udid,
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

        # Deduplicate by full element identity — flat mode emits the same
        # element twice (parent + child traversal), but different elements
        # can legitimately share the same frame.
        seen: set[tuple] = set()
        flat: list[dict] = []
        for el in data:
            f = el.get("frame")
            frame_key = (
                int(f.get("x", 0)), int(f.get("y", 0)),
                int(f.get("width", 0)), int(f.get("height", 0)),
            ) if f else ()
            key = (
                el.get("type", ""),
                el.get("AXLabel", ""),
                el.get("AXUniqueId", el.get("identifier", "")),
                frame_key,
            )
            if key in seen:
                continue
            seen.add(key)
            flat.append(el)

        end = time.perf_counter()
        logger.info(
            f"[PERF] idb.describe_all_flat COMPLETE: "
            f"total={(end-start)*1000:.1f}ms, "
            f"raw={len(data)}, deduped={len(flat)}"
        )

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


    async def tap(self, udid: str, x: float, y: float) -> None:
        """Tap at coordinates. Runs: idb ui tap <x> <y> --duration 0.05 --udid <udid>

        Coordinates are rounded to integers as idb expects int values.
        A small explicit --duration is always passed because idb's default
        tap (no duration) fails to activate SwiftUI Toggle/Switch controls.
        """
        import time
        start = time.perf_counter()
        logger.info(f"[PERF] idb.tap START ({int(round(x))},{int(round(y))})")

        await self._run(
            "ui", "tap", str(int(round(x))), str(int(round(y))),
            "--duration", "0.05", "--udid", udid,
        )

        end = time.perf_counter()
        logger.info(f"[PERF] idb.tap COMPLETE: {(end-start)*1000:.1f}ms")

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

    async def select_all_and_delete(
        self, udid: str, x: float, y: float,
        element_type: str | None = None,
    ) -> None:
        """Select all text in focused field and delete it.

        Triple-taps at (x, y) to select all text, then presses Backspace.
        Coordinates should be the center of the focused text field.
        """
        ix, iy = str(int(round(x))), str(int(round(y)))
        # Triple-tap to select all text in the field
        for _ in range(3):
            await self._run("ui", "tap", ix, iy, "--udid", udid)
        await asyncio.sleep(0.15)
        # Delete the selection: HID Backspace=42
        await self._run("ui", "key", "42", "--udid", udid)
