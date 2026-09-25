"""AdbBackend — async wrapper around adb for Android device management."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path

from server.device.tool_probe import probe_command
from server.models import AppInfo, DeviceError, DeviceInfo, DeviceState, DeviceType

logger = logging.getLogger(__name__)

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


#: Failures `am start` reports in its output while still exiting 0.
#:
#: "Activity not started" covers an unresolvable intent; "does not exist" is
#: what an explicit package that is not installed produces. Matched against
#: stdout and stderr together, because which stream carries it varies by
#: Android version.
#: The subset that really does mean "nothing can open this".
#:
#: Separate from the detection list because `Error: Activity not started` is
#: also how Android reports a resolved activity that refused to launch, and
#: "no app handled it" is the wrong thing to tell someone in that case.
_AM_START_UNRESOLVED = (
    "unable to resolve Intent",
    "Error: Activity class",
)

_AM_START_FAILURES = (
    "Error: Activity not started",
    "unable to resolve Intent",
    # Anchored to the `Error:` line on purpose. `am start` echoes the URL back
    # in its `Starting: Intent { ... dat=<url> }` line, so a bare "does not
    # exist" could be matched out of a URL on a successful launch. Measured on a
    # Pixel 3 XL (Android 10), the missing-package case actually reports
    # "unable to resolve Intent" and this spelling never appeared -- it is kept
    # for the versions that do emit it, which is all the more reason not to let
    # it match loosely.
    "Error: Activity class",
)


def _is_wifi_inet_line(line: str) -> bool:
    """Whether an `ip -4 -o addr show` line is a Wi-Fi interface with an address.

    The name is the test, and it has to be: a phone that dropped off Wi-Fi and
    fell back to cellular still has an address, on `rmnet_data0`. Matching any
    non-loopback interface would call that a successful reattach.

    The *presence of an address* is the other half, and it is sufficient here
    rather than merely convenient. Measured on an LG H932 and a Pixel 3 XL:
    `svc wifi disable` releases the address, and `ip -4 -o addr show` then
    prints no `wlan` line at all -- a downed interface does not sit there
    holding a stale IPv4. The Pixel showed the same from the other direction,
    `<NO-CARRIER,...> state DOWN` with no `inet` to its name.

    Carrier state would be the more direct question, and it is not available to
    ask. On the same unrooted phone `ip link show wlan0`,
    `/sys/class/net/wlan0/carrier` and `.../operstate` all return permission
    denied to the adb shell user, and unrooted physical phones are the entire
    point of this feature. A check that cannot run on the target hardware is
    not a stronger check.

    Format: `30: wlan0    inet 192.168.1.244/24 brd 192.168.1.255 scope global`
    """
    parts = line.split()
    if len(parts) < 3 or "inet" not in parts:
        return False
    name = parts[1].rstrip(":")
    return name.startswith("wlan") and "inet" in parts


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

    def is_installed(self) -> bool:
        """Whether adb is on disk. Cheap, and says nothing about health.

        Split from `is_available` because the two questions have different
        callers. Control flow -- "should I try the Android path at all?" --
        wants this: it runs on hot paths, and the command it guards will fail
        on its own terms if the binary is broken. Reporting wants the probe.
        """
        return self._adb_path is not None

    async def is_available(self) -> bool:
        """Check that adb is installed *and* answers.

        Was a path lookup, which reports a corrupt or half-installed binary as
        healthy -- the shape #181 exists to fix, and one this cannot express
        either, but it can at least stop claiming a tool works without ever
        asking it.
        """
        if not self.is_installed():
            return False
        return await probe_command(str(self._adb_path), "version", tool="adb")

    async def _get_device_property(self, serial: str, prop: str) -> str:
        """Get a single device property via getprop."""
        try:
            stdout, _ = await self._run_adb_for_device(serial, "shell", "getprop", prop)
            return stdout.strip()
        except DeviceError:
            return ""

    async def get_device_properties(self, serial: str) -> dict[str, str]:
        """Every `getprop` key at once, as a dict.

        One `adb shell getprop` costs less than the three single-property
        reads it replaces -- measured at 0.032s for all 924 properties on a
        booted emulator against 0.049s for three individual calls -- because
        the cost is the adb round trip, not the property lookup.

        Returns an empty dict rather than raising: a device that cannot be
        shelled (offline, unauthorized, mid-boot) has no properties to report,
        and that is a normal state during enumeration rather than an error.
        """
        try:
            stdout, _ = await self._run_adb_for_device(serial, "shell", "getprop")
        except Exception:
            logger.debug("Could not read properties from %s", serial, exc_info=True)
            return {}
        props: dict[str, str] = {}
        for line in (stdout or "").splitlines():
            # `[ro.build.tags]: [dev-keys]`
            if not line.startswith("[") or "]: [" not in line:
                continue
            key, _, rest = line[1:].partition("]: [")
            props[key] = rest.rstrip("]")
        return props

    @staticmethod
    def classify_from_properties(props: dict[str, str]) -> DeviceType | None:
        """Emulator or physical device, decided by what the device says it is.

        The serial is a *transport address*, not a property of the device:
        `emulator-5554` means "reached via the local emulator console on port
        5554" and `localhost:5555` means "reached over TCP". Neither says what
        the thing on the other end is, and classifying on the prefix made one
        emulator answer two different types depending on which serial you used
        (#264, #299).

        Measured on a Pixel_6_Dev AVD reachable both ways at once -- every one
        of these properties is byte-identical across the two transports, which
        is the point:

            ro.kernel.qemu             1                   1
            ro.hardware                ranchu              ranchu
            ro.build.characteristics   emulator            emulator
            ro.product.model           sdk_gphone64_arm64  sdk_gphone64_arm64

        and on a physical LG H932 all four are absent or unremarkable
        (`ro.hardware=joan`, `ro.product.model=LG-H932`, no qemu keys).

        Returns None when the device could not be asked, which the caller must
        distinguish from an answer -- guessing here is what #263 is about.
        """
        if not props:
            return None
        if props.get("ro.kernel.qemu") == "1" or props.get("ro.boot.qemu") == "1":
            return DeviceType.ANDROID_EMULATOR
        # `ranchu` is the modern emulator machine type, `goldfish` the older
        # one. Both are emulator-only and neither appears on a phone.
        if props.get("ro.hardware", "") in ("ranchu", "goldfish"):
            return DeviceType.ANDROID_EMULATOR
        if "emulator" in props.get("ro.build.characteristics", ""):
            return DeviceType.ANDROID_EMULATOR
        if props.get("ro.product.model", "").startswith("sdk_"):
            return DeviceType.ANDROID_EMULATOR
        return DeviceType.ANDROID_DEVICE

    @staticmethod
    def is_console_serial(serial: str) -> bool:
        """Whether this serial is the local emulator-console address.

        The one thing the `emulator-` prefix genuinely does tell you. It is a
        fact about the *connection*, and using it to decide `emu` routing is
        correct in a way that using it to decide what the device *is* never
        was: `emulator-5554` means "reached through the console on port 5554",
        which is precisely the question `adb emu` cares about.

        Cheap counterpart to `has_emulator_console`, which actually asks. Use
        this to route, that to verify.
        """
        return serial.startswith("emulator-")

    async def has_emulator_console(self, serial: str) -> bool:
        """Whether `adb emu` commands work on *this serial*.

        Deliberately a question about the transport rather than the device,
        and the reason one `DeviceType` could never be right for both: the
        emulator console is reachable only through the local `emulator-N`
        serial. The same AVD reached over TCP is still an emulator in every
        respect that matters for a certificate, and genuinely cannot answer
        `adb emu kill` or `adb emu geo fix` under that serial.

        Measured on one AVD, both transports live at once:

            emulator-5554    adb emu avd name -> "Pixel_6_Dev"
            localhost:5555   adb emu avd name -> (empty)

        So `set_location` and shutdown-by-console ask this, while certificate
        installation asks `is_rootable`, and they are allowed to disagree.
        """
        try:
            stdout, _ = await self._run_adb_for_device(serial, "emu", "avd", "name")
        except Exception:
            return False
        first = (stdout or "").strip().splitlines()[:1]
        return bool(first and first[0].strip() and "error" not in first[0].lower())

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
        if not self.is_installed():
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

            # Ask the device what it is, rather than reading it off the
            # transport address. `emulator-5554` and `localhost:5555` can be
            # the same running AVD, and the prefix test called them different
            # kinds (#264). Properties do not vary by transport; measured
            # byte-identical across both serials of one emulator.
            props = await self.get_device_properties(serial) if is_available else {}
            device_type = self.classify_from_properties(props)
            if device_type is None:
                # Offline, unauthorized, or mid-boot: there is no shell to ask,
                # so the address is the only evidence left. Kept explicitly as
                # a last resort rather than as the rule, and only reachable for
                # a device that cannot be used for anything yet anyway.
                device_type = (
                    DeviceType.ANDROID_EMULATOR if serial.startswith("emulator-")
                    else DeviceType.ANDROID_DEVICE
                )
            is_emulator = device_type == DeviceType.ANDROID_EMULATOR

            # A *transport* question, deliberately asked of the serial: the
            # emulator console answers only through the local `emulator-N`
            # address, so the same AVD over TCP has no AVD name to fetch even
            # though it is every bit an emulator.
            has_console = serial.startswith("emulator-")

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
            if has_console:
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
                # From the bulk read above rather than three more round trips.
                if not is_emulator and not model:
                    model = props.get("ro.product.model", "")
                    if model:
                        name = model

                os_version = props.get("ro.build.version.release", "")
                api_level = props.get("ro.build.version.sdk", "")

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
        """Whether `adb root` can succeed on this device.

        A property of the *device*, not of how it is reached, and not of
        whether the serial starts with `emulator-`. The cert gate used to ask
        the type instead, which made this wrong on four of the eight rows in
        #299's matrix: a Google Play emulator was offered a system-cert
        install it could never complete, while a genuinely rootable dev-keys
        emulator was refused one purely for being reached over TCP.

        Measured on that dev-keys AVD through both serials at once --
        `adb root` then `id` returned `uid=0(root)` on *both*, so the refusal
        was a false negative about the device, produced by a fact about the
        wire.

        `ro.debuggable` joins `ro.build.tags` here because either is
        sufficient: a userdebug build reports `release-keys` yet still permits
        `adb root`, so testing tags alone under-reports.
        """
        props = await self.get_device_properties(serial)
        if props:
            return props.get("ro.build.tags") == "dev-keys" or props.get("ro.debuggable") == "1"
        # Could not read properties at all. Fall back to the single-property
        # path rather than reporting "not rootable", which would be an answer
        # rather than the absence of one.
        tags = await self._get_device_property(serial, "ro.build.tags")
        debuggable = await self._get_device_property(serial, "ro.debuggable")
        return tags == "dev-keys" or debuggable == "1"

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
            try:
                await self._install_cert_remount(serial, cert_filename, tmp_cert)
            except DeviceError:
                logger.info(
                    "Remount failed on %s (API %d), falling back to tmpfs overlay",
                    serial, api_level,
                )
                await self._install_cert_tmpfs(serial, cert_filename, tmp_cert)

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

    async def _install_cert_tmpfs(self, serial: str, cert_filename: str, tmp_cert: str) -> None:
        """Install cert via tmpfs overlay for API < 34 when remount fails.

        Copies existing certs from /system/etc/security/cacerts/ into a tmpfs
        mounted over the same path. No APEX or zygote nsenter needed — pre-34
        Android reads certs directly from /system/etc/security/cacerts/.
        """
        script = f"""set -e
# Copy existing system certs to temp
mkdir -p -m 700 /data/local/tmp/tmp-ca-copy
cp /system/etc/security/cacerts/* /data/local/tmp/tmp-ca-copy/

# Mount tmpfs over the certs directory
mount -t tmpfs tmpfs /system/etc/security/cacerts

# Restore existing certs + add new one
mv /data/local/tmp/tmp-ca-copy/* /system/etc/security/cacerts/
cp {tmp_cert} /system/etc/security/cacerts/{cert_filename}

# Fix permissions
chown root:root /system/etc/security/cacerts/*
chmod 644 /system/etc/security/cacerts/*
chcon u:object_r:system_file:s0 /system/etc/security/cacerts/*

# Cleanup
rm -f {tmp_cert}
rm -rf /data/local/tmp/tmp-ca-copy
"""
        await self._run_adb_for_device(serial, "shell", script)

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
        """Set the global HTTP proxy on the device.

        Nothing here is emulator-specific: this is `settings put global` on any
        Android device, rooted or not, over USB or TCP. The gate that used to
        restrict it to `ANDROID_EMULATOR` was withholding a universal
        capability -- and the one genuinely emulator-specific detail, the
        `10.0.2.2` address, lived inside the block it guarded.

        **The setting is read when the network attaches**, so on its own this
        does nothing to a device that is already connected. Measured on a
        physical phone: setting it and then browsing produced zero proxied
        requests; the same after `reattach_network()` produced twenty. Callers
        that want it to take effect must reattach.
        """
        await self._run_adb_for_device(
            serial, "shell", "settings", "put", "global",
            "http_proxy", f"{host}:{port}",
        )
        logger.info("Set HTTP proxy on %s to %s:%d", serial, host, port)

    async def clear_http_proxy(self, serial: str) -> None:
        """Remove the global HTTP proxy.

        `settings delete` rather than writing `:0`. The written-sentinel form
        leaves a row saying "proxy: none", which reads to anyone inspecting the
        device as a deliberate configuration rather than an absence -- and it
        is what `settings get` returns instead of `null`, so quern could not
        tell a cleared device from one that had never been set.

        This is the half #265 records as missing entirely: the setting lives in
        the global settings provider and survives reboots, so without an unset
        it is the device's configuration until something else changes it.
        """
        await self._run_adb_for_device(
            serial, "shell", "settings", "delete", "global", "http_proxy",
        )
        logger.info("Cleared HTTP proxy on %s", serial)

    async def get_http_proxy(self, serial: str) -> str | None:
        """What the device's global proxy is set to, or None.

        `settings get` prints the string `null` for an unset key, which is not
        the same as the empty output a failed read gives. Both are reported as
        None here, deliberately: a caller wanting to know whether the *read*
        worked should catch the error rather than read a sentinel.
        """
        stdout, _ = await self._run_adb_for_device(
            serial, "shell", "settings", "get", "global", "http_proxy",
        )
        value = (stdout or "").strip()
        return None if value in ("", "null") else value

    @staticmethod
    def is_network_transport(serial: str) -> bool:
        """Whether adb reaches this device over TCP rather than USB.

        Two forms reach a device over the network. `adb connect` produces
        `host:port`. Android 11+ wireless debugging discovered over mDNS
        produces `adb-<serial>-<suffix>._adb-tls-connect._tcp`, which has no
        colon at all -- so the `host:port` shape alone called it USB and
        cleared it to have its Wi-Fi turned off. USB serials are the hardware
        serial and emulators are `emulator-5554`; neither matches either form.

        This is not cosmetic: `svc wifi disable` on a device whose adb
        connection runs over that same Wi-Fi severs the control channel
        mid-call, and the `enable` that would undo it can never arrive. The
        device is left with Wi-Fi off and no way back that does not involve
        someone walking over to it.
        """
        if "._adb-tls-connect._tcp" in serial or "._adb._tcp" in serial:
            return True
        host, sep, port = serial.rpartition(":")
        return bool(sep and host and port.isdigit())

    async def reattach_network(self, serial: str) -> bool:
        """Bounce Wi-Fi so a changed proxy setting is picked up.

        Returns whether the device came back with an address.

        Required, not cosmetic: the proxy setting is read when the network
        attaches, so changing it on a connected device has no effect until
        something reattaches. This is the step whose absence makes
        `settings put global http_proxy` look like it does not work, which is
        almost certainly why the per-SSID Wi-Fi proxy UI has been assumed
        necessary.

        An emulator has no Wi-Fi -- its network is a QEMU NAT link on `eth0` --
        so `svc wifi` is a no-op there and this reports False rather than
        pretending. Emulators pick the setting up without a bounce.
        """
        if self.is_network_transport(serial):
            # Refused, not attempted. Bouncing Wi-Fi here would cut the
            # connection carrying the command to turn it back on, stranding a
            # device that may be in another building. The proxy setting is
            # already written and takes effect when the device next attaches,
            # so declining costs a delay; going ahead can cost the device.
            logger.warning(
                "Not bouncing Wi-Fi on %s: adb reaches it over the network, "
                "and the bounce would sever that connection", serial,
            )
            return False
        try:
            await self._run_adb_for_device(serial, "shell", "svc", "wifi", "disable")
        except Exception:
            # Ambiguous, not harmless. A nonzero adb exit does not prove the
            # command had no effect -- it may have disabled Wi-Fi and then
            # failed to report back -- so returning here could leave the radio
            # off with nothing left to turn it on. Fall through to the enable
            # attempts instead, which are idempotent and cost nothing when
            # Wi-Fi was never actually disturbed.
            logger.warning(
                "Disabling Wi-Fi on %s failed; attempting to re-enable in case "
                "it took effect anyway", serial, exc_info=True,
            )
        await asyncio.sleep(1.0)
        # Retried where `disable` is not, and warned about rather than logged
        # at debug. `svc wifi enable` is idempotent, and sharing one `try` with
        # the disable meant a transient adb error between the two left the
        # device with its radio off -- reported as a failed reattach, with a
        # hint telling the caller to tap a network on a phone whose Wi-Fi was
        # no longer on.
        for attempt in range(3):
            try:
                await self._run_adb_for_device(
                    serial, "shell", "svc", "wifi", "enable",
                )
                break
            except Exception:
                logger.warning(
                    "Could not re-enable Wi-Fi on %s (attempt %d of 3)",
                    serial, attempt + 1, exc_info=True,
                )
                await asyncio.sleep(0.5)
        else:
            return False
        # Wait for an address rather than a fixed sleep: the reattach is the
        # point, and a caller told "done" before the device has a route would
        # configure a proxy the device cannot yet reach.
        for _ in range(20):
            await asyncio.sleep(0.5)
            try:
                # Every interface, then filtered by name. Hardcoding `wlan0`
                # reported a device on `wlan1` as failed while it was working;
                # accepting any non-loopback address overcorrected, because
                # `rmnet_data0` means the phone fell back to *cellular* with
                # Wi-Fi still down, and an emulator's `eth0` is not Wi-Fi at
                # all. Either way this would confirm a reattach that did not
                # happen -- which is worse than the original bug, since the
                # caller stops looking.
                stdout, _ = await self._run_adb_for_device(
                    serial, "shell", "ip", "-4", "-o", "addr", "show",
                )
            except Exception:
                continue
            if any(_is_wifi_inet_line(line) for line in (stdout or "").splitlines()):
                return True
        return False

    async def get_wifi_ssid(self, serial: str) -> str | None:
        """The SSID the device is associated with, or None.

        Saves a caller typing it. `record_device_proxy_config` keys its
        configs by SSID, and until now a human read it off the device's
        screen and passed it in.
        """
        try:
            stdout, _ = await self._run_adb_for_device(
                serial, "shell", "dumpsys", "wifi",
            )
        except Exception:
            logger.debug("Could not read Wi-Fi state on %s", serial, exc_info=True)
            return None
        for line in (stdout or "").splitlines():
            if "mWifiInfo SSID:" not in line:
                continue
            rest = line.split("mWifiInfo SSID:", 1)[1].strip()
            # `WifiInfo.getSSID()` wraps a valid UTF-8 SSID in double quotes,
            # so the format varies by build: the Pixel 3 XL here emits
            # `SSID: MonaLisaOverdrive,` bare, while quoted builds emit
            # `SSID: "MonaLisaOverdrive",`. Splitting on the first comma
            # handles neither an embedded comma nor the quotes, and the SSID
            # is the *storage key* -- a stray pair of quotes files one network
            # under two records and never matches `detect_current_ssid`, which
            # returns the name unquoted.
            if rest.startswith('"'):
                end = rest.find('"', 1)
                ssid = rest[1:end] if end > 0 else rest[1:]
            else:
                # Unquoted: runs to the next field rather than the next comma,
                # so an SSID containing one survives.
                ssid = rest.split(", BSSID:", 1)[0].rstrip(",").strip()
            # `<unknown ssid>` is what an unassociated device reports.
            if ssid and not ssid.startswith("<"):
                return ssid
            return None
        return None

    async def get_lan_ip(self, serial: str) -> str | None:
        """The device's own address on the network it routes through.

        `ip route get 8.8.8.8` rather than reading an interface, because it
        answers the question that matters -- which address this device would
        use to reach something off-device -- without assuming the interface is
        called `wlan0`. An emulator answers with its NAT address, which is
        correct: that is what it would use.

        This is what `record_device_proxy_config` calls `client_ip`, and what
        flow attribution matches against. Having a human type it is what makes
        Android flows unattributable today (#262).
        """
        try:
            stdout, _ = await self._run_adb_for_device(
                serial, "shell", "ip", "route", "get", "8.8.8.8",
            )
        except Exception:
            logger.debug("Could not read the route on %s", serial, exc_info=True)
            return None
        # `src` as the last token is not hypothetical -- truncated output ends
        # wherever the read ended -- and indexing past it raised straight out
        # of here into a 500. Measured shape on a real device:
        #   8.8.8.8 via 192.168.1.1 dev wlan0 table 1030 src 192.168.1.244 ...
        parts = (stdout or "").split()
        if "src" not in parts:
            return None
        idx = parts.index("src") + 1
        if idx >= len(parts):
            logger.debug("Route output on %s ended at 'src'", serial)
            return None
        return parts[idx] or None

    async def is_screen_on(self, serial: str) -> bool:
        """Check if the device screen is on (interactive)."""
        try:
            stdout, _ = await self._run_adb_for_device(serial, "shell", "dumpsys", "power")
            # Different Android versions expose this differently. Prefer
            # mWakefulness (authoritative on modern Android: Awake / Dreaming /
            # Dozing / Asleep).
            #
            # IMPORTANT: do NOT match a bare "Display Power:" line — on recent
            # Android that line is a listener object reference, e.g.
            #   "Display Power: com.android.server.power.PowerManagerService$1@..."
            # which contains no state and always read as OFF, making the screen
            # look off even when awake. Require "state=" so we only match the
            # actual display-state line ("Display Power: state=ON").
            for line in stdout.splitlines():
                stripped = line.strip()
                if stripped.startswith("mWakefulness="):
                    return "Awake" in stripped
                if stripped.startswith("Display Power: state="):
                    return "state=ON" in stripped
                if stripped.startswith("mScreenOn="):
                    return "true" in stripped
        except DeviceError:
            pass
        return True  # Assume on if we can't tell

    async def wake_screen(self, serial: str) -> None:
        """Wake the device screen and dismiss a non-secure lock screen."""
        if await self.is_screen_on(serial):
            return
        # KEYCODE_WAKEUP (224) turns screen on without toggling
        await self._run_adb_for_device(serial, "shell", "input", "keyevent", "224")
        # Dismiss a non-secure keyguard WITHOUT a content-scrolling gesture.
        # The previous `input swipe 540 1800 540 800` was a full-screen upward
        # swipe that scrolled whatever app was showing (e.g. scrolling a
        # cache-detail RecyclerView out of place), corrupting scroll state on
        # every screenshot / UI read that happened to wake the screen.
        # `wm dismiss-keyguard` is a no-op when there's no keyguard and never
        # touches app content (secure keyguards still require the passcode).
        await self._run_adb_for_device(serial, "shell", "wm", "dismiss-keyguard")

    async def clear_app_data(self, serial: str, package: str) -> None:
        """Wipe an app's data, the Android equivalent of `simctl privacy reset`.

        `pm clear` needs no root and works on any device -- verified on an
        unrooted release-keys Pixel 3 XL, which answered `Success`. Quern
        refused this outright until #299, telling the caller it was "only
        supported on simulators" about a device that demonstrably does it.

        Also terminates the app, which `pm clear` does implicitly, so callers
        do not need a separate stop.
        """
        stdout, stderr = await self._run_adb_for_device(
            serial, "shell", "pm", "clear", package,
        )
        out = f"{stdout or ''}{stderr or ''}"
        if "Success" not in out:
            # Belt and braces rather than the primary check. Measured on a
            # Pixel 3 XL, a bad package prints `Failed` *and* exits 1, so
            # `_run_adb_for_device` raises before this is reached. It stays
            # because exit-code propagation through `adb shell` is a property
            # of the adb version rather than of the command -- the pre-shell-v2
            # protocol did not forward it at all -- so on an older host the
            # status would be 0 and the output would be the only signal.
            raise DeviceError(
                f"Could not clear data for {package} on {serial}: "
                f"{out.strip() or 'no output'}",
                tool="adb",
            )

    async def set_location(
        self, serial: str, latitude: float, longitude: float,
        satellites: int = 4,
    ) -> None:
        """Set simulated GPS location on an emulator.

        Runs: adb -s <serial> emu geo fix <longitude> <latitude> [alt] [satellites]
        Note: the emulator console takes longitude first, then latitude.
        A default satellite count of 4 avoids anti-spoof heuristics that flag 0 satellites.
        """
        if not self.is_console_serial(serial):
            raise DeviceError(
                "Location simulation needs the emulator console, which is "
                f"reachable only through an `emulator-NNNN` serial, not {serial}. "
                "The device may well be an emulator; this connection cannot "
                "carry `adb emu` commands.",
                tool="adb",
            )
        await self._run_adb_for_device(
            serial, "emu", "geo", "fix",
            str(longitude), str(latitude), "0", str(satellites),
        )

    async def open_url(self, serial: str, url: str, package: str | None = None) -> None:
        """Open a URL via Android's VIEW intent.

        Runs: adb -s <serial> shell am start -a android.intent.action.VIEW -d <url> [<package>]
        Supports any URI scheme the device has a handler for: https://, geo:,
        tel:, mailto:, custom app schemes, etc.

        When `package` is given, the intent is delivered directly to that app,
        bypassing Android App Links verification. This is required to drive deep
        links into a debug/staging build: such https links are usually NOT
        verified App Links (autoVerify=false and/or the debug signing cert isn't
        in the domain's assetlinks.json), so a package-less VIEW intent falls
        through to the browser instead of opening the app.
        """
        args = [
            serial, "shell", "am", "start",
            "-a", "android.intent.action.VIEW",
            "-d", url,
        ]
        if package:
            args.append(package)
        stdout, stderr = await self._run_adb_for_device(*args)

        # `am start` exits 0 when nothing can handle the intent, and says so
        # only in its output -- so discarding that made an unhandled URL
        # byte-for-byte identical to a successful dispatch. iOS raises here,
        # which is what made the Android silence surprising rather than merely
        # unhelpful.
        combined = f"{stdout}\n{stderr}"
        for marker in _AM_START_FAILURES:
            if marker in combined:
                detail = next(
                    (
                        line.strip()
                        for line in combined.splitlines()
                        if marker in line
                    ),
                    combined.strip(),
                )
                # Detection stays broad; the *diagnosis* does not. "Activity
                # not started" also covers a resolved intent that was refused
                # -- a permission denial, most often -- and telling someone no
                # app handled their URL sends them to install one when the app
                # is right there and said no.
                unhandled = any(m in combined for m in _AM_START_UNRESOLVED)
                summary = (
                    f"No app on {serial} handled {url}"
                    if unhandled
                    else f"Could not launch {url} on {serial}"
                )
                raise DeviceError(f"{summary}: {detail}", tool="adb")

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
