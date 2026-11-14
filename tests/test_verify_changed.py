"""verify_changed: say on the tap itself whether the screen changed.

#502 says "unchanged" only on a later miss. A caller can ask on the tap
instead -- a tap that must navigate, where landing on nothing has to be known
at once. Measured on 2026-11-14: a HID tap on a physical iPhone that landed on
nothing answered ok, and nothing said so. "Could not tell" must answer null,
never false.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from server.config import ServerConfig
from server.device import frames
from server.device.controller import DeviceController
from server.main import create_app
from tests.test_screen_unchanged import _png, _post

SAME = _png()
CHANGED = _png(box=(0, 200, 400, 300, 255))


class TestTheVerdict:
    def test_the_same_screen_is_unchanged_with_a_note(self):
        verdict = frames.changed_verdict((SAME, 50.0, "wda"), (SAME, 50.0, "wda"))
        assert verdict["screen_changed"] is False
        assert verdict["screen_change"]["fraction"] == 0.0
        assert "probably did not register" in verdict["screen_change"]["note"]

    def test_a_changed_screen(self):
        verdict = frames.changed_verdict((SAME, 50.0, "wda"), (CHANGED, 50.0, "wda"))
        assert verdict["screen_changed"] is True and "note" not in verdict["screen_change"]

    def test_a_one_digit_change_is_a_change(self):
        """The near-exact threshold of #502: a counter going 3 -> 4."""
        digit = _png(box=(200, 400, 8, 12, 255))
        assert frames.changed_verdict((SAME, 1, "wda"), (digit, 1, "wda"))["screen_changed"]

    @pytest.mark.parametrize(("before", "after", "why"), [
        ((None, 0.0, "none"), (SAME, 1.0, "wda"), "before the tap"),
        ((SAME, 1.0, "wda"), (None, 0.0, "wda"), "after the tap"),
        ((SAME, 1.0, "wda"), None, "after the tap"),
        ((SAME, 1.0, "wda"), (b"not a png", 1.0, "wda"), "could not be decoded"),
    ])
    def test_could_not_tell_is_null_not_false(self, before, after, why):
        verdict = frames.changed_verdict(before, after)
        assert verdict["screen_changed"] is None
        assert why in verdict["screen_change"]["reason"]


class TestTheCapture:
    @staticmethod
    def _phone():
        ctrl = MagicMock()
        ctrl._is_android = lambda udid: False
        ctrl._is_physical = lambda udid: True
        ctrl.raw_screenshot = AsyncMock(return_value=SAME)
        return ctrl

    async def test_a_phone_with_no_wda_frame_uses_its_own_screenshot(self, monkeypatch):
        """A phone driven over CoreDevice (#664) may have no WDA to give one."""
        monkeypatch.setattr(frames, "capture", AsyncMock(return_value=(None, 1.0, "wda")))
        monkeypatch.setattr(frames, "device_screenshot", frames._device_screenshot)
        ctrl = self._phone()
        png, _, source = await frames.capture_for_check(ctrl, "PHONE")
        assert png == SAME and source == frames.DEVICE_SOURCE

    async def test_the_after_frame_keeps_the_before_frames_path(self, monkeypatch):
        """Two paths are not known to compare at 0.0 for one screen."""
        wda = AsyncMock(return_value=(CHANGED, 1.0, "wda"))
        monkeypatch.setattr(frames, "capture", wda)
        monkeypatch.setattr(frames, "device_screenshot", frames._device_screenshot)
        ctrl = self._phone()

        png, _, source = await frames.capture_for_check(ctrl, "PHONE", source=frames.DEVICE_SOURCE)
        assert source == frames.DEVICE_SOURCE and png == SAME
        wda.assert_not_called()

        wda.return_value = (None, 1.0, "wda")
        ctrl.raw_screenshot.reset_mock()
        png, _, source = await frames.capture_for_check(ctrl, "PHONE", source="wda")
        assert png is None and source == "wda"                # not switched to the device's
        ctrl.raw_screenshot.assert_not_called()

    async def test_a_simulator_never_takes_the_device_route(self):
        ctrl = self._phone()
        ctrl._is_physical = lambda udid: False
        assert await frames._device_screenshot(ctrl, "SIM") is None
        ctrl.raw_screenshot.assert_not_called()

    async def test_a_failed_device_screenshot_is_no_frame(self):
        ctrl = self._phone()
        ctrl.raw_screenshot = AsyncMock(side_effect=OSError("tunnel gone"))
        assert await frames._device_screenshot(ctrl, "PHONE") is None


@pytest.fixture
def app():
    return create_app(config=ServerConfig(api_key="k"), enable_oslog=False,
                      enable_crash=False, enable_proxy=False)


@pytest.fixture
def controller(app):
    ctrl = DeviceController()
    ctrl._active_udid = "AAAA-1111"
    ctrl.resolve_udid = AsyncMock(return_value="AAAA-1111")
    ctrl.tap_and_report = AsyncMock(return_value={"udid": "AAAA-1111"})
    ctrl.tap_element = AsyncMock(return_value={
        "status": "ok", "tapped": {"label": "Go", "type": "Button", "x": 1, "y": 2}})
    app.state.device_controller = ctrl
    return ctrl


@pytest.fixture
def frames_shown(monkeypatch):
    """The frames verify_changed's capture returns, in order; the wait
    between them is not waited."""
    shown = {"pngs": [SAME, SAME], "n": 0, "sources": []}

    async def capture(controller, udid, source=None):
        shown["sources"].append(source)
        png = shown["pngs"][min(shown["n"], len(shown["pngs"]) - 1)]
        shown["n"] += 1
        return png, 50.0, "wda"

    monkeypatch.setattr(frames, "capture_for_check", capture)
    monkeypatch.setattr(frames, "VERIFY_WAIT_S", 0.0)
    return shown


class TestTheRoutes:
    async def test_a_tap_on_nothing_says_so(self, app, controller, frames_shown):
        status, body = await _post(app, "tap", {"x": 10, "y": 20, "verify_changed": True})
        assert status == 200 and body["screen_changed"] is False
        assert "probably did not register" in body["screen_change"]["note"]
        # The after-frame asked for on the before-frame's path.
        assert frames_shown["sources"] == [None, "wda"]

    async def test_a_tap_that_navigated(self, app, controller, frames_shown):
        frames_shown["pngs"] = [SAME, CHANGED]
        _, body = await _post(app, "tap", {"x": 10, "y": 20, "verify_changed": True})
        assert body["screen_changed"] is True

    async def test_both_frames_are_of_the_device_tapped(self, app, controller, frames_shown):
        await _post(app, "tap", {"x": 10, "y": 20, "verify_changed": True})
        assert controller.tap_and_report.await_args.kwargs["udid"] == "AAAA-1111"

    async def test_not_asked_costs_nothing_and_says_nothing(self, app, controller,
                                                             frames_shown):
        _, body = await _post(app, "tap", {"x": 10, "y": 20})
        assert "screen_changed" not in body and frames_shown["n"] == 0

    async def test_tap_element_says_so_too(self, app, controller, frames_shown):
        _, body = await _post(app, "tap-element", {"label": "Go", "verify_changed": True,
                                                   "note_unchanged": False})
        assert body["status"] == "ok" and body["screen_changed"] is False

    async def test_tap_element_reuses_the_notes_frame(self, app, controller, frames_shown,
                                                      monkeypatch):
        """The frame #502 already takes before the tap is the before-frame."""
        monkeypatch.setattr(frames, "capture", AsyncMock(return_value=(SAME, 50.0, "wda")))
        _, body = await _post(app, "tap-element", {"label": "Go", "verify_changed": True})
        assert body["screen_changed"] is False
        assert frames_shown["sources"] == ["wda"]              # only the after-frame

    async def test_a_tap_element_that_did_not_tap_is_not_judged(self, app, controller,
                                                                frames_shown):
        controller.tap_element.return_value = {"status": "ambiguous", "matches": []}
        _, body = await _post(app, "tap-element", {"label": "Go", "verify_changed": True,
                                                   "note_unchanged": False})
        assert "screen_changed" not in body

    async def test_the_after_frame_waits_for_the_screen(self, app, controller, frames_shown,
                                                        monkeypatch):
        """Taken at once, a screen still on its way reads unchanged: the false
        answer #502's grace exists to avoid."""
        import server.api.device_ui as routes

        monkeypatch.setattr(frames, "VERIFY_WAIT_S", 0.7)
        waited = []
        real_sleep = routes.asyncio.sleep

        async def sleep(s, *a, **k):
            waited.append(s)
            await real_sleep(0)

        monkeypatch.setattr(routes.asyncio, "sleep", sleep)
        await _post(app, "tap", {"x": 10, "y": 20, "verify_changed": True})
        assert 0.7 in waited
