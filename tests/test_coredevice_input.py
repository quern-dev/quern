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
from server.models import DeviceError, DeviceType, InvalidDeviceRequestError, UIElement

PHONE = "B34C4EE9-AF48-53C6-BD13-2BFA66E7EE91"
CONTACT, RELEASE, DOWN, UP = 0xC2, 0x02, 1, 2


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
        #: Fail the contact report with this number (1 is the first).
        self.fail_contact: tuple[int, Exception] | None = None
        #: Names of calls that never return: "send", "display".
        self.hang: set[str] = set()
        self.contacts = 0
        lib = self

        async def maybe_hang(what):
            if what in lib.hang:
                await asyncio.Event().wait()

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
                await maybe_hang("display")
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
                await maybe_hang("send")
                if state == RELEASE and "release" in lib.fail:
                    raise lib.fail["release"]
                if state == CONTACT:
                    lib.contacts += 1
                    if lib.fail_contact and lib.fail_contact[0] == lib.contacts:
                        raise lib.fail_contact[1]
                lib.sent.append(("touch", state, x, y))

        class Indigo:
            def __init__(self, rsd):
                pass

            async def connect(self):
                if "indigo" in lib.fail:
                    raise lib.fail["indigo"]

            async def close(self):
                lib.closed.append("indigo")

            async def send_button(self, page, usage, state):
                await maybe_hang("send")
                if state == DOWN and "button-down" in lib.fail:
                    raise lib.fail["button-down"]
                if state == UP and "button-up" in lib.fail:
                    raise lib.fail["button-up"]
                lib.sent.append(("button", page, usage, state))

        self.ns = SimpleNamespace(
            RemoteServiceDiscoveryService=RSD, DeviceInfoService=DeviceInfo,
            UniversalHID=HID, IndigoHID=Indigo,
            CONTACT=CONTACT, RELEASE=RELEASE, BUTTON_DOWN=DOWN, BUTTON_UP=UP,
        )

    def __call__(self):
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

    @pytest.mark.parametrize("change", ["second primary", "external"])
    def test_not_exactly_one_built_in_primary_is_not_sent(self, change):
        """As AXe: which panel the touchscreen belongs to would be a guess."""
        info = _display()
        if change == "second primary":
            info["displays"][0]["primary"] = True
        else:
            info["displays"][1]["external"] = True
        with pytest.raises(NotSent, match="primary displays"):
            screen_from_display_info(info)

    def test_a_negative_size_is_not_a_screen(self):
        with pytest.raises(NotSent):
            screen_from_display_info(_display(size=(-828.0, 1792.0)))

    @pytest.mark.parametrize("missing", [
        ("nativeOrientation",), ("currentOrientation",),
        ("nativeOrientation", "currentOrientation"),   # equal, as None: not "not turned"
    ])
    def test_an_unread_orientation_is_not_taken_as_portrait(self, missing):
        """'Could not tell' must not read as 'not turned'."""
        info = _display()
        for key in missing:
            del info["displays"][1][key]
        with pytest.raises(NotSent, match="could not be read"):
            screen_from_display_info(info)


class TestTapAndSwipe:
    async def test_a_tap_is_contact_then_release_at_the_point(self, lib, wda):
        carried_by = await CoreDeviceInput(wda).tap(PHONE, 207, 448)
        assert lib.touches() == [("touch", CONTACT, 32768, 32768),
                                 ("touch", RELEASE, 32768, 32768)]
        assert carried_by == "coredevice"
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

    async def test_an_edge_swipe_from_its_edge_goes_over_hid(self, lib, wda, monkeypatch):
        monkeypatch.setattr(coredevice_input, "_sleep", AsyncMock())
        hid = CoreDeviceInput(wda)
        assert await hid.swipe(PHONE, 1, 400, 300, 400, 0.3, edge="left") == "coredevice"
        wda.swipe.assert_not_called()

    async def test_an_edge_swipe_from_the_middle_is_refused_not_sent(self, lib, wda):
        """As WDA refuses it: the device would take it as an ordinary swipe
        across the screen, and report success."""
        hid = CoreDeviceInput(wda)
        with pytest.raises(InvalidDeviceRequestError, match="must start within"):
            await hid.swipe(PHONE, 200, 400, 400, 400, 0.3, edge="left")
        assert lib.touches() == []
        wda.swipe.assert_not_called()
        assert PHONE in hid._sessions          # nothing wrong with the session

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
        carried_by = await CoreDeviceInput(wda).tap(PHONE, 5, 6, hold=None)
        wda.tap.assert_awaited_once_with(PHONE, 5, 6, hold=None)
        assert lib.touches() == []
        assert carried_by == "wda"

    @pytest.mark.parametrize("error", [
        DeviceError("no tunnel", tool="pymobiledevice3"),
        OSError(61, "tunneld refused"),          # not a DeviceError: still nothing sent
    ])
    async def test_no_tunnel_goes_through_wda(self, lib, wda, monkeypatch, error):
        monkeypatch.setattr("server.device.performance._device_address",
                            AsyncMock(side_effect=error))
        await CoreDeviceInput(wda).swipe(PHONE, 1, 2, 3, 4, 0.5, edge="left")
        wda.swipe.assert_awaited_once_with(PHONE, 1, 2, 3, 4, 0.5, hold=0.0, edge="left")

    async def test_an_older_pymobiledevice3_goes_through_wda(self, wda, monkeypatch):
        """The module there, one of the names it needs not: the real _pmd3."""
        from pymobiledevice3.remote.core_device import hid_service

        monkeypatch.delattr(hid_service, "IndigoHIDService")
        monkeypatch.setattr("server.device.performance._device_address",
                            AsyncMock(return_value=("fd00::1", 51000)))
        assert await CoreDeviceInput(wda).tap(PHONE, 1, 1) == "wda"
        wda.tap.assert_awaited_once()

    async def test_a_failed_connect_goes_through_wda(self, lib, wda):
        lib.fail["rsd"] = ConnectionRefusedError(61, "refused")
        await CoreDeviceInput(wda).tap(PHONE, 1, 1)
        wda.tap.assert_awaited_once()
        assert "rsd" in lib.closed

    async def test_a_failed_display_read_goes_through_wda_and_drops_the_session(self, lib, wda):
        """Not kept: a tunnel that moved would otherwise put every action on
        WDA for as long as the phone stays busy."""
        lib.fail["display"] = TimeoutError()
        hid = CoreDeviceInput(wda)
        await hid.tap(PHONE, 1, 1)
        wda.tap.assert_awaited_once()
        assert PHONE not in hid._sessions and "rsd" in lib.closed
        lib.fail.clear()
        assert await hid.tap(PHONE, 1, 1) == "coredevice"
        assert lib.connects == 2

    async def test_a_turned_screen_keeps_the_session(self, lib, wda):
        """Nothing wrong with the connection: only the screen cannot be mapped."""
        lib.display = _display(orientation="rot90")
        hid = CoreDeviceInput(wda)
        await hid.tap(PHONE, 1, 1)
        assert PHONE in hid._sessions

    async def test_a_hung_display_read_is_bounded(self, lib, wda, monkeypatch):
        monkeypatch.setattr(coredevice_input, "SEND_S", 0.1)
        lib.hang.add("display")
        hid = CoreDeviceInput(wda)
        assert await asyncio.wait_for(hid.tap(PHONE, 1, 1), 2) == "wda"
        assert PHONE not in hid._sessions

    async def test_a_hung_write_is_bounded_and_frees_the_phone(self, lib, wda, monkeypatch):
        """A half-open tunnel: the write never returns. Unbounded, it held the
        session lock for good, and every later action queued behind it."""
        monkeypatch.setattr(coredevice_input, "SEND_S", 0.1)
        lib.hang.add("send")
        hid = CoreDeviceInput(wda)
        with pytest.raises(DeviceError, match="not retried"):
            await asyncio.wait_for(hid.tap(PHONE, 1, 1), 2)
        wda.tap.assert_not_called()
        lib.hang.clear()
        assert await asyncio.wait_for(hid.tap(PHONE, 1, 1), 2) == "coredevice"

    async def test_a_failure_on_the_first_contact_is_an_error(self, lib, wda):
        """The write may have reached the phone before it failed."""
        lib.fail_contact = (1, ConnectionResetError("gone"))
        with pytest.raises(DeviceError, match="not retried"):
            await CoreDeviceInput(wda).tap(PHONE, 1, 1)
        wda.tap.assert_not_called()

    async def test_a_failure_part_way_through_a_swipe_is_an_error(self, lib, wda, monkeypatch):
        monkeypatch.setattr(coredevice_input, "_sleep", AsyncMock())
        lib.fail_contact = (5, ConnectionResetError("gone"))
        with pytest.raises(DeviceError, match="not retried"):
            await CoreDeviceInput(wda).swipe(PHONE, 10, 700, 10, 200, duration=0.5)
        wda.swipe.assert_not_called()

    async def test_a_cancelled_tap_drops_its_session(self, lib, wda, monkeypatch):
        """Cancelled with its contact down: the next action must not reuse a
        session with a finger that never lifted."""
        hid = CoreDeviceInput(wda)
        task = asyncio.create_task(hid.tap(PHONE, 1, 1, hold=30))
        while lib.contacts == 0:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert PHONE not in hid._sessions and "rsd" in lib.closed

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

    async def test_a_press_that_fails_on_down_is_an_error(self, lib, wda):
        lib.fail["button-down"] = ConnectionResetError("gone")
        with pytest.raises(DeviceError, match="not retried"):
            await CoreDeviceInput(wda).press_button(PHONE, "home")
        wda.press_button.assert_not_called()

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

    async def test_a_session_in_use_is_not_closed_as_idle(self, lib, wda, monkeypatch):
        """A long press outlasting the idle time keeps its session: the
        release still goes out on it."""
        monkeypatch.setattr(coredevice_input, "IDLE_CLOSE_S", 0.2)
        hid = CoreDeviceInput(wda)
        # Held three times the idle limit: only the session's lock keeps the
        # reaper off it.
        assert await hid.tap(PHONE, 1, 1, hold=0.6) == "coredevice"
        assert "rsd" not in lib.closed and PHONE in hid._sessions

    async def test_close_closes_every_session(self, lib, wda):
        hid = CoreDeviceInput(wda)
        await hid.tap(PHONE, 1, 1)
        await hid.close()
        assert hid._sessions == {} and "rsd" in lib.closed

    async def test_the_server_closes_them_at_shutdown(self, monkeypatch):
        from server.config import ServerConfig
        from server.lifecycle import network_monitor, update_check
        from server.main import create_app, lifespan

        async def _never(*a, **k):
            return None

        monkeypatch.setattr(network_monitor, "update_network_state", lambda *a: None)
        monkeypatch.setattr(network_monitor, "network_monitor_loop", _never)
        monkeypatch.setattr(update_check, "periodic_update_check", _never)
        close = AsyncMock()
        monkeypatch.setattr(CoreDeviceInput, "close", close)
        app = create_app(config=ServerConfig(api_key="k"),
                         enable_oslog=False, enable_crash=False, enable_proxy=False)
        async with lifespan(app):
            close.assert_not_called()
        close.assert_awaited_once()


def _phone(monkeypatch, reader="coredevice", os_version="iOS 27.2.1"):
    ctrl = DeviceController()
    ctrl._active_udid = PHONE
    ctrl._device_type_cache[PHONE] = DeviceType.DEVICE
    ctrl.wda_client._device_os_versions[PHONE] = os_version
    monkeypatch.setattr("server.device.controller_ui.get_device_ui_reader", lambda: reader)
    ctrl.coredevice_input.tap = AsyncMock(return_value="coredevice")
    ctrl.coredevice_input.swipe = AsyncMock(return_value="coredevice")
    ctrl.coredevice_input.press_button = AsyncMock(return_value="coredevice")
    ctrl.wda_client.tap = AsyncMock()
    ctrl.wda_client.swipe = AsyncMock()
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

    async def test_swipes_route_the_same_way(self, monkeypatch):
        ctrl = _phone(monkeypatch)
        await ctrl.swipe(10, 700, 10, 200, udid=PHONE)
        ctrl.coredevice_input.swipe.assert_awaited_once()
        ctrl.wda_client.swipe.assert_not_called()

    async def test_tap_element_taps_over_hid_once_wda_has_found_it(self, monkeypatch):
        ctrl = _phone(monkeypatch)
        found = [UIElement(type="Button", label="Go", identifier="go",
                           frame={"x": 0, "y": 200, "width": 100, "height": 40})]

        async def elements(udid=None, *args, **kwargs):
            return list(found), PHONE

        ctrl.resolve_udid = AsyncMock(return_value=PHONE)
        ctrl._warn_if_input_is_suppressed = AsyncMock()
        ctrl.get_ui_elements = AsyncMock(side_effect=elements)
        ctrl._all_elements_for_context = AsyncMock(return_value=(list(found), True))
        ctrl.wda_client.is_hittable = AsyncMock(return_value=True)
        ctrl._wda_viewport = AsyncMock(return_value={"x": 0, "y": 0, "width": 414, "height": 896})
        result = await ctrl.tap_element(identifier="go", udid=PHONE, scroll_to_find=False,
                                        skip_stability_check=True)
        assert result["status"] == "ok", result
        ctrl.coredevice_input.tap.assert_awaited_once_with(PHONE, 50, 220)
        ctrl.wda_client.tap.assert_not_called()

    async def test_a_tap_report_says_which_carried_this_tap(self, monkeypatch):
        """Read from the call itself: another tap on the phone may have gone
        the other way meanwhile."""
        ctrl = _phone(monkeypatch)
        ctrl._checks_landing = lambda udid: False
        ctrl.coredevice_input.tap = AsyncMock(return_value="wda")
        report = await ctrl.tap_and_report(10, 20, udid=PHONE)
        assert report["input"] == "wda"

    async def test_a_tap_through_wda_alone_says_nothing_about_input(self, monkeypatch):
        ctrl = _phone(monkeypatch, reader="wda")
        ctrl._checks_landing = lambda udid: False
        report = await ctrl.tap_and_report(10, 20, udid=PHONE)
        assert "input" not in report
        ctrl.wda_client.tap.assert_awaited_once()
