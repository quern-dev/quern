"""tap_element folds a control's own parts into it (#490).

A tab-bar item and its icon both carry the label "More"; through WDA on the
probe app, tap_element(label="More") answered `ambiguous` with a Button and an
Image -- the Image entirely inside the Button's frame. They are one control.
Matches side by side stay ambiguous: two controls with one label is a real
ambiguity.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from server.device import controller_ui
from server.device.controller import DeviceController
from server.device.ui_elements import UIElement

SIM = "AAAA-1111"


def _el(kind, label, x, y, w, h, ident=None):
    return {"type": kind, "AXLabel": label, "AXUniqueId": ident,
            "frame": {"x": x, "y": y, "width": w, "height": h},
            "enabled": True, "role": f"AX{kind}", "role_description": kind.lower()}


def _ctrl(tree):
    ctrl = DeviceController()
    ctrl._active_udid = SIM
    ctrl.idb.describe_all = AsyncMock(return_value=tree)
    ctrl.idb.tap = AsyncMock()
    ctrl.simctl.screenshot = AsyncMock(return_value=b"\x89PNGfake")
    return ctrl


MORE_TAB = _el("Button", "More", 294, 773, 74, 54, "More")
MORE_ICON = _el("Image", "More", 319, 788, 25, 10, "ellipsis")


class TestTapElement:
    async def test_a_control_and_its_own_icon_are_one_match(self):
        ctrl = _ctrl([MORE_TAB, MORE_ICON])
        result = await ctrl.tap_element(label="More", scroll_to_find=False,
                                        skip_stability_check=True)
        assert result["status"] == "ok", result
        assert result["tapped"]["type"] == "Button"
        ctrl.idb.tap.assert_called_once_with(SIM, 331.0, 800.0)
        assert result["collapsed"] == [
            {"type": "Image", "label": "More", "identifier": "ellipsis"}]

    async def test_side_by_side_matches_stay_ambiguous(self):
        ctrl = _ctrl([_el("Button", "Save", 20, 100, 80, 40),
                      _el("Button", "Save", 200, 100, 80, 40)])
        result = await ctrl.tap_element(label="Save", scroll_to_find=False,
                                        skip_stability_check=True)
        assert result["status"] == "ambiguous"
        ctrl.idb.tap.assert_not_called()

    async def test_a_plain_tap_reports_nothing_collapsed(self):
        ctrl = _ctrl([MORE_TAB])
        result = await ctrl.tap_element(label="More", scroll_to_find=False,
                                        skip_stability_check=True)
        assert result["status"] == "ok"
        assert "collapsed" not in result


class TestFold:
    @staticmethod
    def _m(kind, label, x, y, w, h, ident=None):
        return UIElement(type=kind, label=label, identifier=ident,
                         frame={"x": x, "y": y, "width": w, "height": h})

    def test_nested_with_the_same_label_folds_into_the_outer(self):
        outer = self._m("Button", "More", 294, 773, 74, 54)
        inner = self._m("Image", "More", 319, 788, 25, 10)
        kept, folded = controller_ui._fold_nested_matches([inner, outer])
        assert kept == [outer] and folded == [inner]

    def test_nested_with_a_different_label_stays(self):
        """label_contains can match a row and its shorter text; different
        labels are not known to be one control."""
        cell = self._m("Cell", "Wi-Fi, On", 0, 100, 390, 44)
        text = self._m("StaticText", "Wi-Fi", 16, 110, 60, 20)
        kept, folded = controller_ui._fold_nested_matches([cell, text])
        assert folded == [] and kept == [cell, text]

    def test_identical_frames_keep_the_control_not_its_text(self):
        text = self._m("StaticText", "Settings", 0, 100, 390, 44)
        cell = self._m("Cell", "Settings", 0, 100, 390, 44)
        kept, folded = controller_ui._fold_nested_matches([text, cell])
        assert kept == [cell] and folded == [text]

    def test_a_control_inside_a_control_stays_but_its_icon_folds(self):
        """A Switch in its Cell: the Cell's centre is not the switch."""
        cell = self._m("Cell", "Row", 0, 100, 390, 60)
        switch = self._m("Switch", "Row", 300, 110, 50, 30)
        icon = self._m("Image", "Row", 20, 110, 20, 20)
        kept, folded = controller_ui._fold_nested_matches([icon, switch, cell])
        assert kept == [switch, cell] and folded == [icon]

    def test_a_match_without_a_frame_is_never_folded(self):
        outer = self._m("Button", "More", 294, 773, 74, 54)
        frameless = UIElement(type="Image", label="More", identifier=None, frame=None)
        kept, folded = controller_ui._fold_nested_matches([outer, frameless])
        assert folded == [] and len(kept) == 2

    def test_two_controls_in_one_frame_stay_ambiguous(self):
        a = self._m("Button", "Go", 0, 0, 50, 50)
        b = self._m("Button", "Go", 0, 0, 50, 50)
        kept, folded = controller_ui._fold_nested_matches([a, b])
        assert len(kept) == 2 and folded == []

    def test_a_container_never_absorbs(self):
        """An Alert titled "Delete" around its Delete button; an Application
        labelled like a button on it (#490 review, CodeRabbit)."""
        alert = self._m("Alert", "Delete", 60, 300, 270, 140)
        button = self._m("Button", "Delete", 200, 400, 120, 40)
        kept, folded = controller_ui._fold_nested_matches([alert, button])
        assert folded == []
        app = self._m("Application", "Settings", 0, 0, 393, 852)
        text = self._m("StaticText", "Settings", 20, 100, 80, 20)
        kept, folded = controller_ui._fold_nested_matches([app, text])
        assert folded == []

    def test_unlabelled_matches_never_fold(self):
        """SwiftUI copies a container's identifier onto its children; two
        empty labels are not the same label."""
        outer = self._m("Button", "", 0, 100, 390, 200, ident="card")
        inner = self._m("Image", "", 300, 110, 40, 40, ident="card")
        kept, folded = controller_ui._fold_nested_matches([outer, inner])
        assert folded == []

    def test_labels_compare_without_case_or_padding(self):
        outer = self._m("Button", "More", 0, 0, 100, 50)
        inner = self._m("Image", " more ", 10, 10, 20, 20)
        kept, folded = controller_ui._fold_nested_matches([outer, inner])
        assert folded == [inner]


class TestTheReviewCases:
    """Each a tap the first version aimed at the wrong element (#490 review)."""

    async def test_a_value_tap_on_a_switch_in_its_row_stays_ambiguous(self):
        cell = _el("Cell", "Airplane Mode", 0, 100, 390, 44)
        switch = dict(_el("Switch", "Airplane Mode", 320, 107, 51, 31), AXValue="1")
        ctrl = _ctrl([cell, switch])
        result = await ctrl.tap_element(label="Airplane Mode", value="1",
                                        scroll_to_find=False, skip_stability_check=True)
        assert result["status"] == "ambiguous", result
        ctrl.idb.tap.assert_not_called()

    async def test_an_alert_titled_like_its_button_stays_ambiguous(self):
        alert = _el("Alert", "Delete", 60, 300, 270, 140)
        button = _el("Button", "Delete", 200, 400, 120, 40)
        ctrl = _ctrl([alert, button])
        result = await ctrl.tap_element(label="Delete", scroll_to_find=False,
                                        skip_stability_check=True)
        assert result["status"] == "ambiguous", result
        ctrl.idb.tap.assert_not_called()

    async def test_an_identifier_on_a_container_and_its_child_stays_ambiguous(self):
        outer = _el("Button", None, 0, 100, 390, 200, "card")
        inner = _el("Image", None, 300, 110, 40, 40, "card")
        ctrl = _ctrl([outer, inner])
        result = await ctrl.tap_element(identifier="card", scroll_to_find=False,
                                        skip_stability_check=True)
        assert result["status"] == "ambiguous", result

    async def test_the_stability_check_keeps_the_control_when_its_icon_comes_first(self):
        """The re-read took its first raw match -- the icon -- read the frame
        change as animation, and tapped the icon."""
        ctrl = _ctrl([MORE_ICON, MORE_TAB])
        ctrl.wait_for_settle = AsyncMock(return_value={"settled": True})
        result = await ctrl.tap_element(label="More", scroll_to_find=False)
        assert result["status"] == "ok", result
        assert result["tapped"]["type"] == "Button"
        ctrl.idb.tap.assert_called_once_with(SIM, 331.0, 800.0)
        ctrl.wait_for_settle.assert_not_called()

    async def test_already_set_reports_what_was_folded(self):
        switch = dict(_el("Switch", "Wi-Fi", 320, 107, 51, 31), AXValue="1")
        glyph = _el("Image", "Wi-Fi", 330, 112, 20, 20)
        ctrl = _ctrl([switch, glyph])
        result = await ctrl.tap_element(label="Wi-Fi", value="1", scroll_to_find=False,
                                        skip_stability_check=True)
        assert result["status"] == "already_set", result
        assert result["collapsed"] == [{"type": "Image", "label": "Wi-Fi", "identifier": None}]


def _landing_ctrl(elements, describe=None):
    """As tests/test_tap_landing.py builds one: the tree is given, the backend
    hit-tests through describe_point."""
    from unittest.mock import MagicMock

    from server.models import DeviceType

    ctrl = DeviceController()
    ctrl._device_type_cache[SIM] = DeviceType.SIMULATOR
    ctrl.resolve_udid = AsyncMock(return_value=SIM)
    ctrl.get_ui_elements = AsyncMock(return_value=(elements, SIM))
    ctrl._warn_if_input_is_suppressed = AsyncMock()
    backend = MagicMock()
    backend.tap = AsyncMock()
    backend.describe_point = AsyncMock(side_effect=describe or (lambda *a: None))
    ctrl._ui_backend = MagicMock(return_value=backend)
    ctrl.wait_for_settle = AsyncMock(return_value={"settled": True})
    return ctrl, backend


async def _landing_tap(ctrl, **kwargs):
    from unittest.mock import patch

    with patch("server.device.controller_ui._capture_screenshot", AsyncMock(return_value=None)):
        return await ctrl.tap_element(skip_stability_check=True, scroll_to_find=False, **kwargs)


class TestEveryAnswerSaysWhatWasFolded:
    async def test_a_web_element_tap(self):
        link = UIElement(type="Link", label="Sign in",
                         extra_attrs={"source": "web-inspector"},
                         frame={"x": 16, "y": 503, "width": 370, "height": 39})
        glyph = UIElement(type="Image", label="Sign in",
                          extra_attrs={"source": "web-inspector"},
                          frame={"x": 20, "y": 510, "width": 20, "height": 20})
        ctrl, backend = _landing_ctrl([link, glyph])
        ctrl._web_element_still_there = AsyncMock(return_value=True)
        result = await _landing_tap(ctrl, label="Sign in")
        assert result["status"] == "ok", result
        assert result["tapped"]["source"] == "web-inspector"
        assert result["collapsed"] == [{"type": "Image", "label": "Sign in", "identifier": None}]

    async def test_an_obstructed_tap(self):
        button = UIElement(type="Button", label="Pay", identifier="pay",
                           frame={"x": 20, "y": 300, "width": 353, "height": 44})
        glyph = UIElement(type="Image", label="Pay",
                          frame={"x": 30, "y": 310, "width": 20, "height": 20})

        def covered(*a):
            return {"type": "StaticText", "AXLabel": "AutoFill",
                    "frame": {"x": 150, "y": 296, "width": 90, "height": 52}}

        ctrl, backend = _landing_ctrl([button, glyph], describe=covered)
        result = await _landing_tap(ctrl, label="Pay")
        assert result["status"] == "obstructed", result
        assert result["collapsed"] == [{"type": "Image", "label": "Pay", "identifier": None}]
        backend.tap.assert_not_called()


async def test_a_moving_control_is_still_the_one_tapped_after_it_settles():
    """The final re-read, after a move, took its first raw match too: with
    the icon listed first, the tap went to the icon (#490 review)."""
    moved_tab = dict(MORE_TAB, frame={"x": 294, "y": 700, "width": 74, "height": 54})
    moved_icon = dict(MORE_ICON, frame={"x": 319, "y": 715, "width": 25, "height": 10})
    reads = [[MORE_ICON, MORE_TAB]] + [[moved_icon, moved_tab]] * 5
    ctrl = _ctrl(reads[0])
    ctrl.idb.describe_all = AsyncMock(side_effect=reads)
    ctrl.wait_for_settle = AsyncMock(return_value={"settled": True})
    result = await ctrl.tap_element(label="More", scroll_to_find=False)
    assert result["status"] == "ok", result
    ctrl.wait_for_settle.assert_awaited()  # it did see the move
    assert result["tapped"]["type"] == "Button"
    ctrl.idb.tap.assert_called_once_with(SIM, 331.0, 727.0)
