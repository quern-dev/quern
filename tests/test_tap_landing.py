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


def test_a_dismiss_popup_layer_can_itself_be_tapped():
    """Tapping the layer by name is how a caller dismisses it; the label check
    once ran before the identity check and refused it as covered by itself."""
    layer = element("dismiss popup", 0, 0, 393, 852, "Group")
    assert _covering_element(layer, hit("dismiss popup", 0, 0, 393, 852, "Group")) is None


def test_frames_half_a_point_apart_are_the_same_element():
    """The tree and the hit-test round frames differently: half a point out on
    one side and in on the other is neither inside nor around, without slack."""
    assert _covering_element(ROW, hit("Sound", 35.5, 53, 321, 28, "CheckBox")) is None


def test_an_element_just_past_the_target_is_covering():
    """Slack is a point, not a licence: shifted two points, it neither sits
    inside the target nor encloses it."""
    assert _covering_element(ROW, hit("Bar", 34, 53, 321, 28, "Heading"))


def test_a_frame_that_is_not_numbers_is_not_covering():
    bad = {"type": "Button", "AXLabel": "x",
           "frame": {"x": "a", "y": None, "width": 1, "height": 1}}
    assert _covering_element(FIELD, bad) is None


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
    ctrl._screen_bounds[SIM] = (0.0, 0.0, 393.0, 852.0)
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


async def test_a_hit_test_with_an_unexpected_shape_does_not_stop_the_tap():
    """A label that is not a string, say: the comparison cannot run, so it
    cannot refuse."""
    ctrl, backend = _controller(describe=lambda *a: ["not", "a", "dict"])
    result = await _tap(ctrl, identifier="field_secure")
    assert result["status"] == "ok"
    backend.tap.assert_awaited_once()


@pytest.mark.parametrize("kind", [DeviceType.DEVICE, DeviceType.ANDROID_EMULATOR,
                                  DeviceType.ANDROID_DEVICE])
async def test_only_a_simulator_is_hit_tested(kind):
    """Not WDA alone: no other backend's hit-testing was measured."""
    ctrl, _ = _controller(kind=kind)
    ctrl._served_by_wda = lambda _udid: False
    assert ctrl._checks_landing(SIM) is False


async def test_a_simulator_is_hit_tested():
    ctrl, _ = _controller()
    assert ctrl._checks_landing(SIM) is True


@pytest.mark.parametrize("x,y,inside", [
    (393, 852, True),     # the far edge is on screen
    (393.5, 400, False),
    (0, 0, True),
    (-0.5, 400, False),
])
async def test_the_screen_edge(x, y, inside):
    target = element("Edge", x - 1, y - 1, 2, 2)
    ctrl, backend = _controller(target=target)
    ctrl._screen_bounds[SIM] = (0.0, 0.0, 393.0, 852.0)
    result = await _tap(ctrl, label="Edge")
    assert (result["status"] == "ok") is inside


async def test_an_app_window_that_does_not_start_at_the_origin():
    """iPad Split View: the right-hand app's frame starts at x=678, and a
    check from (0, 0) refused every element in it."""
    target = element("Done", 900, 40, 60, 30)
    ctrl, backend = _controller(target=target)
    ctrl._screen_bounds[SIM] = (678.0, 0.0, 516.0, 834.0)
    result = await _tap(ctrl, label="Done")
    assert result["status"] == "ok"
    backend.tap.assert_awaited_once()

    left_of_it = element("Back", 300, 40, 60, 30)
    ctrl, backend = _controller(target=left_of_it)
    ctrl._screen_bounds[SIM] = (678.0, 0.0, 516.0, 834.0)
    assert (await _tap(ctrl, label="Back"))["reason"] == "off_screen"


def test_the_first_application_is_the_one_kept():
    """An alert or a keyboard host can be a second Application after the app."""
    ctrl = DeviceController()
    ctrl._remember_screen(SIM, [
        {"type": "Application", "frame": {"x": 0, "y": 0, "width": 393, "height": 852}},
        {"type": "Application", "frame": {"x": 0, "y": 500, "width": 393, "height": 352}},
    ])
    assert ctrl._screen_bounds[SIM] == (0.0, 0.0, 393.0, 852.0)


def test_an_application_with_no_size_is_not_kept():
    ctrl = DeviceController()
    ctrl._remember_screen(SIM, [{"type": "Application",
                                 "frame": {"x": 0, "y": 0, "width": 0, "height": 0}}])
    assert SIM not in ctrl._screen_bounds


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


async def test_the_wait_comes_before_the_hit_test():
    """Hit-testing a screen mid-transition answers for frames it has not
    reached yet; the wait has to come first."""
    order = []
    ctrl, backend = _controller(describe=lambda *a: order.append("hit-test") or
                                hit("", 20, 300, 353, 40, "TextField"))

    async def settle(*_a, **_k):
        order.append("settle")
        return {"settled": True}

    ctrl.wait_for_settle = AsyncMock(side_effect=settle)
    ctrl._last_ui_change[SIM] = time.monotonic()
    await _tap(ctrl, identifier="field_secure", skip=False)
    assert order == ["settle", "hit-test"]


async def test_a_refusal_after_a_wait_says_it_waited():
    ctrl, backend = _controller(describe=lambda *a: hit("AutoFill", 192, 296, 83, 44, "StaticText"))
    ctrl._last_ui_change[SIM] = time.monotonic()
    result = await _tap(ctrl, identifier="field_secure", skip=False)
    assert result["status"] == "obstructed" and "waited_for_settle_ms" in result


@pytest.mark.parametrize("action", ["press_button", "terminate_app", "set_hardware_keyboard"])
async def test_other_screen_changing_actions_count_as_a_change(action):
    """A tap straight after Home, a terminate or a keyboard toggle landed
    mid-transition as surely as one after a tap."""
    ctrl = DeviceController()
    ctrl._device_type_cache[SIM] = DeviceType.SIMULATOR
    ctrl.resolve_udid = AsyncMock(return_value=SIM)
    backend = MagicMock()
    backend.press_button = AsyncMock()
    ctrl._ui_backend = MagicMock(return_value=backend)
    ctrl.simctl = MagicMock()
    ctrl.simctl.terminate_app = AsyncMock()
    ctrl.sim_bridge = MagicMock()
    ctrl.sim_bridge.set_hardware_keyboard = AsyncMock()
    ctrl._sim_bridge_ok = True
    ctrl._warn_if_input_is_suppressed = AsyncMock()
    args = {"press_button": ("home",), "terminate_app": ("com.example.app",),
            "set_hardware_keyboard": (True,)}[action]
    with patch.object(ctrl, "_invalidate_ui_cache", wraps=ctrl._invalidate_ui_cache) as noted:
        await getattr(ctrl, action)(*args, udid=SIM)
    noted.assert_called_with(SIM)
    assert SIM in ctrl._last_ui_change


async def test_a_web_element_tap_waits_for_the_screen_too():
    """Web content has its own path, and a transition swallows a tap there as
    surely as on native."""
    link = UIElement(type="Link", label="Sign in", extra_attrs={"source": "web-inspector"},
                     frame={"x": 16, "y": 503, "width": 370, "height": 39})
    ctrl, backend = _controller(target=link)
    ctrl._web_element_still_there = AsyncMock(return_value=True)
    ctrl._last_ui_change[SIM] = time.monotonic()
    result = await _tap(ctrl, label="Sign in", skip=False)

    assert result["status"] == "ok" and result["tapped"]["source"] == "web-inspector"
    ctrl.wait_for_settle.assert_awaited_once()
    assert "waited_for_settle_ms" in result
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
    assert ctrl._screen_bounds[SIM] == (0.0, 0.0, 393.0, 852.0)


# -- coordinates ------------------------------------------------------------------------


async def test_a_coordinate_tap_says_what_it_landed_on():
    ctrl, backend = _controller(describe=lambda *a: hit("AutoFill", 192, 406, 83, 44, "StaticText"))
    report = await ctrl.tap_and_report(232, 427, udid=SIM)

    assert report["udid"] == SIM
    assert report["landed_on"]["label"] == "AutoFill"
    backend.tap.assert_awaited_once()


async def test_a_coordinate_tap_waits_after_a_change():
    ctrl, _ = _controller()
    ctrl._last_ui_change[SIM] = time.monotonic()
    report = await ctrl.tap_and_report(100, 100, udid=SIM)
    ctrl.wait_for_settle.assert_awaited_once()
    assert "waited_for_settle_ms" in report


async def test_a_coordinate_tap_can_skip_the_wait():
    ctrl, backend = _controller()
    ctrl._last_ui_change[SIM] = time.monotonic()
    report = await ctrl.tap_and_report(100, 100, udid=SIM, skip_settle=True)
    ctrl.wait_for_settle.assert_not_called()
    assert "waited_for_settle_ms" not in report
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
    assert ctrl._screen_bounds[SIM] == (0.0, 0.0, 393.0, 852.0)
