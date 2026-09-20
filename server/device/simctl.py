"""SimctlBackend — async wrapper around xcrun simctl for simulator management."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import plistlib
import re
import shutil
import tempfile
from pathlib import Path

from server.device._xcode import xcode_available
from server.device.tool_probe import probe_command
from server.models import AppInfo, DeviceError, DeviceInfo, DeviceState, DeviceType

logger = logging.getLogger("quern-debug-server.simctl")




#: The first iOS major version that refuses to launch an app with no scene
#: manifest. Below it the same app runs, so the absent manifest says nothing
#: about why a launch failed.
_SCENE_REQUIRED_IOS_MAJOR = 27


def _launched_pid(stdout: str) -> int | None:
    """The pid from `simctl launch`'s own output, or None.

    It prints `<bundle id>: <pid>`. None means the line was not in that
    shape, which is treated as "cannot tell" rather than "failed".
    """
    match = re.search(r":\s*(\d+)\s*$", stdout.strip())
    return int(match.group(1)) if match else None


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
        """Check if xcrun simctl is available and working.

        Preflights via xcode-select so a no-Xcode machine never triggers
        the macOS "install developer tools" dialog (which fires before
        xcrun returns, so try/except on the subprocess can't suppress it).
        """
        if not xcode_available():
            return False
        if await probe_command("xcrun", "simctl", "help", tool="simctl"):
            return True
        # Present but not answering. The probe already logged a timeout if that
        # is what happened; this covers the other way it fails, which looks
        # identical from here and has a specific fix.
        logger.warning(
            "xcrun simctl failed — this often happens when Xcode has been "
            "renamed or moved. Run 'xcode-select -p' to check the current "
            "developer directory, and 'sudo xcode-select -s /path/to/Xcode.app"
            "/Contents/Developer' to fix it."
        )
        return False

    async def list_devices(self) -> list[DeviceInfo]:
        """List all simulators by parsing simctl list devices --json.

        Short-circuits when Xcode isn't installed so the macOS "install
        developer tools" dialog doesn't fire. See server/device/_xcode.py.
        """
        if not xcode_available():
            return []
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

    async def erase(self, udid: str) -> None:
        """Erase a simulator, resetting it to factory state. Must be shutdown first."""
        await self._run_simctl("erase", udid)

    async def install_app(self, udid: str, app_path: str) -> None:
        """Install an app on a simulator."""
        await self._run_simctl("install", udid, app_path)

    async def launch_app(
        self, udid: str, bundle_id: str, env: dict[str, str] | None = None,
    ) -> int | None:
        """Launch an app on a simulator. Returns the pid simctl reported.

        If env is provided, the key-value pairs are passed to the app process
        via the SIMCTL_CHILD_ prefix convention.  QUERN_AUTOMATION=YES is
        always set so apps can detect quern-driven launches.
        """
        launch_env = {**os.environ, "SIMCTL_CHILD_QUERN_AUTOMATION": "YES"}
        if env:
            for key, value in env.items():
                launch_env[f"SIMCTL_CHILD_{key}"] = value
        proc = await asyncio.create_subprocess_exec(
            "xcrun", "simctl", "launch", udid, bundle_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=launch_env,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise DeviceError(
                f"simctl launch failed: {stderr.decode().strip()}",
                tool="simctl",
            )
        return _launched_pid(stdout.decode(errors="replace"))

    @staticmethod
    def process_is_alive(pid: int | None) -> bool:
        """Is that pid still running?

        A simulator's app processes are host processes, so this is a signal
        to the pid rather than anything inside the guest. An unparseable pid
        answers True: not knowing is not evidence of death, and treating it
        as one would turn a formatting change in simctl's output into every
        launch failing.
        """
        if pid is None:
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            pass
        return True

    async def app_display_name(self, udid: str, bundle_id: str) -> str | None:
        """How the accessibility tree will name this app, or None.

        `CFBundleDisplayName` when the app sets one, else `CFBundleName`.
        None when the bundle or its plist cannot be read, which callers must
        treat as "cannot tell" rather than as an answer.
        """
        plist = await self._app_info_plist(udid, bundle_id)
        if plist is None:
            return None
        name = plist.get("CFBundleDisplayName") or plist.get("CFBundleName")
        return str(name) if name else None

    async def _app_info_plist(self, udid: str, bundle_id: str) -> dict | None:
        """The installed app's Info.plist, or None if it cannot be read."""
        try:
            stdout, _ = await self._run_simctl(
                "get_app_container", udid, bundle_id, "app",
            )
            with (Path(stdout.strip()) / "Info.plist").open("rb") as handle:
                return plistlib.load(handle)
        except (OSError, ValueError, DeviceError):
            # `plistlib` raises ValueError subclasses for a malformed file.
            return None

    async def why_launch_failed(self, udid: str, bundle_id: str) -> str:
        """The likeliest reason, as a clause to append, or an empty string.

        Only one cause is named, because only one is diagnosable without
        reading the guest's log: an app with no scene manifest cannot launch
        on iOS 27 or later. Anything else gets the generic sentence, which
        points at the log rather than guessing.
        """
        generic = (
            ". Check `get_latest_crash` and the simulator log for why; the "
            "launch itself was accepted, so the app started and then stopped."
        )
        plist = await self._app_info_plist(udid, bundle_id)
        if plist is None:
            # Unreadable container or plist: say the generic thing rather
            # than claim a cause.
            return generic
        if "UIApplicationSceneManifest" in plist:
            return generic
        # The manifest being absent is not on its own a diagnosis: an app
        # without one runs perfectly well on iOS 26 and earlier, so a crash
        # there would be told the wrong cause. Only a runtime that enforces
        # the rule earns the specific message.
        major = await self._runtime_major(udid)
        if major is None or major < _SCENE_REQUIRED_IOS_MAJOR:
            return generic
        return (
            f". Its Info.plist has no UIApplicationSceneManifest, and iOS "
            f"{major} refuses to launch an app built against that SDK without "
            "one -- UIKit logs \"UIScene life cycle is required for apps built "
            "with this SDK\". Adopt the scene lifecycle, or run it on an "
            "older runtime."
        )

    async def _runtime_major(self, udid: str) -> int | None:
        """The major iOS version this simulator runs, or None.

        None when the device cannot be found or its version cannot be read,
        which callers must treat as "cannot tell" -- naming a cause on a
        runtime nobody identified is how a diagnosis becomes a guess.

        **iOS only.** `os_version` reads "iOS 18.6", "tvOS 27.0", "watchOS
        26.0", and `launch_app` runs against all of them. Taking the digits
        alone turns tvOS 27 into "iOS 27", and the only caller uses this to
        decide whether to blame the scene lifecycle -- a diagnosis that is
        specific to iOS. A non-iOS runtime is "cannot tell", not 27.
        """
        try:
            devices = await self.list_devices()
        except (DeviceError, OSError, ValueError):
            # ValueError covers json.JSONDecodeError from list_devices. This
            # runs while *already* reporting a launch failure, so letting a
            # parse error escape would replace the real diagnosis with a
            # traceback about simctl's output.
            return None
        for device in devices:
            if device.udid != udid:
                continue
            version = (device.os_version or "").strip()
            if not version.startswith("iOS "):
                return None
            digits = ""
            for char in version:
                if char.isdigit():
                    digits += char
                elif digits:
                    break
            return int(digits) if digits else None
        return None

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

    async def open_url(self, udid: str, url: str) -> None:
        """Open a URL on the simulator.

        Runs: xcrun simctl openurl <udid> <url>
        Supports any URI scheme the simulator has a handler for: https://,
        maps://, App-prefs:, custom app schemes, etc. Some schemes (tel:,
        mailto:) are unavailable on simulators.
        """
        await self._run_simctl("openurl", udid, url)

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
