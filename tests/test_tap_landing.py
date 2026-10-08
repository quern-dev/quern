"""tap_element confirms what its tap will land on, and waits out a moving screen (#435).

It reported "ok" for taps that a navigation bar, a menu or nothing received,
and for a tap Settings swallowed mid-transition. The hit-test shapes below were
measured on an iOS 26.5 simulator (sim-bridge's describe_point).
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server.device.controller import DeviceController
from server.device.controller_ui import _covering_element
from server.models import DeviceError, DeviceType, UIElement

SIM = "AAAA-1111"


def element(label, x, y, w, h, type_="Button", identifier=None):
    return UIElement(type=type_, label=label, identifier=identifier,
                     frame={"x": x, "y": y, "width": w, "height": h})


def hit(label, x, y, w, h, type_="Button"):
    return {"type": type_, "AXLabel": label, "frame": {"x": x, "y": y, "width": w, "height": h}}


# -- what counts as covering ------------------------------------------------------

ROW = element("Sound", 36, 53, 321, 28, "CheckBox")
FIELD = element("", 20, 300, 353, 40, "TextField", "field_secure")


def test_the_element_itself_is_not_covering():
    assert _covering_element(ROW, hit("Sound", 36, 53, 321, 28, "CheckBox")) is None


def test_a_child_inside_the_target_is_not_covering():
    """A cell's label, say: the hit-test returns the deepest element."""
    cell = element("Keyboards, 2", 20, 131, 353, 53)
    assert _covering_element(cell, hit("Keyboards", 40, 145, 120, 22, "StaticText")) is None


def test_a_container_around_the_target_is_not_covering():
    """Maps' "Legal" link hit-tests as the map under it, and tapping it works:
    a container cannot be told from an overlay, so it does not refuse."""
    legal = element("Legal", 60, 745, 50, 17, "Link")
    assert _covering_element(legal, hit("Map", 0, 113, 393, 739, "Image")) is None


def test_a_navigation_title_over_a_row_is_covering():
    """A row scrolled under the bar hit-tests as the bar's title."""
    covering = _covering_element(ROW, hit("Keyboards", 154, 71, 85, 21, "Heading"))
    assert covering["label"] == "Keyboards" and covering["type"] == "Heading"


def test_a_floating_search_field_over_a_row_is_covering():
    """iOS 26 Settings' search field floats over the bottom rows."""
    screen_time = element("Screen Time", 16, 758, 361, 52)
    assert _covering_element(screen_time, hit("", 33, 781, 327, 38, "TextField"))


def test_a_menu_item_over_a_field_is_covering():
    """The edit menu above a field: its item is taller than the field, so it
    neither sits inside it nor encloses it."""
    assert _covering_element(FIELD, hit("AutoFill", 192, 296, 83, 44, "StaticText"))


def test_a_popover_dimming_view_is_covering_though_it_encloses_everything():
    """UIKit's "dismiss popup" layer: a Safari tip left it over the screen,
    taking every tap. It encloses the target, so only its label tells."""
    covering = _covering_element(FIELD, hit("dismiss popup", 0, 0, 393, 852, "Group"))
    assert covering["label"] == "dismiss popup"


def test_no_answer_is_not_covering():
    """The backends return None for a miss and for a failed ask alike; a check
    that could not run must not refuse a tap."""
    assert _covering_element(FIELD, None) is None
    assert _covering_element(FIELD, {"type": "Button", "AXLabel": "x"}) is None


# -- the controller ----------------------------------------------------------------


def _controller(*, describe=None, kind=DeviceType.SIMULATOR, target=FIELD):
    ctrl = DeviceController()
    ctrl._device_type_cache[SIM] = kind
    ctrl.resolve_udid = AsyncMock(return_value=SIM)
    ctrl.get_ui_elements = AsyncMock(return_value=([target], SIM))
    ctrl._warn_if_input_is_suppressed = AsyncMock()
    backend = MagicMock()
    backend.tap = AsyncMock()
    backend.describe_point = AsyncMock(side_effect=describe or (lambda *a: None))
    ctrl._ui_backend = MagicMock(return_value=backend)
    ctrl.wait_for_settle = AsyncMock(return_value={"settled": True})
    return ctrl, backend


async def _tap(ctrl, **kwargs):
    with patch("server.device.controller_ui._capture_screenshot", AsyncMock(return_value=None)):
        return await ctrl.tap_element(skip_stability_check=kwargs.pop("skip", True), **kwargs)


async def test_a_covered_element_is_not_tapped():
    ctrl, backend = _controller(describe=lambda *a: hit("AutoFill", 192, 296, 83, 44, "StaticText"))
    result = await _tap(ctrl, identifier="field_secure")

    assert result["status"] == "obstructed"
    assert result["reason"] == "covered"
    assert result["covered_by"]["label"] == "AutoFill"
    assert "AutoFill" in result["detail"]
    backend.tap.assert_not_called()


async def test_an_element_that_is_there_is_tapped():
    ctrl, backend = _controller(describe=lambda *a: hit("", 20, 300, 353, 40, "TextField"))
    result = await _tap(ctrl, identifier="field_secure")

    assert result["status"] == "ok"
    backend.tap.assert_awaited_once()


async def test_an_element_off_screen_is_not_tapped():
    """Rows below the screen were tapped at a point outside it, and "ok"."""
    below = element("Passcode", 16, 900, 361, 52)
    ctrl, backend = _controller(target=below)
    ctrl._screen_bounds[SIM] = (393.0, 852.0)
    result = await _tap(ctrl, label="Passcode")

    assert result["status"] == "obstructed" and result["reason"] == "off_screen"
    assert "scroll" in result["detail"].lower()
    backend.tap.assert_not_called()
    backend.describe_point.assert_not_called()


async def test_a_hit_test_that_fails_does_not_stop_the_tap():
    def broken(*_args):
        raise DeviceError("bridge gone", tool="sim-bridge")

    ctrl, backend = _controller(describe=broken)
    result = await _tap(ctrl, identifier="field_secure")
    assert result["status"] == "ok"
    backend.tap.assert_awaited_once()


async def test_a_physical_device_is_not_hit_tested():
    """Measured on simulators only; WDA's hit-testing is another backend."""
    ctrl, backend = _controller(kind=DeviceType.DEVICE,
                                describe=lambda *a: hit("Bar", 0, 0, 393, 100))
    ctrl._served_by_wda = lambda _udid: True
    ctrl._wda_reachable_only = AsyncMock(side_effect=lambda udid, m: (m, []))
    result = await _tap(ctrl, identifier="field_secure")
    assert result["status"] == "ok"
    backend.describe_point.assert_not_called()


# -- settling first -------------------------------------------------------------------


async def test_a_tap_right_after_a_change_waits_for_the_screen():
    """Settings' push transition swallowed a tap sent ~0.8s after the one that
    started it, three times in three, while the tree reported final frames."""
    ctrl, backend = _controller(describe=lambda *a: hit("", 20, 300, 353, 40, "TextField"))
    ctrl._last_ui_change[SIM] = time.monotonic()
    result = await _tap(ctrl, identifier="field_secure", skip=False)

    ctrl.wait_for_settle.assert_awaited_once()
    assert result["status"] == "ok" and "waited_for_settle_ms" in result


async def test_a_tap_long_after_a_change_does_not_wait():
    ctrl, _ = _controller(describe=lambda *a: hit("", 20, 300, 353, 40, "TextField"))
    ctrl._last_ui_change[SIM] = time.monotonic() - 10
    result = await _tap(ctrl, identifier="field_secure", skip=False)

    ctrl.wait_for_settle.assert_not_called()
    assert "waited_for_settle_ms" not in result


async def test_skip_stability_check_skips_the_wait_too():
    ctrl, _ = _controller(describe=lambda *a: hit("", 20, 300, 353, 40, "TextField"))
    ctrl._last_ui_change[SIM] = time.monotonic()
    await _tap(ctrl, identifier="field_secure", skip=True)
    ctrl.wait_for_settle.assert_not_called()


async def test_a_settle_that_fails_does_not_stop_the_tap():
    ctrl, backend = _controller(describe=lambda *a: hit("", 20, 300, 353, 40, "TextField"))
    ctrl._last_ui_change[SIM] = time.monotonic()
    ctrl.wait_for_settle = AsyncMock(side_effect=DeviceError("no frames", tool="screenshot"))
    result = await _tap(ctrl, identifier="field_secure", skip=False)
    assert result["status"] == "ok"
    backend.tap.assert_awaited_once()


def test_an_action_that_changes_the_screen_is_noted():
    ctrl = DeviceController()
    before = time.monotonic()
    ctrl._invalidate_ui_cache(SIM)
    assert ctrl._last_ui_change[SIM] >= before


def test_the_screen_size_is_kept_from_a_raw_read():
    """A filtered read carries no Application element, so off-screen could
    not be judged from it."""
    ctrl = DeviceController()
    ctrl._remember_screen(SIM, [{"type": "Application", "frame": {"x": 0, "y": 0,
                                                                  "width": 393, "height": 852}},
                                {"type": "Button", "frame": {}}])
    assert ctrl._screen_bounds[SIM] == (393.0, 852.0)


# -- coordinates ------------------------------------------------------------------------


async def test_a_coordinate_tap_says_what_it_landed_on():
    ctrl, backend = _controller(describe=lambda *a: hit("AutoFill", 192, 406, 83, 44, "StaticText"))
    report = await ctrl.tap_and_report(232, 427, udid=SIM)

    assert report["udid"] == SIM
    assert report["landed_on"]["label"] == "AutoFill"
    backend.tap.assert_awaited_once()


async def test_a_coordinate_tap_is_never_refused():
    """No target to compare against: it reports, it does not judge."""
    ctrl, backend = _controller(describe=lambda *a: hit("dismiss popup", 0, 0, 393, 852, "Group"))
    report = await ctrl.tap_and_report(100, 100, udid=SIM)
    assert report["landed_on"]["label"] == "dismiss popup"
    backend.tap.assert_awaited_once()


# -- the route ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_route_answers_409_and_logs_an_answer_not_a_failure():
    from fastapi import HTTPException

    from server.api import device_ui
    from server.models import TapElementRequest

    controller = MagicMock()
    controller.resolve_udid = AsyncMock(return_value=SIM)
    controller.tap_element = AsyncMock(return_value={
        "status": "obstructed", "reason": "covered", "detail": "under the bar",
        "covered_by": {"type": "Heading", "label": "Keyboards"},
    })
    controller.backend_that_served = MagicMock(return_value="sim-bridge")
    request = MagicMock()
    request.is_disconnected = AsyncMock(return_value=False)
    entries = []

    import logging
    handler = logging.Handler()
    handler.emit = lambda record: entries.append(getattr(record, "quern_outcome", None))
    logger = logging.getLogger("server.api.actions")
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        with patch.object(device_ui, "_get_controller", lambda _r: controller):
            with pytest.raises(HTTPException) as caught:
                await device_ui.tap_element(request=request,
                                            body=TapElementRequest(label="Sound", udid=SIM))
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)

    assert caught.value.status_code == 409
    assert caught.value.detail["covered_by"]["label"] == "Keyboards"
    assert "obstructed" in entries


async def test_a_filtered_read_still_records_the_screen_size():
    """tap_element reads with filters, which drop the Application element --
    so the size has to be kept from the raw tree on the way through."""
    ctrl = DeviceController()
    ctrl._device_type_cache[SIM] = DeviceType.SIMULATOR
    ctrl.resolve_udid = AsyncMock(return_value=SIM)
    backend = MagicMock()
    backend.describe_all = AsyncMock(return_value=[
        {"type": "Application", "AXLabel": "Settings",
         "frame": {"x": 0, "y": 0, "width": 393, "height": 852}},
        {"type": "Button", "AXLabel": "General",
         "frame": {"x": 16, "y": 290, "width": 361, "height": 52}},
    ])
    ctrl._ui_backend = MagicMock(return_value=backend)

    elements, _ = await ctrl.get_ui_elements(SIM, filter_label="General", use_cache=False)

    assert [e.label for e in elements] == ["General"], "the read was not filtered"
    assert ctrl._screen_bounds[SIM] == (393.0, 852.0)
