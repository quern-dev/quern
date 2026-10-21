"""Android's scroll_into_view stops at a list's ends and on a screen that does
not scroll (#515).

It used to swipe blind: 10 down and 20 up whatever happened, so a miss on a
screen with nothing to scroll cost 30 swipes and said nothing about why. Now a
frame after each swipe is compared with the one before, as #502's frames are.
"""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server.device.android.u2_client import U2Backend

UDID = "emulator-5554"


class Frame:
    """What `frames.prepare` returns, reduced to an identity and a size."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.size = (100, 200)

    def getextrema(self):
        return (0, 0) if self.name == "black" else (0, 255)


@pytest.fixture(autouse=True)
def frames_by_name(monkeypatch):
    """A screenshot is the frame's name; frames differ when names do."""
    monkeypatch.setattr("server.device.frames.prepare",
                        lambda png, platform: Frame(png.decode()) if png else None)
    monkeypatch.setattr("server.device.settle.changed_fraction",
                        lambda a, b: 0.0 if a.name == b.name else 0.5)


def _device(screens, *, found_after: int | None = None):
    """`screens` names the screen before the first swipe, then after each one.
    The target appears after `found_after` swipes, if given."""
    device = MagicMock()
    device.window_size.return_value = (1080, 2400)
    shots = iter(screens)

    def shot(scale, quality):
        name = next(shots)
        if name is None:
            raise RuntimeError("screenshot failed")
        return base64.b64encode(name.encode()).decode()
    device.jsonrpc.takeScreenshot.side_effect = shot
    swipes = []
    device.swipe.side_effect = lambda *a, **k: swipes.append(a)
    device.swipes = swipes

    def select(**sel):
        obj = MagicMock()
        obj.exists = found_after is not None and len(swipes) >= found_after
        obj.info = {"bounds": {"left": 0, "top": 0, "right": 100, "bottom": 50},
                    "text": "Row 150", "className": "android.widget.TextView"}
        return obj
    device.side_effect = select
    return device


async def _sweep(device, **kw):
    backend = U2Backend()
    backend._connect = lambda serial: device
    report: dict = {}
    found = await backend.scroll_into_view(UDID, label="Row 150", report=report, **kw)
    return found, report


class TestScrollIntoView:
    async def test_a_screen_that_does_not_scroll_costs_two_swipes(self):
        device = _device(["a"] * 100)
        found, report = await _sweep(device)
        assert found is None
        assert report == {"swipes": 3, "moved": False, "ends": ["bottom", "top"],
                          "no_progress": True}, "a third swipe confirms it"

    async def test_a_list_turns_round_at_its_bottom_and_stops_at_its_top(self):
        # down: b, c, then c again (bottom); up: b, a, then a again (top)
        device = _device(["a", "b", "c", "c", "b", "a", "a"])
        found, report = await _sweep(device)
        assert found is None
        assert report["ends"] == ["bottom", "top"]
        assert report["swipes"] == 6 and report["moved"] is True
        assert "no_progress" not in report

    async def test_a_false_still_read_at_the_top_is_caught_by_the_third_swipe(self):
        """Down reads still (a false read), up is truly at the top; the extra
        swipe down moves, so this is a list, not a still screen (review)."""
        device = _device(["a", "a", "a", "b", "b"])
        found, report = await _sweep(device, max_swipes=1)
        assert "no_progress" not in report
        assert report["ends"] == ["top"] and report["moved"] is True

    async def test_a_secure_window_never_reads_as_not_scrolling(self):
        """FLAG_SECURE captures black: blank frames are no frames, and the
        sweep runs as it used to (review)."""
        device = _device(["black"] * 200)
        found, report = await _sweep(device, max_swipes=3)
        assert "no_progress" not in report and "ends" not in report
        assert report["swipes"] == 9

    async def test_a_slow_capture_turns_frames_off(self, monkeypatch):
        import time as _time

        from server.device.android import u2_client

        monkeypatch.setattr(u2_client, "FRAME_SLOW_S", 0.0)
        device = _device(["a"] * 200)
        real = device.jsonrpc.takeScreenshot.side_effect

        def slow(*a):
            _time.sleep(0.001)
            return real(*a)
        device.jsonrpc.takeScreenshot.side_effect = slow
        found, report = await _sweep(device, max_swipes=3)
        assert device.jsonrpc.takeScreenshot.call_count == 1, "only the first"
        assert report["swipes"] == 9 and "no_progress" not in report

    async def test_a_target_that_appears_does_not_claim_movement(self):
        """Found after a swipe whose frame was never compared: not measured."""
        device = _device(["a"], found_after=1)
        found, report = await _sweep(device)
        assert found is not None and report["moved"] is False

    async def test_one_unchanged_direction_is_not_no_progress(self):
        """At the bottom already: the first swipe down changes nothing, the
        first swipe up does. One way only."""
        device = _device(["a", "a", "b", "c", "c"])
        found, report = await _sweep(device)
        assert report["ends"] == ["bottom", "top"]
        assert "no_progress" not in report and report["moved"] is True

    async def test_the_target_found_on_the_way_is_returned(self):
        device = _device(["a", "b", "c", "d"], found_after=2)
        found, report = await _sweep(device)
        assert found is not None and found["label"] == "Row 150"
        assert report["swipes"] == 2 and report["moved"] is True

    async def test_without_frames_it_sweeps_as_before(self):
        """A frame that cannot be taken reads as moved: the old blind sweep,
        never an early stop on a guess."""
        device = _device([None] * 100)
        found, report = await _sweep(device, max_swipes=3)
        assert found is None
        assert report["swipes"] == 9 and "ends" not in report

    async def test_a_failed_frame_is_not_compared_with_the_next(self):
        """b, (failed), b: the failed frame keeps the one before it, so the
        third swipe compares b with b -- an end, correctly."""
        device = _device(["a", "b", None, "b", "a", "a"])
        found, report = await _sweep(device, max_swipes=5)
        assert report["ends"] == ["bottom", "top"]
        assert report["swipes"] == 5          # bottom at the third, top two later


class TestTheAnswerSaysWhy:
    def _ctrl(self):
        from server.device.controller import DeviceController
        from server.models import DeviceType

        ctrl = DeviceController()
        ctrl._device_type_cache[UDID] = DeviceType.ANDROID_EMULATOR
        ctrl.resolve_udid = AsyncMock(return_value=UDID)
        ctrl._invalidate_ui_cache = MagicMock()
        backend = MagicMock()

        async def sweep(udid, identifier=None, label=None, max_swipes=10, report=None):
            report.update(swipes=2, moved=False, ends=["bottom", "top"], no_progress=True)
            return None
        backend.scroll_into_view = sweep
        backend.tap_by_selector = AsyncMock(return_value=None)
        ctrl._ui_backend = MagicMock(return_value=backend)
        return ctrl

    async def test_a_tap_element_miss_carries_the_ends(self):
        ctrl = self._ctrl()
        ctrl.get_ui_elements = AsyncMock(return_value=([], UDID))
        with patch("server.device.controller_ui._capture_screenshot",
                   AsyncMock(return_value=None)):
            result = await ctrl.tap_element(label="Row 150")
        assert result["status"] == "not_found"
        assert result["scroll"]["no_progress"] is True
        assert result["scroll"]["ends"] == ["bottom", "top"]

    async def test_scroll_to_element_says_why_it_stopped(self):
        ctrl = self._ctrl()
        result = await ctrl.scroll_to_element(label="Row 150")
        assert result["status"] == "not_found"
        assert "does not scroll" in result["detail"]
        assert result["scroll"]["swipes"] == 2


class TestRealFrames:
    """The capture path end to end -- the real `prepare` and
    `changed_fraction`, on JPEGs like the ones u2 sends -- since every other
    test here stands both in."""

    @pytest.fixture(autouse=True)
    def frames_by_name(self):
        yield                                   # the real ones, not the fakes

    @staticmethod
    def _jpeg(offset: int) -> bytes:
        import io

        from PIL import Image, ImageDraw

        image = Image.new("RGB", (270, 600), "white")
        draw = ImageDraw.Draw(image)
        for row in range(-2, 20):
            y = row * 40 + offset
            draw.rectangle((10, y + 5, 200, y + 30), fill="black")
        out = io.BytesIO()
        image.save(out, "JPEG", quality=50)
        return out.getvalue()

    async def test_a_list_moved_by_a_swipe_reads_as_moved_and_a_still_one_as_still(self):
        shots = [self._jpeg(0), self._jpeg(0), self._jpeg(0), self._jpeg(13),
                 self._jpeg(13), self._jpeg(13)]
        device = MagicMock()
        device.window_size.return_value = (1080, 2400)
        it = iter(shots)
        device.jsonrpc.takeScreenshot.side_effect = (
            lambda scale, quality: base64.b64encode(next(it)).decode())
        device.side_effect = lambda **sel: MagicMock(exists=False)
        found, report = await _sweep(device, max_swipes=2)
        # down: unchanged (bottom); up: unchanged (top); third: moved
        assert device.jsonrpc.takeScreenshot.call_args.args == (0.25, 50)
        assert report["moved"] is True and "no_progress" not in report



class TestTheFixedSleepIsGone:
    """#515: the tap after a sweep waited a fixed 0.4s; the at-rest check
    (#514) now waits for the list to stop instead. The sleep stays only for
    a caller who turned that check off."""

    def _ctrl(self):
        from server.device.controller import DeviceController
        from server.models import DeviceType

        ctrl = DeviceController()
        ctrl._device_type_cache[UDID] = DeviceType.ANDROID_EMULATOR
        ctrl.resolve_udid = AsyncMock(return_value=UDID)
        ctrl._invalidate_ui_cache = MagicMock()
        backend = MagicMock()
        backend.tap_by_selector = AsyncMock(side_effect=[None, {
            "label": "Row 150", "identifier": None, "type": "StaticText", "x": 1, "y": 2}])
        backend.scroll_into_view = AsyncMock(return_value={"label": "Row 150"})
        ctrl._ui_backend = MagicMock(return_value=backend)
        return ctrl, backend

    @pytest.mark.parametrize("skip, slept", [(False, []), (True, [0.4])])
    async def test_the_sleep_only_without_the_rest_check(self, skip, slept):
        ctrl, backend = self._ctrl()
        sleeps = []

        async def sleep(t):
            sleeps.append(t)
        with patch("server.device.controller_ui.asyncio.sleep", sleep):
            result = await ctrl.tap_element(label="Row 150", skip_stability_check=skip)
        assert result["status"] == "ok"
        assert sleeps == slept
        assert backend.tap_by_selector.await_args_list[1].kwargs["confirm_rest"] is not skip
