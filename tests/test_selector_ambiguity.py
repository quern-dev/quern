"""Android's selector fast path counts before it presses (#512).

It used to press the first match. Two buttons labelled "Twin" got one silent
tap, on whichever uiautomator returned first, and `status: ok` -- where the same
call with `element_type` set went through the dump path and answered
`ambiguous`. Now the fast path presses only a unique match and hands anything
else to the dump path, so both give one answer.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from server.device.android.u2_client import U2Backend
from server.device.controller import DeviceController
from server.models import DeviceType, UIElement

UDID = "emulator-5554"


def _box(left, top, right, bottom):
    return {"left": left, "top": top, "right": right, "bottom": bottom}


def _obj(count: int, *frames) -> MagicMock:
    """`count` matches; side by side unless `frames` says where each is."""
    obj = MagicMock()
    obj.count = count
    frames = frames or tuple(_box(200 * i, 0, 200 * i + 100, 50) for i in range(count))
    obj.info = {"bounds": frames[0] if frames else _box(0, 0, 100, 50),
                "className": "android.widget.Button", "text": "Twin"}
    obj.info_list.return_value = [{"bounds": f} for f in frames]
    return obj


def _backend(by_selector: dict[str, MagicMock]) -> U2Backend:
    """A U2Backend whose device answers each selector key with its own object."""
    def device(**sel):
        (key,) = sel
        return by_selector.get(key) or _obj(0)

    backend = U2Backend()
    backend._connect = lambda serial: device
    return backend


class TestTapBySelectorCounts:
    async def test_two_identifier_matches_press_nothing(self):
        obj = _obj(2)
        result = await _backend({"resourceIdMatches": obj}).tap_by_selector(
            UDID, identifier="tap_twin")
        assert result == {"defer": "ambiguous", "matches": 2}
        obj.click.assert_not_called()

    async def test_two_label_matches_press_nothing(self):
        obj = _obj(2)
        result = await _backend({"text": obj}).tap_by_selector(UDID, label="Twin")
        assert result == {"defer": "ambiguous", "matches": 2}
        obj.click.assert_not_called()

    async def test_a_text_match_and_a_description_match_are_two(self):
        """Either is the label, so one of each is as ambiguous as two texts."""
        text, desc = _obj(1, _box(0, 0, 100, 50)), _obj(1, _box(200, 0, 300, 50))
        result = await _backend({"text": text, "description": desc}).tap_by_selector(
            UDID, label="Twin")
        assert result == {"defer": "ambiguous", "matches": 2}
        text.click.assert_not_called()
        desc.click.assert_not_called()

    async def test_a_bottom_nav_item_and_its_own_title_are_one(self):
        """Stock Material: the item's content-desc copies its title, and the
        TextView inside it shows it. Pressed, as before -- not ambiguous."""
        text = _obj(1, _box(20, 1800, 120, 1840))         # the title
        desc = _obj(1, _box(0, 1760, 270, 1900))          # the item around it
        result = await _backend({"text": text, "description": desc}).tap_by_selector(
            UDID, label="Home")
        assert "defer" not in result, result
        text.click.assert_called_once_with()
        desc.click.assert_not_called()

    async def test_one_element_whose_text_and_description_are_the_label(self):
        same = _box(0, 0, 100, 50)
        text, desc = _obj(1, same), _obj(1, same)
        result = await _backend({"text": text, "description": desc}).tap_by_selector(
            UDID, label="Twin")
        assert "defer" not in result
        text.click.assert_called_once_with()

    async def test_two_bottom_nav_items_with_one_title_are_still_two(self):
        """Nesting is every match inside the largest, not any pair."""
        text = _obj(2, _box(20, 1800, 120, 1840), _box(290, 1800, 390, 1840))
        desc = _obj(1, _box(0, 1760, 270, 1900))
        result = await _backend({"text": text, "description": desc}).tap_by_selector(
            UDID, label="Home")
        assert result == {"defer": "ambiguous", "matches": 3}

    async def test_a_count_that_fails_defers_rather_than_misses(self):
        """None would sweep the screen for an element that may be in view."""
        text, desc = _obj(1), _obj(1)
        desc.info_list.side_effect = TimeoutError()
        result = await _backend({"text": text, "description": desc}).tap_by_selector(
            UDID, label="Twin")
        assert result == {"defer": "uncounted"}
        text.click.assert_not_called()

    async def test_the_pressed_element_is_reported_from_the_same_read(self):
        """No second `info` round trip: the frame comes from `info_list`."""
        obj = _obj(1, _box(10, 20, 110, 70))
        obj.info = {"bounds": _box(999, 999, 1000, 1000)}   # would be wrong
        result = await _backend({"text": obj}).tap_by_selector(UDID, label="Twin")
        assert (result["x"], result["y"]) == (60, 45)

    async def test_a_unique_label_match_is_pressed(self):
        desc = _obj(1)
        result = await _backend({"description": desc}).tap_by_selector(UDID, label="Twin")
        desc.click.assert_called_once_with()
        assert result["x"] == 50 and result["y"] == 25

    async def test_a_unique_identifier_match_wins_before_the_label_is_counted(self):
        """Order unchanged: identifier first, and its count is its own."""
        ident, text = _obj(1), _obj(3)
        result = await _backend({"resourceIdMatches": ident, "text": text}).tap_by_selector(
            UDID, identifier="tap_target", label="Twin")
        ident.click.assert_called_once_with()
        text.click.assert_not_called()
        assert "ambiguous" not in result

    async def test_no_match_is_still_none(self):
        assert await _backend({}).tap_by_selector(UDID, label="Twin") is None

    async def test_a_long_press_is_held_back_too(self):
        obj = _obj(2)
        result = await _backend({"text": obj}).tap_by_selector(UDID, label="Twin", hold=1.0)
        assert result == {"defer": "ambiguous", "matches": 2}
        obj.long_click.assert_not_called()


def _twin(x: float) -> UIElement:
    return UIElement(type="Button", label="Twin", identifier=None,
                     frame={"x": x, "y": 156, "width": 160, "height": 48})


class TestTapElementHandsAmbiguityToTheDumpPath:
    def _ctrl(self, tap_result):
        ctrl = DeviceController()
        ctrl._device_type_cache[UDID] = DeviceType.ANDROID_EMULATOR
        ctrl.resolve_udid = AsyncMock(return_value=UDID)
        ctrl._invalidate_ui_cache = MagicMock()
        backend = MagicMock()
        backend.tap_by_selector = AsyncMock(return_value=tap_result)
        backend.scroll_into_view = AsyncMock()
        backend.tap = AsyncMock()
        ctrl._ui_backend = MagicMock(return_value=backend)
        ctrl.get_ui_elements = AsyncMock(return_value=([_twin(16), _twin(200)], UDID))
        return ctrl, backend

    async def test_it_answers_ambiguous_with_both_candidates(self):
        ctrl, backend = self._ctrl({"defer": "ambiguous", "matches": 2})
        with patch("server.device.controller_ui._capture_screenshot",
                   AsyncMock(return_value=None)):
            result = await ctrl.tap_element(label="Twin")
        assert result["status"] == "ambiguous", result
        assert len(result["matches"]) == 2
        backend.tap.assert_not_called()
        ctrl.get_ui_elements.assert_awaited()

    async def test_an_ambiguous_match_is_not_a_miss_so_nothing_scrolls(self):
        ctrl, backend = self._ctrl({"defer": "ambiguous", "matches": 2})
        with patch("server.device.controller_ui._capture_screenshot",
                   AsyncMock(return_value=None)):
            result = await ctrl.tap_element(label="Twin")
        backend.scroll_into_view.assert_not_called()
        assert "scroll" not in result

    async def test_a_unique_match_still_skips_the_dump(self):
        ctrl, backend = self._ctrl(
            {"label": "Twin", "identifier": None, "type": "Button", "x": 1, "y": 2})
        result = await ctrl.tap_element(label="Twin")
        assert result["status"] == "ok"
        ctrl.get_ui_elements.assert_not_called()

    async def test_an_uncounted_match_goes_to_the_dump_path_without_a_sweep(self):
        ctrl, backend = self._ctrl({"defer": "uncounted"})
        with patch("server.device.controller_ui._capture_screenshot",
                   AsyncMock(return_value=None)):
            await ctrl.tap_element(label="Twin")
        backend.scroll_into_view.assert_not_called()
        ctrl.get_ui_elements.assert_awaited()

    async def test_ambiguous_after_a_sweep_is_not_read_as_a_tap(self):
        """The retry after `scroll_into_view` has its own guard: without it the
        defer dict came back as `status: ok, tapped: {...}` (review)."""
        ctrl, backend = self._ctrl(None)
        backend.tap_by_selector = AsyncMock(
            side_effect=[None, {"defer": "ambiguous", "matches": 2}])
        backend.scroll_into_view = AsyncMock(return_value={"label": "Twin"})
        with patch("server.device.controller_ui._capture_screenshot",
                   AsyncMock(return_value=None)), \
                patch("server.device.controller_ui.asyncio.sleep", AsyncMock()):
            result = await ctrl.tap_element(label="Twin")
        assert result["status"] == "ambiguous", result
        backend.tap.assert_not_called()
        # The device was swiped, and the answer says so.
        assert result["scroll"]["attempted"] is True, result
