"""Tests for DevicectlBackend — mock subprocess calls to xcrun devicectl."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server.device.ios.devicectl import DevicectlBackend
from server.models import DeviceError, DeviceState, DeviceType, InvalidDeviceRequestError

#: This file *is* the discovery code -- the autouse stubs in conftest replace
#: these very methods, so it opts out rather than asserting against them.
#: The subprocess layer beneath is mocked, so nothing here reaches the machine.
pytestmark = pytest.mark.device_discovery

FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_proc(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""):
    """Create a mock subprocess result."""
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


# ---------------------------------------------------------------------------
# is_available
# ---------------------------------------------------------------------------


class TestIsAvailable:
    async def test_available(self):
        backend = DevicectlBackend()
        with patch("asyncio.create_subprocess_exec", return_value=_make_proc(0)):
            assert await backend.is_available() is True

    async def test_not_available(self):
        backend = DevicectlBackend()
        with patch("asyncio.create_subprocess_exec", return_value=_make_proc(1)):
            assert await backend.is_available() is False

    async def test_exception(self):
        backend = DevicectlBackend()
        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
            assert await backend.is_available() is False


# ---------------------------------------------------------------------------
# list_devices
# ---------------------------------------------------------------------------


class TestListDevices:
    async def test_parses_paired_devices(self):
        """Only reachable paired devices are returned (unreachable ones are skipped)."""
        fixture = _load_fixture("devicectl_list_output.json")
        backend = DevicectlBackend()

        backend._run_devicectl = AsyncMock(return_value=(fixture, ""))

        devices = await backend.list_devices()

        # Only the connected device should appear; the unavailable "Old iPad" is skipped
        assert len(devices) == 1

        dev = devices[0]
        assert dev.udid == "53DA57AA-1234-5678-9ABC-DEF012345678"
        assert dev.name == "John's iPhone"
        assert dev.state == DeviceState.BOOTED
        assert dev.device_type == DeviceType.DEVICE
        assert dev.os_version == "iOS 18.3.2"
        assert dev.connection_type == "usb"
        assert dev.is_connected is True

    async def test_wifi_disconnected_tunnel_is_booted(self):
        """WiFi devices with tunnelState=disconnected are reachable → booted."""
        fixture_data = json.loads(_load_fixture("devicectl_list_output.json"))
        # Modify the second device to look like a WiFi-reachable device
        dev2 = fixture_data["result"]["devices"][1]
        dev2["connectionProperties"]["tunnelState"] = "disconnected"
        dev2["connectionProperties"]["transportType"] = "localNetwork"

        backend = DevicectlBackend()
        backend._run_devicectl = AsyncMock(return_value=(json.dumps(fixture_data), ""))

        devices = await backend.list_devices()
        assert len(devices) == 2
        assert devices[1].state == DeviceState.BOOTED
        assert devices[1].connection_type == "localNetwork"
        assert devices[1].is_connected is True

    async def test_unpaired_devices_excluded(self):
        """Devices that aren't paired are skipped."""
        fixture_data = json.loads(_load_fixture("devicectl_list_output.json"))
        for dev in fixture_data["result"]["devices"]:
            dev["connectionProperties"]["pairingState"] = "unpaired"

        backend = DevicectlBackend()
        backend._run_devicectl = AsyncMock(return_value=(json.dumps(fixture_data), ""))

        devices = await backend.list_devices()
        assert len(devices) == 0

    async def test_devicectl_error_returns_empty(self):
        """If devicectl fails, return empty list (graceful degradation)."""
        backend = DevicectlBackend()
        backend._run_devicectl = AsyncMock(side_effect=DeviceError("fail", tool="devicectl"))

        devices = await backend.list_devices()
        assert devices == []

    async def test_bad_json_returns_empty(self):
        backend = DevicectlBackend()
        backend._run_devicectl = AsyncMock(return_value=("not json", ""))

        devices = await backend.list_devices()
        assert devices == []

    async def test_empty_device_list(self):
        backend = DevicectlBackend()
        backend._run_devicectl = AsyncMock(
            return_value=(json.dumps({"result": {"devices": []}}), ""),
        )

        devices = await backend.list_devices()
        assert devices == []


# ---------------------------------------------------------------------------
# launch_app
# ---------------------------------------------------------------------------


class TestLaunchApp:
    async def test_launch_returns_pid(self):
        launch_response = json.dumps(
            {
                "result": {"process": {"processIdentifier": 12345}},
            }
        )
        backend = DevicectlBackend()
        backend._run_devicectl = AsyncMock(return_value=(launch_response, ""))

        pid = await backend.launch_app("SOME-UUID", "com.example.app")

        assert pid == 12345

    async def test_launch_bad_json_returns_zero(self):
        backend = DevicectlBackend()
        backend._run_devicectl = AsyncMock(return_value=("not json", ""))

        pid = await backend.launch_app("UUID", "com.example.app")
        assert pid == 0

    async def test_launch_error_propagates(self):
        backend = DevicectlBackend()
        backend._run_devicectl = AsyncMock(
            side_effect=DeviceError("launch failed", tool="devicectl"),
        )

        with pytest.raises(DeviceError, match="launch failed"):
            await backend.launch_app("UUID", "com.example.app")


# ---------------------------------------------------------------------------
# terminate_app
# ---------------------------------------------------------------------------


APP_URL = "file:///private/var/containers/Bundle/Application/DF56/QuernProbeScene.app/"


def _apps(*apps) -> str:
    return json.dumps({"result": {"apps": list(apps)}})


def _processes(*processes) -> str:
    return json.dumps({"result": {"runningProcesses": list(processes)}})


class TestTerminateApp:
    """By what is running now, not a pid stored at launch (#664): measured on an
    iPhone 11, an app's bundle URL is the prefix of its main executable."""

    def _backend(self, apps, processes, *, ignores_term=(), ignores_kill=(),
                 already_gone=()):
        """A phone where a terminate removes the pid from the process list,
        unless the pid ignores that signal; `already_gone` pids error."""
        backend = DevicectlBackend()
        backend.TERMINATE_WAIT_S = 0.05
        backend.TERMINATE_POLL_S = 0.01
        running = list(processes)
        self.signals: list[tuple[str, bool]] = []

        async def run(*args, **kwargs):
            if args[:3] == ("device", "info", "apps"):
                return json.dumps({"result": {"apps": list(apps)}}), ""
            if args[:3] == ("device", "info", "processes"):
                if running == ["unreadable"]:
                    return "not json", ""
                return json.dumps({"result": {"runningProcesses": running}}), ""
            if args[:3] == ("device", "process", "terminate"):
                pid, kill = int(args[args.index("--pid") + 1]), "--kill" in args
                self.signals.append((str(pid), kill))
                if pid in already_gone:
                    running[:] = [p for p in running if p["processIdentifier"] != pid]
                    raise DeviceError("devicectl device failed: No such process",
                                      tool="devicectl")
                if pid not in (ignores_kill if kill else ignores_term) and \
                        pid not in ignores_kill:
                    running[:] = [p for p in running if p["processIdentifier"] != pid]
                return "", ""
            return "", ""
        backend._run_devicectl = AsyncMock(side_effect=run)
        return backend

    APP = {"bundleIdentifier": "com.quern.probe.scene", "url": APP_URL}

    def _main(self, pid, url=APP_URL):
        return {"executable": url.rstrip("/") + "/QuernProbeScene", "processIdentifier": pid}

    async def test_the_running_app_is_terminated_and_confirmed_gone(self):
        backend = self._backend([self.APP], [
            self._main(1653),
            # An extension of the same app, deeper in the bundle: left alone.
            {"executable": APP_URL + "PlugIns/Widget.appex/Widget", "processIdentifier": 1654},
            {"executable": "file:///Applications/Preferences.app/Preferences",
             "processIdentifier": 1488}])

        assert await backend.terminate_app("UUID", "com.quern.probe.scene") is True
        assert self.signals == [("1653", False)]

    async def test_not_running_terminates_nothing(self):
        backend = self._backend([self.APP], [])
        assert await backend.terminate_app("UUID", "com.quern.probe.scene") is False
        assert self.signals == []

    async def test_every_instance_is_terminated(self):
        """A dying one beside a new one, after a restarting launch."""
        backend = self._backend([self.APP], [self._main(10), self._main(11)])
        assert await backend.terminate_app("UUID", "com.quern.probe.scene") is True
        assert sorted(self.signals) == [("10", False), ("11", False)]

    async def test_an_instance_already_gone_is_not_an_error(self):
        """It died between the listing and its signal: the app is stopped,
        which is what was asked, and the other instance is still signalled."""
        backend = self._backend([self.APP], [self._main(10), self._main(11)],
                                already_gone=(10,))
        assert await backend.terminate_app("UUID", "com.quern.probe.scene") is True
        assert ("11", False) in self.signals

    async def test_a_pid_that_ignores_sigterm_is_killed(self):
        backend = self._backend([self.APP], [self._main(10)], ignores_term=(10,))
        assert await backend.terminate_app("UUID", "com.quern.probe.scene") is True
        assert self.signals == [("10", False), ("10", True)]

    async def test_one_that_survives_sigkill_is_an_error_not_terminated(self):
        backend = self._backend([self.APP], [self._main(10)], ignores_kill=(10,))
        with pytest.raises(DeviceError, match="still running"):
            await backend.terminate_app("UUID", "com.quern.probe.scene")

    async def test_the_app_is_picked_out_of_a_listing_of_many(self):
        """--include-all-apps may ignore --bundle-id, per devicectl's help."""
        other = {"bundleIdentifier": "com.apple.Preferences",
                 "url": "file:///Applications/Preferences.app/"}
        backend = self._backend([other, self.APP], [
            {"executable": "file:///Applications/Preferences.app/Preferences",
             "processIdentifier": 1488}, self._main(1653)])
        assert await backend.terminate_app("UUID", "com.quern.probe.scene") is True
        assert self.signals == [("1653", False)]

    @pytest.mark.parametrize(("app_url", "executable"), [
        (APP_URL.rstrip("/"), APP_URL + "QuernProbeScene"),        # no trailing slash
        ("file:///private/var/containers/Bundle/Application/X/My%20App.app/",
         "file:///var/containers/Bundle/Application/X/My App.app/My App"),
        ("file:///var/containers/Bundle/Application/X/Caf\u00e9.app/",
         "file:///var/containers/Bundle/Application/X/Cafe\u0301.app/Cafe\u0301"),
    ])
    async def test_the_two_listings_need_not_spell_the_path_alike(self, app_url, executable):
        backend = self._backend([{"bundleIdentifier": "com.x", "url": app_url}],
                                [{"executable": executable, "processIdentifier": 7}])
        assert await backend.terminate_app("UUID", "com.x") is True

    async def test_an_app_not_installed_is_refused(self):
        """A mistyped bundle id is not a clean stop (#629)."""
        backend = self._backend([], [])
        with pytest.raises(InvalidDeviceRequestError, match="not installed"):
            await backend.terminate_app("UUID", "com.example.nothere")

    async def test_an_app_without_a_url_is_not_called_not_installed(self):
        backend = self._backend([{"bundleIdentifier": "com.quern.probe.scene"}], [])
        with pytest.raises(DeviceError, match="no bundle URL") as caught:
            await backend.terminate_app("UUID", "com.quern.probe.scene")
        assert not isinstance(caught.value, InvalidDeviceRequestError)

    async def test_an_unreadable_list_is_an_error_not_not_running(self):
        backend = self._backend([self.APP], ["unreadable"])
        with pytest.raises(DeviceError, match="process list could not be read"):
            await backend.terminate_app("UUID", "com.quern.probe.scene")


# ---------------------------------------------------------------------------
# uninstall_app
# ---------------------------------------------------------------------------


class TestUninstallApp:
    async def test_uninstall_app(self):
        backend = DevicectlBackend()
        backend._run_devicectl = AsyncMock(return_value=("", ""))

        await backend.uninstall_app("UUID", "com.example.app")

        backend._run_devicectl.assert_called_once_with(
            "device",
            "uninstall",
            "app",
            "--device",
            "UUID",
            "com.example.app",
        )


# ---------------------------------------------------------------------------
# list_apps
# ---------------------------------------------------------------------------


class TestListApps:
    async def test_parses_apps(self):
        apps_response = json.dumps(
            {
                "result": {
                    "apps": [
                        {"bundleIdentifier": "com.example.app", "name": "My App", "type": "User"},
                        {
                            "bundleIdentifier": "com.apple.mobilesafari",
                            "name": "Safari",
                            "type": "System",
                        },
                    ],
                },
            }
        )
        backend = DevicectlBackend()
        backend._run_devicectl = AsyncMock(return_value=(apps_response, ""))

        apps = await backend.list_apps("UUID")

        assert len(apps) == 2
        assert apps[0].bundle_id == "com.example.app"
        assert apps[0].name == "My App"
        assert apps[0].app_type == "User"
        assert apps[1].bundle_id == "com.apple.mobilesafari"

    async def test_bad_json_returns_empty(self):
        backend = DevicectlBackend()
        backend._run_devicectl = AsyncMock(return_value=("bad", ""))

        apps = await backend.list_apps("UUID")
        assert apps == []


# ---------------------------------------------------------------------------
# install_app
# ---------------------------------------------------------------------------


class TestInstallApp:
    async def test_install_calls_devicectl(self):
        backend = DevicectlBackend()
        backend._run_devicectl = AsyncMock(return_value=("", ""))

        await backend.install_app("UUID", "/path/to/app.ipa")

        backend._run_devicectl.assert_called_once_with(
            "device",
            "install",
            "app",
            "--device",
            "UUID",
            "/path/to/app.ipa",
        )


# ---------------------------------------------------------------------------
# _run_devicectl
# ---------------------------------------------------------------------------


class TestRunDevicectl:
    async def test_non_zero_exit_raises(self):
        backend = DevicectlBackend()

        with patch(
            "asyncio.create_subprocess_exec", return_value=_make_proc(1, stderr=b"error msg")
        ):
            with pytest.raises(DeviceError, match="error msg"):
                await backend._run_devicectl("list", "devices")

    async def test_json_output_reads_temp_file(self, tmp_path):
        backend = DevicectlBackend()
        expected_json = '{"result": "ok"}'

        with (
            patch("asyncio.create_subprocess_exec", return_value=_make_proc(0)),
            patch("tempfile.NamedTemporaryFile") as mock_tmp,
        ):
            mock_file = MagicMock()
            json_file = tmp_path / "output.json"
            json_file.write_text(expected_json)
            mock_file.name = str(json_file)
            mock_file.close = MagicMock()
            mock_tmp.return_value = mock_file

            stdout, _ = await backend._run_devicectl("list", "devices", json_output=True)
            assert stdout == expected_json


# ---------------------------------------------------------------------------
# Xcode-availability gate
# ---------------------------------------------------------------------------


class TestXcodeGate:
    """On a no-Xcode Mac, devicectl methods short-circuit without invoking
    xcrun. See server/device/ios/_xcode.py."""

    async def test_is_available_returns_false_without_invoking_xcrun(self, monkeypatch):
        monkeypatch.setattr("server.device.ios.devicectl.xcode_available", lambda: False)
        backend = DevicectlBackend()
        with patch("asyncio.create_subprocess_exec") as exec_mock:
            assert await backend.is_available() is False
        assert exec_mock.call_count == 0

    async def test_list_devices_returns_empty_without_invoking_xcrun(self, monkeypatch):
        monkeypatch.setattr("server.device.ios.devicectl.xcode_available", lambda: False)
        backend = DevicectlBackend()
        with patch("asyncio.create_subprocess_exec") as exec_mock:
            assert await backend.list_devices() == []
        assert exec_mock.call_count == 0


class TestSimulatorsAreNotPhysicalDevices:
    """Xcode 26 registers simulators as CoreDevices, so they show up here.

    Reported against 0.16.1: `resolve_device` wrote `{"type": "device"}` for a
    simulator, UI reads routed to WDA instead of sim-bridge, and
    `get_screen_summary` returned HTTP 500 against a healthy booted simulator.
    `hardwareProperties.reality` is exactly the signal needed -- "simulated" or
    "physical" -- and nothing read it.
    """

    def _with_reality(self, *realities: str) -> dict:
        """The fixture, with one entry per given reality value."""
        data = json.loads(_load_fixture("devicectl_list_output.json"))
        template = data["result"]["devices"][0]
        devices = []
        for i, reality in enumerate(realities):
            dev = json.loads(json.dumps(template))
            dev["identifier"] = f"UDID-{i}"
            dev["deviceProperties"]["name"] = f"Device {i}"
            dev["hardwareProperties"]["reality"] = reality
            devices.append(dev)
        data["result"]["devices"] = devices
        return data

    @pytest.mark.asyncio
    async def test_a_simulated_device_is_not_returned(self):
        # simctl is authoritative about simulators: it knows the runtime and
        # the real boot state. A second, worse entry for the same UDID is what
        # caused the misclassification.
        backend = DevicectlBackend()
        backend._run_devicectl = AsyncMock(
            return_value=(json.dumps(self._with_reality("simulated")), "")
        )
        assert await backend.list_devices() == []

    @pytest.mark.asyncio
    async def test_physical_devices_are_still_returned(self):
        backend = DevicectlBackend()
        backend._run_devicectl = AsyncMock(
            return_value=(json.dumps(self._with_reality("physical")), "")
        )
        devices = await backend.list_devices()
        assert len(devices) == 1
        assert devices[0].device_type == DeviceType.DEVICE

    @pytest.mark.asyncio
    async def test_only_the_simulated_ones_are_dropped(self):
        backend = DevicectlBackend()
        backend._run_devicectl = AsyncMock(
            return_value=(json.dumps(self._with_reality("physical", "simulated", "physical")), "")
        )
        devices = await backend.list_devices()
        assert [d.udid for d in devices] == ["UDID-0", "UDID-2"]

    @pytest.mark.asyncio
    async def test_a_missing_reality_field_is_treated_as_physical(self):
        # Older devicectl versions do not emit it. Dropping those devices would
        # be a worse failure than the one being fixed -- physical devices would
        # vanish -- so only an explicit "simulated" filters.
        data = self._with_reality("physical")
        del data["result"]["devices"][0]["hardwareProperties"]["reality"]
        backend = DevicectlBackend()
        backend._run_devicectl = AsyncMock(return_value=(json.dumps(data), ""))
        assert len(await backend.list_devices()) == 1


class TestIsInstalled:
    """#629: a phone's terminate refuses an app that is not installed."""

    @staticmethod
    def _apps(*ids):
        return json.dumps({"result": {"apps": [{"bundleIdentifier": i} for i in ids]}})

    async def test_asks_for_all_apps_so_system_apps_count(self):
        """Without --include-all-apps devicectl lists user apps only: measured,
        4 of 74 on an iPhone 11, Settings not among them."""
        backend = DevicectlBackend()
        backend._run_devicectl = AsyncMock(return_value=(self._apps("com.apple.Preferences"), ""))
        assert await backend.is_installed("UUID", "com.apple.Preferences") is True
        assert "--include-all-apps" in backend._run_devicectl.call_args.args

    async def test_an_absent_app(self):
        backend = DevicectlBackend()
        backend._run_devicectl = AsyncMock(return_value=(self._apps("com.example.app"), ""))
        assert await backend.is_installed("UUID", "com.typo.app") is False

    @pytest.mark.parametrize("stdout", ["not json", "{}", '{"result": {"apps": 3}}'])
    async def test_an_unreadable_list_raises_rather_than_reading_as_absent(self, stdout):
        backend = DevicectlBackend()
        backend._run_devicectl = AsyncMock(return_value=(stdout, ""))
        with pytest.raises(DeviceError, match="could not be read"):
            await backend.is_installed("UUID", "com.example.app")
