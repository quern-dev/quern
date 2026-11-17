"""get_web_content on a physical iPhone (#708).

Through the phone's Web Inspector service, which pymobiledevice3 speaks with
no Xcode, collected with the simulator's own script, and placed on screen from
the tree push, which already carries the web view's elements.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.device.controller import DeviceController
from server.device.web import phone_webinspector
from server.device.web.phone_webinspector import PhoneWebInspector
from server.device.web.web_content import native_anchors
from server.models import DeviceError, DeviceType

PHONE = "B34C4EE9-AF48-53C6-BD13-2BFA66E7EE91"
USB = "00008030-000C59623A69802E"

#: The probe's web page, as `_COLLECT_JS` reported it on an iPhone 11.
PAGE = {"url": "about:blank", "title": "", "viewport": {"width": 414, "height": 657},
        "scroll": {"x": 0, "y": 0}, "elements": [
            {"tag": "h1", "id": "web_heading", "text": "Probe Web Heading",
             "x": 24, "y": 24, "width": 294.2, "height": 39},
            {"tag": "button", "id": "web_button", "text": "Web Button",
             "x": 36, "y": 123.4, "width": 61.4, "height": 14, "interactive": True}]}
#: The same screen's push: the web view's elements are in it, in screen space.
NATIVE = [{"type": "Application", "AXLabel": "QuernProbeHosted",
           "frame": {"x": 0, "y": 0, "width": 414, "height": 896}},
          {"type": "Heading", "AXLabel": "Probe Web Heading",
           "frame": {"x": 24, "y": 180, "width": 295, "height": 39}},
          {"type": "Button", "AXLabel": "Web Button",
           "frame": {"x": 24, "y": 276, "width": 86, "height": 21}}]


def _entry(app_id="PID:748", bundle="com.quern.probe.hosted", page_id=1,
           type_="WIRTypeWebPage"):
    return SimpleNamespace(
        application=SimpleNamespace(id_=app_id, bundle=bundle, name="Probe", pid=748),
        page=SimpleNamespace(id_=page_id, web_title="", web_url="about:blank",
                             type_=SimpleNamespace(value=type_)))


class FakeService:
    def __init__(self, entries, contents=PAGE, raises=None):
        self.entries, self.contents, self.raises = entries, contents, raises
        self.closed = False

    async def connect(self):
        if self.raises:
            raise self.raises

    async def get_open_application_pages(self, timeout):
        return self.entries

    async def inspector_session(self, app, page):
        contents = self.contents

        class Session:
            async def runtime_enable(self):
                pass

            async def runtime_evaluate(self, exp, return_by_value=False):
                assert "elements" in exp                     # the collector, not a guess
                return contents
        return Session()

    async def close(self):
        self.closed = True


def _patch(monkeypatch, service):
    import pymobiledevice3.lockdown as lockdown
    import pymobiledevice3.services.webinspector as webinspector

    seen = {}

    async def connect(**kw):
        seen.update(kw)
        return "lockdown"
    monkeypatch.setattr(lockdown, "create_using_usbmux", connect)
    monkeypatch.setattr(webinspector, "WebinspectorService", lambda lockdown: service)
    return seen


# -- the adapter ------------------------------------------------------------------


class TestTheInspector:
    async def test_it_answers_as_collection_asks(self, monkeypatch):
        service = FakeService([_entry(), _entry(page_id=2, type_="WIRTypeJavaScript")])
        seen = _patch(monkeypatch, service)

        async with PhoneWebInspector(USB) as inspector:
            apps = await inspector.connected_applications()
            pages = await inspector.pages("PID:748")
            contents = await inspector.page_contents("PID:748", 1)

        assert seen == {"serial": USB, "autopair": False}
        assert apps == [{"application_id": "PID:748", "bundle_id": "com.quern.probe.hosted",
                         "name": "Probe", "pid": 748}]
        assert pages == [{"page_id": 1, "title": "", "url": "about:blank"}]   # no JSContext
        assert contents["elements"][0]["id"] == "web_heading"
        assert service.closed

    async def test_the_switch_being_off_says_where_it_is(self, monkeypatch):
        from pymobiledevice3.exceptions import WebInspectorNotEnabledError

        service = FakeService([], raises=WebInspectorNotEnabledError())
        _patch(monkeypatch, service)
        with pytest.raises(DeviceError, match="Safari > Advanced > Web Inspector"):
            async with PhoneWebInspector(USB):
                pass
        assert service.closed

    async def test_another_failure_is_said_too(self, monkeypatch):
        service = FakeService([], raises=OSError("no usbmux"))
        _patch(monkeypatch, service)
        with pytest.raises(DeviceError, match="could not reach the phone's Web Inspector: "
                                              "no usbmux"):
            async with PhoneWebInspector(USB):
                pass

    async def test_a_call_that_hangs_is_given_up(self, monkeypatch):
        import asyncio

        monkeypatch.setattr(phone_webinspector, "CALL_TIMEOUT_S", 0.05)
        with pytest.raises(DeviceError, match="did not answer"):
            await phone_webinspector.bounded(asyncio.sleep(5))


# -- anchoring from the push ----------------------------------------------------------


def test_a_text_both_sides_carry_gives_the_origin():
    """y=24 in the page and y=180 on screen, measured: the page starts at 156."""
    [anchor] = native_anchors(NATIVE, PAGE)[:1]
    assert (anchor.dx, anchor.dy) == (0.0, 156.0)


def test_a_text_said_twice_anchors_nothing():
    native = NATIVE + [{"type": "StaticText", "AXLabel": "Probe Web Heading",
                        "frame": {"x": 0, "y": 600, "width": 9, "height": 9}},
                       {"type": "StaticText", "AXLabel": "Web Button",
                        "frame": {"x": 0, "y": 700, "width": 9, "height": 9}}]
    assert native_anchors(native, PAGE) == []
    assert native_anchors([], PAGE) == []                   # a simulator's tree


# -- the controller --------------------------------------------------------------------


def _phone(monkeypatch, *, pushed=True):
    ctrl = DeviceController()
    ctrl._active_udid = PHONE
    ctrl._device_type_cache[PHONE] = DeviceType.DEVICE
    ctrl._device_tree_reader = lambda udid: ctrl.coredevice_ax if pushed else None
    from server.models import UIElement

    elements = [UIElement(type=e["type"], label=e["AXLabel"], frame=e["frame"]) for e in NATIVE]
    ctrl._native_ui_elements = AsyncMock(return_value=(elements, PHONE))
    ctrl.get_libimobiledevice_udid = AsyncMock(return_value=USB)

    async def hit(udid, x, y):
        for e in NATIVE[1:]:
            f = e["frame"]
            if f["x"] <= x <= f["x"] + f["width"] and f["y"] <= y <= f["y"] + f["height"]:
                return {"type": e["type"], "AXLabel": e["AXLabel"], "frame": f}
        return None
    ctrl.coredevice_ax.describe_point = hit
    return ctrl


async def test_a_phone_s_web_view_is_read_and_placed_on_screen(monkeypatch):
    ctrl = _phone(monkeypatch)
    seen = _patch(monkeypatch, FakeService([_entry()]))

    result = await ctrl.get_web_content(udid=PHONE)

    assert seen["serial"] == USB                             # the phone's own UDID
    assert result["route"] == "inspector" and result["anchored"]
    assert result["anchors"][0]["strategy"] == "native" and result["probes"] == 1
    button = next(e for e in result["elements"] if e.get("dom_id") == "web_button")
    assert button["frame"]["y"] == pytest.approx(279.4)
    assert result["device_verified"] is True


async def test_a_phone_wda_reads_is_pointed_at_its_tree(monkeypatch):
    ctrl = _phone(monkeypatch, pushed=False)
    # Should the refusal ever go, the inspector is still a fake, not the phone.
    _patch(monkeypatch, FakeService([_entry()]))
    with pytest.raises(DeviceError, match="already includes web content"):
        await ctrl.get_web_content(udid=PHONE)


# -- tapping what was read (#708) ----------------------------------------------------


def test_a_web_element_the_push_carries_is_not_added_twice():
    """The push frames the probe's button 24,276 86x21, the DOM 36,279 61x14:
    added as a second "Web Button", a tap by label was refused as ambiguous."""
    from server.models import UIElement

    ctrl = DeviceController()
    native = [UIElement(type="Button", label="Web Button",
                        frame={"x": 24, "y": 276, "width": 86, "height": 21})]
    ctrl._store_web_overlay(PHONE, [
        {"type": "Button", "AXLabel": "Web Button", "source": "web-inspector",
         "dom_id": "web_button", "frame": {"x": 36, "y": 279.4, "width": 61.4, "height": 14}},
        {"type": "StaticText", "AXLabel": "only in the DOM", "source": "web-inspector",
         "frame": {"x": 24, "y": 400, "width": 120, "height": 20}}])

    merged = ctrl._merge_web_overlay(PHONE, native)

    assert [e.label for e in merged] == ["Web Button", "only in the DOM"]


async def test_a_web_tap_on_a_phone_is_checked_with_the_push_s_hit_test(monkeypatch):
    """The UI backend there is WDA, which has no point lookup: every web
    tap read as stale."""
    from server.models import UIElement

    ctrl = _phone(monkeypatch)
    ctrl.wda_client.describe_point = AsyncMock(side_effect=AssertionError("asked WDA"))
    element = UIElement(type="Button", label="Web Button",
                        frame={"x": 36, "y": 279.4, "width": 61.4, "height": 14})
    assert await ctrl._web_element_still_there(PHONE, element)
