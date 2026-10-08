"""A lookup that misses on web content says get_web_content is the next step (#436).

On a page in Safari, or a web modal, the accessibility tree holds none of the
page, so `type_text label="Username"` answered "no text field matching" and
nothing more -- read as "the field does not exist", and the caller fell back to
coordinate taps and unverified typing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from server.device.controller import DeviceController
from server.device.controller_ui import _web_content_hint
from server.models import DeviceError, DeviceType, UIElement


def element(type_, label, w=100.0, h=40.0, identifier=None):
    return UIElement(type=type_, label=label, identifier=identifier,
                     frame={"x": 0, "y": 0, "width": w, "height": h})


#: What Safari's tree held on iOS 18.6 with a sign-in form on the page.
SAFARI = [
    element("Application", "Safari", 402, 874),
    element("Button", "Page Menu", identifier="PageFormatMenuButton"),
    element("TextField", "Address", identifier="TabBarItemTitle"),
    element("Button", "refresh", identifier="ReloadButton"),
]
#: A presented web modal: the tree collapses to the app itself.
WEB_MODAL = [element("Application", "Geocaching", 402, 874)]
#: A bridge poisoned by XCUITest (#66) collapses too, but nameless (null, which
#: parses to "") and 0x0.
POISONED = [element("Application", "", 0, 0)]
NATIVE = [element("Application", "Settings", 402, 874), element("Button", "General")]


# -- the detection --------------------------------------------------------------


def test_safari_in_front_gets_the_hint():
    """Measured on iOS 26.5: after get_web_content, typing into a Safari field
    by label verified."""
    hint = _web_content_hint(SAFARI)
    assert "Safari" in hint and "get_web_content" in hint
    assert "can then be found by label" in hint


def test_a_tree_collapsed_to_the_app_gets_the_hint():
    hint = _web_content_hint(WEB_MODAL)
    assert "nothing but the app" in hint and "can then be found by label" in hint


def test_a_poisoned_bridge_does_not():
    """get_web_content would not help; the hint would send the caller the
    wrong way."""
    assert _web_content_hint(POISONED) is None
    # Either half alone is enough to withhold it: a collapse that is nameless,
    # or one with no real frame, is not the web-modal shape.
    assert _web_content_hint([element("Application", "", 402, 874)]) is None
    assert _web_content_hint([element("Application", "Geocaching", 0, 0)]) is None


def test_an_ordinary_native_screen_does_not():
    """Every miss carrying the hint would make it noise."""
    assert _web_content_hint(NATIVE) is None
    assert _web_content_hint([]) is None


# -- where it applies -------------------------------------------------------------


def _ctrl(kind=DeviceType.SIMULATOR, udid="SIM"):
    ctrl = DeviceController()
    ctrl._device_type_cache[udid] = kind
    return ctrl


def test_only_a_simulator_the_tool_can_serve_gets_it():
    assert _ctrl()._web_hint_for("SIM", SAFARI)
    assert _ctrl(DeviceType.DEVICE)._web_hint_for("SIM", SAFARI) is None, \
        "get_web_content refuses a physical iPhone"
    assert _ctrl(DeviceType.ANDROID_EMULATOR)._web_hint_for("SIM", SAFARI) is None
    served = _ctrl()
    served._served_by_wda = lambda _udid: True
    assert served._web_hint_for("SIM", SAFARI) is None, \
        "get_web_content refuses while WDA serves the simulator"


def test_not_when_web_content_was_already_searched():
    ctrl = _ctrl()
    ctrl._store_web_overlay("SIM", [{"type": "Button", "AXLabel": "Sign in",
                                     "source": "web-inspector",
                                     "frame": {"x": 0, "y": 0, "width": 9, "height": 9}}])
    assert ctrl._web_hint_for("SIM", SAFARI) is None


# -- the three misses ---------------------------------------------------------------


def _reading(ctrl, elements, udid="SIM"):
    ctrl.get_ui_elements = AsyncMock(return_value=(elements, udid))
    ctrl.resolve_udid = AsyncMock(return_value=udid)
    return ctrl


async def test_type_text_says_so():
    ctrl = _reading(_ctrl(), SAFARI)
    with pytest.raises(DeviceError, match="no text field matching 'Username'.*get_web_content"):
        await ctrl._find_text_field("SIM", label="Username", identifier=None)


async def test_get_element_says_so():
    ctrl = _reading(_ctrl(), SAFARI)
    with pytest.raises(DeviceError,
                       match="No element found matching label='Username'.*get_web_content"):
        await ctrl.get_element(label="Username", udid="SIM")


async def test_a_native_miss_is_unchanged():
    ctrl = _reading(_ctrl(), NATIVE)
    with pytest.raises(DeviceError) as caught:
        await ctrl._find_text_field("SIM", label="Username", identifier=None)
    assert "get_web_content" not in str(caught.value)


async def test_tap_element_says_so(monkeypatch):
    ctrl = _reading(_ctrl(), SAFARI)
    monkeypatch.setattr(ctrl, "screenshot", AsyncMock(side_effect=DeviceError("no", tool="x")))
    result = await ctrl.tap_element(label="Sign in", udid="SIM", scroll_to_find=False)
    assert result["status"] == "not_found"
    assert "get_web_content" in result["web_content_hint"]
    assert "get_web_content" in result["detail"]
