"""get_web_content on a physical iPhone (#708).

Through the phone's Web Inspector service, which pymobiledevice3 speaks with
no Xcode, collected with the simulator's own script, and placed on screen from
the tree push, which already carries the web view's elements.
"""

from __future__ import annotations

import asyncio
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
        self.is_connected = True
        self.connects = 0
        #: pymobiledevice3's listing cache, which a kept connection empties.
        self.application_pages: dict = {}
        #: (session id, application id, page id) for every page released.
        self.torn_down: list[tuple[str, str, int]] = []

    async def connect(self):
        self.connects += 1
        if self.raises:
            raise self.raises

    async def teardown_inspector_socket(self, session_id, app_id, page_id):
        self.torn_down.append((session_id, app_id, page_id))

    async def get_open_application_pages(self, timeout):
        return self.entries

    async def inspector_session(self, app, page):
        contents = self.contents

        class Session:
            async def runtime_enable(self):
                pass

            async def runtime_evaluate(self, exp, return_by_value=False):
                if exp == "document.visibilityState":
                    return "visible"
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
    _stamp(service)
    return seen


def _stamp(service):
    """Every session `service` hands out carries the id its page is released
    by, as pymobiledevice3's does, whichever fake made it."""
    made = service.inspector_session
    count = iter(range(1, 1000))

    async def inspector_session(app, page):
        session = await made(app, page)
        session.protocol = SimpleNamespace(id_=f"S{next(count)}")
        return session
    service.inspector_session = inspector_session
    return service


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
        # Kept for the next call (#718), with the page it opened released.
        assert not service.closed
        assert service.torn_down == [("S1", "PID:748", 1)]

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


# -- page URLs and the instant clear (#708) ------------------------------------------


class Evaluating(FakeService):
    """Answers the clear script per page, and the collector otherwise."""

    def __init__(self, entries, cleared_on=None, background=()):
        super().__init__(entries)
        self.cleared_on, self.asked, self.background = cleared_on, [], set(background)

    async def inspector_session(self, app, page):
        outer = self

        class Session:
            async def runtime_enable(self):
                pass

            async def runtime_evaluate(self, exp, return_by_value=False):
                if page.id_ in outer.background:
                    await asyncio.sleep(60)                  # a tab behind another
                if exp == "document.visibilityState":
                    return "visible"
                outer.asked.append(page.id_)
                if "elements" in exp:
                    return PAGE
                return "cleared" if page.id_ == outer.cleared_on else "no-focus"
        return Session()


async def test_a_phone_s_page_urls_carry_their_app(monkeypatch):
    ctrl = _phone(monkeypatch)
    helper = _entry(app_id="PID:9", bundle="com.apple.WebKit.WebContent")
    _patch(monkeypatch, FakeService([_entry(), helper]))
    assert await ctrl.web_page_urls(PHONE) == [
        {"url": "about:blank", "process": "com.quern.probe.hosted"}]   # no WebKit helper


async def test_the_focused_web_input_is_cleared_on_the_page_that_has_it(monkeypatch):
    ctrl = _phone(monkeypatch)
    service = Evaluating([_entry(page_id=1), _entry(page_id=2)], cleared_on=2)
    _patch(monkeypatch, service)
    assert await ctrl._clear_web_input(PHONE) is True
    assert service.asked == [1, 2]


async def test_no_focused_input_is_no_clear(monkeypatch):
    ctrl = _phone(monkeypatch)
    _patch(monkeypatch, Evaluating([_entry()], cleared_on=None))
    assert await ctrl._clear_web_input(PHONE) is False


async def test_where_the_inspector_cannot_be_asked_both_say_so_quietly(monkeypatch):
    """URLs: None, not [] -- an empty list is evidence there are no pages.
    The clear: False, so the keystroke path runs."""
    from pymobiledevice3.exceptions import WebInspectorNotEnabledError

    ctrl = _phone(monkeypatch)
    _patch(monkeypatch, FakeService([], raises=WebInspectorNotEnabledError()))
    assert await ctrl.web_page_urls(PHONE) is None
    assert await ctrl._clear_web_input(PHONE) is False
    wda = _phone(monkeypatch, pushed=False)
    assert await wda.web_page_urls(PHONE) is None


# -- CodeRabbit on #715 ----------------------------------------------------------------


def test_a_container_that_says_what_a_control_says_does_not_hide_it():
    """A web view labelled with the page's title, around the heading that
    carries it: a different element, so the heading is still added."""
    from server.models import UIElement

    ctrl = DeviceController()
    native = [UIElement(type="WebView", label="Probe Web Heading",
                        frame={"x": 0, "y": 156, "width": 414, "height": 657})]
    ctrl._store_web_overlay(PHONE, [
        {"type": "Heading", "AXLabel": "Probe Web Heading", "source": "web-inspector",
         "frame": {"x": 24, "y": 180, "width": 294.2, "height": 39}}])

    merged = ctrl._merge_web_overlay(PHONE, native)

    assert [e.type for e in merged] == ["WebView", "Heading"]


async def test_the_screen_under_a_phone_s_web_view_is_read_from_the_push(monkeypatch):
    """Through the real `_native_ui_elements`: a read that is not the whole
    screen goes to WDA, which a phone read without it does not have."""
    from tests.test_coredevice_ax import _phone_controller

    ctrl = _phone_controller(monkeypatch=monkeypatch)
    ctrl.get_libimobiledevice_udid = AsyncMock(return_value=USB)
    _patch(monkeypatch, FakeService([_entry()]))

    await ctrl.get_web_content(udid=PHONE)

    ctrl.coredevice_ax.describe_all.assert_awaited()
    ctrl.wda_client.describe_all.assert_not_awaited()


async def test_a_connect_given_up_on_closes_the_service(monkeypatch):
    import asyncio

    class Hanging(FakeService):
        async def connect(self):
            await asyncio.sleep(5)

    service = Hanging([])
    _patch(monkeypatch, service)
    monkeypatch.setattr(phone_webinspector, "CALL_TIMEOUT_S", 0.05)

    async def enter():
        async with PhoneWebInspector(USB):
            pass
    with pytest.raises(DeviceError, match="did not answer"):
        await phone_webinspector.bounded(enter())
    assert service.closed


async def test_a_page_has_one_session_and_its_loop_ends_with_the_inspector(monkeypatch):
    """pymobiledevice3's InspectorSession starts a loop that never ends by
    itself; left running, every read added one. And a second session on the
    same page never answers, so the visibility probe and the collection that
    follows it share one."""
    import asyncio

    loops = []

    class Looping(FakeService):
        async def inspector_session(self, app, page):
            session = await super().inspector_session(app, page)
            session._receive_task = asyncio.create_task(asyncio.sleep(60))
            loops.append(session._receive_task)
            return session

    _patch(monkeypatch, Looping([_entry()]))
    async with PhoneWebInspector(USB) as inspector:
        assert await inspector.pages("PID:748")
        await inspector.page_contents("PID:748", 1)
        assert len(loops) == 1 and not loops[0].done()
    await asyncio.sleep(0)
    assert loops[0].cancelled()


async def test_a_page_in_the_background_is_neither_waited_for_nor_cleared(monkeypatch):
    """Measured on an iPhone 11: a Safari tab behind another never answers.
    Asked in turn, it held the clear until the call's 30 s bound; and what is
    sent to it may run when the tab comes back."""
    import time

    monkeypatch.setattr(phone_webinspector, "VISIBLE_S", 0.05)
    ctrl = _phone(monkeypatch)
    service = Evaluating([_entry(page_id=1), _entry(page_id=2)], cleared_on=2,
                         background={1})
    _patch(monkeypatch, service)
    started = time.monotonic()
    assert await ctrl._clear_web_input(PHONE) is True
    assert time.monotonic() - started < 1
    assert service.asked == [2]                              # the clear went to page 2 only
    # Its URL is still listed: that is read from the listing, not the page.
    assert [u["url"] for u in await ctrl.web_page_urls(PHONE)] == ["about:blank"] * 2


async def test_a_page_whose_session_never_started_is_not_asked_again(monkeypatch):
    """The probe gives up on a page while its session is still starting. A
    second session on that page would not answer either, so the page now
    answers None at once, rather than through a session never enabled."""
    class SlowToStart(FakeService):
        async def inspector_session(self, app, page):
            session = await super().inspector_session(app, page)

            async def never():
                await asyncio.sleep(60)
            session.runtime_enable = never
            return session

    monkeypatch.setattr(phone_webinspector, "VISIBLE_S", 0.05)
    _patch(monkeypatch, SlowToStart([_entry()]))
    async with PhoneWebInspector(USB) as inspector:
        assert await inspector.pages("PID:748") == []
        assert await asyncio.wait_for(inspector.page_contents("PID:748", 1), 1) is None


# -- one connection per phone, kept between calls (#718) --------------------------------


async def _one_call(page_id=1):
    async with PhoneWebInspector(USB) as inspector:
        return await inspector.page_contents("PID:748", page_id)


async def test_the_next_call_reuses_the_connection_and_lists_afresh(monkeypatch):
    """The phone admits a new connection only ~10 s after the last began:
    measured, a second took 7.15 s, where a kept one served the next call's
    session in 0.02 s."""
    service = FakeService([_entry()])
    _patch(monkeypatch, service)
    await _one_call()
    service.entries = [_entry(page_id=7)]                   # a navigation since
    assert await _one_call(7) is not None
    assert service.connects == 1
    # A fresh session per call, each released: a page left held never
    # answered the next call's session on the same connection.
    assert service.torn_down == [("S1", "PID:748", 1), ("S2", "PID:748", 7)]
    assert not service.closed


async def test_a_call_that_failed_does_not_keep_its_connection(monkeypatch):
    service = FakeService([_entry()])
    _patch(monkeypatch, service)
    with pytest.raises(RuntimeError):
        async with PhoneWebInspector(USB) as inspector:
            await inspector.page_contents("PID:748", 1)
            raise RuntimeError("the caller's own failure")
    assert service.closed
    assert service.torn_down == [("S1", "PID:748", 1)]       # released all the same
    assert await phone_webinspector.POOL.take(USB) is None


async def test_a_release_that_did_not_go_through_closes_the_connection(monkeypatch):
    class Stuck(FakeService):
        async def teardown_inspector_socket(self, *args):
            raise OSError("broken pipe")

    service = Stuck([_entry()])
    _patch(monkeypatch, service)
    await _one_call()
    assert service.closed
    assert await phone_webinspector.POOL.take(USB) is None


async def test_a_session_whose_setup_was_given_up_on_closes_the_connection(monkeypatch):
    """The visibility probe gives a page VISIBLE_S and carries on. If that was
    the session's own setup, the phone may hold the page with nothing here to
    release it, so the connection must not be handed to the next call."""
    class SlowSetup(FakeService):
        async def inspector_session(self, app, page):
            await asyncio.sleep(5)

    monkeypatch.setattr(phone_webinspector, "VISIBLE_S", 0.05)
    service = SlowSetup([_entry()])
    _patch(monkeypatch, service)
    async with PhoneWebInspector(USB) as inspector:
        assert await inspector.pages("PID:748") == []           # not on screen
    assert service.closed


async def test_a_dropped_connection_is_replaced(monkeypatch):
    first = FakeService([_entry()])
    _patch(monkeypatch, first)
    await _one_call()
    first.is_connected = False                               # a cable pull
    second = FakeService([_entry()])
    _patch(monkeypatch, second)
    await _one_call()
    assert first.closed
    assert second.connects == 1


async def test_an_idle_connection_is_closed(monkeypatch):
    monkeypatch.setattr(phone_webinspector, "IDLE_S", 0.05)
    service = FakeService([_entry()])
    _patch(monkeypatch, service)
    await _one_call()
    assert not service.closed
    await asyncio.sleep(0.2)
    assert service.closed
    assert await phone_webinspector.POOL.take(USB) is None


async def test_a_call_while_the_phone_is_busy_gets_a_connection_of_its_own(monkeypatch):
    """Two at once on one connection could each open a session on the same
    page, and the second of those never answers. Waiting its turn instead
    spent the second call's budget behind a long script and then reported
    the phone as not answering (review on #718), so it connects alone, as
    every call did before, and that connection is not kept."""
    import pymobiledevice3.lockdown as lockdown
    import pymobiledevice3.services.webinspector as webinspector

    services = []

    class Slow(FakeService):
        async def get_open_application_pages(self, timeout):
            await asyncio.sleep(0.05)
            return self.entries

    def make(lockdown):
        services.append(_stamp(Slow([_entry()])))
        return services[-1]

    async def connect(**kw):
        return "lockdown"
    monkeypatch.setattr(lockdown, "create_using_usbmux", connect)
    monkeypatch.setattr(webinspector, "WebinspectorService", make)
    await asyncio.gather(_one_call(), _one_call())
    assert len(services) == 2
    assert sorted(s.closed for s in services) == [False, True]   # one kept, one closed
    kept = await phone_webinspector.POOL.take(USB)
    assert kept is not None and not kept.closed


async def test_a_call_inside_the_idle_window_restarts_it(monkeypatch):
    monkeypatch.setattr(phone_webinspector, "IDLE_S", 0.6)
    service = FakeService([_entry()])
    _patch(monkeypatch, service)
    await _one_call()
    await asyncio.sleep(0.4)
    await _one_call()                                    # in use: the clock restarts
    await asyncio.sleep(0.4)                             # 0.8 s after the first keep
    assert not service.closed
    await asyncio.sleep(0.6)
    assert service.closed


async def test_a_stale_expiry_does_not_close_the_connection_kept_since(monkeypatch):
    """The timer fired, then a call took the connection and put the same
    object back before the expiry ran (review on #718)."""
    service = FakeService([_entry()])
    _patch(monkeypatch, service)
    pool = phone_webinspector.POOL
    await _one_call()
    stale = pool._keeps[USB]
    await _one_call()                                    # taken and kept again
    await pool._expire(USB, stale)
    assert not service.closed
    await pool._expire(USB, pool._keeps[USB])            # the current keep does
    assert service.closed


async def test_a_cancel_during_the_close_still_hands_the_phone_on(monkeypatch):
    """The lock was released after the close, so a cancel inside the close
    left the phone's turn held for good, and the connection open (review on
    #718)."""
    class SlowClose(FakeService):
        async def close(self):
            await asyncio.sleep(0.2)
            self.closed = True

    service = SlowClose([_entry()])
    _patch(monkeypatch, service)

    async def failing_call():
        async with PhoneWebInspector(USB) as inspector:
            await inspector.page_contents("PID:748", 1)
            raise RuntimeError("fails, so the connection is closed")
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(failing_call(), 0.1)        # cancelled mid-close
    assert not phone_webinspector.POOL.lock(USB).locked()
    await asyncio.sleep(0.3)
    assert service.closed                                # the shielded close finished


async def test_a_cancel_mid_release_still_ends_every_receive_loop(monkeypatch):
    """pymobiledevice3's receive loop polls every 1 ms and never ends by
    itself; cancelled per page, a cancel during the first release left the
    rest running (review on #718)."""
    loops = []

    class Looping(FakeService):
        async def inspector_session(self, app, page):
            session = await super().inspector_session(app, page)
            session._receive_task = asyncio.ensure_future(asyncio.sleep(60))
            loops.append(session._receive_task)
            return session

        async def teardown_inspector_socket(self, *args):
            await asyncio.sleep(1)

    service = Looping([_entry(), _entry(page_id=2)])
    _patch(monkeypatch, service)

    async def call():
        async with PhoneWebInspector(USB) as inspector:
            await inspector.page_contents("PID:748", 1)
            await inspector.page_contents("PID:748", 2)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(call(), 0.2)
    await asyncio.sleep(0)
    assert len(loops) == 2 and all(t.cancelled() for t in loops)


async def test_a_kept_connection_whose_reader_died_is_not_used(monkeypatch):
    """pymobiledevice3 marks only a lost connection; a receive task that died
    of anything else leaves is_connected True while nothing is read."""
    first = FakeService([_entry()])
    _patch(monkeypatch, first)
    await _one_call()
    dead = asyncio.get_running_loop().create_future()
    dead.set_result(None)
    first._recv_task = dead
    second = FakeService([_entry()])
    _patch(monkeypatch, second)
    await _one_call()
    assert first.closed and second.connects == 1


async def test_a_kept_connection_that_fails_its_listing_is_replaced(monkeypatch):
    class Broken(FakeService):
        async def get_open_application_pages(self, timeout):
            if getattr(self, "broken", False):
                raise OSError("connection reset")
            return self.entries

    first = Broken([_entry()])
    _patch(monkeypatch, first)
    await _one_call()
    first.broken = True
    second = FakeService([_entry()])
    _patch(monkeypatch, second)
    assert await _one_call() is not None                 # not "could not reach"
    assert first.closed and second.connects == 1


async def test_a_kept_connection_lists_afresh_not_from_its_cache(monkeypatch):
    """pymobiledevice3 lists from its cache, which the phone's reply refills.
    A reply that does not come in time left the last call's pages there; on
    a new connection it would have left none."""
    class Cached(FakeService):
        async def get_open_application_pages(self, timeout):
            if not self.application_pages:                # the reply refills it
                self.application_pages["PID:748"] = list(self.entries)
            return [e for pages in self.application_pages.values() for e in pages]

    service = Cached([_entry()])
    _patch(monkeypatch, service)
    await _one_call()
    service.entries = [_entry(page_id=7)]                 # navigated since
    assert await _one_call(7) is not None                 # page 7, not the cached 1


async def test_a_kept_connection_that_lists_nothing_is_replaced(monkeypatch):
    """One sending into silence lists nothing, call after call; only a new
    connection's handshake proves the phone is answering (review on #718)."""
    first = FakeService([_entry()])
    _patch(monkeypatch, first)
    await _one_call()
    first.entries = []                                    # nothing comes back
    second = FakeService([])
    _patch(monkeypatch, second)
    async with PhoneWebInspector(USB) as inspector:
        assert await inspector.connected_applications() == []
    assert first.closed and second.connects == 1


async def test_the_server_stopping_closes_the_kept_connection(monkeypatch):
    service = FakeService([_entry()])
    _patch(monkeypatch, service)
    await _one_call()
    await phone_webinspector.POOL.close_all()
    assert service.closed
