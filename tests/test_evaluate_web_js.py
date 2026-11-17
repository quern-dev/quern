"""evaluate_web_js: a caller's script, run in a web page (#713).

The replies below are WebKit's own, as an iOS 27.2 simulator gave them, so
what is tested is how quern reads them, not a guess at their shape.
"""

from __future__ import annotations

import asyncio

import pytest

from server.device.controller import DeviceController
from server.device.controller_ui import _choose_web_page
from server.device.web import scripts
from server.device.web.scripts import NoAnswer, run_script
from server.models import DeviceError, InvalidDeviceRequestError

OBJECT_ID = '{"injectedScriptId":21,"id":1}'


def _reply(result: dict, thrown: bool = False) -> dict:
    return {"result": {"result": result, "wasThrown": thrown}, "id": 3}


class Page:
    """Answers each protocol method from a script of replies, and records
    what it was sent."""

    def __init__(self, **replies):
        self.replies, self.sent = replies, []

    async def __call__(self, method: str, params: dict) -> dict | None:
        self.sent.append((method, params))
        reply = self.replies.get(method)
        return reply(params) if callable(reply) else reply


# -- reading what the script gave ----------------------------------------------------


async def test_a_primitive_is_its_value():
    page = Page(**{"Runtime.evaluate": _reply({"type": "number", "value": 42})})
    assert await run_script(page, "6 * 7") == {"threw": False, "type": "number", "value": 42}
    # The script is run once, as written, and nothing is taken by value there.
    evaluate = [p for m, p in page.sent if m == "Runtime.evaluate"]
    assert evaluate == [{"expression": "6 * 7", "objectGroup": scripts.GROUP}]


async def test_undefined_and_null_say_so():
    for result, expected in (({"type": "undefined"}, "undefined"),
                             ({"type": "object", "subtype": "null", "value": None}, "null")):
        page = Page(**{"Runtime.evaluate": _reply(result)})
        assert await run_script(page, "x") == {"threw": False, "type": expected, "value": None}


async def test_a_throw_is_an_answer_with_its_text():
    page = Page(**{"Runtime.evaluate": _reply(
        {"type": "object", "objectId": OBJECT_ID, "subtype": "error",
         "className": "ReferenceError",
         "description": "ReferenceError: Can't find variable: nope"}, thrown=True)})
    assert await run_script(page, "nope.x") == {
        "threw": True, "exception": "ReferenceError: Can't find variable: nope"}


async def test_an_object_is_fetched_by_value_from_what_the_script_returned():
    """Not by running the script again, which would do what it does twice."""
    page = Page(**{
        "Runtime.evaluate": _reply({"type": "object", "objectId": OBJECT_ID,
                                    "className": "Object", "description": "Object"}),
        "Runtime.callFunctionOn": _reply({"type": "object", "value": {"a": [1, 2]}})})
    answer = await run_script(page, "({a: [1, 2]})")
    assert answer["value"] == {"a": [1, 2]} and answer["type"] == "object"
    assert [m for m, _ in page.sent].count("Runtime.evaluate") == 1
    call = next(p for m, p in page.sent if m == "Runtime.callFunctionOn")
    assert call["objectId"] == OBJECT_ID and call["returnByValue"] is True


async def test_a_node_is_described_not_given_as_an_empty_object():
    """By value, WebKit answers {} for a node -- an object that reads as
    really there and empty."""
    page = Page(**{"Runtime.evaluate": _reply(
        {"type": "object", "objectId": OBJECT_ID, "subtype": "node",
         "className": "HTMLInputElement", "description": '<input id="q">'})})
    answer = await run_script(page, "document.getElementById('q')")
    assert answer == {"threw": False, "type": "node", "value": None,
                      "class": "HTMLInputElement", "description": '<input id="q">'}
    assert "Runtime.callFunctionOn" not in [m for m, _ in page.sent]


async def test_a_promise_is_awaited_and_its_result_read_the_same_way():
    page = Page(**{
        "Runtime.evaluate": _reply({"type": "object", "objectId": OBJECT_ID,
                                    "className": "Promise", "description": "Promise"}),
        "Runtime.awaitPromise": _reply({"type": "number", "value": 42})})
    assert await run_script(page, "Promise.resolve(42)") == {
        "threw": False, "type": "number", "value": 42, "awaited": True}


async def test_a_rejection_is_a_throw():
    page = Page(**{
        "Runtime.evaluate": _reply({"type": "object", "objectId": OBJECT_ID,
                                    "className": "Promise", "description": "Promise"}),
        "Runtime.awaitPromise": _reply({"type": "object", "subtype": "error",
                                        "className": "TypeError",
                                        "description": "TypeError: nah"}, thrown=True)})
    assert await run_script(page, "Promise.reject(new TypeError('nah'))") == {
        "threw": True, "exception": "TypeError: nah", "awaited": True}


async def test_an_object_that_cannot_be_serialised_says_so():
    page = Page(**{
        "Runtime.evaluate": _reply({"type": "object", "objectId": OBJECT_ID,
                                    "className": "Object", "description": "Object"}),
        "Runtime.callFunctionOn": {"error": {"code": -32000, "message": (
            "Object has too long reference chain (must not be longer than 1000)")}}})
    answer = await run_script(page, "cyclic")
    assert answer["value"] is None and "not serialisable" in answer["note"]


async def test_a_page_that_stops_answering_is_no_answer_not_a_value():
    for replies in ({"Runtime.evaluate": None},
                    {"Runtime.evaluate": _reply({"type": "object", "objectId": OBJECT_ID,
                                                 "className": "Promise"}),
                     "Runtime.awaitPromise": None}):
        with pytest.raises(NoAnswer):
            await run_script(Page(**replies), "x")


async def test_what_the_script_returned_is_released():
    page = Page(**{"Runtime.evaluate": _reply({"type": "number", "value": 1})})
    await run_script(page, "1")
    assert page.sent[-1] == ("Runtime.releaseObjectGroup", {"objectGroup": scripts.GROUP})


# -- which page --------------------------------------------------------------------


SAFARI = {"application_id": "PID:9", "bundle_id": "com.apple.mobilesafari"}
APP = {"application_id": "PID:7", "bundle_id": "com.example.app"}
PAGES = {"PID:9": [{"page_id": 1, "url": "b"}, {"page_id": 21, "url": "a"}],
         "PID:7": [{"page_id": 1, "url": "app"}]}


async def _pages(app_id):
    return PAGES[app_id]


def _visible(*shown):
    async def on_screen(app_id, page_id):
        return (app_id, page_id) in shown
    return on_screen


async def test_without_page_id_the_one_page_on_screen_is_chosen():
    app, page = await _choose_web_page([SAFARI], _pages, _visible(("PID:9", 21)), None, None)
    assert (app, page["page_id"]) == (SAFARI, 21)


async def test_none_or_several_on_screen_is_refused_with_the_listing():
    with pytest.raises(InvalidDeviceRequestError, match="no web page is on screen.*page_id 21"):
        await _choose_web_page([SAFARI], _pages, _visible(), None, None)
    with pytest.raises(InvalidDeviceRequestError, match="several web pages.*pass page_id"):
        await _choose_web_page([SAFARI, APP], _pages,
                               _visible(("PID:9", 21), ("PID:7", 1)), None, None)


async def test_a_page_id_is_taken_as_given_on_screen_or_not():
    """A background tab answers on a simulator, so it may be asked."""
    app, page = await _choose_web_page([SAFARI], _pages, _visible(), None, 1)
    assert page["url"] == "b"


async def test_a_page_id_two_apps_list_needs_the_bundle():
    with pytest.raises(InvalidDeviceRequestError, match="pass bundle_id"):
        await _choose_web_page([SAFARI, APP], _pages, _visible(), None, 1)
    app, page = await _choose_web_page([SAFARI, APP], _pages, _visible(), "com.example.app", 1)
    assert page["url"] == "app"


async def test_an_unknown_page_or_bundle_is_refused():
    with pytest.raises(InvalidDeviceRequestError, match="no page 999 is listed"):
        await _choose_web_page([SAFARI], _pages, _visible(), None, 999)
    with pytest.raises(InvalidDeviceRequestError, match="not connected to the Web Inspector"):
        await _choose_web_page([SAFARI], _pages, _visible(), "com.nope", None)


# -- the controller --------------------------------------------------------------------


class Inspector:
    def __init__(self, reply):
        self.reply, self.sent = reply, []

    async def connected_applications(self):
        return [SAFARI]

    async def pages(self, app_id):
        return [{"page_id": 21, "url": "a", "title": "A"}]

    async def on_screen(self, app_id, page_id):
        return True

    async def send_command(self, app_id, page_id, method, params, timeout=None):
        self.sent.append((method, timeout))
        return self.reply if method == "Runtime.evaluate" else {"result": {}}


def _simulator(monkeypatch, inspector) -> DeviceController:
    import server.device.web.webinspector as wi

    ctrl = DeviceController()
    ctrl._active_udid = "SIM"
    ctrl._is_android = lambda _u: False
    ctrl._is_physical = lambda _u: False

    async def one():
        return 1

    async def mine(_app_id):
        return "SIM"

    async def connected():
        return inspector
    ctrl._booted_simulator_count = one
    ctrl._connected_web_inspector = connected
    monkeypatch.setattr(wi, "simulator_udid_for_application", mine)
    return ctrl


async def test_a_simulator_runs_it_and_says_where(monkeypatch):
    inspector = Inspector(_reply({"type": "string", "value": "A"}))
    ctrl = _simulator(monkeypatch, inspector)
    answer = await ctrl.evaluate_web_js("document.title", udid="SIM", timeout=3)
    assert answer == {"bundle_id": "com.apple.mobilesafari", "page_id": 21, "url": "a",
                      "title": "A", "threw": False, "type": "string", "value": "A",
                      "udid": "SIM"}
    # Each step has what is left of the one bound, never more.
    assert all(0 < t <= 3 for _, t in inspector.sent)


async def test_a_page_on_another_simulator_is_not_run_in(monkeypatch):
    import server.device.web.webinspector as wi

    inspector = Inspector(_reply({"type": "number", "value": 1}))
    ctrl = _simulator(monkeypatch, inspector)

    async def elsewhere(_app_id):
        return "OTHER-SIM"
    monkeypatch.setattr(wi, "simulator_udid_for_application", elsewhere)
    with pytest.raises(InvalidDeviceRequestError, match="no web page is on screen"):
        await ctrl.evaluate_web_js("1", udid="SIM")
    assert inspector.sent == []


async def test_a_page_that_does_not_answer_is_an_error_and_drops_the_overlay(monkeypatch):
    inspector = Inspector(None)
    ctrl = _simulator(monkeypatch, inspector)
    ctrl._web_overlay["SIM"] = ([], 0.0)
    with pytest.raises(DeviceError, match="did not answer within 2 s"):
        await ctrl.evaluate_web_js("new Promise(() => {})", udid="SIM", timeout=2)
    # What it changed before it stopped answering may have moved the page.
    assert "SIM" not in ctrl._web_overlay


async def test_android_is_refused_as_unsupported(monkeypatch):
    from server.models import DeviceOperationUnsupportedError

    ctrl = DeviceController()
    ctrl._active_udid = "emulator-5554"
    ctrl._is_android = lambda _u: True
    with pytest.raises(DeviceOperationUnsupportedError, match="Android"):
        await ctrl.evaluate_web_js("1", udid="emulator-5554")


async def test_a_phone_runs_it_through_its_own_inspector(monkeypatch):
    """The phone lists only pages on screen, so its one page is chosen."""
    from server.device.web import phone_webinspector

    sent = []

    class Phone:
        def __init__(self, udid):
            self.udid = udid

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            pass

        async def connected_applications(self):
            return [SAFARI]

        async def pages(self, app_id):
            return [{"page_id": 2, "url": "https://example.com/", "title": "Example"}]

        async def send_command(self, app_id, page_id, method, params):
            sent.append((page_id, method))
            if method == "Runtime.evaluate":
                return _reply({"type": "string", "value": "Example"})
            return {"result": {}}

    monkeypatch.setattr(phone_webinspector, "PhoneWebInspector", Phone)
    ctrl = DeviceController()
    ctrl._active_udid = "PHONE"
    ctrl._is_android = lambda _u: False
    ctrl._is_physical = lambda _u: True

    async def usb(_udid):
        return "00008030-USB"
    ctrl.get_libimobiledevice_udid = usb
    answer = await ctrl.evaluate_web_js("document.title", udid="PHONE")
    assert answer["value"] == "Example" and answer["page_id"] == 2
    assert ("Runtime.evaluate" in [m for _, m in sent])


async def test_a_phone_script_that_hangs_is_given_up_on(monkeypatch):
    from server.device.web import phone_webinspector

    class Hanging:
        def __init__(self, udid):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            pass

        async def connected_applications(self):
            return [SAFARI]

        async def pages(self, app_id):
            return [{"page_id": 2, "url": "u"}]

        async def send_command(self, *a):
            await asyncio.sleep(60)

    monkeypatch.setattr(phone_webinspector, "PhoneWebInspector", Hanging)
    ctrl = DeviceController()
    ctrl._active_udid = "PHONE"
    ctrl._is_android = lambda _u: False
    ctrl._is_physical = lambda _u: True

    async def usb(_udid):
        return None
    ctrl.get_libimobiledevice_udid = usb
    with pytest.raises(DeviceError, match="did not answer within 0.1 s"):
        await ctrl.evaluate_web_js("x", udid="PHONE", timeout=0.1)


# -- the route: logged as an action, with the script ----------------------------------


@pytest.fixture
def route(monkeypatch):
    from server.api import device_ui

    def install(controller):
        monkeypatch.setattr(device_ui, "_get_controller", lambda request: controller)
        from types import SimpleNamespace
        return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    return install


def _controller(evaluate):
    from unittest.mock import AsyncMock, MagicMock

    controller = MagicMock()
    controller.resolve_udid = AsyncMock(return_value="SIM")
    controller._is_physical = MagicMock(return_value=False)
    controller.evaluate_web_js = evaluate
    return controller


async def test_the_action_entry_carries_the_script(route):
    """It acts on the page, so a trace says what was run, not only that
    something was."""
    from unittest.mock import AsyncMock

    from server.api import device_ui
    from server.models import EvaluateWebJsRequest
    from tests.test_action_log import _capture

    request = route(_controller(AsyncMock(return_value={"threw": False, "type": "string"})))
    script = "document.querySelector('#q').value = 'x'"
    [entry] = await _capture(lambda: device_ui.evaluate_web_js(
        request=request, body=EvaluateWebJsRequest(script=script, page_id=21)))
    assert entry.action == "evaluate_web_js" and entry.category == "device.action"
    assert script in entry.message and "page 21" in entry.message
    assert entry.outcome == "ok"


async def test_a_long_script_is_kept_in_part_and_a_failure_still_logged(route):
    from unittest.mock import AsyncMock

    from server.api import device_ui
    from server.models import EvaluateWebJsRequest
    from tests.test_action_log import _capture

    request = route(_controller(AsyncMock(side_effect=DeviceError("did not answer",
                                                                  tool="web-js"))))
    script = "x".join(str(n) for n in range(2000))
    [entry] = await _capture(lambda: device_ui.evaluate_web_js(
        request=request, body=EvaluateWebJsRequest(script=script)))
    assert entry.outcome == "failed"
    assert f"({len(script)} characters)" in entry.message
    assert len(entry.message) < len(script)
