"""AdbBackend — async wrapper around adb for Android device management."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path

from server.models import AppInfo, DeviceError, DeviceInfo, DeviceState, DeviceType

logger = logging.getLogger("quern-debug-server.adb")

# Short permission name → full Android permission string.
# Matches the names used by iOS simctl where possible.
_PERMISSION_MAP: dict[str, str] = {
    "camera": "android.permission.CAMERA",
    "location": "android.permission.ACCESS_FINE_LOCATION",
    "location-always": "android.permission.ACCESS_BACKGROUND_LOCATION",
    "coarse-location": "android.permission.ACCESS_COARSE_LOCATION",
    "microphone": "android.permission.RECORD_AUDIO",
    "contacts": "android.permission.READ_CONTACTS",
    "calendar": "android.permission.READ_CALENDAR",
    "photos": "android.permission.READ_MEDIA_IMAGES",
    "storage": "android.permission.READ_EXTERNAL_STORAGE",
    "phone": "android.permission.READ_PHONE_STATE",
    "sms": "android.permission.READ_SMS",
    "call-log": "android.permission.READ_CALL_LOG",
    "body-sensors": "android.permission.BODY_SENSORS",
    "nearby-devices": "android.permission.BLUETOOTH_CONNECT",
    "notifications": "android.permission.POST_NOTIFICATIONS",
}

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
        self._booting_avds: set[str] = set()  # AVD names currently being booted
        self._serial_to_avd: dict[str, str] = {}  # Cache: emulator serial → AVD name
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

    # API level → Android version (major releases)
    _API_TO_VERSION: dict[int, str] = {
        21: "5.0", 22: "5.1", 23: "6.0", 24: "7.0", 25: "7.1",
        26: "8.0", 27: "8.1", 28: "9", 29: "10", 30: "11",
        31: "12", 32: "12L", 33: "13", 34: "14", 35: "15", 36: "16",
    }

    def _read_avd_version(self, avd_name: str) -> tuple[str, str]:
        """Read OS version from an AVD's config. Returns (os_version, runtime)."""
        avd_dir = Path.home() / ".android" / "avd"

        # The AVD directory may not match the AVD name (e.g. "Medium_Phone_API_36.1"
        # maps to "Medium_Phone.avd"). Check the .ini file for the real path.
        config_path = None
        ini_path = avd_dir / f"{avd_name}.ini"
        if ini_path.exists():
            try:
                for line in ini_path.read_text().splitlines():
                    if line.startswith("path="):
                        real_dir = Path(line.split("=", 1)[1])
                        candidate = real_dir / "config.ini"
                        if candidate.exists():
                            config_path = candidate
                        break
            except Exception:
                pass

        if config_path is None:
            config_path = avd_dir / f"{avd_name}.avd" / "config.ini"

        # Fall back to parsing target from .ini if no config.ini found
        if not config_path.exists():
            if ini_path.exists():
                try:
                    for line in ini_path.read_text().splitlines():
                        if line.startswith("target=android-"):
                            api_str = line.split("android-", 1)[1]
                            return self._parse_api_string(api_str, "")
                except Exception:
                    pass
            return "", ""

        api_str = ""
        tag_id = ""
        try:
            for line in config_path.read_text().splitlines():
                if line.startswith("image.sysdir.1="):
                    for part in line.split("/"):
                        if part.startswith("android-"):
                            api_str = part.removeprefix("android-")
                elif line.startswith("tag.id="):
                    tag_id = line.split("=", 1)[1].strip()
        except Exception:
            pass

        if api_str:
            return self._parse_api_string(api_str, tag_id)
        return "", ""

    # tag.id → human-readable label
    _TAG_LABELS: dict[str, str] = {
        "google_apis": "Google APIs",
        "google_apis_playstore": "Google Play",
        "default": "AOSP",
    }

    def _parse_api_string(self, api_str: str, tag_id: str = "") -> tuple[str, str]:
        """Parse an API level string like '33' or '36.1' into (os_version, runtime)."""
        tag_label = self._TAG_LABELS.get(tag_id, "")
        try:
            api = int(api_str)
            version = self._API_TO_VERSION.get(api, api_str)
            runtime = f"API {api}"
            if tag_label:
                runtime = f"{runtime} · {tag_label}"
            return version, runtime
        except ValueError:
            try:
                api = int(api_str.split(".")[0])
                version = self._API_TO_VERSION.get(api, api_str)
                runtime = f"API {api_str}"
                if tag_label:
                    runtime = f"{runtime} · {tag_label}"
                return version, runtime
            except ValueError:
                return api_str, ""

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
        """List all Android devices and emulators.

        Combines running devices from ``adb devices -l`` with shutdown
        AVDs from ``emulator -list-avds`` so that unbooted emulators
        appear in the device list.
        """
        if not await self.is_available():
            return []

        try:
            stdout, _ = await self._run_adb("devices", "-l")
        except DeviceError:
            return []

        devices: list[DeviceInfo] = []
        running_avd_names: set[str] = set()

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

            # For emulators, always try to resolve the AVD name so we
            # can suppress the duplicate shutdown AVD entry.
            if is_emulator:
                avd_name = await self._get_emulator_name(serial)
                if avd_name and avd_name != serial:
                    name = avd_name
                    self._serial_to_avd[serial] = avd_name
                    running_avd_names.add(avd_name)
                elif serial in self._serial_to_avd:
                    # Offline/shutting down — use cached AVD name
                    name = self._serial_to_avd[serial]
                    running_avd_names.add(name)

            if is_available:
                if not is_emulator and not model:
                    model = await self._get_device_property(serial, "ro.product.model")
                    if model:
                        name = model

                os_version = await self._get_device_property(serial, "ro.build.version.release")
                api_level = await self._get_device_property(serial, "ro.build.version.sdk")

            runtime = f"API {api_level}" if api_level else ""

            # For running emulators, enrich runtime with image type from AVD config
            if is_emulator and api_level and name and name != serial:
                _, avd_runtime = self._read_avd_version(name)
                if avd_runtime and "·" in avd_runtime:
                    tag_label = avd_runtime.split("·", 1)[1].strip()
                    runtime = f"{runtime} · {tag_label}"

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

        # Clean up cache for serials no longer in adb devices
        active_serials = {d.udid for d in devices}
        for stale in list(self._serial_to_avd):
            if stale not in active_serials:
                del self._serial_to_avd[stale]

        # Add shutdown AVDs that aren't currently running or booting
        avds = await self.list_avds()
        for avd_name in avds:
            if avd_name not in running_avd_names and avd_name not in self._booting_avds:
                os_version, runtime = self._read_avd_version(avd_name)
                devices.append(DeviceInfo(
                    udid=f"avd:{avd_name}",
                    name=avd_name,
                    state=DeviceState.SHUTDOWN,
                    device_type=DeviceType.ANDROID_EMULATOR,
                    os_version=os_version,
                    runtime=runtime,
                    is_available=True,
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

    async def boot_emulator(
        self, avd_name: str, timeout: float = 60, headless: bool = False,
    ) -> str:
        """Boot an Android emulator by AVD name. Returns the adb serial.

        Launches the emulator process in the background and waits for it
        to appear as 'device' in ``adb devices``.

        If headless=True, launches with -no-window (no GUI, adb still works).
        """
        if not self._emulator_path:
            raise DeviceError("emulator command not found", tool="emulator")

        self._booting_avds.add(avd_name)
        try:
            return await self._boot_emulator_inner(avd_name, timeout, headless)
        finally:
            self._booting_avds.discard(avd_name)

    async def _boot_emulator_inner(
        self, avd_name: str, timeout: float, headless: bool,
    ) -> str:
        avds = await self.list_avds()
        if avd_name not in avds:
            raise DeviceError(
                f"AVD '{avd_name}' not found. Available: {', '.join(avds) or 'none'}",
                tool="emulator",
            )

        # Collect existing emulator serials so we can detect the new one
        existing_serials = {
            d.udid for d in await self.list_devices()
            if d.udid.startswith("emulator-")
        }

        # Launch emulator in background (detached, no window block)
        args = [self._emulator_path, "-avd", avd_name, "-no-snapshot-load"]
        if headless:
            args.append("-no-window")
        await asyncio.create_subprocess_exec(
            *args,
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
        """Launch an app's main/launcher activity."""
        # Resolve the launcher activity from the package manifest
        stdout, _ = await self._run_adb_for_device(
            serial, "shell", "cmd", "package", "resolve-activity",
            "--brief", "-a", "android.intent.action.MAIN",
            "-c", "android.intent.category.LAUNCHER",
            package,
        )
        # Output is two lines: priority/preferred line, then component (package/activity)
        lines = [ln.strip() for ln in stdout.strip().splitlines() if "/" in ln]
        if lines:
            component = lines[-1]
            await self._run_adb_for_device(
                serial, "shell", "am", "start", "-n", component,
            )
        else:
            # Fallback: let am figure it out (works on some Android versions)
            await self._run_adb_for_device(
                serial, "shell", "am", "start",
                "-a", "android.intent.action.MAIN",
                "-c", "android.intent.category.LAUNCHER",
                "-n", f"{package}/.MainActivity",
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

    async def is_screen_on(self, serial: str) -> bool:
        """Check if the device screen is on."""
        try:
            stdout, _ = await self._run_adb_for_device(serial, "shell", "dumpsys", "power")
            # Different Android versions use different keys
            for line in stdout.splitlines():
                stripped = line.strip()
                if stripped.startswith("Display Power:"):
                    return "ON" in stripped
                if stripped.startswith("mScreenOn="):
                    return "true" in stripped
        except DeviceError:
            pass
        return True  # Assume on if we can't tell

    async def wake_screen(self, serial: str) -> None:
        """Wake the device screen and dismiss the lock screen (no passcode)."""
        if await self.is_screen_on(serial):
            return
        # KEYCODE_WAKEUP (224) turns screen on without toggling
        await self._run_adb_for_device(serial, "shell", "input", "keyevent", "224")
        # Swipe up to dismiss lock screen (no passcode assumed)
        await self._run_adb_for_device(
            serial, "shell", "input", "swipe", "540", "1800", "540", "800", "300",
        )

    async def set_location(self, serial: str, latitude: float, longitude: float) -> None:
        """Set simulated GPS location on an emulator.

        Runs: adb -s <serial> emu geo fix <longitude> <latitude>
        Note: the emulator console takes longitude first, then latitude.
        """
        if not serial.startswith("emulator-"):
            raise DeviceError(
                "Location simulation is only supported on Android emulators",
                tool="adb",
            )
        await self._run_adb_for_device(
            serial, "emu", "geo", "fix",
            str(longitude), str(latitude),
        )

    async def grant_permission(self, serial: str, package: str, permission: str) -> None:
        """Grant a runtime permission to an app.

        Runs: adb -s <serial> shell pm grant <package> <permission>
        The permission can be a short name (e.g. "camera") which is mapped
        to the full Android permission string, or a full permission string.
        """
        full_permission = _PERMISSION_MAP.get(permission.lower(), permission)
        await self._run_adb_for_device(
            serial, "shell", "pm", "grant", package, full_permission,
        )

    async def set_locale(self, serial: str, lang: str, country: str = "") -> None:
        """Set the system locale via the Quern Driver broadcast receiver.

        On API ≤ 32 this works with just CHANGE_CONFIGURATION permission.
        On API 33+ rootable emulators, falls back to setprop + restart.
        On API 33+ non-rootable devices, may fail (WRITE_SETTINGS required).
        """
        # Ensure Quern Driver is installed and has permission
        await self._ensure_quern_driver_permission(
            serial, "android.permission.CHANGE_CONFIGURATION",
        )

        # Try broadcast receiver first
        args = ["--es", "lang", lang]
        if country:
            args.extend(["--es", "country", country])

        stdout, _ = await self._run_adb_for_device(
            serial, "shell", "am", "broadcast",
            "-a", "com.github.uiautomator.SET_LOCALE",
            "-n", "com.github.uiautomator/.LocaleReceiver",
            *args,
        )

        # Check if broadcast succeeded by reading logcat
        api_level = await self.get_api_level(serial)
        if api_level >= 33:
            # On API 33+, the broadcast may fail silently. Check logcat.
            log_out, _ = await self._run_adb_for_device(
                serial, "logcat", "-d", "-s", "QuernLocale", "-t", "5",
            )
            if "Locale changed successfully" in log_out:
                return
            if "Failed to set locale" in log_out:
                # Try setprop fallback for rootable emulators
                if await self.is_rootable(serial):
                    locale_tag = f"{lang}-{country}" if country else lang
                    await self._enable_root(serial)
                    await self._run_adb_for_device(
                        serial, "shell",
                        f"setprop persist.sys.locale {locale_tag}; stop; sleep 3; start",
                    )
                    return
                raise DeviceError(
                    "Locale change failed on API 33+ non-rootable device. "
                    "Use a Google APIs (dev-keys) emulator image instead.",
                    tool="adb",
                )

    async def _ensure_quern_driver_permission(self, serial: str, permission: str) -> None:
        """Grant a permission to the Quern Driver APK if installed."""
        try:
            await self._run_adb_for_device(
                serial, "shell", "pm", "grant", "com.github.uiautomator", permission,
            )
        except DeviceError:
            pass  # May already be granted or app not installed

    async def set_font_scale(self, serial: str, scale: float) -> None:
        """Set the font scale. 1.0 = default, 0.85 = small, 1.15 = large, 1.30 = largest."""
        await self._run_adb_for_device(
            serial, "shell", "settings", "put", "system", "font_scale", str(scale),
        )

    async def get_font_scale(self, serial: str) -> float:
        """Get the current font scale."""
        stdout, _ = await self._run_adb_for_device(
            serial, "shell", "settings", "get", "system", "font_scale",
        )
        try:
            return float(stdout.strip())
        except (ValueError, TypeError):
            return 1.0

    async def set_display_density(self, serial: str, dpi: int | None = None) -> None:
        """Set display density override, or reset to default if dpi is None."""
        if dpi is None:
            await self._run_adb_for_device(serial, "shell", "wm", "density", "reset")
        else:
            await self._run_adb_for_device(serial, "shell", "wm", "density", str(dpi))

    async def get_display_density(self, serial: str) -> dict:
        """Get current display density (physical and override)."""
        stdout, _ = await self._run_adb_for_device(serial, "shell", "wm", "density")
        result: dict = {}
        for line in stdout.strip().splitlines():
            if "Physical" in line:
                result["physical"] = int(line.split(":")[-1].strip())
            elif "Override" in line:
                result["override"] = int(line.split(":")[-1].strip())
        return result

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
