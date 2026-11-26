"""No WebDriverAgent where it cannot help: a phone quern has no tunnel to, and
a runner that never answered.

Measured on 2026-11-25 on an iPhone 11 (iOS 27.2.1) on Wi-Fi only, where
quern's tunneld has no tunnel to it. get_screen_summary's push read failed
with "no tunnel", fell back to WDA, and auto-started a runner on the phone --
which quern reaches through that same tunnel, so it never answered. The call
took 8.6 s and said "WDA did not become responsive" instead of why, and the
runner stayed up on the phone for six hours, through two server restarts.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from server.device.ios import coredevice_input, wda_client
from server.device.ios import wda as wda_module
from server.device.ios.coredevice_input import CoreDeviceInput
from server.device.ios.wda_client import WdaBackend
from server.models import DeviceError, NoTunnelError
from tests.test_coredevice_ax import PHONE, _phone_controller
from tests.test_coredevice_input import FakeLib


class TestNoTunnelIsItsOwnFailure:
    async def test_the_tunnel_lookup_says_no_tunnel_by_type(self, monkeypatch):
        from server.device import performance
        from server.device.ios import tunneld

        monkeypatch.setattr(tunneld, "is_tunneld_running", AsyncMock(return_value=True))
        monkeypatch.setattr(tunneld, "resolve_tunnel_udid", AsyncMock(return_value=None))
        with pytest.raises(NoTunnelError, match="no tunnel to"):
            await performance._device_address(PHONE)


class TestAReadWithNoTunnel:
    async def test_it_is_not_read_through_wda(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl.coredevice_ax.describe_all = AsyncMock(
            side_effect=NoTunnelError("no tunnel to 00008030", tool="pymobiledevice3"))

        with pytest.raises(NoTunnelError, match="no tunnel to"):
            await ctrl.get_ui_elements(PHONE, use_cache=False, whole_screen=True)

        ctrl.wda_client.describe_all.assert_not_called()

    async def test_another_failure_still_is(self, monkeypatch):
        """The cut is narrow: a read that failed for another reason, with a
        tunnel there, still goes to WDA."""
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl.coredevice_ax.describe_all = AsyncMock(
            side_effect=DeviceError("accessibility service failed", tool="pymobiledevice3"))
        ctrl._no_wda_fallback = AsyncMock(return_value=None)

        await ctrl.get_ui_elements(PHONE, use_cache=False, whole_screen=True)

        ctrl.wda_client.describe_all.assert_awaited()


class TestInputWithNoTunnel:
    @pytest.fixture
    def lib(self, monkeypatch):
        fake = FakeLib()
        monkeypatch.setattr(coredevice_input, "_pmd3", fake)
        monkeypatch.setattr("server.device.performance._device_address",
                            AsyncMock(side_effect=NoTunnelError("no tunnel to 00008030",
                                                                tool="pymobiledevice3")))
        return fake

    async def test_a_tap_is_not_sent_through_wda(self, lib):
        wda = AsyncMock()
        with pytest.raises(NoTunnelError, match="was not sent: .*no tunnel to"):
            await CoreDeviceInput(wda).tap(PHONE, 10, 10)
        wda.tap.assert_not_called()


@pytest.fixture
def backend(monkeypatch):
    """A WDA client that never reaches a WDA by itself, so every call takes
    the auto-start path."""
    b = WdaBackend()
    b._try_tunneld_connection = AsyncMock(return_value=None)
    b._start_usbmux_forward = AsyncMock(side_effect=DeviceError("no forward", tool="wda"))
    monkeypatch.setattr(wda_client, "_refuse_if_held", lambda udid, what: None)
    from server.tooling import wda_availability
    monkeypatch.setattr(wda_availability, "wda_unavailable_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(wda_module, "start_in_flight", lambda udid: None)
    return b


class TestARunnerThatNeverAnswered:
    @pytest.mark.usefixtures("wda_fallback_on")
    @pytest.mark.parametrize("os_version, stopped", [("iOS 27.2.1", True), ("18.5", False)])
    async def test_it_is_stopped_on_a_phone_the_push_reads(
            self, backend, monkeypatch, os_version, stopped):
        """Before iOS 27 WDA is the only reader, and a slow first start must
        not be killed over and over; from 27 it is a fallback nobody used."""
        backend._device_os_versions[PHONE] = os_version
        monkeypatch.setattr(wda_module, "start_driver",
                            AsyncMock(return_value={"status": "started", "ready": False}))
        stop = AsyncMock(return_value={"status": "stopped"})
        monkeypatch.setattr(wda_module, "stop_driver", stop)

        with pytest.raises(DeviceError, match="did not become responsive") as caught:
            await backend._get_base_url(PHONE)

        assert stop.await_count == (1 if stopped else 0)
        assert ("stopped again" in str(caught.value)) is stopped


    @pytest.mark.usefixtures("wda_fallback_on")
    async def test_the_next_read_does_not_start_another_one(self, backend, monkeypatch):
        """Stopping it must not turn every read into a start and a stop: the
        next one is refused for a while, saying why (CodeRabbit on #779)."""
        backend._device_os_versions[PHONE] = "iOS 27.2.1"
        never = "WDA announced its server, but /status did not answer"
        start = AsyncMock(return_value={"status": "started", "ready": False, "error": never})
        monkeypatch.setattr(wda_module, "start_driver", start)
        stop = AsyncMock(return_value={"status": "stopped"})
        monkeypatch.setattr(wda_module, "stop_driver", stop)

        with pytest.raises(DeviceError, match="did not become responsive"):
            await backend._get_base_url(PHONE)
        with pytest.raises(DeviceError, match="never answered .* so it was stopped") as again:
            await backend._get_base_url(PHONE)

        assert start.await_count == 1
        assert "running but not answering" not in str(again.value)
        backend.note_started(PHONE)
        assert backend.no_auto_start_reason(PHONE) is None
