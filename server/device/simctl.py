"""SimctlBackend — async wrapper around xcrun simctl for simulator management."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import tempfile
from pathlib import Path

from server.models import AppInfo, DeviceError, DeviceInfo, DeviceState, DeviceType

logger = logging.getLogger("quern-debug-server.simctl")


class SimctlBackend:
    """Manages iOS simulators via xcrun simctl subprocess calls."""

    async def _run_simctl(self, *args: str) -> tuple[str, str]:
        """Run an xcrun simctl command and return (stdout, stderr).

        Raises DeviceError on non-zero exit code.
        """
        proc = await asyncio.create_subprocess_exec(
            "xcrun", "simctl", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise DeviceError(
                f"simctl {args[0]} failed: {stderr.decode().strip()}",
                tool="simctl",
            )
        return stdout.decode(), stderr.decode()

    async def _run_shell(self, cmd: str) -> tuple[str, str]:
        """Run a shell pipeline and return (stdout, stderr).

        Used for commands that require piping (e.g. listapps | plutil).
        Raises DeviceError on non-zero exit code.
        """
        proc = await asyncio.create_subprocess_exec(
            "sh", "-c", cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise DeviceError(
                f"shell command failed: {stderr.decode().strip()}",
                tool="simctl",
            )
        return stdout.decode(), stderr.decode()

    async def is_available(self) -> bool:
        """Check if xcrun simctl is available."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "which", "xcrun",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            return proc.returncode == 0
        except Exception:
            return False

    async def list_devices(self) -> list[DeviceInfo]:
        """List all simulators by parsing simctl list devices --json."""
        stdout, _ = await self._run_simctl("list", "devices", "--json")
        data = json.loads(stdout)
        devices: list[DeviceInfo] = []

        for runtime_key, device_list in data.get("devices", {}).items():
            os_version = self._parse_runtime(runtime_key)
            for dev in device_list:
                if not dev.get("isAvailable", False):
                    continue
                state_str = dev.get("state", "Shutdown").lower()
                try:
                    state = DeviceState(state_str)
                except ValueError:
                    state = DeviceState.SHUTDOWN

                device_type_id = dev.get("deviceTypeIdentifier", "")
                device_family = self._parse_device_family(device_type_id)

                devices.append(DeviceInfo(
                    udid=dev["udid"],
                    name=dev["name"],
                    state=state,
                    device_type=DeviceType.SIMULATOR,
                    os_version=os_version,
                    runtime=runtime_key,
                    is_available=True,
                    device_family=device_family,
                ))

        return devices

    @staticmethod
    def _parse_runtime(runtime_key: str) -> str:
        """Extract human-readable OS version from a runtime identifier.

        e.g. 'com.apple.CoreSimulator.SimRuntime.iOS-18-6' -> 'iOS 18.6'
        """
        match = re.search(r"SimRuntime\.(.+)$", runtime_key)
        if not match:
            return runtime_key
        raw = match.group(1)  # e.g. 'iOS-18-6'
        parts = raw.split("-", 1)
        if len(parts) == 2:
            return f"{parts[0]} {parts[1].replace('-', '.')}"
        return raw

    @staticmethod
    def _parse_device_family(device_type_identifier: str) -> str:
        """Extract device family from a deviceTypeIdentifier.

        e.g. 'com.apple.CoreSimulator.SimDeviceType.iPhone-16-Pro' -> 'iPhone'
             'com.apple.CoreSimulator.SimDeviceType.iPad-Pro-13-inch-M4' -> 'iPad'
             'com.apple.CoreSimulator.SimDeviceType.Apple-Watch-Series-10-46mm' -> 'Apple Watch'
             'com.apple.CoreSimulator.SimDeviceType.Apple-TV-4K-3rd-generation-4K' -> 'Apple TV'
        """
        # Extract the part after SimDeviceType.
        match = re.search(r"SimDeviceType\.(.+)$", device_type_identifier)
        if not match:
            return ""
        name = match.group(1)  # e.g. 'iPhone-16-Pro'
        if name.startswith("iPhone"):
            return "iPhone"
        if name.startswith("iPad"):
            return "iPad"
        if name.startswith("Apple-Watch"):
            return "Apple Watch"
        if name.startswith("Apple-TV"):
            return "Apple TV"
        return ""

    async def boot(self, udid: str) -> None:
        """Boot a simulator."""
        await self._run_simctl("boot", udid)

    async def shutdown(self, udid: str) -> None:
        """Shutdown a simulator."""
        await self._run_simctl("shutdown", udid)

    async def install_app(self, udid: str, app_path: str) -> None:
        """Install an app on a simulator."""
        await self._run_simctl("install", udid, app_path)

    async def launch_app(self, udid: str, bundle_id: str) -> None:
        """Launch an app on a simulator."""
        await self._run_simctl("launch", udid, bundle_id)

    async def terminate_app(self, udid: str, bundle_id: str) -> None:
        """Terminate an app on a simulator."""
        await self._run_simctl("terminate", udid, bundle_id)

    async def uninstall_app(self, udid: str, bundle_id: str) -> None:
        """Uninstall an app from a simulator."""
        await self._run_simctl("uninstall", udid, bundle_id)

    async def list_apps(self, udid: str) -> list[AppInfo]:
        """List installed apps on a simulator.

        simctl listapps outputs NeXT-style plist, so we pipe through plutil
        to convert to JSON.
        """
        cmd = f"xcrun simctl listapps {udid} | plutil -convert json -o - -- -"
        stdout, _ = await self._run_shell(cmd)
        data = json.loads(stdout)

        apps: list[AppInfo] = []
        for bundle_id, info in data.items():
            apps.append(AppInfo(
                bundle_id=bundle_id,
                name=info.get("CFBundleDisplayName") or info.get("CFBundleName", ""),
                app_type=info.get("ApplicationType", ""),
            ))
        return apps

    async def set_location(self, udid: str, latitude: float, longitude: float) -> None:
        """Set the simulated GPS location.

        Runs: xcrun simctl location <udid> set <lat>,<lon>
        """
        await self._run_simctl("location", udid, "set", f"{latitude},{longitude}")

    async def grant_permission(self, udid: str, bundle_id: str, permission: str) -> None:
        """Grant an app permission.

        Runs: xcrun simctl privacy <udid> grant <permission> <bundle_id>
        """
        await self._run_simctl("privacy", udid, "grant", permission, bundle_id)

    async def clear_app_data(self, udid: str, bundle_id: str) -> None:
        """Delete all contents of the app's data container (Documents, Library, tmp, etc.)."""
        stdout, _ = await self._run_simctl("get_app_container", udid, bundle_id, "data")
        container = Path(stdout.strip())
        if not container.exists():
            raise DeviceError(f"App data container not found for {bundle_id}", tool="simctl")
        for child in container.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()

    async def screenshot(self, udid: str) -> bytes:
        """Capture a screenshot from a simulator.

        Writes to a temp file, reads bytes, then cleans up.
        """
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            await self._run_simctl("io", udid, "screenshot", tmp_path)
            return Path(tmp_path).read_bytes()
        finally:
            Path(tmp_path).unlink(missing_ok=True)
