"""A physical iPhone driven from a Mac without Xcode (#706).

`devicectl` comes with Xcode, so on a Mac with only the Command Line Tools a
phone on iOS 17 or later was not listed at all, its crash reports read as
"not connected over USB", and launching, stopping, listing apps and opening
URLs failed. Those go over the CoreDevice app service through
pymobiledevice3 instead, which is what devicectl talks to.
"""

from __future__ import annotations

import contextlib
import signal
from unittest.mock import AsyncMock

import pytest

from server.device.controller import DeviceController
from server.device.ios.coredevice_apps import CoreDeviceApps
from server.device.ios.usbmux import UsbmuxBackend
from server.models import (
    DeviceError,
    DeviceOperationUnsupportedError,
    DeviceType,
    InvalidDeviceRequestError,
)

PHONE = "00008030-000C59623A69802E"
SETTINGS = {"bundleIdentifier": "com.apple.Preferences", "name": "Settings",
            "path": "/Applications/Preferences.app", "isFirstParty": True}
PROBE = {"bundleIdentifier": "com.quern.probe", "name": "Probe", "isFirstParty": False,
         "path": "/private/var/containers/Bundle/Application/X/Probe.app"}


def _process(pid: int, path: str) -> dict:
    return {"executableURL": {"relative": f"file://{path}"}, "processIdentifier": pid}


class FakeAppService:
    """The CoreDevice app service, as pymobiledevice3 exposes it: a signal
    removes a pid unless that pid ignores it."""

    def __init__(self, apps, processes, *, ignores=(), launch_fails=False):
        self.apps, self.processes = list(apps), list(processes)
        self.ignores, self.launch_fails = set(ignores), launch_fails
        self.signals: list[tuple[int, int]] = []
        self.launches: list[dict] = []

    async def list_apps(self):
        return list(self.apps)

    async def list_processes(self):
        return list(self.processes)

    async def launch_application(self, bundle_id, **kw):
        if self.launch_fails:
            raise RuntimeError("The application failed to launch. (code 10002)")
        self.launches.append({"bundle_id": bundle_id, **kw})
        return {"processToken": {"processIdentifier": 2560}}

    async def send_signal_to_process(self, pid, sig):
        self.signals.append((pid, sig))
        if pid not in self.ignores:
            self.processes = [p for p in self.processes if p["processIdentifier"] != pid]
        return {}

    async def invoke(self, feature, body):
        self.launches.append({"feature": feature, **body})
        return {"processToken": {"processIdentifier": 2561}}


def _apps(service: FakeAppService) -> CoreDeviceApps:
    backend = CoreDeviceApps()
    backend.TERMINATE_WAIT_S = 0.05
    backend.TERMINATE_POLL_S = 0.01

    @contextlib.asynccontextmanager
    async def connected(uuid):
        yield service
    backend._apps = connected
    return backend


# -- listing -----------------------------------------------------------------------


class TestListing:
    RAW = [{"UniqueDeviceID": PHONE, "DeviceName": "iPhone 11", "ProductVersion": "27.2.1",
            "ConnectionType": "USB", "DeviceClass": "iPhone"}]

    def test_usbmux_lists_a_new_phone_only_when_asked_for_every_version(self):
        assert UsbmuxBackend._parse_devices(self.RAW) == []
        [phone] = UsbmuxBackend._parse_devices(self.RAW, every_version=True)
        assert (phone.udid, phone.os_version) == (PHONE, "iOS 27.2.1")

    @pytest.mark.parametrize("devicectl", [True, False])
    async def test_the_controller_asks_for_every_version_without_devicectl(self, devicectl):
        ctrl = DeviceController()
        ctrl.simctl.list_devices = AsyncMock(return_value=[])
        ctrl.devicectl.list_devices = AsyncMock(return_value=[])
        ctrl.devicectl.is_available = AsyncMock(return_value=devicectl)
        ctrl.usbmux.list_devices = AsyncMock(return_value=[])
        ctrl.adb.list_devices = AsyncMock(return_value=[])

        await ctrl.list_devices()

        ctrl.usbmux.list_devices.assert_awaited_once_with(every_version=not devicectl)

    @pytest.mark.device_discovery    # the real is_available, which conftest stubs
    async def test_devicectl_is_asked_about_once_a_minute(self, monkeypatch):
        from server.device.ios import devicectl as module

        probe = AsyncMock(return_value=False)
        monkeypatch.setattr(module, "probe_command", probe)
        monkeypatch.setattr(module, "xcode_available", lambda: True)
        clock = [100.0]
        monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
        backend = module.DevicectlBackend()

        assert await module.DevicectlBackend.is_available(backend) is False
        assert await module.DevicectlBackend.is_available(backend) is False
        assert probe.await_count == 1
        clock[0] += 60                                  # Xcode installed since
        probe.return_value = True
        assert await module.DevicectlBackend.is_available(backend) is True

    async def test_a_phone_listed_by_its_own_udid_is_on_usb(self):
        """Its crash reports were "not connected over USB": the check took
        only the 40-character UDIDs of phones before iOS 17."""
        ctrl = DeviceController()
        ctrl.list_devices = AsyncMock(return_value=[])
        ctrl._device_type_cache[PHONE] = DeviceType.DEVICE
        ctrl.usbmux.get_usb_devices = AsyncMock(return_value=[(PHONE, "iPhone 11")])
        assert await ctrl.get_libimobiledevice_udid(PHONE) == PHONE

        ctrl.usbmux.get_usb_devices = AsyncMock(return_value=[])     # on Wi-Fi
        assert await ctrl.get_libimobiledevice_udid(PHONE) is None


# -- the app service -------------------------------------------------------------------


class TestApps:
    async def test_a_launch_answers_the_pid_and_carries_env(self):
        service = FakeAppService([SETTINGS], [])
        pid = await _apps(service).launch_app(PHONE, "com.apple.Preferences",
                                              env={"QUERN_AUTOMATION": "YES"},
                                              terminate_existing=True)
        assert pid == 2560
        [launch] = service.launches
        assert launch["environment"] == {"QUERN_AUTOMATION": "YES"}
        assert launch["kill_existing"] is True

    async def test_an_app_that_is_not_installed_is_refused_as_such(self):
        service = FakeAppService([SETTINGS], [], launch_fails=True)
        with pytest.raises(InvalidDeviceRequestError, match="not installed"):
            await _apps(service).launch_app(PHONE, "com.example.nothere")

    async def test_a_launch_that_fails_for_an_installed_app_says_why(self):
        service = FakeAppService([SETTINGS], [], launch_fails=True)
        with pytest.raises(DeviceError, match="failed to launch") as e:
            await _apps(service).launch_app(PHONE, "com.apple.Preferences")
        assert not isinstance(e.value, InvalidDeviceRequestError)

    async def test_terminate_stops_the_main_executable_not_an_extension(self):
        service = FakeAppService([PROBE], [
            _process(711, PROBE["path"] + "/Probe"),
            _process(712, PROBE["path"] + "/PlugIns/Widget.appex/Widget"),
        ])
        assert await _apps(service).terminate_app(PHONE, "com.quern.probe") is True
        assert service.signals == [(711, int(signal.SIGTERM))]

    async def test_one_that_ignores_sigterm_is_killed(self):
        service = FakeAppService([PROBE], [_process(711, PROBE["path"] + "/Probe")],
                                 ignores=[711])
        with pytest.raises(DeviceError, match="still running"):
            await _apps(service).terminate_app(PHONE, "com.quern.probe")
        assert service.signals == [(711, int(signal.SIGTERM)), (711, int(signal.SIGKILL))]

    async def test_not_running_and_not_installed(self):
        service = FakeAppService([PROBE], [])
        assert await _apps(service).terminate_app(PHONE, "com.quern.probe") is False
        with pytest.raises(InvalidDeviceRequestError):
            await _apps(service).terminate_app(PHONE, "com.example.nothere")

    async def test_listing_marks_system_apps(self):
        apps = await _apps(FakeAppService([SETTINGS, PROBE], [])).list_apps(PHONE)
        assert [(a.bundle_id, a.name, a.app_type) for a in apps] == [
            ("com.apple.Preferences", "Settings", "System"),
            ("com.quern.probe", "Probe", "User")]

    async def test_install_goes_through_the_installation_proxy(self, monkeypatch, tmp_path):
        app = tmp_path / "Probe.app"
        app.mkdir()
        installed = []

        class Proxy:
            def __init__(self, lockdown):
                self.lockdown = lockdown

            async def install_from_local(self, path):
                installed.append((self.lockdown, path))
        monkeypatch.setattr(
            "pymobiledevice3.services.installation_proxy.InstallationProxyService", Proxy)
        backend = CoreDeviceApps()

        @contextlib.asynccontextmanager
        async def tunnel(uuid):
            yield "rsd"
        backend._rsd = tunnel

        await backend.install_app(PHONE, str(app))
        assert installed == [("rsd", app)]

    async def test_an_install_the_phone_refuses_says_why(self, monkeypatch, tmp_path):
        class Proxy:
            def __init__(self, lockdown):
                pass

            async def install_from_local(self, path):
                raise RuntimeError("ApplicationVerificationFailed")
        monkeypatch.setattr(
            "pymobiledevice3.services.installation_proxy.InstallationProxyService", Proxy)
        backend = CoreDeviceApps()

        @contextlib.asynccontextmanager
        async def tunnel(uuid):
            yield "rsd"
        backend._rsd = tunnel
        (tmp_path / "Probe.app").mkdir()

        with pytest.raises(DeviceError, match="installing Probe.app on 00008030 failed: "
                                              "ApplicationVerificationFailed"):
            await backend.install_app(PHONE, str(tmp_path / "Probe.app"))

    async def test_a_path_that_is_not_there_is_refused_before_connecting(self):
        with pytest.raises(InvalidDeviceRequestError, match="does not exist"):
            await CoreDeviceApps().install_app(PHONE, "/nowhere/App.app")

    async def test_a_url_is_the_launch_request_s_payload(self):
        service = FakeAppService([SETTINGS], [])
        await _apps(service).open_url(PHONE, "maps://?q=Louvre", "com.apple.Maps")
        [call] = service.launches
        assert call["feature"] == "com.apple.coredevice.feature.launchapplication"
        assert call["applicationSpecifier"] == {"bundleIdentifier": {"_0": "com.apple.Maps"}}
        assert call["options"]["payloadURL"] == {"relative": "maps://?q=Louvre"}

    async def test_a_service_that_fails_is_a_device_error(self):
        backend = CoreDeviceApps()

        @contextlib.asynccontextmanager
        async def broken(uuid):
            raise OSError("no tunnel")
            yield
        backend._apps = broken
        with pytest.raises(DeviceError, match="listing apps on 00008030 failed: no tunnel"):
            await backend.list_apps(PHONE)


# -- the controller ---------------------------------------------------------------------


async def test_apps_go_over_coredevice_only_without_devicectl():
    ctrl = DeviceController()
    ctrl.devicectl.is_available = AsyncMock(return_value=True)
    assert await ctrl._apps() is ctrl.devicectl
    ctrl.devicectl.is_available = AsyncMock(return_value=False)
    assert await ctrl._apps() is ctrl.coredevice_apps


def _phone(monkeypatch, *, wda: str | None):
    ctrl = DeviceController()
    ctrl._active_udid = PHONE
    ctrl._device_type_cache[PHONE] = DeviceType.DEVICE
    ctrl.wda_client._device_os_versions[PHONE] = "iOS 27.2.1"
    monkeypatch.setattr("server.device.controller_ui.get_device_ui_reader", lambda: "coredevice")

    async def reason():
        return wda
    monkeypatch.setattr("server.tooling.wda_availability.wda_unavailable_reason", reason)
    ctrl.coredevice_apps.open_url = AsyncMock()
    ctrl.wda_client.open_url = AsyncMock()
    ctrl._invalidate_ui_cache = lambda *a: None
    return ctrl


class TestOpenUrlWithoutWda:
    async def test_a_web_link_opens_in_safari_and_says_so(self, monkeypatch):
        ctrl = _phone(monkeypatch, wda="Xcode is not installed")
        _, outcome = await ctrl.open_url("https://example.com", udid=PHONE)
        ctrl.coredevice_apps.open_url.assert_awaited_once_with(
            PHONE, "https://example.com", "com.apple.mobilesafari")
        ctrl.wda_client.open_url.assert_not_called()
        assert outcome["via"] == "coredevice" and outcome["route"] == "safari"
        assert "universal link" in outcome["note"]

    async def test_with_a_bundle_it_goes_to_that_app_and_is_checked(self, monkeypatch):
        ctrl = _phone(monkeypatch, wda="Xcode is not installed")
        monkeypatch.setattr(DeviceController, "_OPEN_URL_SETTLE_S", 0)
        front = AsyncMock(return_value="com.apple.Maps")
        ctrl._front_bundle_over_coredevice = front

        _, outcome = await ctrl.open_url("maps://?q=Louvre", udid=PHONE,
                                         bundle_id="com.apple.Maps")

        ctrl.coredevice_apps.open_url.assert_awaited_once_with(
            PHONE, "maps://?q=Louvre", "com.apple.Maps")
        assert outcome["route"] == "app" and outcome["opened_in_app"] is True
        assert "note" not in outcome

    async def test_another_scheme_needs_the_app(self, monkeypatch):
        ctrl = _phone(monkeypatch, wda="Xcode is not installed")
        with pytest.raises(DeviceOperationUnsupportedError, match="needs bundle_id"):
            await ctrl.open_url("maps://?q=Louvre", udid=PHONE)
        ctrl.coredevice_apps.open_url.assert_not_called()

    async def test_where_wda_can_run_the_system_route_is_unchanged(self, monkeypatch):
        ctrl = _phone(monkeypatch, wda=None)
        _, outcome = await ctrl.open_url("https://example.com", udid=PHONE)
        ctrl.wda_client.open_url.assert_awaited_once()
        ctrl.coredevice_apps.open_url.assert_not_called()
        assert outcome == {"via": "wda", "route": "system"}


async def test_the_app_in_front_is_named_by_its_pid(monkeypatch):
    ctrl = DeviceController()
    monkeypatch.setattr("server.device.ios.coredevice_ax.front_app",
                        AsyncMock(return_value=(711, "Probe")))
    ctrl.coredevice_apps.app_url = AsyncMock(return_value=PROBE["path"])
    ctrl.coredevice_apps.running_pids = AsyncMock(return_value=[711])
    assert await ctrl._front_bundle_over_coredevice(PHONE, "com.quern.probe") == \
        "com.quern.probe"

    ctrl.coredevice_apps.running_pids = AsyncMock(return_value=[900])   # another app
    assert await ctrl._front_bundle_over_coredevice(PHONE, "com.quern.probe") == "Probe"


# -- readiness: Developer Mode and the developer disk image -----------------------------


class TestReadiness:
    async def test_each_is_read_on_its_own(self, monkeypatch):
        from server.device.ios import phone_readiness as r

        class Lockdown:
            all_values = {"DeviceName": "iPhone 11"}
            product_version = "27.2.1"

            async def get_developer_mode_status(self):
                raise OSError("amfi said no")

        class Mounter:
            def __init__(self, lockdown):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def copy_devices(self):
                return [{"IsMounted": True, "MountPath": "/System/Developer"}]

        monkeypatch.setattr(r, "_lockdown", AsyncMock(return_value=Lockdown()))
        monkeypatch.setattr(
            "pymobiledevice3.services.mobile_image_mounter.MobileImageMounterService", Mounter)

        state = await r.readiness(PHONE)

        assert state.image_mounted is True
        assert state.developer_mode is None            # could not tell, not "off"
        assert state.errors == ["Developer Mode could not be read: amfi said no"]

    async def test_a_phone_that_cannot_be_reached_reads_nothing(self, monkeypatch):
        from server.device.ios import phone_readiness as r

        monkeypatch.setattr(r, "_lockdown", AsyncMock(side_effect=OSError("not paired")))
        state = await r.readiness(PHONE)
        assert (state.developer_mode, state.image_mounted) == (None, None)
        assert "not paired" in state.errors[0]


class TestDoctor:
    def _phones(self, monkeypatch, *, mode=True, mounted=True, errors=None,
                mount=None, reveal=True):
        from server import main
        from server.device.ios import phone_readiness as r

        monkeypatch.setattr(r, "phones", AsyncMock(return_value=[(PHONE, "iPhone 11", "27.2.1")]))
        monkeypatch.setattr(r, "readiness", AsyncMock(return_value=r.Readiness(
            PHONE, "iPhone 11", "27.2.1", developer_mode=mode, image_mounted=mounted,
            errors=errors or [])))
        self.mount = AsyncMock(side_effect=mount)
        monkeypatch.setattr(r, "mount_image", self.mount)
        self.reveal = []
        monkeypatch.setattr(main, "_reveal_developer_mode",
                            lambda udid: self.reveal.append(udid) or reveal)
        return main

    def test_a_ready_phone(self, monkeypatch, capsys):
        main = self._phones(monkeypatch)
        assert main._report_phones() == (True, None)
        out = capsys.readouterr().out
        assert "Developer Mode on" in out and "developer disk image mounted" in out

    def test_a_missing_image_is_said_and_mounted_only_with_fix(self, monkeypatch, capsys):
        main = self._phones(monkeypatch, mounted=False)
        assert main._report_phones() == (True, None)
        self.mount.assert_not_awaited()
        assert "GitHub" in capsys.readouterr().out      # where the image would come from

        assert main._report_phones(fix=True) == (True, True)
        self.mount.assert_awaited_once_with(PHONE)

    def test_a_mount_that_fails_fails_the_fix(self, monkeypatch, capsys):
        from server.models import DeviceError

        main = self._phones(monkeypatch, mounted=False,
                            mount=DeviceError("no tunnel to 00008030", tool="pymobiledevice3"))
        assert main._report_phones(fix=True) == (True, False)
        assert "start it, then run this again" in capsys.readouterr().out

    def test_developer_mode_off_is_shown_in_settings_with_fix(self, monkeypatch, capsys):
        main = self._phones(monkeypatch, mode=False)
        assert main._report_phones(fix=True) == (True, True)
        assert self.reveal == [PHONE]
        assert "Privacy & Security > Developer Mode" in capsys.readouterr().out

    def test_what_could_not_be_read_fails_the_check(self, monkeypatch, capsys):
        main = self._phones(monkeypatch, mode=None, mounted=None,
                            errors=["could not connect: not paired"])
        assert main._report_phones() == (False, None)
        assert "? iPhone 11 (iOS 27.2.1): could not connect" in capsys.readouterr().out
