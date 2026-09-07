"""Pmd3Backend — async wrapper around pymobiledevice3 CLI for physical device services."""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

from server.models import DeviceError

logger = logging.getLogger("quern-debug-server.pmd3")

# A wedge is permanent; a failed probe may not be. Re-read before acting.
WEDGE_CONFIRM_DELAY_SECONDS = 2.0


async def _recover_tunneld_if_wedged() -> bool:
    """Heal a wedged tunneld in passing, if that needs no interaction.

    Only for the *wedged* status. A stopped daemon is a different problem and
    signalling a job that is not running fixes nothing, so a broad "restart it
    and hope" would trade a clear failure for a confusing one.

    Gated on an existing passwordless grant because a screenshot is the wrong
    moment to raise an authentication dialog: the caller is usually a script,
    and a blocked prompt is indistinguishable from a hang.
    """
    from server.device.tunneld import (
        can_recover_unattended,
        recover_wedged_tunneld,
        tunneld_health,
    )

    health = await tunneld_health()
    if health.status != "wedged":
        return False

    # Ask whether we may act before spending time establishing that we should.
    # Without the grant the answer cannot change, so confirming first would
    # add seconds to a screenshot that is going to fail either way.
    if not can_recover_unattended():
        logger.warning(
            "tunneld is wedged (pid %s) and recovery needs root — run "
            "`./quern tunneld restart`, or authorise automatic recovery with "
            "`./quern tunneld grant-recovery`",
            health.pid,
        )
        return False

    # One failed probe is not a wedge. The verdict is "HTTP down while launchd
    # says running", and a probe that times out under load produces exactly
    # that reading against a daemon which is fine. The remedy is not cheap
    # enough to spend on a guess -- killing tunneld drops the live tunnels of
    # every connected device -- so require the reading to persist, and require
    # the same process to still be the one holding it.
    await asyncio.sleep(WEDGE_CONFIRM_DELAY_SECONDS)
    confirmed = await tunneld_health()
    if confirmed.status != "wedged" or confirmed.pid != health.pid:
        logger.info(
            "tunneld looked wedged but recovered on its own (status %s) — "
            "leaving it alone",
            confirmed.status,
        )
        return False

    logger.warning(
        "tunneld is wedged (pid %s) — killing it so launchd can respawn it",
        health.pid,
    )
    return await asyncio.to_thread(recover_wedged_tunneld, True)


def _no_tunnel_hint(tunnel_udid: str | None, tunneld_serving: bool) -> str:
    """Explain a failure that silently took the no-tunnel path.

    The usbmuxd fallback is right for iOS 16 and earlier and wrong for
    everything since, but there is no device version to branch on here without
    paying a subprocess per screenshot. So the fallback stays and the likely
    cause is named when it fails, rather than surfacing pymobiledevice3's
    downstream error alone.

    Naming it matters most for the wedged case: that daemon is alive, so it
    looks installed and running to anyone who checks the obvious way (#73).
    """
    if tunnel_udid is not None:
        return ""
    if not tunneld_serving:
        return (
            " — no tunnel was available because tunneld is not serving, and "
            "iOS 17+ devices need one. Check `./quern tunneld status`; if it "
            "reports a live pid, it is wedged: `./quern tunneld restart`."
        )
    return (
        " — tunneld is serving but reported no tunnel for this device, and "
        "iOS 17+ devices need one. Reconnect it and accept the trust prompt."
    )


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
        tunneld_serving = await is_tunneld_running()
        if not tunneld_serving:
            tunneld_serving = await _recover_tunneld_if_wedged()
        if tunneld_serving:
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
                if tunneld_serving:
                    logger.info(
                        "No tunnel for device %s, trying direct usbmuxd connection",
                        uuid[:8],
                    )
                else:
                    # Correct for iOS 16 and earlier, silently wrong after it,
                    # and tunneld being down is itself worth surfacing (#73).
                    logger.warning(
                        "tunneld is not serving, so device %s falls back to "
                        "usbmuxd — this only works on iOS 16 and earlier",
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
                    f"pymobiledevice3 screenshot failed: {error_msg}"
                    + _no_tunnel_hint(tunnel_udid, tunneld_serving),
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

    async def _run_lockdown(
        self, binary: str, hw_udid: str,
        command: str, value: str | None = None,
    ) -> str:
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
