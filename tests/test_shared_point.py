"""A tap whose target shares its point with another control, layered with it
(#781's dropped-phone run).

A phone's tree push lists an overlay's rows and the rows hidden under it, and
its hit-test calls both reachable. Measured on an iPhone 11 with Settings'
search open: `tap_element` on the hidden "Wi-Fi" row answered ok, and the tap
opened Accessibility, the visible suggestion over it. The push cannot say
which is on top, so the tap goes ahead and says so, with whether the screen
changed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from server.device.controller_ui import _sharing_the_point
from server.models import UIElement
from tests.test_coredevice_ax import PHONE, SETTINGS, _phone_controller, parse_push


def el(type_, label, x, y, w, h, identifier=None):
    return UIElement(type=type_, label=label, identifier=identifier,
                     frame={"x": x, "y": y, "width": w, "height": h})


# Measured on the iPhone 11, Settings with its search open.
WIFI = el("Button", "Wi-Fi, MonaLisaOverdrive", 20, 266.5, 374, 52, "com.apple.settings.wifi")
SUGGESTION = el("Button", "Accessibility", 0, 284.5, 414, 58)


class TestTheRule:
    def test_a_control_crossing_the_target_at_the_point_shares_it(self):
        assert _sharing_the_point(WIFI, 207, 292.5, [WIFI, SUGGESTION]) == [SUGGESTION]

    def test_the_targets_own_parts_do_not(self):
        own = el("Button", "MonaLisaOverdrive", 200, 280, 150, 20)
        assert _sharing_the_point(WIFI, 207, 292.5, [WIFI, own]) == []

    def test_a_container_round_the_target_does_not(self):
        cell = el("Cell", "row", 0, 260, 414, 70)
        assert _sharing_the_point(WIFI, 207, 292.5, [WIFI, cell]) == []

    def test_rows_edge_to_edge_do_not(self):
        below = el("Button", "Bluetooth, On", 20, 318.5, 374, 52)
        assert _sharing_the_point(WIFI, 207, 292.5, [WIFI, below]) == []

    def test_something_that_is_not_a_control_does_not(self):
        heading = el("Heading", "Suggestions", 0, 280, 414, 40)
        assert _sharing_the_point(WIFI, 207, 292.5, [WIFI, heading]) == []

    def test_one_crossing_the_target_away_from_the_point_does_not(self):
        aside = el("Button", "Edge", 380, 250, 60, 40)
        assert _sharing_the_point(WIFI, 207, 292.5, [WIFI, aside]) == []

    def test_no_screen_is_nothing_to_say(self):
        assert _sharing_the_point(WIFI, 207, 292.5, None) == []


class TestTheTap:
    def _ctrl(self, monkeypatch, frames_after):
        """Settings' push with an overlay row crossing Wi-Fi at its centre."""
        from server.device import frames

        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl.coredevice_input.tap = AsyncMock(return_value="coredevice")
        ctrl.wda_client.tap = AsyncMock()
        ctrl.coredevice_ax.describe_point = AsyncMock(return_value=None)
        wifi = next(e for e in parse_push(SETTINGS)[1]
                    if e["AXUniqueId"] == "com.apple.settings.wifi")
        f = wifi["frame"]
        overlay = {"type": "Button", "AXLabel": "Accessibility", "AXUniqueId": None,
                   "AXValue": None, "enabled": True, "role": "AXButton",
                   "role_description": "", "help": None, "custom_actions": [],
                   "frame": {"x": 0.0, "y": f["y"] + f["height"] / 2 - 8,
                             "width": 414.0, "height": 58.0}}
        ctrl.coredevice_ax.describe_all = AsyncMock(
            return_value=[*parse_push(SETTINGS)[1], overlay])
        captured = []

        async def capture(controller, udid, source=None):
            captured.append(source)
            return (b"before" if len(captured) == 1 else frames_after, 0.0, "pmd3")

        monkeypatch.setattr(frames, "capture_for_check", capture)
        monkeypatch.setattr(frames, "VERIFY_WAIT_S", 0.0)
        # The real verdict's shape, from (frame, time, source) tuples.
        monkeypatch.setattr(frames, "changed_verdict", lambda before, after: (
            {"screen_changed": None, "screen_change": {"reason": "no frame after"}}
            if after is None or after[0] is None else
            {"screen_changed": after[0] != before[0], "screen_change": {"fraction": 0.5}}))
        return ctrl, captured

    async def test_it_taps_and_says_the_screen_did_not_change(self, monkeypatch):
        ctrl, captured = self._ctrl(monkeypatch, frames_after=b"before")
        result = await ctrl.tap_element(identifier="com.apple.settings.wifi", udid=PHONE,
                                        scroll_to_find=False)

        assert result["status"] == "ok"
        ctrl.coredevice_input.tap.assert_awaited_once()
        assert [o["label"] for o in result["shares_point_with"]] == ["Accessibility"]
        assert result["screen_changed"] is False
        assert "did not change" in result["warning"]
        assert "Read the screen before tapping again" in result["warning"]
        assert "then tap again" not in result["warning"]
        assert len(captured) == 2                 # a frame before the tap, one after

    async def test_a_changed_screen_says_to_read_which_took_it(self, monkeypatch):
        ctrl, _ = self._ctrl(monkeypatch, frames_after=b"after")
        result = await ctrl.tap_element(identifier="com.apple.settings.wifi", udid=PHONE,
                                        scroll_to_find=False)
        assert result["screen_changed"] is True
        assert "The screen changed" in result["warning"]

    async def test_no_frame_is_could_not_tell_not_a_failure(self, monkeypatch):
        ctrl, _ = self._ctrl(monkeypatch, frames_after=None)
        result = await ctrl.tap_element(identifier="com.apple.settings.wifi", udid=PHONE,
                                        scroll_to_find=False)
        assert result["status"] == "ok"
        assert result["screen_changed"] is None
        assert "could not be told" in result["warning"]

    async def test_a_tap_with_nothing_sharing_the_point_is_not_checked(self, monkeypatch):
        """The plain case costs nothing: no frames, no warning."""
        ctrl, captured = self._ctrl(monkeypatch, frames_after=b"before")
        ctrl.coredevice_ax.describe_all = AsyncMock(return_value=parse_push(SETTINGS)[1])
        result = await ctrl.tap_element(identifier="com.apple.settings.wifi", udid=PHONE,
                                        scroll_to_find=False)
        assert result["status"] == "ok"
        assert "shares_point_with" not in result and "warning" not in result
        assert captured == []
