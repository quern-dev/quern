"""Tests for SimctlBackend — mock asyncio.create_subprocess_exec."""

from __future__ import annotations

import json
import plistlib
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from server.device.simctl import SimctlBackend
from server.models import DeviceError, DeviceState, DeviceType

#: This file *is* the discovery code -- the autouse stubs in conftest replace
#: these very methods, so it opts out rather than asserting against them.
#: The subprocess layer beneath is mocked, so nothing here reaches the machine.
pytestmark = pytest.mark.device_discovery

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_proc(stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
    """Create a mock async subprocess."""
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    return proc


# ---------------------------------------------------------------------------
# _run_simctl
# ---------------------------------------------------------------------------


class TestRunSimctl:
    async def test_success(self):
        backend = SimctlBackend()
        proc = _mock_proc(stdout=b"ok\n")
        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            stdout, stderr = await backend._run_simctl("list", "devices")
            assert stdout == "ok\n"
            mock_exec.assert_called_once_with(
                "xcrun",
                "simctl",
                "list",
                "devices",
                stdout=-1,
                stderr=-1,
            )

    async def test_nonzero_exit_raises(self):
        backend = SimctlBackend()
        proc = _mock_proc(stderr=b"no such device", returncode=1)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with pytest.raises(DeviceError, match="no such device"):
                await backend._run_simctl("boot", "bad-udid")


# ---------------------------------------------------------------------------
# _run_shell
# ---------------------------------------------------------------------------


class TestRunShell:
    async def test_success(self):
        backend = SimctlBackend()
        proc = _mock_proc(stdout=b'{"ok": true}')
        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            stdout, _ = await backend._run_shell("xcrun simctl listapps X | plutil ...")
            assert stdout == '{"ok": true}'
            mock_exec.assert_called_once_with(
                "sh",
                "-c",
                "xcrun simctl listapps X | plutil ...",
                stdout=-1,
                stderr=-1,
            )

    async def test_failure_raises(self):
        backend = SimctlBackend()
        proc = _mock_proc(stderr=b"plutil error", returncode=1)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with pytest.raises(DeviceError, match="plutil error"):
                await backend._run_shell("bad command")


# ---------------------------------------------------------------------------
# is_available
# ---------------------------------------------------------------------------


class TestIsAvailable:
    async def test_available(self):
        backend = SimctlBackend()
        proc = _mock_proc(returncode=0)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            assert await backend.is_available() is True

    async def test_not_available(self):
        backend = SimctlBackend()
        proc = _mock_proc(returncode=1)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            assert await backend.is_available() is False

    async def test_exception_returns_false(self):
        backend = SimctlBackend()
        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
            assert await backend.is_available() is False


# ---------------------------------------------------------------------------
# list_devices
# ---------------------------------------------------------------------------


class TestListDevices:
    async def test_parse_fixture(self):
        backend = SimctlBackend()
        fixture_data = (FIXTURES / "simctl_list_output.json").read_bytes()
        proc = _mock_proc(stdout=fixture_data)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            devices = await backend.list_devices()

        # Should have 5 available devices (1 booted + 4 shutdown); 1 unavailable excluded
        assert len(devices) == 5

        # Check the booted device
        booted = [d for d in devices if d.state == DeviceState.BOOTED]
        assert len(booted) == 1
        assert booted[0].name == "iPhone 16 Pro"
        assert booted[0].udid == "AAAA-1111-2222-3333-444444444444"
        assert booted[0].os_version == "iOS 18.6"
        assert booted[0].device_type == DeviceType.SIMULATOR

        # Check OS version parsing for iOS 17.2 devices
        ios17 = [d for d in devices if d.os_version == "iOS 17.2"]
        assert len(ios17) == 2  # iPhone 15 and iPhone 15 Pro (unavailable excluded)

    async def test_empty_runtime(self):
        """Empty device arrays are skipped."""
        backend = SimctlBackend()
        data = {"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-14-4": []}}
        proc = _mock_proc(stdout=json.dumps(data).encode())
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            devices = await backend.list_devices()
        assert devices == []

    async def test_unavailable_excluded(self):
        """Devices with isAvailable=false are excluded."""
        backend = SimctlBackend()
        data = {
            "devices": {
                "com.apple.CoreSimulator.SimRuntime.iOS-18-6": [
                    {"udid": "X", "name": "Test", "state": "Booted", "isAvailable": False},
                ]
            }
        }
        proc = _mock_proc(stdout=json.dumps(data).encode())
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            devices = await backend.list_devices()
        assert devices == []


# ---------------------------------------------------------------------------
# _parse_runtime
# ---------------------------------------------------------------------------


class TestParseRuntime:
    def test_ios_18_6(self):
        assert (
            SimctlBackend._parse_runtime("com.apple.CoreSimulator.SimRuntime.iOS-18-6")
            == "iOS 18.6"
        )

    def test_ios_17_2(self):
        assert (
            SimctlBackend._parse_runtime("com.apple.CoreSimulator.SimRuntime.iOS-17-2")
            == "iOS 17.2"
        )

    def test_watchos(self):
        assert (
            SimctlBackend._parse_runtime("com.apple.CoreSimulator.SimRuntime.watchOS-11-0")
            == "watchOS 11.0"
        )

    def test_unknown_format(self):
        result = SimctlBackend._parse_runtime("some-weird-runtime")
        assert result == "some-weird-runtime"


# ---------------------------------------------------------------------------
# boot / shutdown / install / launch / terminate
# ---------------------------------------------------------------------------


class TestSimctlCommands:
    async def test_boot(self):
        backend = SimctlBackend()
        proc = _mock_proc()
        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await backend.boot("AAAA-1111")
            mock_exec.assert_called_once_with(
                "xcrun",
                "simctl",
                "boot",
                "AAAA-1111",
                stdout=-1,
                stderr=-1,
            )

    async def test_shutdown(self):
        backend = SimctlBackend()
        proc = _mock_proc()
        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await backend.shutdown("AAAA-1111")
            mock_exec.assert_called_once_with(
                "xcrun",
                "simctl",
                "shutdown",
                "AAAA-1111",
                stdout=-1,
                stderr=-1,
            )

    async def test_install_app(self):
        backend = SimctlBackend()
        proc = _mock_proc()
        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await backend.install_app("AAAA-1111", "/path/to/App.app")
            mock_exec.assert_called_once_with(
                "xcrun",
                "simctl",
                "install",
                "AAAA-1111",
                "/path/to/App.app",
                stdout=-1,
                stderr=-1,
            )

    async def test_launch_app(self):
        backend = SimctlBackend()
        proc = _mock_proc()
        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await backend.launch_app("AAAA-1111", "com.example.App")
            mock_exec.assert_called_once()
            args, kwargs = mock_exec.call_args
            assert args == ("xcrun", "simctl", "launch", "AAAA-1111", "com.example.App")
            assert kwargs["env"]["SIMCTL_CHILD_QUERN_AUTOMATION"] == "YES"

    async def test_launch_app_with_custom_env(self):
        backend = SimctlBackend()
        proc = _mock_proc()
        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await backend.launch_app("AAAA-1111", "com.example.App", env={"DEBUG": "1"})
            mock_exec.assert_called_once()
            _, kwargs = mock_exec.call_args
            assert kwargs["env"]["SIMCTL_CHILD_QUERN_AUTOMATION"] == "YES"
            assert kwargs["env"]["SIMCTL_CHILD_DEBUG"] == "1"

    async def test_terminate_app(self):
        backend = SimctlBackend()
        proc = _mock_proc()
        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await backend.terminate_app("AAAA-1111", "com.example.App")
            mock_exec.assert_called_once_with(
                "xcrun",
                "simctl",
                "terminate",
                "AAAA-1111",
                "com.example.App",
                stdout=-1,
                stderr=-1,
            )

    async def test_uninstall_app(self):
        backend = SimctlBackend()
        proc = _mock_proc()
        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await backend.uninstall_app("AAAA-1111", "com.example.App")
            mock_exec.assert_called_once_with(
                "xcrun",
                "simctl",
                "uninstall",
                "AAAA-1111",
                "com.example.App",
                stdout=-1,
                stderr=-1,
            )


# ---------------------------------------------------------------------------
# list_apps
# ---------------------------------------------------------------------------


class TestListApps:
    async def test_parse_fixture(self):
        backend = SimctlBackend()
        fixture_data = (FIXTURES / "simctl_listapps_output.json").read_bytes()
        proc = _mock_proc(stdout=fixture_data)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            apps = await backend.list_apps("AAAA-1111")

        assert len(apps) == 3

        by_id = {a.bundle_id: a for a in apps}
        assert "com.example.MyApp" in by_id
        assert by_id["com.example.MyApp"].name == "My App"
        assert by_id["com.example.MyApp"].app_type == "User"

        assert "com.apple.mobilesafari" in by_id
        assert by_id["com.apple.mobilesafari"].name == "Safari"
        assert by_id["com.apple.mobilesafari"].app_type == "System"


# ---------------------------------------------------------------------------
# screenshot
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# set_location
# ---------------------------------------------------------------------------


class TestSetLocation:
    async def test_set_location(self):
        backend = SimctlBackend()
        proc = _mock_proc()
        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await backend.set_location("AAAA-1111", 37.7749, -122.4194)
            mock_exec.assert_called_once_with(
                "xcrun",
                "simctl",
                "location",
                "AAAA-1111",
                "set",
                "37.7749,-122.4194",
                stdout=-1,
                stderr=-1,
            )

    async def test_set_location_error(self):
        backend = SimctlBackend()
        proc = _mock_proc(stderr=b"invalid coordinates", returncode=1)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with pytest.raises(DeviceError, match="invalid coordinates"):
                await backend.set_location("AAAA-1111", 999, 999)


# ---------------------------------------------------------------------------
# grant_permission
# ---------------------------------------------------------------------------


class TestGrantPermission:
    async def test_grant_permission(self):
        backend = SimctlBackend()
        proc = _mock_proc()
        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await backend.grant_permission("AAAA-1111", "com.example.App", "photos")
            mock_exec.assert_called_once_with(
                "xcrun",
                "simctl",
                "privacy",
                "AAAA-1111",
                "grant",
                "photos",
                "com.example.App",
                stdout=-1,
                stderr=-1,
            )

    async def test_grant_permission_error(self):
        backend = SimctlBackend()
        proc = _mock_proc(stderr=b"unknown permission", returncode=1)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with pytest.raises(DeviceError, match="unknown permission"):
                await backend.grant_permission("AAAA-1111", "com.example.App", "badperm")


# ---------------------------------------------------------------------------
# screenshot
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# clear_app_data
# ---------------------------------------------------------------------------


class TestClearAppData:
    async def test_clear_app_data_success(self, tmp_path):
        backend = SimctlBackend()
        # Create fake container with some files/dirs
        container = tmp_path / "container"
        container.mkdir()
        (container / "Documents").mkdir()
        (container / "Library").mkdir()
        (container / "prefs.plist").write_text("data")

        async def mock_run_simctl(*args):
            return str(container) + "\n", ""

        with patch.object(backend, "_run_simctl", side_effect=mock_run_simctl):
            await backend.clear_app_data("AAAA-1111", "com.example.App")

        # Container dir itself must still exist
        assert container.exists()
        # All contents must be gone
        assert list(container.iterdir()) == []

    async def test_clear_app_data_missing_container(self, tmp_path):
        backend = SimctlBackend()
        nonexistent = tmp_path / "no-such-container"

        async def mock_run_simctl(*args):
            return str(nonexistent) + "\n", ""

        with patch.object(backend, "_run_simctl", side_effect=mock_run_simctl):
            with pytest.raises(DeviceError, match="App data container not found"):
                await backend.clear_app_data("AAAA-1111", "com.example.App")


class TestScreenshot:
    async def test_screenshot_reads_temp_file(self, tmp_path):
        backend = SimctlBackend()
        fake_png = b"\x89PNG\r\n\x1a\nfake-image-data"

        async def mock_run_simctl(*args):
            # Write fake image to the temp file path (last arg)
            path = args[-1]
            Path(path).write_bytes(fake_png)
            return "", ""

        with patch.object(backend, "_run_simctl", side_effect=mock_run_simctl):
            result = await backend.screenshot("AAAA-1111")

        assert result == fake_png


# ---------------------------------------------------------------------------
# Xcode-availability gate
# ---------------------------------------------------------------------------


class TestXcodeGate:
    """On a no-Xcode Mac, simctl methods short-circuit without invoking
    xcrun. The macOS install dialog fires before xcrun returns, so the
    backend can't recover after the fact — see server/device/_xcode.py."""

    async def test_is_available_returns_false_without_invoking_xcrun(self, monkeypatch):
        monkeypatch.setattr("server.device.simctl.xcode_available", lambda: False)
        backend = SimctlBackend()
        with patch("asyncio.create_subprocess_exec") as exec_mock:
            assert await backend.is_available() is False
        assert exec_mock.call_count == 0

    async def test_list_devices_returns_empty_without_invoking_xcrun(self, monkeypatch):
        monkeypatch.setattr("server.device.simctl.xcode_available", lambda: False)
        backend = SimctlBackend()
        with patch("asyncio.create_subprocess_exec") as exec_mock:
            assert await backend.list_devices() == []
        assert exec_mock.call_count == 0


class TestALaunchThatDidNotSurvive:
    """`simctl launch` reports the launch it *requested*.

    It exits 0 and prints a pid for an app the system then refuses. On iOS 27
    that is every app without a scene manifest: quern answered `launched`, the
    screen stayed on SpringBoard, and every later call failed as "no element
    found" — a reason with nothing to do with the cause (#235).
    """

    async def test_launch_reports_the_pid_it_was_given(self):
        backend = SimctlBackend()
        proc = _mock_proc(stdout=b"com.example.App: 4242")
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            assert await backend.launch_app("AAAA-1111", "com.example.App") == 4242

    async def test_an_unreadable_pid_is_reported_as_unknown(self):
        backend = SimctlBackend()
        with patch("asyncio.create_subprocess_exec", return_value=_mock_proc(stdout=b"ok")):
            assert await backend.launch_app("AAAA-1111", "com.example.App") is None

    def test_a_pid_that_is_gone_is_not_alive(self):
        with patch("server.device.simctl.os.kill", side_effect=ProcessLookupError):
            assert SimctlBackend.process_is_alive(4242) is False

    def test_an_unknown_pid_counts_as_alive(self):
        """Deliberately fails open: not knowing is not evidence of death, and
        treating it as one would turn a formatting change in simctl's output
        into every launch failing."""
        assert SimctlBackend.process_is_alive(None) is True

    def test_someone_elses_process_counts_as_alive(self):
        with patch("server.device.simctl.os.kill", side_effect=PermissionError):
            assert SimctlBackend.process_is_alive(4242) is True

    @staticmethod
    def _device(os_version: str):
        from server.models import DeviceInfo, DeviceState, DeviceType

        return DeviceInfo(
            udid="AAAA-1111", name="iPhone", state=DeviceState.BOOTED,
            device_type=DeviceType.SIMULATOR, os_version=os_version,
        )

    async def test_a_missing_scene_manifest_is_named_on_a_runtime_that_enforces_it(
        self, tmp_path,
    ):
        """The one cause diagnosable without reading the guest's log."""
        (tmp_path / "Info.plist").write_bytes(
            plistlib.dumps({"CFBundleIdentifier": "com.example.App"})
        )
        backend = SimctlBackend()
        with (
            patch.object(backend, "_run_simctl",
                         AsyncMock(return_value=(str(tmp_path), ""))),
            patch.object(backend, "list_devices",
                         AsyncMock(return_value=[self._device("iOS 27.0")])),
        ):
            reason = await backend.why_launch_failed("AAAA-1111", "com.example.App")
        assert "UIApplicationSceneManifest" in reason
        assert "iOS 27" in reason

    @pytest.mark.parametrize("os_version", ["iOS 18.6", "iOS 26.5"])
    async def test_a_missing_manifest_is_not_blamed_on_an_older_runtime(
        self, tmp_path, os_version,
    ):
        """An app with no manifest runs fine on iOS 26 and earlier, so a crash
        there would be told the wrong cause (CodeRabbit on #247)."""
        (tmp_path / "Info.plist").write_bytes(
            plistlib.dumps({"CFBundleIdentifier": "com.example.App"})
        )
        backend = SimctlBackend()
        with (
            patch.object(backend, "_run_simctl",
                         AsyncMock(return_value=(str(tmp_path), ""))),
            patch.object(backend, "list_devices",
                         AsyncMock(return_value=[self._device(os_version)])),
        ):
            reason = await backend.why_launch_failed("AAAA-1111", "com.example.App")
        assert "UIApplicationSceneManifest" not in reason
        assert "get_latest_crash" in reason

    async def test_an_unreadable_runtime_does_not_get_the_specific_reason(
        self, tmp_path,
    ):
        """Naming a cause on a runtime nobody identified is a guess."""
        (tmp_path / "Info.plist").write_bytes(plistlib.dumps({}))
        backend = SimctlBackend()
        with (
            patch.object(backend, "_run_simctl",
                         AsyncMock(return_value=(str(tmp_path), ""))),
            patch.object(backend, "list_devices", AsyncMock(return_value=[])),
        ):
            reason = await backend.why_launch_failed("AAAA-1111", "com.example.App")
        assert "UIApplicationSceneManifest" not in reason

    @pytest.mark.parametrize("os_version", ["tvOS 27.0", "watchOS 27.0", "xrOS 27.0"])
    async def test_a_non_ios_runtime_is_never_blamed_on_the_scene_lifecycle(
        self, tmp_path, os_version,
    ):
        """The scene requirement is an iOS rule, and launch_app runs against
        every simulator family. Taking the digits alone read "tvOS 27.0" as
        iOS 27 and told a tvOS app to adopt a lifecycle iOS enforces
        (CodeRabbit on #247)."""
        (tmp_path / "Info.plist").write_bytes(
            plistlib.dumps({"CFBundleIdentifier": "com.example.App"})
        )
        backend = SimctlBackend()
        with (
            patch.object(backend, "_run_simctl",
                         AsyncMock(return_value=(str(tmp_path), ""))),
            patch.object(backend, "list_devices",
                         AsyncMock(return_value=[self._device(os_version)])),
        ):
            reason = await backend.why_launch_failed("AAAA-1111", "com.example.App")
        assert "UIApplicationSceneManifest" not in reason
        assert "get_latest_crash" in reason

    async def test_unparseable_simctl_output_does_not_replace_the_failure(
        self, tmp_path,
    ):
        """This runs while already reporting a launch failure. list_devices
        calls json.loads, whose JSONDecodeError is a ValueError -- letting it
        escape would swap the real diagnosis for a traceback about simctl's
        output (CodeRabbit on #247)."""
        import json

        (tmp_path / "Info.plist").write_bytes(
            plistlib.dumps({"CFBundleIdentifier": "com.example.App"})
        )
        backend = SimctlBackend()
        with (
            patch.object(backend, "_run_simctl",
                         AsyncMock(return_value=(str(tmp_path), ""))),
            patch.object(
                backend, "list_devices",
                AsyncMock(side_effect=json.JSONDecodeError("bad", "{", 0)),
            ),
        ):
            reason = await backend.why_launch_failed("AAAA-1111", "com.example.App")
        assert "get_latest_crash" in reason
        assert "UIApplicationSceneManifest" not in reason

    async def test_an_app_that_has_a_scene_manifest_gets_the_generic_reason(
        self, tmp_path,
    ):
        """Naming the scene lifecycle for an app that already adopts it would
        send the reader to the wrong place."""
        (tmp_path / "Info.plist").write_bytes(plistlib.dumps(
            {"UIApplicationSceneManifest": {"UIApplicationSupportsMultipleScenes": False}}
        ))
        backend = SimctlBackend()
        with patch.object(backend, "_run_simctl",
                          AsyncMock(return_value=(str(tmp_path), ""))):
            reason = await backend.why_launch_failed("AAAA-1111", "com.example.App")
        assert "UIApplicationSceneManifest" not in reason
        assert "get_latest_crash" in reason

    async def test_an_unreadable_container_still_answers(self):
        backend = SimctlBackend()
        with patch.object(backend, "_run_simctl",
                          AsyncMock(side_effect=DeviceError("no container", tool="simctl"))):
            reason = await backend.why_launch_failed("AAAA-1111", "com.example.App")
        assert "get_latest_crash" in reason

    @pytest.mark.parametrize("keys,expected", [
        ({"CFBundleDisplayName": "Shown", "CFBundleName": "Short"}, "Shown"),
        ({"CFBundleName": "Short"}, "Short"),
        ({}, None),
    ])
    async def test_the_display_name_is_what_the_tree_will_show(
        self, tmp_path, keys, expected,
    ):
        (tmp_path / "Info.plist").write_bytes(plistlib.dumps(keys))
        backend = SimctlBackend()
        with patch.object(backend, "_run_simctl",
                          AsyncMock(return_value=(str(tmp_path), ""))):
            assert await backend.app_display_name("AAAA-1111", "com.example.App") == expected
