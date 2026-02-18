"""DevicectlBackend — async wrapper around xcrun devicectl for physical device management."""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from pathlib import Path

from server.models import AppInfo, DeviceError, DeviceInfo, DeviceState, DeviceType

logger = logging.getLogger("quern-debug-server.devicectl")


class DevicectlBackend:
    """Manages physical iOS devices via xcrun devicectl subprocess calls."""

    def __init__(self) -> None:
        # Track launched PIDs: (uuid, bundle_id) -> PID
        self._launched_pids: dict[tuple[str, str], int] = {}

    async def _run_devicectl(
        self, *args: str, json_output: bool = False,
    ) -> tuple[str, str]:
        """Run an xcrun devicectl command and return (stdout, stderr).

        When json_output=True, uses --json-output with a temp file (devicectl
        writes JSON to a file, not stdout).

        Raises DeviceError on non-zero exit code.
        """
        cmd_args = ["xcrun", "devicectl"]

        if json_output:
            tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
            tmp_path = tmp.name
            tmp.close()
            cmd_args.extend(["--json-output", tmp_path])

        cmd_args.extend(args)

        proc = await asyncio.create_subprocess_exec(
            *cmd_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await proc.communicate()

        if proc.returncode != 0:
            if json_output:
                Path(tmp_path).unlink(missing_ok=True)
            raise DeviceError(
                f"devicectl {args[0]} failed: {stderr_bytes.decode().strip()}",
                tool="devicectl",
            )

        if json_output:
            try:
                json_data = Path(tmp_path).read_text()
            finally:
                Path(tmp_path).unlink(missing_ok=True)
            return json_data, stderr_bytes.decode()

        return stdout_bytes.decode(), stderr_bytes.decode()

    async def is_available(self) -> bool:
        """Check if xcrun devicectl is available."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "xcrun", "devicectl", "list", "devices", "--help",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            return proc.returncode == 0
        except Exception:
            return False

    async def list_devices(self) -> list[DeviceInfo]:
        """List connected physical devices by parsing devicectl list devices output."""
        try:
            stdout, _ = await self._run_devicectl(
                "list", "devices", json_output=True,
            )
        except DeviceError:
            # devicectl not available or no devices — graceful degradation
            return []

        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            logger.warning("Failed to parse devicectl JSON output")
            return []

        devices: list[DeviceInfo] = []
        result = data.get("result", {})
        device_list = result.get("devices", [])

        for dev in device_list:
            connection_props = dev.get("connectionProperties", {})
            pairing_state = connection_props.get("pairingState", "")

            # Only show paired devices
            if pairing_state != "paired":
                continue

            identifier = dev.get("identifier", "")
            name = dev.get("deviceProperties", {}).get("name", "Unknown")
            os_version_str = dev.get("deviceProperties", {}).get("osVersionNumber", "")
            connection_type = connection_props.get("transportType", "")
            tunnel_state = connection_props.get("tunnelState", "")

            # Map state: connected tunnel = booted, otherwise check bootState
            boot_state = dev.get("deviceProperties", {}).get("bootState", "")
            if tunnel_state == "connected":
                state = DeviceState.BOOTED
            elif boot_state == "booted":
                state = DeviceState.BOOTED
            else:
                state = DeviceState.SHUTDOWN

            devices.append(DeviceInfo(
                udid=identifier,
                name=name,
                state=state,
                device_type=DeviceType.DEVICE,
                os_version=f"iOS {os_version_str}" if os_version_str else "",
                connection_type=connection_type,
            ))

        return devices

    async def launch_app(self, uuid: str, bundle_id: str) -> int:
        """Launch an app on a physical device. Returns the PID."""
        stdout, _ = await self._run_devicectl(
            "device", "process", "launch",
            "--device", uuid,
            bundle_id,
            json_output=True,
        )

        try:
            data = json.loads(stdout)
            pid = data.get("result", {}).get("process", {}).get("processIdentifier", 0)
        except (json.JSONDecodeError, KeyError):
            pid = 0

        if pid:
            self._launched_pids[(uuid, bundle_id)] = pid

        return pid

    async def terminate_app(self, uuid: str, bundle_id: str) -> None:
        """Terminate an app on a physical device using stored PID."""
        pid = self._launched_pids.get((uuid, bundle_id))
        if not pid:
            raise DeviceError(
                f"No known PID for {bundle_id} on {uuid[:8]}. "
                "App must be launched via Quern to track its PID.",
                tool="devicectl",
            )

        await self._run_devicectl(
            "device", "process", "terminate",
            "--device", uuid,
            "--pid", str(pid),
        )

        self._launched_pids.pop((uuid, bundle_id), None)

    async def uninstall_app(self, uuid: str, bundle_id: str) -> None:
        """Uninstall an app from a physical device."""
        await self._run_devicectl(
            "device", "uninstall", "app",
            "--device", uuid,
            bundle_id,
        )

    async def list_apps(self, uuid: str) -> list[AppInfo]:
        """List installed apps on a physical device."""
        stdout, _ = await self._run_devicectl(
            "device", "info", "apps",
            "--device", uuid,
            json_output=True,
        )

        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            return []

        apps: list[AppInfo] = []
        app_list = data.get("result", {}).get("apps", [])

        for app in app_list:
            bundle_id = app.get("bundleIdentifier", "")
            name = app.get("name", "")
            app_type = app.get("type", "")

            apps.append(AppInfo(
                bundle_id=bundle_id,
                name=name,
                app_type=app_type,
            ))

        return apps

    async def install_app(self, uuid: str, app_path: str) -> None:
        """Install an app on a physical device."""
        await self._run_devicectl(
            "device", "install", "app",
            "--device", uuid,
            app_path,
        )

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
                cmd = [
                    str(binary), "developer", "dvt", "screenshot",
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
