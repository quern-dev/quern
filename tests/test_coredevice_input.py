"""CoreDevice HID input for physical iPhones (#664, phase 2a): touch and buttons.

pymobiledevice3 is replaced by a fake that records every report, so each test
can say exactly what reached the "phone". The display info is the iPhone 11's
as measured on iOS 27.2.1 (828x1792 pixels at scale 2, rot0).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.device.controller import DeviceController
from server.device.ios import coredevice_input
from server.device.ios.coredevice_input import (
    CoreDeviceInput,
    NotSent,
    Screen,
    screen_from_display_info,
)
from server.models import DeviceError, DeviceType

PHONE = "B34C4EE9-AF48-53C6-BD13-2BFA66E7EE91"
CONTACT, RELEASE, DOWN, UP, SHIFT = 0xC2, 0x02, 1, 2, 0xE1


def _display(orientation="rot0", native="rot0", size=(828.0, 1792.0), scale=2):
    return {"displays": [
        {"primary": False, "currentMode": {"size": [0.0, 0.0], "preferredUIScale": 1}},
        {"primary": True, "nativeOrientation": native, "currentOrientation": orientation,
         "currentMode": {"size": list(size), "preferredUIScale": scale}},
    ]}


class FakeLib:
    """A pymobiledevice3 stand-in: every report lands in `sent`."""

    def __init__(self, display=None):
        self.sent: list[tuple] = []
        self.display = display or _display()
        self.connects = 0
        self.closed: list[str] = []
        self.fail: dict[str, Exception] = {}
        self.settles_itself = True
        lib = self

        class RSD:
            def __init__(self, address):
                pass

            async def connect(self):
                lib.connects += 1
                if "rsd" in lib.fail:
                    raise lib.fail["rsd"]

            async def close(self):
                lib.closed.append("rsd")

        class DeviceInfo:
            def __init__(self, rsd):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get_display_info(self):
                if "display" in lib.fail:
                    raise lib.fail["display"]
                return lib.display

        class HID:
            def __init__(self, rsd):
                self.name = "hid"

            async def connect(self):
                pass

            async def close(self):
                lib.closed.append(self.name)

            async def send_touchscreen(self, state, x, y):
                if state == RELEASE and "release" in lib.fail:
                    raise lib.fail["release"]
                lib.sent.append(("touch", state, x, y))

            async def create_keyboard_service(self):
                self.name = "keyboard"
                if "keyboard" in lib.fail:
                    raise lib.fail["keyboard"]
                return 7

            async def send_keyboard(self, service, usages):
                lib.sent.append(("key", service, tuple(usages)))

        class Indigo:
            def __init__(self, rsd):
                pass

            async def connect(self):
                if "indigo" in lib.fail:
                    raise lib.fail["indigo"]

            async def close(self):
                lib.closed.append("indigo")

            async def send_button(self, page, usage, state):
                if state == UP and "button-up" in lib.fail:
                    raise lib.fail["button-up"]
                lib.sent.append(("button", page, usage, state))

        self.ns = SimpleNamespace(
            RemoteServiceDiscoveryService=RSD, DeviceInfoService=DeviceInfo,
            UniversalHID=HID, IndigoHID=Indigo,
            CONTACT=CONTACT, RELEASE=RELEASE, BUTTON_DOWN=DOWN, BUTTON_UP=UP,
            ASCII_TO_HID={"a": (4, False), "A": (4, True), "b": (5, False), " ": (44, False)},
            KEY_LEFT_SHIFT=SHIFT,
        )

    def __call__(self):
        self.ns.settles_itself = self.settles_itself
        return self.ns

    def touches(self):
        return [s for s in self.sent if s[0] == "touch"]


@pytest.fixture
def lib(monkeypatch):
    fake = FakeLib()
    monkeypatch.setattr(coredevice_input, "_pmd3", fake)
    monkeypatch.setattr("server.device.performance._device_address",
                        AsyncMock(return_value=("fd00::1", 51000)))
    monkeypatch.setattr(coredevice_input, "TAP_HOLD_S", 0)
    return fake


@pytest.fixture
def wda():
    return SimpleNamespace(TOOL_NAME="wda", tap=AsyncMock(), swipe=AsyncMock(),
                           press_button=AsyncMock(), type_text=AsyncMock())


class TestTheScreen:
    def test_the_iphone_11_in_points(self):
        screen = screen_from_display_info(_display())
        assert (screen.width, screen.height, screen.orientation) == (414.0, 896.0, "rot0")

    def test_points_map_onto_the_panel(self):
        screen = Screen(414.0, 896.0, "rot0")
        assert screen.point(0, 0) == (0, 0)
        assert screen.point(207, 448) == (32768, 32768)
        assert screen.point(414, 896) == (65535, 65535)
        assert screen.point(-5, 2000) == (0, 65535)      # clamped, never wrapped

    def test_a_turned_screen_is_not_sent(self):
        with pytest.raises(NotSent, match="turned"):
            screen_from_display_info(_display(orientation="rot90"))

    @pytest.mark.parametrize("info", [
        {}, {"displays": []},
        {"displays": [{"primary": True, "currentMode": {"size": [0, 0],
                                                        "preferredUIScale": 2}}]},
        {"displays": [{"primary": True, "currentMode": {"size": [828, 1792]}}]},
    ])
    def test_an_unreadable_display_is_not_sent(self, info):
        with pytest.raises(NotSent):
            screen_from_display_info(info)


class TestTapAndSwipe:
    async def test_a_tap_is_contact_then_release_at_the_point(self, lib, wda):
        hid = CoreDeviceInput(wda)
        await hid.tap(PHONE, 207, 448)
        assert lib.touches() == [("touch", CONTACT, 32768, 32768),
                                 ("touch", RELEASE, 32768, 32768)]
        assert hid.delivered_by[PHONE] == "coredevice"
        wda.tap.assert_not_called()

    async def test_a_long_press_holds_its_contact(self, lib, wda, monkeypatch):
        slept = []
        real_sleep = asyncio.sleep
        monkeypatch.setattr(coredevice_input, "_sleep",
                            lambda s: slept.append(s) or real_sleep(0))
        await CoreDeviceInput(wda).tap(PHONE, 10, 10, hold=1.0)
        assert 1.0 in slept

    async def test_a_session_is_held_between_actions(self, lib, wda):
        hid = CoreDeviceInput(wda)
        await hid.tap(PHONE, 1, 1)
        await hid.tap(PHONE, 2, 2)
        assert lib.connects == 1

    async def test_a_swipe_streams_contacts_then_releases_at_the_end(self, lib, wda,
                                                                     monkeypatch):
        monkeypatch.setattr(coredevice_input, "_sleep", AsyncMock())
        await CoreDeviceInput(wda).swipe(PHONE, 207, 700, 207, 200, duration=0.5)
        touches = lib.touches()
        assert len(touches) == 32                     # 31 samples at 60 Hz, then release
        assert touches[0][1:] == (CONTACT, 32768, round(700 / 896 * 65535))
        assert touches[-2][1:] == (CONTACT, 32768, round(200 / 896 * 65535))
        assert touches[-1][1] == RELEASE

    async def test_a_swipe_held_at_the_end_waits_before_lifting(self, lib, wda, monkeypatch):
        slept = []
        monkeypatch.setattr(coredevice_input, "_sleep",
                            AsyncMock(side_effect=lambda s: slept.append(s)))
        await CoreDeviceInput(wda).swipe(PHONE, 10, 10, 10, 100, duration=0.1, hold=0.4)
        assert slept[-1] == 0.4


class TestNeverReSent:
    """Nothing sent: do it through WDA. Something sent: fail, never redo."""

    async def test_a_turned_screen_goes_through_wda(self, lib, wda):
        lib.display = _display(orientation="rot90")
        hid = CoreDeviceInput(wda)
        await hid.tap(PHONE, 5, 6, hold=None)
        wda.tap.assert_awaited_once_with(PHONE, 5, 6, hold=None)
        assert lib.touches() == []
        assert hid.delivered_by[PHONE] == "wda"

    async def test_no_tunnel_goes_through_wda(self, lib, wda, monkeypatch):
        monkeypatch.setattr("server.device.performance._device_address",
                            AsyncMock(side_effect=DeviceError("no tunnel", tool="pymobiledevice3")))
        await CoreDeviceInput(wda).swipe(PHONE, 1, 2, 3, 4, 0.5, edge="left")
        wda.swipe.assert_awaited_once_with(PHONE, 1, 2, 3, 4, 0.5, hold=0.0, edge="left")

    async def test_a_failed_connect_goes_through_wda(self, lib, wda):
        lib.fail["rsd"] = ConnectionRefusedError(61, "refused")
        await CoreDeviceInput(wda).tap(PHONE, 1, 1)
        wda.tap.assert_awaited_once()
        assert "rsd" in lib.closed

    async def test_a_failed_display_read_goes_through_wda(self, lib, wda):
        lib.fail["display"] = TimeoutError()
        await CoreDeviceInput(wda).tap(PHONE, 1, 1)
        wda.tap.assert_awaited_once()

    async def test_a_failure_after_contact_is_an_error_not_a_second_tap(self, lib, wda):
        lib.fail["release"] = ConnectionResetError("gone")
        hid = CoreDeviceInput(wda)
        with pytest.raises(DeviceError, match="not retried") as caught:
            await hid.tap(PHONE, 1, 1)
        assert caught.value.tool == "coredevice"
        wda.tap.assert_not_called()
        # A session that failed is dropped; the next action opens a fresh one.
        lib.fail.clear()
        await hid.tap(PHONE, 1, 1)
        assert lib.connects == 2


class TestButtons:
    async def test_home_is_pressed_and_released(self, lib, wda):
        await CoreDeviceInput(wda).press_button(PHONE, "home")
        assert [s for s in lib.sent if s[0] == "button"] == [
            ("button", 0x0C, 0x40, DOWN), ("button", 0x0C, 0x40, UP)]
        wda.press_button.assert_not_called()

    async def test_a_button_without_a_code_goes_through_wda(self, lib, wda):
        await CoreDeviceInput(wda).press_button(PHONE, "lock")
        wda.press_button.assert_awaited_once_with(PHONE, "lock")
        assert lib.sent == []

    async def test_a_failed_button_service_goes_through_wda(self, lib, wda):
        lib.fail["indigo"] = OSError("refused")
        await CoreDeviceInput(wda).press_button(PHONE, "volumeUp")
        wda.press_button.assert_awaited_once()

    async def test_a_press_that_fails_after_down_is_an_error(self, lib, wda):
        """A second Home press opens the app switcher."""
        lib.fail["button-up"] = ConnectionResetError("gone")
        with pytest.raises(DeviceError):
            await CoreDeviceInput(wda).press_button(PHONE, "home")
        wda.press_button.assert_not_called()


class TestSessions:
    async def test_an_idle_session_is_closed(self, lib, wda, monkeypatch):
        monkeypatch.setattr(coredevice_input, "IDLE_CLOSE_S", 0.2)
        hid = CoreDeviceInput(wda)
        await hid.tap(PHONE, 1, 1)
        await asyncio.sleep(0.5)
        assert PHONE not in hid._sessions
        assert "rsd" in lib.closed

    async def test_close_closes_every_session(self, lib, wda):
        hid = CoreDeviceInput(wda)
        await hid.tap(PHONE, 1, 1)
        await hid.close()
        assert hid._sessions == {} and "rsd" in lib.closed


def _phone(monkeypatch, reader="coredevice", os_version="iOS 27.2.1"):
    ctrl = DeviceController()
    ctrl._active_udid = PHONE
    ctrl._device_type_cache[PHONE] = DeviceType.DEVICE
    ctrl.wda_client._device_os_versions[PHONE] = os_version
    monkeypatch.setattr("server.device.controller_ui.get_device_ui_reader", lambda: reader)
    ctrl.coredevice_input.tap = AsyncMock()
    ctrl.coredevice_input.press_button = AsyncMock()
    ctrl.wda_client.tap = AsyncMock()
    ctrl.wda_client.press_button = AsyncMock()
    return ctrl


class TestTheControllerRoutes:
    async def test_typing_stays_with_wda(self, monkeypatch):
        """A HID keyboard types through the phone's hardware-keyboard layout:
        "Hello abc" arrived as "Hっぉあ BC" on a phone with a Japanese
        keyboard. WDA inserts the text itself."""
        ctrl = _phone(monkeypatch)
        ctrl.wda_client.type_text = AsyncMock()
        await ctrl.type_text("Hello abc", udid=PHONE)
        ctrl.wda_client.type_text.assert_awaited_once_with(PHONE, "Hello abc")
        assert not hasattr(ctrl.coredevice_input, "type_text")

    async def test_opted_in_on_ios_27_taps_over_hid(self, monkeypatch):
        ctrl = _phone(monkeypatch)
        await ctrl.tap(10, 20, udid=PHONE)
        ctrl.coredevice_input.tap.assert_awaited_once_with(PHONE, 10, 20)
        ctrl.wda_client.tap.assert_not_called()

    @pytest.mark.parametrize(("reader", "os_version"), [
        ("wda", "iOS 27.2.1"), ("coredevice", "iOS 26.7")])
    def test_otherwise_wda(self, monkeypatch, reader, os_version):
        ctrl = _phone(monkeypatch, reader, os_version)
        assert ctrl._input_backend(PHONE) is ctrl.wda_client

    async def test_buttons_route_the_same_way(self, monkeypatch):
        ctrl = _phone(monkeypatch)
        await ctrl.press_button("home", udid=PHONE)
        ctrl.coredevice_input.press_button.assert_awaited_once_with(PHONE, "home")

    async def test_a_tap_report_says_which_carried_it(self, monkeypatch):
        ctrl = _phone(monkeypatch)
        ctrl._checks_landing = lambda udid: False
        ctrl.coredevice_input.delivered_by[PHONE] = "wda"
        report = await ctrl.tap_and_report(10, 20, udid=PHONE)
        assert report["input"] == "wda"
