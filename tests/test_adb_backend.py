"""Tests for AdbBackend — mock all adb subprocess calls."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from server.device.adb import AdbBackend
from server.models import DeviceError, DeviceState, DeviceType


FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# is_available
# ---------------------------------------------------------------------------


class TestIsAvailable:
    async def test_available_when_adb_on_path(self):
        with patch("server.device.adb._find_sdk_tool", return_value="/usr/bin/adb"):
            backend = AdbBackend()
        assert await backend.is_available() is True

    async def test_unavailable_when_no_adb(self):
        with patch("server.device.adb._find_sdk_tool", return_value=None):
            backend = AdbBackend()
        assert await backend.is_available() is False


# ---------------------------------------------------------------------------
# list_devices
# ---------------------------------------------------------------------------


class TestListDevices:
    async def test_parse_adb_devices_output(self):
        """Parse the fixture file with emulator + physical + unauthorized + offline."""
        backend = AdbBackend()
        fixture_text = (FIXTURES_DIR / "adb_devices.txt").read_text()

        async def mock_run_adb(*args):
            if args == ("devices", "-l"):
                return fixture_text, ""
            raise DeviceError("unexpected call", tool="adb")

        backend._run_adb = AsyncMock(side_effect=mock_run_adb)
        backend._adb_path = "/usr/bin/adb"
        backend._get_device_property = AsyncMock(return_value="")
        backend._get_emulator_name = AsyncMock(return_value="Pixel_7_API_34")

        devices = await backend.list_devices()

        assert len(devices) == 4

        # Emulator
        emu = devices[0]
        assert emu.udid == "emulator-5554"
        assert emu.device_type == DeviceType.ANDROID_EMULATOR
        assert emu.state == DeviceState.BOOTED
        assert emu.is_available is True
        assert emu.device_family == "Android"
        assert emu.name == "Pixel_7_API_34"

        # Physical device
        phys = devices[1]
        assert phys.udid == "R5CR10XXXXX"
        assert phys.device_type == DeviceType.ANDROID_DEVICE
        assert phys.state == DeviceState.BOOTED
        assert phys.is_available is True
        assert phys.name == "SM G960F"  # model with underscores replaced

        # Unauthorized
        unauth = devices[2]
        assert unauth.udid == "AAAA1234BBBB"
        assert unauth.state == DeviceState.UNAUTHORIZED
        assert unauth.is_available is False

        # Offline
        offline = devices[3]
        assert offline.udid == "offline-device"
        assert offline.state == DeviceState.SHUTDOWN
        assert offline.is_available is False

    async def test_empty_when_no_adb(self):
        backend = AdbBackend()
        backend._adb_path = None
        devices = await backend.list_devices()
        assert devices == []

    async def test_empty_on_adb_error(self):
        backend = AdbBackend()
        backend._adb_path = "/usr/bin/adb"
        backend._run_adb = AsyncMock(side_effect=DeviceError("server not running", tool="adb"))
        devices = await backend.list_devices()
        assert devices == []


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------


class TestAppLifecycle:
    async def test_install_app(self):
        backend = AdbBackend()
        backend._run_adb_for_device = AsyncMock(return_value=("Success", ""))
        await backend.install_app("emulator-5554", "/path/to/app.apk")
        backend._run_adb_for_device.assert_called_once_with(
            "emulator-5554", "install", "-r", "/path/to/app.apk"
        )

    async def test_launch_app(self):
        backend = AdbBackend()
        backend._run_adb_for_device = AsyncMock(return_value=("", ""))
        await backend.launch_app("emulator-5554", "com.example.app")
        backend._run_adb_for_device.assert_called_once_with(
            "emulator-5554", "shell", "monkey",
            "-p", "com.example.app",
            "-c", "android.intent.category.LAUNCHER",
            "1",
        )

    async def test_terminate_app(self):
        backend = AdbBackend()
        backend._run_adb_for_device = AsyncMock(return_value=("", ""))
        await backend.terminate_app("emulator-5554", "com.example.app")
        backend._run_adb_for_device.assert_called_once_with(
            "emulator-5554", "shell", "am", "force-stop", "com.example.app"
        )

    async def test_uninstall_app(self):
        backend = AdbBackend()
        backend._run_adb_for_device = AsyncMock(return_value=("Success", ""))
        await backend.uninstall_app("emulator-5554", "com.example.app")
        backend._run_adb_for_device.assert_called_once_with(
            "emulator-5554", "uninstall", "com.example.app"
        )

    async def test_list_apps(self):
        backend = AdbBackend()
        backend._run_adb_for_device = AsyncMock(return_value=(
            "package:com.example.app\npackage:com.example.other\n", ""
        ))
        apps = await backend.list_apps("emulator-5554")
        assert len(apps) == 2
        assert apps[0].bundle_id == "com.example.app"
        assert apps[1].bundle_id == "com.example.other"
        assert apps[0].app_type == "User"


# ---------------------------------------------------------------------------
# Screenshot
# ---------------------------------------------------------------------------


class TestScreenshot:
    async def test_screenshot_returns_bytes(self):
        backend = AdbBackend()
        fake_png = b"\x89PNG\r\n\x1a\nfakedata"

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(fake_png, b""))
            mock_proc.returncode = 0
            mock_exec.return_value = mock_proc

            result = await backend.screenshot("emulator-5554")

        assert result == fake_png

    async def test_screenshot_raises_on_error(self):
        backend = AdbBackend()

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(b"", b"error: device not found"))
            mock_proc.returncode = 1
            mock_exec.return_value = mock_proc

            with pytest.raises(DeviceError, match="adb screencap failed"):
                await backend.screenshot("emulator-5554")
