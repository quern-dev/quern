"""Making web content tappable through the ordinary UI path.

get_web_content returns elements the accessibility tree cannot see. Storing them
as an overlay lets tap_element resolve them by label like anything else -- but
their coordinates describe the page as it was when it was read, and nothing in
the tree will ever contradict that. A page that scrolls, navigates or gets
covered leaves the overlay confidently wrong.

So the overlay is dropped whenever anything changes the screen, and a tap
against a surviving entry is still probed first. A stale overlay then costs one
probe and an honest error instead of a tap on whatever now occupies that pixel.
"""

from __future__ import annotations

import time

from server.device.controller import DeviceController
from server.models import UIElement


def native(type_, label, x, y, w=100, h=40):
    return UIElement(type=type_, label=label,
                     frame={"x": x, "y": y, "width": w, "height": h})


def web(label, x, y, w=100, h=40, type_="Button", **attrs):
    return {"type": type_, "AXLabel": label, "source": "web-inspector",
            "frame": {"x": x, "y": y, "width": w, "height": h}, **attrs}


def controller_with_overlay(elements, udid="SIM"):
    ctrl = DeviceController()
    ctrl._store_web_overlay(udid, elements)
    return ctrl


# ---------------------------------------------------------------- merging

def test_web_elements_join_the_native_list():
    ctrl = controller_with_overlay([web("Toggle menu", 351, 160)])
    merged = ctrl._merge_web_overlay("SIM", [native("Button", "Done", 8, 78)])
    assert [e.label for e in merged] == ["Done", "Toggle menu"]


def test_a_device_without_an_overlay_is_untouched():
    ctrl = DeviceController()
    original = [native("Button", "Done", 8, 78)]
    assert ctrl._merge_web_overlay("SIM", original) is original


def test_an_element_the_tree_already_reports_is_not_duplicated():
    """A duplicate would make tap_element ambiguous for a single real button."""
    ctrl = controller_with_overlay([web("Done", 8, 78)])
    merged = ctrl._merge_web_overlay("SIM", [native("Button", "Done", 8, 78)])
    assert len(merged) == 1


def test_an_expired_overlay_is_dropped_rather_than_merged():
    ctrl = controller_with_overlay([web("Toggle menu", 351, 160)])
    stored, _ = ctrl._web_overlay["SIM"]
    ctrl._web_overlay["SIM"] = (stored, time.time() - ctrl._WEB_OVERLAY_TTL - 1)
    merged = ctrl._merge_web_overlay("SIM", [native("Button", "Done", 8, 78)])
    assert [e.label for e in merged] == ["Done"]
    assert "SIM" not in ctrl._web_overlay


def test_filters_apply_to_web_elements_too():
    """Otherwise a filtered read would return web elements the caller excluded."""
    ctrl = controller_with_overlay([web("Toggle menu", 351, 160), web("Servers", 24, 296)])
    merged = ctrl._merge_web_overlay("SIM", [], filter_label="Servers")
    assert [e.label for e in merged] == ["Servers"]


def test_the_overlay_belongs_to_one_device():
    ctrl = controller_with_overlay([web("Toggle menu", 351, 160)], udid="SIM-A")
    assert ctrl._merge_web_overlay("SIM-B", []) == []


# ---------------------------------------------------------------- invalidation

def test_anything_that_changes_the_screen_drops_the_overlay():
    """Every mutating operation already funnels through this one call."""
    ctrl = controller_with_overlay([web("Toggle menu", 351, 160)])
    ctrl._invalidate_ui_cache("SIM")
    assert "SIM" not in ctrl._web_overlay


def test_clearing_every_device_clears_every_overlay():
    ctrl = controller_with_overlay([web("A", 0, 0)], udid="SIM-A")
    ctrl._store_web_overlay("SIM-B", [web("B", 0, 0)])
    ctrl._invalidate_ui_cache()
    assert ctrl._web_overlay == {}


# ---------------------------------------------------------------- storage

def test_provenance_survives_into_the_stored_element():
    ctrl = controller_with_overlay([web("Sign up", 0, 0, dom_id="cta", page_id=3)])
    stored, _ = ctrl._web_overlay["SIM"]
    assert stored[0].extra_attrs["source"] == "web-inspector"
    assert stored[0].extra_attrs["dom_id"] == "cta"


def test_a_dom_id_is_not_offered_as_an_accessibility_identifier():
    """A DOM id never appears in the native tree. Letting it match identifier=
    would make a web-only lookup indistinguishable from an ordinary one."""
    ctrl = controller_with_overlay([web("Sign up", 0, 0, dom_id="cta")])
    stored, _ = ctrl._web_overlay["SIM"]
    assert stored[0].identifier is None


def test_reading_a_page_with_nothing_on_it_clears_any_previous_overlay():
    ctrl = controller_with_overlay([web("Toggle menu", 351, 160)])
    ctrl._store_web_overlay("SIM", [])
    assert "SIM" not in ctrl._web_overlay


# ---------------------------------------------------------------- pre-tap probe

class FakeBackend:
    def __init__(self, hit):
        self.hit, self.probes = hit, []

    async def describe_point(self, udid, x, y):
        self.probes.append((x, y))
        return self.hit


def with_backend(ctrl, hit):
    backend = FakeBackend(hit)
    ctrl._ui_backend = lambda _udid: backend
    return backend


async def test_an_element_still_in_place_is_confirmed():
    ctrl = DeviceController()
    with_backend(ctrl, {"AXLabel": "Toggle menu",
                        "frame": {"x": 351, "y": 160, "width": 27, "height": 19}})
    element = UIElement(type="Button", label="Toggle menu",
                        frame={"x": 351, "y": 160, "width": 27, "height": 19})
    assert await ctrl._web_element_still_there("SIM", element) is True


async def test_something_else_at_those_coordinates_is_refused():
    """The page scrolled; the pixel now belongs to a different control."""
    ctrl = DeviceController()
    with_backend(ctrl, {"AXLabel": "Donate",
                        "frame": {"x": 351, "y": 160, "width": 27, "height": 19}})
    element = UIElement(type="Button", label="Toggle menu",
                        frame={"x": 351, "y": 160, "width": 27, "height": 19})
    assert await ctrl._web_element_still_there("SIM", element) is False


async def test_a_probe_landing_outside_any_element_is_refused():
    """A hit-test in whitespace answers with the nearest element, so the
    returned frame has to be checked for the point."""
    ctrl = DeviceController()
    with_backend(ctrl, {"AXLabel": "Toggle menu",
                        "frame": {"x": 0, "y": 0, "width": 10, "height": 10}})
    element = UIElement(type="Button", label="Toggle menu",
                        frame={"x": 351, "y": 160, "width": 27, "height": 19})
    assert await ctrl._web_element_still_there("SIM", element) is False


async def test_an_unlabelled_control_is_confirmed_by_position_alone():
    """An icon-only button carries no text to compare; landing inside its frame
    is the only evidence available, and refusing would make it untappable."""
    ctrl = DeviceController()
    with_backend(ctrl, {"AXLabel": "",
                        "frame": {"x": 24, "y": 144, "width": 182, "height": 58}})
    element = UIElement(type="Link", label="",
                        frame={"x": 24, "y": 144, "width": 182, "height": 58})
    assert await ctrl._web_element_still_there("SIM", element) is True


async def test_a_probe_that_raises_is_treated_as_unconfirmed():
    ctrl = DeviceController()

    class Exploding:
        async def describe_point(self, udid, x, y):
            raise RuntimeError("bridge is wedged")

    ctrl._ui_backend = lambda _udid: Exploding()
    element = UIElement(type="Button", label="Toggle menu",
                        frame={"x": 351, "y": 160, "width": 27, "height": 19})
    assert await ctrl._web_element_still_there("SIM", element) is False


async def test_the_probe_targets_the_centre_of_the_recorded_frame():
    ctrl = DeviceController()
    backend = with_backend(ctrl, None)
    element = UIElement(type="Button", label="x",
                        frame={"x": 100, "y": 200, "width": 40, "height": 20})
    await ctrl._web_element_still_there("SIM", element)
    assert backend.probes == [(120.0, 210.0)]


async def test_a_previous_read_cannot_influence_the_next_one():
    """get_web_content must anchor against the native tree alone.

    The overlay feeds page-title ranking, app_frame and the candidate origins.
    Reading through the merged view would let a stale result help decide where
    the next one thinks the page is -- a feedback loop with no way out.
    """
    ctrl = DeviceController()
    ctrl._store_web_overlay("SIM", [web("Servers", 24, 296)])
    seen: dict = {}

    async def fake_native(udid=None, *args, **kwargs):
        return [native("Application", "App", 0, 0, 402, 874)], "SIM"

    async def fake_collect(udid, bundle_id, describe_point, inspector, native_arg, **kw):
        seen["labels"] = [e["AXLabel"] for e in native_arg]
        return {"elements": [], "pages": [], "probes": 0, "anchored": False}

    ctrl._native_ui_elements = fake_native
    ctrl.resolve_udid = lambda udid=None: _immediate("SIM")
    ctrl._is_android = lambda _u: False
    ctrl._is_physical = lambda _u: False
    ctrl._booted_simulator_count = lambda: _immediate(1)
    ctrl._connected_web_inspector = lambda: _immediate(object())

    import server.device.web_content as wc
    original = wc.collect_web_content
    wc.collect_web_content = fake_collect
    try:
        await ctrl.get_web_content(udid="SIM")
    finally:
        wc.collect_web_content = original

    assert seen["labels"] == ["App"], "the stale overlay leaked into anchoring"


async def _immediate(value):
    return value
