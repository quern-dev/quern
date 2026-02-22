"""Tests for UsbmuxBackend — pymobiledevice3 usbmux device discovery."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from server.device.usbmux import UsbmuxBackend
from server.models import DeviceState, DeviceType


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def backend():
    b = UsbmuxBackend()
    b._binary = "/usr/local/bin/pymobiledevice3"
    return b


def _load_fixture() -> list[dict]:
    return json.loads((FIXTURES / "usbmux_list_output.json").read_text())


# ---------------------------------------------------------------------------
# _parse_devices (unit tests — no subprocess)
# ---------------------------------------------------------------------------


class TestParseDevices:
    def test_filters_ios17_and_above(self):
        raw = _load_fixture()
        devices = UsbmuxBackend._parse_devices(raw)
        # Fixture has 4 devices: iOS 15, 16, 17, 18 — only 15 and 16 should pass
        assert len(devices) == 2
        versions = {d.os_version for d in devices}
        assert versions == {"iOS 15.8.3", "iOS 16.7.2"}

    def test_maps_fields_correctly(self):
        raw = _load_fixture()
        devices = UsbmuxBackend._parse_devices(raw)
        iphone7 = next(d for d in devices if "iPhone 7" in d.name)

        assert iphone7.udid == "5f9f02e25a8d7b3c1e4f6a9d2b8c0e1f3a5d7b9c"
        assert iphone7.name == "iPhone 7"
        assert iphone7.state == DeviceState.BOOTED
        assert iphone7.device_type == DeviceType.DEVICE
        assert iphone7.os_version == "iOS 15.8.3"
        assert iphone7.connection_type == "usb"
        assert iphone7.device_family == "iPhone"
        assert iphone7.is_connected is True

    def test_empty_list(self):
        assert UsbmuxBackend._parse_devices([]) == []

    def test_missing_product_version(self):
        raw = [{"UniqueDeviceID": "abc123", "DeviceName": "Test"}]
        assert UsbmuxBackend._parse_devices(raw) == []

    def test_missing_udid(self):
        raw = [{"ProductVersion": "15.0", "DeviceName": "Test"}]
        assert UsbmuxBackend._parse_devices(raw) == []

    def test_invalid_version_string(self):
        raw = [{"UniqueDeviceID": "abc", "ProductVersion": "beta", "DeviceName": "T"}]
        assert UsbmuxBackend._parse_devices(raw) == []

    def test_all_ios17_plus_filtered(self):
        raw = [
            {"UniqueDeviceID": "aaa", "ProductVersion": "17.0", "DeviceName": "A", "DeviceClass": "iPhone", "ConnectionType": "USB"},
            {"UniqueDeviceID": "bbb", "ProductVersion": "18.1", "DeviceName": "B", "DeviceClass": "iPad", "ConnectionType": "USB"},
        ]
        assert UsbmuxBackend._parse_devices(raw) == []

    def test_connection_type_lowercased(self):
        raw = [
            {"UniqueDeviceID": "aaa", "ProductVersion": "16.0", "DeviceName": "A", "DeviceClass": "iPhone", "ConnectionType": "WiFi"},
        ]
        devices = UsbmuxBackend._parse_devices(raw)
        assert devices[0].connection_type == "wifi"

    def test_missing_connection_type_defaults_to_usb(self):
        raw = [
            {"UniqueDeviceID": "aaa", "ProductVersion": "16.0", "DeviceName": "A", "DeviceClass": "iPhone"},
        ]
        devices = UsbmuxBackend._parse_devices(raw)
        assert devices[0].connection_type == "usb"


# ---------------------------------------------------------------------------
# list_devices (async with mocked subprocess)
# ---------------------------------------------------------------------------


class TestListDevices:
    async def test_happy_path(self, backend):
        fixture_json = (FIXTURES / "usbmux_list_output.json").read_bytes()
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(fixture_json, b""))
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            devices = await backend.list_devices()

        assert len(devices) == 2
        assert all(d.device_type == DeviceType.DEVICE for d in devices)

    async def test_binary_not_found(self):
        backend = UsbmuxBackend()
        # _binary is None, _find_binary returns None
        with patch("server.device.usbmux.UsbmuxBackend._find_binary", return_value=None):
            devices = await backend.list_devices()
        assert devices == []

    async def test_nonzero_exit_code(self, backend):
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"error"))
        mock_proc.returncode = 1

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            devices = await backend.list_devices()
        assert devices == []

    async def test_invalid_json(self, backend):
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"not json", b""))
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            devices = await backend.list_devices()
        assert devices == []

    async def test_subprocess_exception(self, backend):
        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
            devices = await backend.list_devices()
        assert devices == []


# ---------------------------------------------------------------------------
# is_available
# ---------------------------------------------------------------------------


class TestIsAvailable:
    async def test_available_when_binary_found(self):
        backend = UsbmuxBackend()
        with patch.object(backend, "_find_binary", return_value="/usr/local/bin/pymobiledevice3"):
            assert await backend.is_available() is True

    async def test_not_available_when_no_binary(self):
        backend = UsbmuxBackend()
        with patch.object(backend, "_find_binary", return_value=None):
            assert await backend.is_available() is False


# ---------------------------------------------------------------------------
# get_usb_udid_map
# ---------------------------------------------------------------------------


class TestGetUsbUdidMap:
    async def test_returns_all_devices_unfiltered(self, backend):
        """get_usb_udid_map returns ALL devices, including iOS 17+."""
        fixture_json = (FIXTURES / "usbmux_list_output.json").read_bytes()
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(fixture_json, b""))
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await backend.get_usb_udid_map()

        # Fixture has 4 devices — all should be included (no version filter)
        assert len(result) == 4
        assert result["iPhone 7"] == "5f9f02e25a8d7b3c1e4f6a9d2b8c0e1f3a5d7b9c"
        assert result["iPhone 14 Pro"] == "b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1"
        assert result["iPad Air"] == "c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2"

    async def test_empty_on_failure(self, backend):
        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
            result = await backend.get_usb_udid_map()
        assert result == {}

    async def test_skips_entries_without_name_or_udid(self, backend):
        raw = json.dumps([
            {"UniqueDeviceID": "aaa", "DeviceName": ""},
            {"UniqueDeviceID": "", "DeviceName": "Test"},
            {"UniqueDeviceID": "bbb", "DeviceName": "Good Device"},
        ]).encode()
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(raw, b""))
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await backend.get_usb_udid_map()

        assert result == {"Good Device": "bbb"}
