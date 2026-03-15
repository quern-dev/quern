"""Pmd3Backend — async wrapper around pymobiledevice3 CLI for physical device services."""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

from server.models import DeviceError

logger = logging.getLogger("quern-debug-server.pmd3")


class Pmd3Backend:
    """Manages physical iOS device operations via pymobiledevice3 subprocess calls."""

    async def is_available(self) -> bool:
        """Check if pymobiledevice3 is available."""
        from server.device.tunneld import find_pymobiledevice3_binary

        return find_pymobiledevice3_binary() is not None

    async def screenshot(self, uuid: str) -> bytes:
        """Capture a screenshot from a physical device via pymobiledevice3.

        iOS 17+: Uses tunneld for RemoteXPC tunnel, then
                 `pymobiledevice3 developer dvt screenshot --tunnel <udid>`
        iOS 16-: Falls back to usbmuxd-based connection without tunnel:
                 `pymobiledevice3 developer dvt screenshot`
        """
        from server.device.tunneld import (
            find_pymobiledevice3_binary,
            is_tunneld_running,
            resolve_tunnel_udid,
        )

        binary = find_pymobiledevice3_binary()
        if not binary:
            raise DeviceError(
                "pymobiledevice3 not found. Install: pipx install pymobiledevice3",
                tool="pymobiledevice3",
            )

        # Try tunneld route first (iOS 17+)
        tunnel_udid = None
        if await is_tunneld_running():
            tunnel_udid = await resolve_tunnel_udid(uuid)

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            if tunnel_udid:
                # iOS 17+: use tunnel
                cmd = [
                    str(binary), "developer", "dvt", "screenshot",
                    "--tunnel", tunnel_udid,
                    tmp_path,
                ]
            else:
                # iOS 16-: direct usbmuxd connection (no tunnel needed)
                # Must pass --udid to avoid interactive device prompt when
                # multiple USB devices are connected.
                cmd = [
                    str(binary), "developer", "dvt", "screenshot",
                    "--udid", uuid,
                    tmp_path,
                ]
                logger.info(
                    "No tunnel for device %s, trying direct usbmuxd connection",
                    uuid[:8],
                )

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()

            if proc.returncode != 0:
                error_msg = stderr.decode().strip()
                if "DeveloperDiskImage" in error_msg or "developer disk" in error_msg.lower():
                    raise DeviceError(
                        f"Developer disk image not mounted on device {uuid[:8]}. "
                        "Run: pymobiledevice3 mounter auto-mount",
                        tool="pymobiledevice3",
                    )
                raise DeviceError(
                    f"pymobiledevice3 screenshot failed: {error_msg}",
                    tool="pymobiledevice3",
                )

            return Path(tmp_path).read_bytes()
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    async def get_language(self, hw_udid: str) -> str:
        """Get the device language via lockdown (requires USB connection)."""
        binary = await self._get_binary()
        return await self._run_lockdown(binary, hw_udid, "language")

    async def get_locale(self, hw_udid: str) -> str:
        """Get the device locale via lockdown (requires USB connection)."""
        binary = await self._get_binary()
        return await self._run_lockdown(binary, hw_udid, "locale")

    async def set_language(self, hw_udid: str, language: str) -> None:
        """Set the device language via lockdown (requires USB connection).

        Takes effect after SpringBoard restart (device will briefly show
        a loading screen). Example values: 'en-US', 'ja-JP', 'fr-FR'.
        """
        binary = await self._get_binary()
        await self._run_lockdown(binary, hw_udid, "language", language)

    async def set_locale(self, hw_udid: str, locale: str) -> None:
        """Set the device locale via lockdown (requires USB connection).

        Takes effect after SpringBoard restart. Example values: 'en_US', 'ja_JP', 'fr_FR'.
        """
        binary = await self._get_binary()
        await self._run_lockdown(binary, hw_udid, "locale", locale)

    async def _get_binary(self) -> str:
        """Get the pymobiledevice3 binary path."""
        from server.device.tunneld import find_pymobiledevice3_binary
        binary = find_pymobiledevice3_binary()
        if not binary:
            raise DeviceError(
                "pymobiledevice3 not found. Install: pipx install pymobiledevice3",
                tool="pymobiledevice3",
            )
        return str(binary)

    async def _run_lockdown(self, binary: str, hw_udid: str, command: str, value: str | None = None) -> str:
        """Run a pymobiledevice3 lockdown command via usbmux."""
        cmd = [binary, "lockdown", command, "--udid", hw_udid]
        if value is not None:
            cmd.append(value)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            error_msg = stderr.decode().strip()
            # Extract just the error message, not the full traceback
            for line in reversed(error_msg.splitlines()):
                line = line.strip()
                if line and not line.startswith(("│", "╰", "╭", "─")):
                    error_msg = line
                    break
            raise DeviceError(
                f"pymobiledevice3 lockdown {command} failed: {error_msg}",
                tool="pymobiledevice3",
            )

        # Output is JSON-quoted string, e.g. "en-US"
        result = stdout.decode().strip().strip('"')
        return result
