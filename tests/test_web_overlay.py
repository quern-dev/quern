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

import asyncio
import time

import pytest

from server.device.controller import DeviceController
from server.models import DeviceError, UIElement


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


async def test_an_enclosing_element_confirms_a_nested_one():
    """Accessibility flattens nesting the DOM keeps: an emoji <span> inside a
    link is reported as the link. A tap there still activates what was asked
    for, so the differing name is not evidence of staleness."""
    ctrl = DeviceController()
    with_backend(ctrl, {"AXLabel": "Source - GoToSocial 0.22.1",
                        "frame": {"x": 59, "y": 599, "width": 304, "height": 23}})
    span = UIElement(type="StaticText", label="\U0001f9a5",
                     frame={"x": 17, "y": 599, "width": 42, "height": 23})
    # The span sits inside the link's line box even though its own frame starts
    # further left; what matters is that the answering element is larger.
    span.frame = {"x": 100, "y": 601, "width": 30, "height": 19}
    assert await ctrl._web_element_still_there("SIM", span) is True


async def test_a_same_sized_element_with_another_name_is_still_refused():
    """Equal frames say nothing about nesting. Accepting them would let an
    element swapped in at the same coordinates pass for the original."""
    ctrl = DeviceController()
    with_backend(ctrl, {"AXLabel": "Donate",
                        "frame": {"x": 351, "y": 160, "width": 27, "height": 19}})
    element = UIElement(type="Button", label="Toggle menu",
                        frame={"x": 351, "y": 160, "width": 27, "height": 19})
    assert await ctrl._web_element_still_there("SIM", element) is False


# ------------------------------------------------- clearing the right field

def _controller_with_fields(*fields):
    ctrl = DeviceController()

    async def elements(udid=None, **kw):
        return list(fields), "SIM"

    ctrl.get_ui_elements = elements
    ctrl.resolve_udid = lambda udid=None: _immediate("SIM")
    ctrl._is_physical = lambda _u: False
    ctrl._invalidate_ui_cache = lambda *_a, **_k: None
    cleared: list = []

    class Backend:
        async def select_all_and_delete(self, udid, x, y, element_type):
            cleared.append((x, y, element_type))

    ctrl._ui_backend = lambda _u: Backend()
    return ctrl, cleared


def _field(label, x, y, value=None, type_="TextField"):
    el = UIElement(type=type_, label=label,
                   frame={"x": x, "y": y, "width": 300, "height": 40})
    el.value = value
    return el


async def test_clearing_by_label_targets_that_field():
    """Regression for a live failure: on a sign-in form, clearing before typing
    the password emptied the email field instead, because the chosen field is
    the first one holding a value rather than the one just tapped."""
    email = _field("Email", 16, 350, value="someone@example.test")
    password = _field("Password", 16, 433)
    ctrl, cleared = _controller_with_fields(email, password)

    await ctrl.clear_text(label="Password")

    assert cleared == [(166.0, 453.0, "TextField")], "cleared the wrong field"


async def test_clearing_without_a_selector_picks_the_field_holding_text():
    """The documented fallback, pinned so the surprise is at least a known one."""
    email = _field("Email", 16, 350, value="someone@example.test")
    password = _field("Password", 16, 433)
    ctrl, cleared = _controller_with_fields(email, password)

    await ctrl.clear_text()

    assert cleared == [(166.0, 370.0, "TextField")]


async def test_clearing_a_field_that_is_not_there_is_an_error():
    """Silently clearing some other field is how the email got emptied."""
    ctrl, cleared = _controller_with_fields(_field("Email", 16, 350, value="x"))
    with pytest.raises(DeviceError):
        await ctrl.clear_text(label="Nonexistent")
    assert cleared == []


# ------------------------------------------------- the probe route

class _Sweep:
    def __init__(self, elements, probes=7, reason=None):
        self.elements, self.probes = elements, probes
        self.elapsed_ms, self.truncated, self.reason = 120.0, False, reason


def _web_controller(collect_result, sweep):
    """A controller whose Inspector and sweep are both stubbed."""
    ctrl = DeviceController()
    ctrl.resolve_udid = lambda udid=None: _immediate("SIM")
    ctrl._is_android = lambda _u: False
    ctrl._is_physical = lambda _u: False
    ctrl._booted_simulator_count = lambda: _immediate(1)
    ctrl._connected_web_inspector = lambda: _immediate(object())
    ctrl._close_web_inspector = lambda: _immediate(None)

    async def native(udid=None, *a, **kw):
        return [], "SIM"

    ctrl._native_ui_elements = native
    ctrl.swept = []

    async def do_sweep(udid, native_elements):
        ctrl.swept.append(udid)
        return sweep

    ctrl._sweep_web_content = do_sweep

    import server.device.web_content as wc
    ctrl._original_collect = wc.collect_web_content

    async def collect(*a, **kw):
        return dict(collect_result)

    wc.collect_web_content = collect
    return ctrl


def _restore(ctrl):
    import server.device.web_content as wc
    wc.collect_web_content = ctrl._original_collect


async def test_an_inspector_that_sees_nothing_falls_back_to_probing():
    """An ASWebAuthenticationSession is presented with no connected application
    at all, so the OAuth screen every app has is the one the Inspector cannot
    describe. Hit-testing still reaches it."""
    ctrl = _web_controller(
        {"elements": [], "pages": [], "probes": 0, "anchored": False,
         "reason": "no app is connected to the Web Inspector."},
        _Sweep([{"type": "Button", "AXLabel": "Sign in",
                 "frame": {"x": 16, "y": 503, "width": 370, "height": 39}}]))
    try:
        result = await ctrl.get_web_content(udid="SIM")
    finally:
        _restore(ctrl)

    assert result["route"] == "probe"
    assert [e["AXLabel"] for e in result["elements"]] == ["Sign in"]
    assert result["reason"] is None, "a route that worked must not report a failure"
    assert result["probes"] == 7


async def test_probing_is_skipped_when_the_inspector_already_answered():
    """The sweep costs a screenshot, text recognition and a dozen probes. It is
    the route of last resort, not a supplement."""
    ctrl = _web_controller(
        {"elements": [{"type": "Button", "AXLabel": "Toggle menu",
                       "frame": {"x": 1, "y": 2, "width": 3, "height": 4},
                       "source": "web-inspector"}],
         "pages": [{"page_id": 1}], "probes": 1, "anchored": True, "reason": None},
        _Sweep([{"type": "Button", "AXLabel": "should not appear",
                 "frame": {"x": 0, "y": 0, "width": 9, "height": 9}}]))
    try:
        result = await ctrl.get_web_content(udid="SIM")
    finally:
        _restore(ctrl)

    assert result["route"] == "inspector"
    assert ctrl.swept == [], "swept despite the Inspector having answered"


async def test_when_neither_route_finds_anything_both_failures_are_reported():
    ctrl = _web_controller(
        {"elements": [], "pages": [], "probes": 0, "anchored": False,
         "reason": "no app is connected to the Web Inspector."},
        _Sweep([], probes=3, reason="nothing on screen to aim at"))
    try:
        result = await ctrl.get_web_content(udid="SIM")
    finally:
        _restore(ctrl)

    assert result["route"] is None
    assert "no app is connected" in result["reason"]
    assert "nothing on screen to aim at" in result["reason"]


async def test_a_probed_element_is_tappable_like_an_inspected_one():
    """tap_element gates the pre-tap probe on the element's source, so a route
    it does not recognise silently loses its verification."""
    ctrl = DeviceController()
    with_backend(ctrl, {"AXLabel": "Sign in",
                        "frame": {"x": 16, "y": 503, "width": 370, "height": 39}})
    ctrl._store_web_overlay("SIM", [web("Sign in", 16, 503, 370, 39)])
    stored, _ = ctrl._web_overlay["SIM"]
    assert stored[0].extra_attrs["source"] == "web-inspector"

    ctrl._store_web_overlay("SIM", [
        {"type": "Button", "AXLabel": "Sign in", "source": "web-probe",
         "frame": {"x": 16, "y": 503, "width": 370, "height": 39}}])
    stored, _ = ctrl._web_overlay["SIM"]
    assert stored[0].extra_attrs["source"] == "web-probe"
    assert await ctrl._web_element_still_there("SIM", stored[0]) is True


async def test_tap_element_verifies_a_probed_element_before_tapping():
    """The pre-tap probe is gated on the element's source. A route the gate does
    not recognise falls through to the native path, which re-reads the tree,
    finds the same overlay entry and taps it unverified -- so a probed element
    would be tapped on trust while an inspected one is checked."""
    ctrl = DeviceController()
    ctrl.resolve_udid = lambda udid=None: _immediate("SIM")
    ctrl._is_android = lambda _u: False
    ctrl._is_physical = lambda _u: False
    ctrl._invalidate_ui_cache = lambda *_a, **_k: None
    ctrl._store_web_overlay("SIM", [
        {"type": "Button", "AXLabel": "Sign in", "source": "web-probe",
         "frame": {"x": 16, "y": 503, "width": 370, "height": 39}}])
    overlay, _ = ctrl._web_overlay["SIM"]

    async def elements(udid=None, **kw):
        return list(overlay), "SIM"

    ctrl.get_ui_elements = elements
    probed, tapped = [], []

    class Backend:
        async def describe_point(self, udid, x, y):
            probed.append((x, y))
            # Something else is there now: the page moved under us.
            return {"AXLabel": "Cancel",
                    "frame": {"x": 16, "y": 503, "width": 370, "height": 39}}

        async def tap(self, udid, x, y):
            tapped.append((x, y))

    ctrl._ui_backend = lambda _u: Backend()

    result = await ctrl.tap_element(label="Sign in", element_type="Button", udid="SIM")

    assert probed, "the probed element was tapped without being verified"
    assert tapped == [], "tapped an element that is no longer there"
    assert result["status"] == "not_found"
    assert result["reason"] == "stale_web_content"


# ------------------------------------------------- listing pages for identity

class _FakeInspector:
    def __init__(self, apps, pages):
        self.apps, self.pages_by_app = apps, pages

    async def connected_applications(self):
        return self.apps

    async def pages(self, application_id):
        return self.pages_by_app.get(application_id, [])


def _url_controller(apps, pages, *, owners, booted=1):
    ctrl = DeviceController()
    ctrl._is_android = lambda _u: False
    ctrl._is_physical = lambda _u: False
    ctrl._booted_simulator_count = lambda: _immediate(booted)
    ctrl._connected_web_inspector = lambda: _immediate(_FakeInspector(apps, pages))
    import server.device.webinspector as wi
    ctrl._original_attr = wi.simulator_udid_for_application

    async def attribute(app_id):
        return owners.get(app_id)

    wi.simulator_udid_for_application = attribute
    return ctrl


def _restore_attr(ctrl):
    import server.device.webinspector as wi
    wi.simulator_udid_for_application = ctrl._original_attr


APP_A = {"application_id": "PID:1", "bundle_id": "com.example.app"}
HELPER = {"application_id": "PID:2", "bundle_id": "com.apple.WebKit.WebContent"}


async def test_helper_processes_contribute_no_urls():
    """WebKit's own processes are not the app under test."""
    ctrl = _url_controller(
        [APP_A, HELPER],
        {"PID:1": [{"url": "https://x.test/settings"}],
         "PID:2": [{"url": "https://helper.test/"}]},
        owners={"PID:1": "SIM", "PID:2": "SIM"})
    try:
        assert await ctrl.web_page_urls("SIM") == [
            {"url": "https://x.test/settings", "process": "com.example.app"}]
    finally:
        _restore_attr(ctrl)


async def test_another_simulators_pages_are_not_this_devices():
    """The socket carries no UDID, so a page can belong to another booted
    simulator -- and would otherwise satisfy a landmark for this one."""
    ctrl = _url_controller(
        [APP_A], {"PID:1": [{"url": "https://x.test/settings"}]},
        owners={"PID:1": "OTHER-SIM"}, booted=2)
    try:
        assert await ctrl.web_page_urls("SIM") == []
    finally:
        _restore_attr(ctrl)


async def test_unattributable_pages_are_refused_when_several_are_booted():
    ctrl = _url_controller(
        [APP_A], {"PID:1": [{"url": "https://x.test/settings"}]},
        owners={}, booted=2)
    try:
        assert await ctrl.web_page_urls("SIM") == []
    finally:
        _restore_attr(ctrl)


async def test_unattributable_pages_are_allowed_when_only_one_is_booted():
    """With a single simulator there is nothing to confuse it with, and
    refusing would make the feature unusable on the ordinary setup."""
    ctrl = _url_controller(
        [APP_A], {"PID:1": [{"url": "https://x.test/settings"}]},
        owners={}, booted=1)
    try:
        assert await ctrl.web_page_urls("SIM") == [
            {"url": "https://x.test/settings", "process": "com.example.app"}]
    finally:
        _restore_attr(ctrl)


async def test_listing_pages_holds_the_shared_connection_lock():
    """The connection is shared and the protocol interleaves replies, so a
    listing running alongside a content read would consume its messages."""
    ctrl = _url_controller(
        [APP_A], {"PID:1": [{"url": "https://x.test/settings"}]},
        owners={"PID:1": "SIM"})
    try:
        await ctrl._web_inspector_op_lock.acquire()
        task = asyncio.create_task(ctrl.web_page_urls("SIM"))
        await asyncio.sleep(0.1)
        assert not task.done(), "listed pages while another operation held the lock"
        ctrl._web_inspector_op_lock.release()
        # Bounded: if the listing stays blocked after the lock is free, this
        # should fail rather than hang the suite.
        assert await asyncio.wait_for(task, timeout=5) == [
            {"url": "https://x.test/settings", "process": "com.example.app"}]
    finally:
        _restore_attr(ctrl)


async def test_a_device_that_cannot_have_web_pages_reports_no_listing():
    """None, not []: an empty list would be evidence that no page is open, and
    on Android there is simply no listing to consult."""
    ctrl = DeviceController()
    ctrl._is_android = lambda _u: True
    assert await ctrl.web_page_urls("emulator-5554") is None


async def test_an_uncountable_simulator_is_not_treated_as_a_single_one():
    """The count decides whether an unattributable Inspector connection is safe
    to trust. A failed count read as "one" turns that into a yes."""
    ctrl = DeviceController()

    class Broken:
        async def list_devices(self):
            raise OSError("simctl unavailable")

    ctrl.simctl = Broken()
    assert await ctrl._booted_simulator_count() is None


async def test_pages_are_refused_when_the_simulator_count_is_unknown():
    ctrl = _url_controller(
        [APP_A], {"PID:1": [{"url": "https://x.test/settings"}]}, owners={})
    ctrl._booted_simulator_count = lambda: _immediate(None)
    try:
        assert await ctrl.web_page_urls("SIM") == []
    finally:
        _restore_attr(ctrl)


async def test_a_failed_listing_reports_no_listing_rather_than_no_pages():
    ctrl = DeviceController()
    ctrl._is_android = lambda _u: False
    ctrl._is_physical = lambda _u: False
    ctrl._booted_simulator_count = lambda: _immediate(1)

    async def boom():
        raise RuntimeError("inspector unreachable")

    ctrl._connected_web_inspector = boom
    assert await ctrl.web_page_urls("SIM") is None


def test_a_web_fields_value_survives_into_the_overlay():
    """clear_text sizes its work from the value. A field that reports None
    reads as already empty, and the clear silently does nothing."""
    ctrl = controller_with_overlay([
        {"type": "TextField", "AXLabel": "Instance", "source": "web-inspector",
         "value": "https://social.example",
         "frame": {"x": 35, "y": 325, "width": 332, "height": 39}}])
    stored, _ = ctrl._web_overlay["SIM"]
    assert stored[0].value == "https://social.example"


# ------------------------------------------------- clearing a web field

class _ClearBackend:
    def __init__(self):
        self.select_calls, self.taps, self.deleted = [], [], []

    async def select_all_and_delete(self, udid, x, y, element_type):
        self.select_calls.append((x, y, element_type))

    async def tap(self, udid, x, y):
        self.taps.append((x, y))

    async def delete_backwards(self, udid, count):
        self.deleted.append(count)


def _clear_controller(field, *, web_clear=False):
    ctrl = DeviceController()
    ctrl.resolve_udid = lambda udid=None: _immediate("SIM")
    ctrl._is_physical = lambda _u: False
    ctrl._is_android = lambda _u: False
    ctrl._invalidate_ui_cache = lambda *_a, **_k: None
    backend = _ClearBackend()
    ctrl._ui_backend = lambda _u: backend

    async def elements(udid=None, **kw):
        return [field], "SIM"

    ctrl.get_ui_elements = elements
    ctrl._clear_web_input = lambda _u: _immediate(web_clear)
    return ctrl, backend


def _web_field(value: str):
    el = UIElement(type="TextField", label="Instance",
                   frame={"x": 35, "y": 325, "width": 332, "height": 39})
    el.value = value
    el.extra_attrs = {"source": "web-inspector"}
    return el


def _native_field(value: str):
    el = UIElement(type="TextArea", label="",
                   identifier="composition.text",
                   frame={"x": 0, "y": 100, "width": 300, "height": 80})
    el.value = value
    return el


async def test_a_native_field_is_done_after_the_triple_tap():
    """It selects the whole value there -- measured at 60 characters to 0 in one
    call -- so deleting again would spend seconds re-clearing an empty field."""
    ctrl, backend = _clear_controller(_native_field("x" * 61))
    await ctrl.clear_text(identifier="composition.text")
    assert backend.select_calls, "the fast path did not run"
    assert backend.deleted == [], "spent keystrokes on an already-cleared field"


async def test_a_web_field_is_finished_off_by_keystrokes():
    """The triple-tap removes one character there, so the field looks cleared
    without being cleared."""
    ctrl, backend = _clear_controller(_web_field("y" * 60), web_clear=False)
    await ctrl.clear_text(label="Instance")
    assert backend.deleted, "left the web field partly filled"
    assert backend.deleted[0] >= 60, "deleted fewer characters than the field held"


async def test_the_caret_is_moved_to_the_end_before_deleting():
    """Backspace only removes what is behind the caret, so a tap in the middle
    of the text would leave everything after it."""
    ctrl, backend = _clear_controller(_web_field("z" * 20), web_clear=False)
    await ctrl.clear_text(label="Instance")
    assert backend.taps, "no caret-placing tap"
    x, _ = backend.taps[-1]
    centre = 35 + 332 / 2
    assert x > centre, "tapped at or before the centre, leaving a suffix behind"


async def test_the_inspector_clear_short_circuits_the_keystrokes():
    """6ms against ~63ms per character; the keystroke path is the fallback."""
    ctrl, backend = _clear_controller(_web_field("w" * 100), web_clear=True)
    await ctrl.clear_text(label="Instance")
    assert backend.deleted == [], "typed backspaces despite the page being cleared"


async def test_an_empty_web_field_costs_nothing_extra():
    ctrl, backend = _clear_controller(_web_field(""), web_clear=False)
    await ctrl.clear_text(label="Instance")
    assert backend.deleted == []


async def test_a_field_too_long_to_clear_is_refused_not_half_cleared():
    """Sending the cap and reporting ok would leave a value partly there while
    saying it was cleared -- the failure this whole change is about."""
    ctrl, backend = _clear_controller(_web_field("q" * 900), web_clear=False)
    with pytest.raises(DeviceError) as exc:
        await ctrl.clear_text(label="Instance")
    assert "900" in str(exc.value)
    assert backend.deleted == [], "typed backspaces it knew could not finish"
    # And it did not edit the field on the way to refusing: the triple-tap
    # removes a character from a web input, so running it first would leave 899.
    assert backend.select_calls == [], "modified a field it then refused to clear"


async def test_the_inspector_is_tried_even_when_the_cached_value_looks_empty():
    """`value` is an overlay snapshot. If it is stale and empty, gating on it
    would skip both routes and report a clear that never happened."""
    ctrl, backend = _clear_controller(_web_field(""), web_clear=True)
    cleared: list = []
    ctrl._clear_web_input = lambda _u: (cleared.append(True), _immediate(True))[1]
    await ctrl.clear_text(label="Instance")
    assert cleared, "never asked the inspector because the snapshot looked empty"


async def test_a_web_clear_on_another_simulator_does_not_count():
    """Clearing a focused input on a different booted simulator and reporting
    success would leave the field actually asked about untouched."""
    ctrl = DeviceController()
    ctrl._is_android = lambda _u: False
    ctrl._is_physical = lambda _u: False
    ctrl._booted_simulator_count = lambda: _immediate(2)
    cleared: list = []

    class Inspector:
        async def connected_applications(self):
            return [{"application_id": "PID:9", "bundle_id": "com.example.app"}]

        async def pages(self, app_id):
            return [{"page_id": 1}]

        async def clear_focused_input(self, app_id, page_id):
            cleared.append((app_id, page_id))
            return True

    ctrl._connected_web_inspector = lambda: _immediate(Inspector())
    ctrl._close_web_inspector = lambda: _immediate(None)
    import server.device.webinspector as wi
    original = wi.simulator_udid_for_application

    async def elsewhere(_app_id):
        return "SOME-OTHER-SIM"

    wi.simulator_udid_for_application = elsewhere
    try:
        assert await ctrl._clear_web_input("SIM") is False
        assert cleared == [], "cleared an input on another simulator"
    finally:
        wi.simulator_udid_for_application = original
