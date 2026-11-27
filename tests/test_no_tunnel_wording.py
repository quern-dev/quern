"""A "no tunnel" answer leads with the way the phone was last reached (#781).

Measured with an iPhone 11 taken off Wi-Fi: every read, tap and screenshot
opened with tunneld's USB advice -- "Reconnect it and accept the trust
prompt" -- and the screenshot with usbmux's "Device not found", before the
one reason that applied: Apple's tunnel to a Wi-Fi phone did not open.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server.device.ios import devicectl
from server.device.ios.pmd3 import Pmd3Backend
from server.device.performance import no_tunnel_message
from server.models import DeviceError

CORE = "B34C4EE9-AF48-53C6-BD13-2BFA66E7EE91"
PHONE = "00008030-000C59623A69802E"
WHY = "Apple's tunnel to it did not open within 5s"
TUNNELD = "server.device.ios.tunneld"
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def on_wifi(monkeypatch):
    monkeypatch.setitem(devicectl._identity_aliases, CORE, CORE)
    monkeypatch.setitem(devicectl._identity_aliases, PHONE, CORE)
    monkeypatch.setitem(devicectl._last_transports, CORE, "localNetwork")


class TestTheMessage:
    def test_a_phone_last_seen_on_wifi_is_told_about_wifi_alone(self, on_wifi):
        message = no_tunnel_message(PHONE, True, WHY)
        assert message.startswith("no tunnel to 00008030, last seen on Wi-Fi: Apple's tunnel")
        assert "trust prompt" not in message and "tunneld" not in message
        assert "paired with this Mac, unlocked, and on the same network" in message

    def test_a_usb_phone_keeps_tunneld_first(self, monkeypatch):
        monkeypatch.setitem(devicectl._last_transports, CORE, "usb")
        message = no_tunnel_message(CORE, True, WHY)
        assert message.index("trust prompt") < message.index("Apple's tunnel to it")

    def test_a_phone_never_listed_is_told_both(self):
        message = no_tunnel_message(CORE, True, WHY)
        assert "trust prompt" in message and "Apple's tunnel to it" in message

    def test_the_reason_is_not_doubly_stopped(self, on_wifi):
        assert ".." not in no_tunnel_message(PHONE, True, WHY + ".")


class TestTheListingRemembersTheTransport:
    # The conftest stubs replace devicectl's listing unless a test opts out.
    @pytest.mark.device_discovery
    async def test_devicectl_records_how_each_phone_was_seen(self):
        data = json.loads((FIXTURES / "devicectl_list_output.json").read_text())
        second = data["result"]["devices"][1]["connectionProperties"]
        second["transportType"], second["tunnelState"] = "localNetwork", "disconnected"
        backend = devicectl.DevicectlBackend()
        backend._run_devicectl = AsyncMock(return_value=(json.dumps(data), ""))

        devices = await backend.list_devices()

        assert devicectl.last_transport(devices[0].udid) == "usb"
        assert devicectl.last_transport(devices[1].udid) == "localNetwork"
        assert devicectl.last_transport("never-listed") == ""


def _exec(seen):
    async def fake_exec(*argv, **kwargs):
        seen.append(list(argv))
        proc = MagicMock(returncode=1)
        proc.communicate = AsyncMock(return_value=(b"", b"Device not found"))
        return proc
    return fake_exec


def _no_tunnels():
    return (patch(f"{TUNNELD}.is_tunneld_running", AsyncMock(return_value=True)),
            patch(f"{TUNNELD}.resolve_tunnel_udid", AsyncMock(return_value=None)),
            patch(f"{TUNNELD}.find_pymobiledevice3_binary", return_value=Path("/bin/pmd3")))


class TestTheScreenshot:
    async def test_a_wifi_phone_with_no_tunnel_is_told_why_without_usbmux(self, on_wifi):
        seen: list = []
        a, b, c = _no_tunnels()
        with a, b, c, patch("asyncio.create_subprocess_exec", _exec(seen)):
            with pytest.raises(DeviceError) as e:
                await Pmd3Backend().screenshot(CORE)

        assert seen == []                              # usbmux was not asked
        assert "last seen on Wi-Fi" in str(e.value)
        assert "Device not found" not in str(e.value)

    async def test_a_phone_on_ios_17_with_no_tunnel_is_too(self):
        seen: list = []
        a, b, c = _no_tunnels()
        with a, b, c, patch("asyncio.create_subprocess_exec", _exec(seen)):
            with pytest.raises(DeviceError, match="no tunnel to"):
                await Pmd3Backend().screenshot(CORE, os_version="iOS 27.2.1")
        assert seen == []

    async def test_an_older_phone_is_asked_for_by_its_hardware_udid(self, monkeypatch):
        """The usbmux fallback was given the CoreDevice UUID, which usbmux
        does not know a phone by."""
        monkeypatch.setitem(devicectl._identity_aliases, CORE, CORE)
        monkeypatch.setitem(devicectl._identity_aliases, PHONE, CORE)
        seen: list = []
        a, b, c = _no_tunnels()
        with a, b, c, patch("asyncio.create_subprocess_exec", _exec(seen)):
            with pytest.raises(DeviceError):
                await Pmd3Backend().screenshot(CORE, os_version="16.7.10")
        assert seen[0][4:6] == ["--udid", PHONE]
