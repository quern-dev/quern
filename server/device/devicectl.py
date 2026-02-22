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
            raw_transport = connection_props.get("transportType", "")
            connection_type = "usb" if raw_transport == "wired" else raw_transport
            tunnel_state = connection_props.get("tunnelState", "")
            device_type_str = dev.get("hardwareProperties", {}).get("deviceType", "")

            # Map state: any active/reachable tunnel or explicit bootState = booted.
            # tunnelState values: "connected" (active tunnel), "disconnected"
            # (reachable but no tunnel yet — e.g. WiFi devices), "unavailable".
            boot_state = dev.get("deviceProperties", {}).get("bootState", "")
            if tunnel_state in ("connected", "disconnected"):
                state = DeviceState.BOOTED
            elif boot_state == "booted":
                state = DeviceState.BOOTED
            else:
                state = DeviceState.SHUTDOWN

            # Device is reachable if tunnel is anything other than "unavailable"
            # "connected" = active tunnel, "disconnected" = reachable but no tunnel yet
            is_connected = tunnel_state != "unavailable"

            # Skip unreachable devices — they can't be used for anything
            if not is_connected:
                continue

            devices.append(DeviceInfo(
                udid=identifier,
                name=name,
                state=state,
                device_type=DeviceType.DEVICE,
                os_version=f"iOS {os_version_str}" if os_version_str else "",
                connection_type=connection_type,
                device_family=device_type_str,  # Already "iPhone", "iPad", etc.
                is_connected=is_connected,
            ))

        return devices

    async def launch_app(self, uuid: str, bundle_id: str) -> int:
        """Launch an app on a physical device. Returns the PID."""
        try:
            stdout, _ = await self._run_devicectl(
                "device", "process", "launch",
                "--device", uuid,
                bundle_id,
                json_output=True,
            )
        except DeviceError as e:
            msg = str(e)
            if "app not found" in msg.lower() or "no such" in msg.lower() or "unable to find" in msg.lower():
                raise DeviceError(
                    f"Failed to launch {bundle_id} on physical device {uuid[:8]}: app is not installed. "
                    f"Install it first with install_app, or use device_type='simulator' in "
                    f"resolve_device/ensure_devices to target simulators instead.",
                    tool="devicectl",
                )
            raise DeviceError(
                f"Failed to launch {bundle_id} on physical device {uuid[:8]}: {msg}. "
                f"Physical devices require the app to be signed and installed. "
                f"If you intended to use a simulator, pass device_type='simulator' to "
                f"resolve_device or ensure_devices.",
                tool="devicectl",
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
