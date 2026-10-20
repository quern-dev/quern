"""#502: on a miss, say when the screen has not changed since the last tap.

No knowledge base needed: a frame before each delivered tap_element, compared
with one at the next miss. Said on the miss only, second to #499's
previous_tap, and silenced by any screen change in between.
"""

from __future__ import annotations

import io
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image

from server.config import ServerConfig
from server.device import frames
from server.device.controller import DeviceController
from server.main import create_app
from server.models import Landmark, NavEdge, ScreenLandmarks


def _png(color: int = 128, size=(400, 800), box=None) -> bytes:
    image = Image.new("L", size, color)
    if box is not None:
        x, y, w, h, c = box
        image.paste(c, (x, y, x + w, y + h))
    out = io.BytesIO()
    image.convert("RGB").save(out, format="PNG")
    return out.getvalue()


def _tap(png=None, at=100.0, stamp=5.0):
    return frames.TapFrame(tapped={"label": "Go", "type": "Button"}, png=png or _png(),
                           platform="ios", at=at, stamp=stamp, capture_ms=80.0, source="wda")


class TestUnchangedNote:
    def test_the_same_screen(self):
        note = frames.unchanged_note(_tap(), _png(), 104.0, stamp=5.0)
        assert note["change"] == 0.0 and note["seconds_ago"] == 4.0
        assert "'Go'" in note["note"] and "not blindly for a submit" in note["note"]
        assert note["tapped"] == {"label": "Go", "identifier": None, "type": "Button"}

    def test_a_changed_screen(self):
        after = _png(box=(0, 200, 400, 300, 255))
        assert frames.unchanged_note(_tap(), after, 104.0, stamp=5.0) is None

    def test_a_small_change_below_the_threshold(self):
        # A blinking caret or a spinner: a few pixels.
        after = _png(box=(200, 400, 4, 12, 255))
        assert frames.unchanged_note(_tap(), after, 104.0, stamp=5.0) is not None

    def test_the_status_bar_is_not_compared(self):
        # The clock ticked: a change in the top band only.
        after = _png(box=(0, 0, 400, 40, 255))
        assert frames.unchanged_note(_tap(), after, 104.0, stamp=5.0) is not None

    def test_another_screen_change_since(self):
        assert frames.unchanged_note(_tap(), _png(), 104.0, stamp=6.0) is None

    def test_too_long_ago(self):
        assert frames.unchanged_note(_tap(), _png(), 100.0 + 121, stamp=5.0) is None
        assert frames.unchanged_note(_tap(), _png(), 100.0 + 119, stamp=5.0)

    def test_no_frame_after(self):
        assert frames.unchanged_note(_tap(), None, 104.0, stamp=5.0) is None

    def test_an_undecodable_frame_is_no_note(self):
        assert frames.unchanged_note(_tap(), b"junk", 104.0, stamp=5.0) is None
        assert frames.unchanged_note(_tap(png=b"junk"), _png(), 104.0, stamp=5.0) is None

    def test_android_crops_its_own_status_bar(self):
        image = frames.prepare(_png(), "android")
        assert image.width == frames.FRAME_WIDTH
        assert image.height == 200 - int(200 * frames.STATUS_BAR_FRACTION["android"])

    def test_a_rotation(self):
        after = _png(size=(800, 400))
        assert frames.unchanged_note(_tap(), after, 104.0, stamp=5.0) is None


class TestCapture:
    """The real capture, each backend mocked."""

    def _controller(self, *, wda=False, android=False, bridge=False):
        c = MagicMock()
        c.u2.frame = AsyncMock(return_value=_png())
        c._served_by_wda.return_value = wda
        c._is_android.return_value = android
        c.sim_bridge = object()
        c._ui_backend.return_value = c.sim_bridge if bridge else object()
        c.wda_client.screenshot = AsyncMock(return_value=_png())
        c.raw_screenshot = AsyncMock(return_value=_png())
        c.sim_bridge = MagicMock()
        c.sim_bridge.screenshot = AsyncMock(return_value=_png())
        if bridge:
            c._ui_backend.return_value = c.sim_bridge
        return c

    @pytest.mark.parametrize("kind, source", [
        ({"wda": True}, "wda"), ({"android": True}, "u2"),
        ({"bridge": True}, "sim-bridge"), ({}, "simctl"),
    ])
    async def test_each_backend_uses_its_fastest_path(self, kind, source):
        c = self._controller(**kind)
        png, ms, used = await frames._capture(c, "UDID")
        assert used == source and png == _png() and ms >= 0
        assert frames.platform_of(used) == ("android" if source == "u2" else "ios")

        called = {
            "wda": c.wda_client.screenshot, "u2": c.u2.frame,
            "sim-bridge": c.sim_bridge.screenshot, "simctl": c.raw_screenshot,
        }[source]
        called.assert_awaited_once()

    async def test_android_falls_back_to_adb(self):
        c = self._controller(android=True)
        c.u2.frame = AsyncMock(side_effect=OSError("u2 server down"))
        png, _, used = await frames._capture(c, "SERIAL")
        assert used == "adb" and png == _png() and frames.platform_of(used) == "android"
        c.raw_screenshot.assert_awaited_once()

    async def test_the_capture_is_not_decoded_at_the_tap(self, monkeypatch):
        import server.device.settle as settle

        def no_decode(png):
            raise AssertionError("decoded at the tap")

        monkeypatch.setattr(settle, "_prepare", no_decode)
        png, _, _ = await frames._capture(self._controller(wda=True), "UDID")
        assert png == _png()

    def test_the_status_bar_is_cropped(self):
        image = frames.prepare(_png(), "ios")
        assert image.height == 200 - int(200 * frames.STATUS_BAR_FRACTION["ios"])

    def test_frames_from_different_sources_compare(self):
        # A scaled JPEG-sized capture against a full-size one of the same screen.
        small = frames.prepare(_png(size=(100, 200)), "android")
        full = frames.prepare(_png(size=(1080, 2160)), "android")
        assert small.size == full.size

    async def test_a_failed_capture_is_no_frame(self):
        c = self._controller(wda=True)
        c.wda_client.screenshot = AsyncMock(side_effect=OSError("gone"))
        png, _, source = await frames._capture(c, "UDID")
        assert png is None and source == "wda"

    async def test_an_empty_capture_is_no_frame(self):
        c = self._controller(wda=True)
        c.wda_client.screenshot = AsyncMock(return_value=b"")
        png, _, _ = await frames._capture(c, "UDID")
        assert png is None

    async def test_a_slow_capture_is_abandoned(self, monkeypatch):
        import asyncio

        async def slow(udid):
            await asyncio.sleep(1.0)
            return _png()

        monkeypatch.setattr(frames, "CAPTURE_TIMEOUT", 0.05)
        c = self._controller(wda=True)
        c.wda_client.screenshot = slow
        png, ms, _ = await frames._capture(c, "UDID")
        assert png is None and ms < 900


# ---------------------------------------------------------------------------
# Through the routes
# ---------------------------------------------------------------------------


@pytest.fixture
def app():
    return create_app(config=ServerConfig(api_key="k"), enable_oslog=False,
                      enable_crash=False, enable_proxy=False)


@pytest.fixture
def controller(app):
    ctrl = DeviceController()
    ctrl._active_udid = "AAAA-1111"
    ctrl.resolve_udid = AsyncMock(return_value="AAAA-1111")
    ctrl.tap_element = AsyncMock(return_value={
        "status": "ok", "tapped": {"label": "Go", "type": "Button", "x": 1, "y": 2}})
    app.state.device_controller = ctrl
    return ctrl


@pytest.fixture
def screens(monkeypatch):
    """Frames the capture returns, in order, repeating the last."""
    shown = {"pngs": [_png()], "n": 0}

    async def capture(controller, udid):
        i = min(shown["n"], len(shown["pngs"]) - 1)
        shown["n"] += 1
        return shown["pngs"][i], 50.0, "wda"

    monkeypatch.setattr(frames, "capture", capture)
    return shown


NOT_FOUND = {"status": "not_found", "detail": "No element found",
             "screen_context": {"identified_as": None, "confidence": "none"}}


async def _post(app, path, body):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/api/v1/device/ui/{path}", json=body,
                                 headers={"Authorization": "Bearer k"})
    return resp.status_code, resp.json()


def _misses(controller, context=None):
    """Every later tap misses, each with a fresh result: a shared dict would
    carry one response's note into the next."""
    def miss(*a, **k):
        return {**NOT_FOUND, **({"screen_context": context} if context else {})}
    controller.tap_element.side_effect = miss


async def _tap_then_miss(app, controller):
    await _post(app, "tap-element", {"label": "Go"})
    _misses(controller)
    return await _post(app, "tap-element", {"label": "Next"})


class TestRoute:
    async def test_a_miss_on_an_unchanged_screen_says_so(self, app, controller, screens):
        code, body = await _tap_then_miss(app, controller)
        note = body["detail"]["screen_unchanged_since_tap"]
        assert code == 404 and note["change"] == 0.0 and note["tapped"]["label"] == "Go"
        assert note["capture_ms"] == {"before": 50, "after": 50}

    async def test_a_changed_screen_says_nothing(self, app, controller, screens):
        screens["pngs"] = [_png(), _png(box=(0, 200, 400, 300, 255))]
        _, body = await _tap_then_miss(app, controller)
        assert "screen_unchanged_since_tap" not in body["detail"]

    async def test_another_action_in_between_says_nothing(self, app, controller, screens):
        await _post(app, "tap-element", {"label": "Go"})
        controller._last_ui_change["AAAA-1111"] = 99.0   # a swipe, say
        _misses(controller)
        _, body = await _post(app, "tap-element", {"label": "Next"})
        assert "screen_unchanged_since_tap" not in body["detail"]

    async def test_it_is_said_once(self, app, controller, screens):
        await _tap_then_miss(app, controller)
        _, body = await _post(app, "tap-element", {"label": "Next"})
        assert "screen_unchanged_since_tap" not in body["detail"]

    async def test_previous_tap_comes_first(self, app, controller, screens, monkeypatch):
        from server.knowledge import navigation

        monkeypatch.setattr(navigation, "MISS_MIN_AGE", 0.0)
        app.state.landmark_registry.load("probe", [
            ScreenLandmarks(screen="a", landmarks=[Landmark(element="Heading", label="A")],
                            leads_to=[NavEdge(target="b", label="Go")]),
            ScreenLandmarks(screen="b", landmarks=[Landmark(element="Heading", label="B")]),
        ])
        await _post(app, "tap-element", {"label": "Go"})
        _misses(controller, {"identified_as": "a", "confidence": "exact",
                             "identified_app": "probe"})
        _, body = await _post(app, "tap-element", {"label": "Next"})
        assert "previous_tap" in body["detail"]
        assert "screen_unchanged_since_tap" not in body["detail"]
        # And it cost no frame at the miss: one before the tap only.
        assert screens["n"] == 2   # before "Go", before "Next"

    async def test_opting_out_takes_no_frame(self, app, controller, screens):
        await _post(app, "tap-element", {"label": "Go", "note_unchanged": False})
        assert screens["n"] == 0
        _misses(controller)
        _, body = await _post(app, "tap-element", {"label": "Next", "note_unchanged": False})
        assert "screen_unchanged_since_tap" not in body["detail"] and screens["n"] == 0

    async def test_opting_out_drops_an_earlier_frame(self, app, controller, screens):
        # The opted-out tap moved the screen as far as anyone knows: an
        # earlier frame must not be compared across it.
        await _post(app, "tap-element", {"label": "Go"})
        await _post(app, "tap-element", {"label": "Fast", "note_unchanged": False})
        _misses(controller)
        _, body = await _post(app, "tap-element", {"label": "Next"})
        assert "screen_unchanged_since_tap" not in body["detail"]

    async def test_a_long_press_takes_no_frame(self, app, controller, screens):
        await _post(app, "tap-element", {"label": "Go", "duration": 1.0})
        assert screens["n"] == 0

    async def test_a_tap_that_tapped_nothing_keeps_the_earlier_frame(
        self, app, controller, screens,
    ):
        await _post(app, "tap-element", {"label": "Go"})
        controller.tap_element.return_value = {"status": "ambiguous", "matches": []}
        await _post(app, "tap-element", {"label": "Row"})
        _misses(controller)
        _, body = await _post(app, "tap-element", {"label": "Next"})
        assert body["detail"]["screen_unchanged_since_tap"]["tapped"]["label"] == "Go"

    async def test_no_frame_before_is_no_note(self, app, controller, monkeypatch):
        async def none(controller, udid):
            return None, 3000.0, "wda"

        monkeypatch.setattr(frames, "capture", none)
        code, body = await _tap_then_miss(app, controller)
        assert code == 404 and "screen_unchanged_since_tap" not in body["detail"]

    async def test_a_failing_capture_does_not_fail_the_tap_or_the_miss(
        self, app, controller, monkeypatch,
    ):
        async def boom(controller, udid):
            raise RuntimeError("bug")

        monkeypatch.setattr(frames, "capture", boom)
        code, body = await _post(app, "tap-element", {"label": "Go"})
        assert code == 200 and body["status"] == "ok"
        _misses(controller)
        code, body = await _post(app, "tap-element", {"label": "Next"})
        assert code == 404

    async def test_a_wait_that_times_out_carries_it(self, app, controller, screens):
        await _post(app, "tap-element", {"label": "Go"})
        controller.wait_for_element = AsyncMock(return_value=(
            {"matched": False, "screen_context": {}}, "AAAA-1111"))
        _, body = await _post(app, "wait-for-element",
                              {"identifier": "x", "condition": "exists", "timeout": 1})
        assert body["screen_unchanged_since_tap"]["tapped"]["label"] == "Go"

    async def test_per_device(self, app, controller, screens):
        await _post(app, "tap-element", {"label": "Go"})
        controller.resolve_udid.return_value = "BBBB-2222"
        _misses(controller)
        _, body = await _post(app, "tap-element", {"label": "Next"})
        assert "screen_unchanged_since_tap" not in body["detail"]
