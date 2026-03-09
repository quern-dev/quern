"""AdbBackend — async wrapper around adb for Android device management."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path

from server.models import AppInfo, DeviceError, DeviceInfo, DeviceState, DeviceType

logger = logging.getLogger("quern-debug-server.adb")

# Well-known Android SDK locations (macOS / Linux)
_SDK_SEARCH_PATHS = [
    Path.home() / "Library" / "Android" / "sdk",   # Android Studio default (macOS)
    Path.home() / "Android" / "Sdk",                # Android Studio default (Linux)
]


def _find_sdk_tool(name: str, subdir: str = "platform-tools") -> str | None:
    """Find an Android SDK tool on PATH or in well-known SDK locations."""
    found = shutil.which(name)
    if found:
        return found
    # Check ANDROID_HOME / ANDROID_SDK_ROOT
    for env_var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        sdk = os.environ.get(env_var)
        if sdk:
            candidate = Path(sdk) / subdir / name
            if candidate.is_file():
                return str(candidate)
    # Check well-known paths
    for sdk_path in _SDK_SEARCH_PATHS:
        candidate = sdk_path / subdir / name
        if candidate.is_file():
            return str(candidate)
    return None


class AdbBackend:
    """Manages Android devices and emulators via adb subprocess calls."""

    def __init__(self) -> None:
        self._adb_path: str | None = _find_sdk_tool("adb")
        self._emulator_path: str | None = _find_sdk_tool("emulator", "emulator")
        if self._adb_path:
            logger.info("adb found at %s", self._adb_path)
        if self._emulator_path:
            logger.info("emulator found at %s", self._emulator_path)

    async def _run_adb(self, *args: str) -> tuple[str, str]:
        """Run an adb command and return (stdout, stderr).

        Raises DeviceError on non-zero exit code.
        """
        if not self._adb_path:
            raise DeviceError("adb not found", tool="adb")
        proc = await asyncio.create_subprocess_exec(
            self._adb_path, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise DeviceError(
                f"adb {args[0]} failed: {stderr.decode().strip()}",
                tool="adb",
            )
        return stdout.decode(), stderr.decode()

    async def _run_adb_for_device(self, serial: str, *args: str) -> tuple[str, str]:
        """Run an adb command targeting a specific device."""
        return await self._run_adb("-s", serial, *args)

    async def is_available(self) -> bool:
        """Check if adb is available."""
        return self._adb_path is not None

    async def _get_device_property(self, serial: str, prop: str) -> str:
        """Get a single device property via getprop."""
        try:
            stdout, _ = await self._run_adb_for_device(serial, "shell", "getprop", prop)
            return stdout.strip()
        except DeviceError:
            return ""

    async def _get_emulator_name(self, serial: str) -> str:
        """Get the AVD name for an emulator."""
        try:
            stdout, _ = await self._run_adb_for_device(serial, "emu", "avd", "name")
            # First line is the AVD name, second may be "OK"
            lines = stdout.strip().splitlines()
            return lines[0].strip() if lines else serial
        except DeviceError:
            return serial

    async def list_devices(self) -> list[DeviceInfo]:
        """List all Android devices by parsing `adb devices -l`."""
        if not await self.is_available():
            return []

        try:
            stdout, _ = await self._run_adb("devices", "-l")
        except DeviceError:
            return []

        devices: list[DeviceInfo] = []
        for line in stdout.strip().splitlines()[1:]:  # Skip header
            line = line.strip()
            if not line:
                continue

            parts = line.split()
            if len(parts) < 2:
                continue

            serial = parts[0]
            status = parts[1]

            # Determine state
            if status == "device":
                state = DeviceState.BOOTED
                is_available = True
            elif status == "unauthorized":
                state = DeviceState.UNAUTHORIZED
                is_available = False
            elif status == "offline":
                state = DeviceState.SHUTDOWN
                is_available = False
            else:
                state = DeviceState.SHUTDOWN
                is_available = False

            # Determine device type
            is_emulator = serial.startswith("emulator-")
            device_type = DeviceType.ANDROID_EMULATOR if is_emulator else DeviceType.ANDROID_DEVICE

            # Extract model from the -l output (e.g. model:Pixel_7)
            model = ""
            for part in parts[2:]:
                if part.startswith("model:"):
                    model = part.split(":", 1)[1].replace("_", " ")
                    break

            # Get more details for online devices
            name = model or serial
            os_version = ""
            api_level = ""

            if is_available:
                if is_emulator:
                    avd_name = await self._get_emulator_name(serial)
                    if avd_name and avd_name != serial:
                        name = avd_name
                elif not model:
                    model = await self._get_device_property(serial, "ro.product.model")
                    if model:
                        name = model

                os_version = await self._get_device_property(serial, "ro.build.version.release")
                api_level = await self._get_device_property(serial, "ro.build.version.sdk")

            runtime = f"API {api_level}" if api_level else ""

            devices.append(DeviceInfo(
                udid=serial,
                name=name,
                state=state,
                device_type=device_type,
                os_version=os_version,
                runtime=runtime,
                is_available=is_available,
                device_family="Android",
            ))

        return devices

    async def list_avds(self) -> list[str]:
        """List available AVD names via the emulator command."""
        if not self._emulator_path:
            return []
        try:
            proc = await asyncio.create_subprocess_exec(
                self._emulator_path, "-list-avds",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            return [line.strip() for line in stdout.decode().strip().splitlines() if line.strip()]
        except Exception:
            return []

    async def boot_emulator(self, avd_name: str, timeout: float = 60) -> str:
        """Boot an Android emulator by AVD name. Returns the adb serial.

        Launches the emulator process in the background and waits for it
        to appear as 'device' in ``adb devices``.
        """
        if not self._emulator_path:
            raise DeviceError("emulator command not found", tool="emulator")

        avds = await self.list_avds()
        if avd_name not in avds:
            raise DeviceError(
                f"AVD '{avd_name}' not found. Available: {', '.join(avds) or 'none'}",
                tool="emulator",
            )

        # Collect existing emulator serials so we can detect the new one
        existing_serials = {d.udid for d in await self.list_devices() if d.udid.startswith("emulator-")}

        # Launch emulator in background (detached, no window block)
        await asyncio.create_subprocess_exec(
            self._emulator_path, "-avd", avd_name, "-no-snapshot-load",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        # Wait for new emulator serial to appear and become ready
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(2)
            devices = await self.list_devices()
            for d in devices:
                if (
                    d.udid.startswith("emulator-")
                    and d.udid not in existing_serials
                    and d.state == DeviceState.BOOTED
                ):
                    logger.info("Android emulator booted: %s (AVD: %s)", d.udid, avd_name)
                    return d.udid

        raise DeviceError(
            f"Timed out waiting for emulator '{avd_name}' to boot after {timeout}s",
            tool="emulator",
        )

    async def install_app(self, serial: str, apk_path: str) -> None:
        """Install an APK on a device."""
        await self._run_adb_for_device(serial, "install", "-r", apk_path)

    async def launch_app(self, serial: str, package: str) -> None:
        """Launch an app using monkey (no need to know the activity class)."""
        await self._run_adb_for_device(
            serial, "shell", "monkey",
            "-p", package,
            "-c", "android.intent.category.LAUNCHER",
            "1",
        )

    async def terminate_app(self, serial: str, package: str) -> None:
        """Force-stop an app."""
        await self._run_adb_for_device(serial, "shell", "am", "force-stop", package)

    async def uninstall_app(self, serial: str, package: str) -> None:
        """Uninstall an app."""
        await self._run_adb_for_device(serial, "uninstall", package)

    async def list_apps(self, serial: str) -> list[AppInfo]:
        """List third-party installed apps."""
        stdout, _ = await self._run_adb_for_device(serial, "shell", "pm", "list", "packages", "-3")
        apps: list[AppInfo] = []
        for line in stdout.strip().splitlines():
            line = line.strip()
            if line.startswith("package:"):
                package = line[len("package:"):]
                apps.append(AppInfo(
                    bundle_id=package,
                    name=package,
                    app_type="User",
                ))
        return apps

    async def screenshot(self, serial: str) -> bytes:
        """Capture a screenshot as PNG bytes."""
        proc = await asyncio.create_subprocess_exec(
            "adb", "-s", serial, "exec-out", "screencap", "-p",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise DeviceError(
                f"adb screencap failed: {stderr.decode().strip()}",
                tool="adb",
            )
        return stdout
