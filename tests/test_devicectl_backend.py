"""Tests for DevicectlBackend — mock subprocess calls to xcrun devicectl."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server.device.devicectl import DevicectlBackend
from server.models import AppInfo, DeviceError, DeviceState, DeviceType

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
        """All paired devices are returned with correct state."""
        fixture = _load_fixture("devicectl_list_output.json")
        backend = DevicectlBackend()

        backend._run_devicectl = AsyncMock(return_value=(fixture, ""))

        devices = await backend.list_devices()

        # Both paired devices should appear
        assert len(devices) == 2

        # First device: connected tunnel + booted
        dev = devices[0]
        assert dev.udid == "53DA57AA-1234-5678-9ABC-DEF012345678"
        assert dev.name == "John's iPhone"
        assert dev.state == DeviceState.BOOTED
        assert dev.device_type == DeviceType.DEVICE
        assert dev.os_version == "iOS 18.3.2"
        assert dev.connection_type == "usb"

        # Second device: disconnected, no bootState → shutdown
        dev2 = devices[1]
        assert dev2.udid == "BBBB2222-3333-4444-5555-666677778888"
        assert dev2.name == "Old iPad"
        assert dev2.state == DeviceState.SHUTDOWN
        assert dev2.device_type == DeviceType.DEVICE

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
        launch_response = json.dumps({
            "result": {"process": {"processIdentifier": 12345}},
        })
        backend = DevicectlBackend()
        backend._run_devicectl = AsyncMock(return_value=(launch_response, ""))

        pid = await backend.launch_app("SOME-UUID", "com.example.app")

        assert pid == 12345
        assert backend._launched_pids[("SOME-UUID", "com.example.app")] == 12345

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


class TestTerminateApp:
    async def test_terminate_with_known_pid(self):
        backend = DevicectlBackend()
        backend._launched_pids[("UUID", "com.example.app")] = 999
        backend._run_devicectl = AsyncMock(return_value=("", ""))

        await backend.terminate_app("UUID", "com.example.app")

        backend._run_devicectl.assert_called_once_with(
            "device", "process", "terminate",
            "--device", "UUID",
            "--pid", "999",
        )
        assert ("UUID", "com.example.app") not in backend._launched_pids

    async def test_terminate_unknown_pid_raises(self):
        backend = DevicectlBackend()

        with pytest.raises(DeviceError, match="No known PID"):
            await backend.terminate_app("UUID", "com.example.app")


# ---------------------------------------------------------------------------
# uninstall_app
# ---------------------------------------------------------------------------


class TestUninstallApp:
    async def test_uninstall_app(self):
        backend = DevicectlBackend()
        backend._run_devicectl = AsyncMock(return_value=("", ""))

        await backend.uninstall_app("UUID", "com.example.app")

        backend._run_devicectl.assert_called_once_with(
            "device", "uninstall", "app",
            "--device", "UUID",
            "com.example.app",
        )


# ---------------------------------------------------------------------------
# list_apps
# ---------------------------------------------------------------------------


class TestListApps:
    async def test_parses_apps(self):
        apps_response = json.dumps({
            "result": {
                "apps": [
                    {"bundleIdentifier": "com.example.app", "name": "My App", "type": "User"},
                    {"bundleIdentifier": "com.apple.mobilesafari", "name": "Safari", "type": "System"},
                ],
            },
        })
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
            "device", "install", "app",
            "--device", "UUID",
            "/path/to/app.ipa",
        )


# ---------------------------------------------------------------------------
# screenshot
# ---------------------------------------------------------------------------


class TestScreenshot:
    async def test_binary_not_found(self):
        backend = DevicectlBackend()

        with patch("server.device.tunneld.find_pymobiledevice3_binary", return_value=None):
            with pytest.raises(DeviceError, match="pymobiledevice3 not found"):
                await backend.screenshot("UUID")

    async def test_tunnel_route_success(self, tmp_path):
        """iOS 17+ path: tunneld running, tunnel found → uses --tunnel flag."""
        backend = DevicectlBackend()
        fake_png = b"\x89PNG\r\n\x1a\nfake_image_data"

        with patch("server.device.tunneld.is_tunneld_running", return_value=True), \
             patch("server.device.tunneld.find_pymobiledevice3_binary", return_value=Path("/bin/pmd3")), \
             patch("server.device.tunneld.resolve_tunnel_udid", return_value="00008130-AAAA"), \
             patch("asyncio.create_subprocess_exec") as mock_exec, \
             patch("tempfile.NamedTemporaryFile") as mock_tmp:
            mock_file = MagicMock()
            tmp_file = tmp_path / "screenshot.png"
            tmp_file.write_bytes(fake_png)
            mock_file.name = str(tmp_file)
            mock_file.__enter__ = MagicMock(return_value=mock_file)
            mock_file.__exit__ = MagicMock(return_value=False)
            mock_tmp.return_value = mock_file

            mock_exec.return_value = _make_proc(0)

            result = await backend.screenshot("UUID")
            assert result == fake_png

            # Verify --tunnel flag is used
            call_args = mock_exec.call_args[0]
            assert "--tunnel" in call_args
            assert "00008130-AAAA" in call_args

    async def test_fallback_no_tunneld(self, tmp_path):
        """iOS 16- path: tunneld not running → direct usbmuxd (no --tunnel)."""
        backend = DevicectlBackend()
        fake_png = b"\x89PNG\r\n\x1a\nfake_image_data"

        with patch("server.device.tunneld.is_tunneld_running", return_value=False), \
             patch("server.device.tunneld.find_pymobiledevice3_binary", return_value=Path("/bin/pmd3")), \
             patch("asyncio.create_subprocess_exec") as mock_exec, \
             patch("tempfile.NamedTemporaryFile") as mock_tmp:
            mock_file = MagicMock()
            tmp_file = tmp_path / "screenshot.png"
            tmp_file.write_bytes(fake_png)
            mock_file.name = str(tmp_file)
            mock_file.__enter__ = MagicMock(return_value=mock_file)
            mock_file.__exit__ = MagicMock(return_value=False)
            mock_tmp.return_value = mock_file

            mock_exec.return_value = _make_proc(0)

            result = await backend.screenshot("UUID")
            assert result == fake_png

            # Verify no --tunnel flag
            call_args = mock_exec.call_args[0]
            assert "--tunnel" not in call_args
            assert "developer" in call_args
            assert "screenshot" in call_args

    async def test_fallback_no_tunnel_for_device(self, tmp_path):
        """Tunneld running but device not in tunnel list → falls back to direct."""
        backend = DevicectlBackend()
        fake_png = b"\x89PNG\r\n\x1a\nfake_image_data"

        with patch("server.device.tunneld.is_tunneld_running", return_value=True), \
             patch("server.device.tunneld.find_pymobiledevice3_binary", return_value=Path("/bin/pmd3")), \
             patch("server.device.tunneld.resolve_tunnel_udid", return_value=None), \
             patch("asyncio.create_subprocess_exec") as mock_exec, \
             patch("tempfile.NamedTemporaryFile") as mock_tmp:
            mock_file = MagicMock()
            tmp_file = tmp_path / "screenshot.png"
            tmp_file.write_bytes(fake_png)
            mock_file.name = str(tmp_file)
            mock_file.__enter__ = MagicMock(return_value=mock_file)
            mock_file.__exit__ = MagicMock(return_value=False)
            mock_tmp.return_value = mock_file

            mock_exec.return_value = _make_proc(0)

            result = await backend.screenshot("UUID")
            assert result == fake_png

            call_args = mock_exec.call_args[0]
            assert "--tunnel" not in call_args

    async def test_screenshot_failure(self, tmp_path):
        backend = DevicectlBackend()

        with patch("server.device.tunneld.is_tunneld_running", return_value=True), \
             patch("server.device.tunneld.find_pymobiledevice3_binary", return_value=Path("/bin/pmd3")), \
             patch("server.device.tunneld.resolve_tunnel_udid", return_value="00008130-AAAA"), \
             patch("asyncio.create_subprocess_exec") as mock_exec, \
             patch("tempfile.NamedTemporaryFile") as mock_tmp:
            mock_file = MagicMock()
            tmp_file = tmp_path / "screenshot.png"
            tmp_file.write_bytes(b"")
            mock_file.name = str(tmp_file)
            mock_file.__enter__ = MagicMock(return_value=mock_file)
            mock_file.__exit__ = MagicMock(return_value=False)
            mock_tmp.return_value = mock_file

            mock_exec.return_value = _make_proc(1, stderr=b"capture error")

            with pytest.raises(DeviceError, match="pymobiledevice3 screenshot failed"):
                await backend.screenshot("UUID")

    async def test_developer_disk_error(self, tmp_path):
        """Older iOS without DeveloperDiskImage gives a helpful error."""
        backend = DevicectlBackend()

        with patch("server.device.tunneld.is_tunneld_running", return_value=False), \
             patch("server.device.tunneld.find_pymobiledevice3_binary", return_value=Path("/bin/pmd3")), \
             patch("asyncio.create_subprocess_exec") as mock_exec, \
             patch("tempfile.NamedTemporaryFile") as mock_tmp:
            mock_file = MagicMock()
            tmp_file = tmp_path / "screenshot.png"
            tmp_file.write_bytes(b"")
            mock_file.name = str(tmp_file)
            mock_file.__enter__ = MagicMock(return_value=mock_file)
            mock_file.__exit__ = MagicMock(return_value=False)
            mock_tmp.return_value = mock_file

            mock_exec.return_value = _make_proc(
                1, stderr=b"DeveloperDiskImage is not mounted",
            )

            with pytest.raises(DeviceError, match="Developer disk image not mounted"):
                await backend.screenshot("UUID")


# ---------------------------------------------------------------------------
# _run_devicectl
# ---------------------------------------------------------------------------


class TestRunDevicectl:
    async def test_non_zero_exit_raises(self):
        backend = DevicectlBackend()

        with patch("asyncio.create_subprocess_exec", return_value=_make_proc(1, stderr=b"error msg")):
            with pytest.raises(DeviceError, match="error msg"):
                await backend._run_devicectl("list", "devices")

    async def test_json_output_reads_temp_file(self, tmp_path):
        backend = DevicectlBackend()
        expected_json = '{"result": "ok"}'

        with patch("asyncio.create_subprocess_exec", return_value=_make_proc(0)), \
             patch("tempfile.NamedTemporaryFile") as mock_tmp:
            mock_file = MagicMock()
            json_file = tmp_path / "output.json"
            json_file.write_text(expected_json)
            mock_file.name = str(json_file)
            mock_file.close = MagicMock()
            mock_tmp.return_value = mock_file

            stdout, _ = await backend._run_devicectl("list", "devices", json_output=True)
            assert stdout == expected_json
