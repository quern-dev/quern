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

    def test_three_levels_fold_to_the_outermost(self):
        cell = self._m("Cell", "Row", 0, 100, 390, 60)
        button = self._m("Button", "Row", 10, 105, 200, 50)
        icon = self._m("Image", "Row", 20, 110, 20, 20)
        kept, folded = controller_ui._fold_nested_matches([icon, button, cell])
        assert kept == [cell] and {id(f) for f in folded} == {id(icon), id(button)}

    def test_a_match_without_a_frame_is_never_folded(self):
        outer = self._m("Button", "More", 294, 773, 74, 54)
        frameless = UIElement(type="Image", label="More", identifier=None, frame=None)
        kept, folded = controller_ui._fold_nested_matches([outer, frameless])
        assert folded == [] and len(kept) == 2

    def test_equal_duplicates_are_folded_once_not_both_dropped(self):
        """Folded by identity: two equal elements compare equal."""
        a = self._m("Button", "Go", 0, 0, 50, 50)
        b = self._m("Button", "Go", 0, 0, 50, 50)
        kept, folded = controller_ui._fold_nested_matches([a, b])
        assert len(kept) == 1 and len(folded) == 1

    def test_labels_compare_without_case_or_padding(self):
        outer = self._m("Button", "More", 0, 0, 100, 50)
        inner = self._m("Image", " more ", 10, 10, 20, 20)
        kept, folded = controller_ui._fold_nested_matches([outer, inner])
        assert folded == [inner]
