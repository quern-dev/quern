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

    async def is_rootable(self, serial: str) -> bool:
        """Check if the device supports adb root (dev-keys build)."""
        tags = await self._get_device_property(serial, "ro.build.tags")
        return tags == "dev-keys"

    async def get_api_level(self, serial: str) -> int:
        """Get the device API level as an integer."""
        sdk = await self._get_device_property(serial, "ro.build.version.sdk")
        try:
            return int(sdk)
        except (ValueError, TypeError):
            return 0

    async def _enable_root(self, serial: str) -> None:
        """Enable adb root on a dev-keys device."""
        proc = await asyncio.create_subprocess_exec(
            self._adb_path, "-s", serial, "root",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        output = (stdout.decode() + stderr.decode()).strip()
        if "cannot run as root" in output or "production builds" in output:
            raise DeviceError(
                f"Cannot enable root on {serial}: {output}", tool="adb",
            )
        # adb root restarts adbd — wait for device to come back
        await asyncio.sleep(2)
        await self._run_adb("-s", serial, "wait-for-device")

    async def _get_cert_hash(self, cert_path: Path) -> str:
        """Get the Android cert hash filename (subject_hash_old)."""
        proc = await asyncio.create_subprocess_exec(
            "openssl", "x509", "-inform", "PEM",
            "-subject_hash_old", "-in", str(cert_path), "-noout",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise DeviceError(
                f"openssl failed: {stderr.decode().strip()}", tool="openssl",
            )
        return stdout.decode().strip()

    async def install_system_cert(self, serial: str, cert_path: Path) -> bool:
        """Install a CA cert into the system trust store.

        Requires a rootable (dev-keys) device. Detects API level and uses
        the appropriate technique:
        - API < 34: adb root + remount + push to /system/etc/security/cacerts/
        - API >= 34: adb root + nsenter APEX injection

        Returns True if newly installed, False if already present.
        """
        if not cert_path.exists():
            raise DeviceError(f"Cert file not found: {cert_path}", tool="adb")

        if not await self.is_rootable(serial):
            raise DeviceError(
                "Device is not rootable (requires Google APIs image, not Google Play)",
                tool="adb",
            )

        cert_hash = await self._get_cert_hash(cert_path)
        cert_filename = f"{cert_hash}.0"
        api_level = await self.get_api_level(serial)

        # Check if already installed
        if await self.is_system_cert_installed(serial, cert_filename):
            logger.info("Cert %s already installed on %s", cert_filename, serial)
            return False

        await self._enable_root(serial)

        # Push cert to temp location
        tmp_cert = f"/data/local/tmp/{cert_filename}"
        proc = await asyncio.create_subprocess_exec(
            self._adb_path, "-s", serial, "push", str(cert_path), tmp_cert,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

        if api_level >= 34:
            await self._install_cert_api34(serial, cert_filename, tmp_cert)
        else:
            await self._install_cert_remount(serial, cert_filename, tmp_cert)

        logger.info("Installed system cert %s on %s (API %d)", cert_filename, serial, api_level)
        return True

    async def _install_cert_remount(self, serial: str, cert_filename: str, tmp_cert: str) -> None:
        """Install cert via remount for API < 34."""
        await self._run_adb_for_device(serial, "remount")
        await self._run_adb_for_device(
            serial, "shell",
            f"cp {tmp_cert} /system/etc/security/cacerts/{cert_filename} && "
            f"chmod 644 /system/etc/security/cacerts/{cert_filename} && "
            f"chown root:root /system/etc/security/cacerts/{cert_filename}",
        )

    async def _install_cert_api34(self, serial: str, cert_filename: str, tmp_cert: str) -> None:
        """Install cert via nsenter APEX injection for API >= 34."""
        # Script runs on the device as root
        script = f"""set -e
# Copy existing APEX certs to temp
mkdir -p -m 700 /data/local/tmp/tmp-ca-copy
cp /apex/com.android.conscrypt/cacerts/* /data/local/tmp/tmp-ca-copy/

# Create tmpfs mount over system certs dir
mount -t tmpfs tmpfs /system/etc/security/cacerts

# Restore existing certs + add new one
mv /data/local/tmp/tmp-ca-copy/* /system/etc/security/cacerts/
cp {tmp_cert} /system/etc/security/cacerts/{cert_filename}

# Fix permissions
chown root:root /system/etc/security/cacerts/*
chmod 644 /system/etc/security/cacerts/*
chcon u:object_r:system_file:s0 /system/etc/security/cacerts/*

# Inject into Zygote mount namespaces
ZYGOTE_PID=$(pidof zygote || true)
ZYGOTE64_PID=$(pidof zygote64 || true)

for Z_PID in $ZYGOTE_PID $ZYGOTE64_PID; do
    if [ -n "$Z_PID" ]; then
        nsenter --mount=/proc/$Z_PID/ns/mnt -- \\
            /bin/mount --bind /system/etc/security/cacerts \\
            /apex/com.android.conscrypt/cacerts
    fi
done

# Inject into all running app processes
echo "$ZYGOTE_PID $ZYGOTE64_PID" | \\
    xargs -n1 ps -o PID -P 2>/dev/null | grep -v PID | while read PID; do
        nsenter --mount=/proc/$PID/ns/mnt -- \\
            /bin/mount --bind /system/etc/security/cacerts \\
            /apex/com.android.conscrypt/cacerts 2>/dev/null || true
    done

# Cleanup
rm -f {tmp_cert}
rm -rf /data/local/tmp/tmp-ca-copy
"""
        await self._run_adb_for_device(serial, "shell", script)

    async def is_system_cert_installed(self, serial: str, cert_filename: str) -> bool:
        """Check if a cert file exists in the system cert store."""
        try:
            # Check both the classic and APEX locations
            await self._run_adb_for_device(
                serial, "shell",
                f"test -f /system/etc/security/cacerts/{cert_filename} || "
                f"test -f /apex/com.android.conscrypt/cacerts/{cert_filename}",
            )
            return True
        except DeviceError:
            return False

    async def set_http_proxy(self, serial: str, host: str, port: int) -> None:
        """Set the global HTTP proxy on the device."""
        await self._run_adb_for_device(
            serial, "shell", "settings", "put", "global",
            "http_proxy", f"{host}:{port}",
        )
        logger.info("Set HTTP proxy on %s to %s:%d", serial, host, port)

    async def screenshot(self, serial: str) -> bytes:
        """Capture a screenshot as PNG bytes."""
        if not self._adb_path:
            raise DeviceError("adb not found", tool="adb")
        proc = await asyncio.create_subprocess_exec(
            self._adb_path, "-s", serial, "exec-out", "screencap", "-p",
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
