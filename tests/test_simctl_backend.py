"""Tests for SimctlBackend — mock asyncio.create_subprocess_exec."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from server.device.simctl import SimctlBackend
from server.models import DeviceError, DeviceState, DeviceType

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
                "xcrun", "simctl", "list", "devices",
                stdout=-1, stderr=-1,
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
                "sh", "-c", "xcrun simctl listapps X | plutil ...",
                stdout=-1, stderr=-1,
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
        assert SimctlBackend._parse_runtime(
            "com.apple.CoreSimulator.SimRuntime.iOS-18-6"
        ) == "iOS 18.6"

    def test_ios_17_2(self):
        assert SimctlBackend._parse_runtime(
            "com.apple.CoreSimulator.SimRuntime.iOS-17-2"
        ) == "iOS 17.2"

    def test_watchos(self):
        assert SimctlBackend._parse_runtime(
            "com.apple.CoreSimulator.SimRuntime.watchOS-11-0"
        ) == "watchOS 11.0"

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
                "xcrun", "simctl", "boot", "AAAA-1111",
                stdout=-1, stderr=-1,
            )

    async def test_shutdown(self):
        backend = SimctlBackend()
        proc = _mock_proc()
        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await backend.shutdown("AAAA-1111")
            mock_exec.assert_called_once_with(
                "xcrun", "simctl", "shutdown", "AAAA-1111",
                stdout=-1, stderr=-1,
            )

    async def test_install_app(self):
        backend = SimctlBackend()
        proc = _mock_proc()
        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await backend.install_app("AAAA-1111", "/path/to/App.app")
            mock_exec.assert_called_once_with(
                "xcrun", "simctl", "install", "AAAA-1111", "/path/to/App.app",
                stdout=-1, stderr=-1,
            )

    async def test_launch_app(self):
        backend = SimctlBackend()
        proc = _mock_proc()
        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await backend.launch_app("AAAA-1111", "com.example.App")
            mock_exec.assert_called_once_with(
                "xcrun", "simctl", "launch", "AAAA-1111", "com.example.App",
                stdout=-1, stderr=-1,
            )

    async def test_terminate_app(self):
        backend = SimctlBackend()
        proc = _mock_proc()
        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await backend.terminate_app("AAAA-1111", "com.example.App")
            mock_exec.assert_called_once_with(
                "xcrun", "simctl", "terminate", "AAAA-1111", "com.example.App",
                stdout=-1, stderr=-1,
            )

    async def test_uninstall_app(self):
        backend = SimctlBackend()
        proc = _mock_proc()
        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await backend.uninstall_app("AAAA-1111", "com.example.App")
            mock_exec.assert_called_once_with(
                "xcrun", "simctl", "uninstall", "AAAA-1111", "com.example.App",
                stdout=-1, stderr=-1,
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
                "xcrun", "simctl", "location", "AAAA-1111", "set", "37.7749,-122.4194",
                stdout=-1, stderr=-1,
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
                "xcrun", "simctl", "privacy", "AAAA-1111", "grant", "photos", "com.example.App",
                stdout=-1, stderr=-1,
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
