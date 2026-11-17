"""Apps on a physical iPhone without devicectl (#706).

`devicectl` comes with Xcode: on a Mac with only the Command Line Tools it
answers "devicectl is installed by Xcode", and `xcrun` cannot find it. The
CoreDevice app service it talks to is reachable over the device's tunnel all
the same, through pymobiledevice3, so on such a Mac launching, stopping and
listing apps go that way. The answers are devicectl's own, from the same
service: measured on an iPhone 11 (iOS 27.2.1) with Xcode hidden, Settings
launched in 1.1 s and the app and process lists matched devicectl's.

Only the primitives differ. What is built on them -- finding an app by what
is running, SIGTERM then SIGKILL, confirming from the process list -- is
`DevicectlBackend`'s, unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

from server.device.ios.devicectl import DevicectlBackend, _comparable_path
from server.models import AppInfo, DeviceError, InvalidDeviceRequestError

TOOL = "coredevice"

#: How long one call to the app service may take, connect included. A launch
#: took 1.1 s and a listing about as long (measured, iPhone 11).
CALL_TIMEOUT_S = 20.0
#: An install uploads the whole bundle first: the probe app took 7 s over USB,
#: and an app hundreds of MB over Wi-Fi takes minutes.
INSTALL_TIMEOUT_S = 600.0


def _bundle_id_of(path: Path) -> str | None:
    """The CFBundleIdentifier of an `.app` directory or an `.ipa`, or None
    when it cannot be read: then nothing is stopped first."""
    import plistlib
    import zipfile

    try:
        if path.is_dir():
            info = plistlib.loads((path / "Info.plist").read_bytes())
        else:
            with zipfile.ZipFile(path) as ipa:
                name = next(n for n in ipa.namelist()
                            if n.startswith("Payload/") and n.count("/") == 2
                            and n.endswith(".app/Info.plist"))
                info = plistlib.loads(ipa.read(name))
    except Exception:  # noqa: BLE001 -- any unreadable bundle stops nothing first
        return None
    if not isinstance(info, dict):
        return None
    bundle_id = info.get("CFBundleIdentifier")
    return bundle_id if isinstance(bundle_id, str) and bundle_id else None


class CoreDeviceApps(DevicectlBackend):
    """`DevicectlBackend`'s app methods, over the CoreDevice app service."""

    @contextlib.asynccontextmanager
    async def _rsd(self, uuid: str) -> AsyncIterator[Any]:
        """A connection to the device over its tunnel, USB or Wi-Fi."""
        from server.device.ios.coredevice_ax import _pmd3
        from server.device.performance import _device_address

        rsd = _pmd3().RemoteServiceDiscoveryService(await _device_address(uuid))
        try:
            await rsd.connect()
            yield rsd
        finally:
            with contextlib.suppress(Exception):
                await rsd.close()

    @contextlib.asynccontextmanager
    async def _apps(self, uuid: str) -> AsyncIterator[Any]:
        """The device's app service, on a connection of its own."""
        from server.device.ios.coredevice_ax import _pmd3

        async with self._rsd(uuid) as rsd, _pmd3().AppService(rsd) as apps:
            yield apps

    async def _call(
        self, uuid: str, what: str, op: Callable[[Any], Awaitable[Any]], *,
        timeout: float = CALL_TIMEOUT_S, write: bool = False,
        service: Callable[[str], AbstractAsyncContextManager[Any]] | None = None,
    ) -> Any:
        """`op(apps)` on the device's app service -- or on what `service`
        opens instead -- with its failures as DeviceError. A `write` that
        times out may still have happened on the phone, and says so."""
        async def run() -> Any:
            async with (service or self._apps)(uuid) as apps:
                return await op(apps)
        try:
            return await asyncio.wait_for(run(), timeout)
        except DeviceError:
            raise
        except TimeoutError as e:
            raise DeviceError(
                f"{what} on {uuid[:8]} took over {timeout:.0f} s"
                + (": it may have been sent, and may have happened on the phone all the "
                   "same; check before doing it again" if write else ""),
                tool=TOOL) from e
        except Exception as e:  # noqa: BLE001 -- pymobiledevice3 raises its own kinds
            from server.device.ios.phone_readiness import NO_IMAGE_FIX, missing_image

            if missing_image(e):
                raise DeviceError(f"{what} on {uuid[:8]} failed. {NO_IMAGE_FIX}.",
                                  tool=TOOL) from e
            raise DeviceError(f"{what} on {uuid[:8]} failed: {e}", tool=TOOL) from e

    async def open_url(self, uuid: str, url: str, bundle_id: str) -> None:
        """Hand `url` to `bundle_id`, launching it if need be: devicectl's
        `--payload-url`, which is the launch request's `payloadURL` option.
        Measured on an iPhone 11 with Xcode hidden: Safari opened a page and
        Maps a `maps://` search this way."""
        import plistlib

        options = {
            "arguments": [], "environmentVariables": {},
            "standardIOUsesPseudoterminals": True, "startStopped": False,
            "terminateExisting": False, "user": {"shortName": "mobile"},
            "payloadURL": {"relative": url},
            "platformSpecificOptions": plistlib.dumps({}),
        }
        try:
            await self._call(
                uuid, f"opening {url} in {bundle_id}", write=True, op=lambda a: a.invoke(
                    "com.apple.coredevice.feature.launchapplication",
                    {"applicationSpecifier": {"bundleIdentifier": {"_0": bundle_id}},
                     "options": options, "standardIOIdentifiers": {}}))
        except DeviceError as e:
            if not await self.is_installed(uuid, bundle_id):
                raise InvalidDeviceRequestError(
                    f"{bundle_id} is not installed on {uuid[:8]}", tool=TOOL) from e
            raise

    async def _installed(self, uuid: str) -> list[dict]:
        apps = await self._call(uuid, "listing apps", lambda a: a.list_apps())
        return [a for a in apps if isinstance(a, dict)]

    async def launch_app(self, uuid: str, bundle_id: str, *,
                         env: dict[str, str] | None = None,
                         terminate_existing: bool = False,
                         arguments: list[str] | None = None) -> int:
        """Launch an app: its pid. Refused for an app that is not installed,
        which the service reports only as "failed to launch"."""
        try:
            reply = await self._call(
                uuid, f"launching {bundle_id}", write=True, op=lambda a: a.launch_application(
                    bundle_id, arguments=arguments or [], kill_existing=terminate_existing,
                    environment=env or {}))
        except DeviceError as e:
            if not await self.is_installed(uuid, bundle_id):
                raise InvalidDeviceRequestError(
                    f"{bundle_id} is not installed on {uuid[:8]}", tool=TOOL) from e
            raise
        pid = (reply.get("processToken", {}) if isinstance(reply, dict) else {}).get(
            "processIdentifier")
        return pid if isinstance(pid, int) else 0

    async def app_url(self, uuid: str, bundle_id: str) -> str | None:
        """The installed app's bundle path, or None when it is not installed."""
        mine = [a for a in await self._installed(uuid)
                if a.get("bundleIdentifier") == bundle_id]
        if not mine:
            return None
        path = mine[0].get("path")
        if not isinstance(path, str) or not path:
            raise DeviceError(
                f"{bundle_id} is installed on {uuid[:8]}, but the app service gave no "
                "bundle path to find its process by", tool=TOOL)
        return path

    async def running_pids(self, uuid: str, app_url: str) -> list[int]:
        """The pids of the app's main executable: directly inside the bundle,
        not an extension under PlugIns, as `DevicectlBackend.running_pids`."""
        processes = await self._call(uuid, "listing processes", lambda a: a.list_processes())
        bundle = _comparable_path(app_url).rstrip("/") + "/"
        pids = []
        for p in processes:
            if not isinstance(p, dict):
                continue
            url = (p.get("executableURL") or {}).get("relative") or ""
            executable = _comparable_path(str(url))
            pid = p.get("processIdentifier")
            rest = executable[len(bundle):] if executable.startswith(bundle) else ""
            if rest and "/" not in rest and isinstance(pid, int):
                pids.append(pid)
        return pids

    async def _signal(self, uuid: str, pid: int, kill: bool) -> None:
        sig = signal.SIGKILL if kill else signal.SIGTERM
        await self._call(uuid, f"signalling pid {pid}", write=True,
                         op=lambda a: a.send_signal_to_process(pid, int(sig)))

    async def is_installed(self, uuid: str, bundle_id: str) -> bool:
        return any(a.get("bundleIdentifier") == bundle_id
                   for a in await self._installed(uuid))

    async def list_apps(self, uuid: str) -> list[AppInfo]:
        return [AppInfo(bundle_id=a.get("bundleIdentifier", ""), name=a.get("name", ""),
                        app_type="System" if a.get("isFirstParty") else "User")
                for a in await self._installed(uuid)]

    async def uninstall_app(self, uuid: str, bundle_id: str) -> None:
        await self._call(uuid, f"uninstalling {bundle_id}", write=True,
                         op=lambda a: a.uninstall_app(bundle_id))

    async def install_app(self, uuid: str, app_path: str) -> None:
        """Install a built, signed `.app` or `.ipa` through the installation
        proxy, over the tunnel as the app service is. Measured on an iPhone 11
        with Xcode hidden: the probe app installed in 7 s. Signing is the
        build's: a bundle not signed for this phone is refused by the phone,
        and that refusal is the error."""
        path = Path(app_path)
        if not path.exists():
            raise InvalidDeviceRequestError(f"{app_path} does not exist", tool=TOOL)
        # Not over a running copy: measured on an iPhone 11 (iOS 27.2.1), the
        # proxy took the upload and then never answered while the app ran,
        # and answered in 1 s once it was stopped. An install ends it anyway.
        bundle_id = _bundle_id_of(path)
        if bundle_id:
            try:
                await self.terminate_app(uuid, bundle_id)
            except InvalidDeviceRequestError:
                pass                                        # not installed yet

        @contextlib.asynccontextmanager
        async def installer(udid: str) -> AsyncIterator[Any]:
            from pymobiledevice3.services.installation_proxy import InstallationProxyService

            async with self._rsd(udid) as rsd:
                yield InstallationProxyService(lockdown=rsd)

        await self._call(uuid, f"installing {path.name}",
                         lambda proxy: proxy.install_from_local(path),
                         timeout=INSTALL_TIMEOUT_S, write=True, service=installer)
