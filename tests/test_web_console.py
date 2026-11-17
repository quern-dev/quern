"""get_web_console: a web page's console, read on demand (#714).

The messages are shaped as an iOS 27.2 simulator sent them.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from server.device.controller import DeviceController
from server.device.web import console
from server.device.web.console import Collector

LOG = {"source": "console-api", "level": "log", "text": "two args", "type": "log",
       "url": "undefined", "line": 1, "timestamp": 1794905894.133524,
       "parameters": [{"type": "string", "value": "two args"},
                      {"type": "number", "value": 4, "description": "4"},
                      {"type": "object", "objectId": "x", "className": "Object",
                       "description": "Object"}]}
UNCAUGHT = {"source": "javascript", "level": "error", "text": "Error: boom 714",
            "type": "log", "url": "http://127.0.0.1:8716/a.html", "line": 3, "column": 9,
            "timestamp": 1794905894.303049}


# -- reading a message --------------------------------------------------------------


def test_every_argument_is_in_the_text_not_only_the_first():
    """`console.log('two args', 4, {k: 1})` carries "two args" alone in text."""
    entry = console.entry(dict(LOG, repeat=1))
    assert entry["text"] == "two args 4 Object"
    assert entry["level"] == "log" and entry["source"] == "console-api"
    assert entry["timestamp"] == "2026-11-17T08:58:14.133524+00:00"
    assert "url" not in entry                       # WebKit says "undefined"


def test_an_uncaught_error_keeps_where_it_was_thrown():
    entry = console.entry(dict(UNCAUGHT, repeat=1))
    assert entry["text"] == "Error: boom 714" and entry["source"] == "javascript"
    assert (entry["url"], entry["line"], entry["column"]) == (
        "http://127.0.0.1:8716/a.html", 3, 9)


def test_a_repeat_is_counted_on_the_message_it_repeats():
    collector = Collector()
    collector.on_event({"method": "Console.messageAdded", "params": {"message": LOG}})
    collector.on_event({"method": "Console.messageRepeatCountUpdated", "params": {"count": 3}})
    collector.on_event({"method": "Runtime.executionContextCreated", "params": {}})
    assert len(collector.messages) == 1
    assert console.entry(collector.messages[0])["repeat"] == 3


# -- the simulator: the replay arrives before the enable's answer -----------------------


def _from_page(page_id, inner):
    from server.device.web import webinspector as wi

    return {"__selector": wi._SEL_APPLICATION_SENT_DATA, "__argument": {
        "WIRPageIdentifierKey": page_id, "WIRMessageDataKey": json.dumps(
            {"method": "Target.dispatchMessageFromTarget",
             "params": {"message": json.dumps(inner)}}).encode()}}


async def test_a_simulator_page_s_history_is_what_came_before_the_enable_was_answered():
    """Measured: the replayed messages, then the enable's reply. Before this,
    every event that arrived while a reply was awaited was dropped."""
    from server.device.web import webinspector as wi

    created = {"__selector": wi._SEL_APPLICATION_SENT_DATA, "__argument": {
        "WIRPageIdentifierKey": 21, "WIRMessageDataKey": json.dumps(
            {"method": "Target.targetCreated",
             "params": {"targetInfo": {"targetId": "page-21"}}}).encode()}}

    class Page:
        def __init__(self):
            self.sent, self.queue = [], [created]

        async def send_plist(self, message):
            data = message["__argument"].get("WIRSocketDataKey")
            if not data:
                return
            inner = json.loads(json.loads(bytes(data).decode())["params"]["message"])
            self.sent.append(inner["method"])
            if inner["method"] == "Console.enable":
                self.queue += [
                    _from_page(21, {"method": "Console.messageAdded", "params": {"message": LOG}}),
                    _from_page(21, {"method": "Console.messageAdded",
                                    "params": {"message": UNCAUGHT}}),
                    _from_page(21, {"id": inner["id"], "result": {}}),
                    # Nothing after the answer is part of the history.
                    _from_page(21, {"method": "Console.messageAdded",
                                    "params": {"message": dict(LOG, text="later")}})]
            else:
                self.queue.append(_from_page(21, {"id": inner["id"], "result": {}}))

        async def recv_plist(self):
            if self.queue:
                return self.queue.pop(0)
            await asyncio.sleep(60)

    inspector = wi.SimulatorWebInspector(timeout=2.0)
    page = Page()
    inspector._service = page
    messages = await inspector.console_history("PID:9", 21)
    assert [m["text"] for m in messages] == ["two args", "Error: boom 714"]
    assert page.sent == ["Console.enable", "Console.disable"]      # nothing left streaming


async def test_a_simulator_page_that_does_not_answer_has_no_history_not_an_empty_one():
    """None, not []: an empty list says the page logged nothing."""
    from server.device.web import webinspector as wi

    class Silent:
        async def send_plist(self, _message):
            pass

        async def recv_plist(self):
            await asyncio.sleep(60)

    inspector = wi.SimulatorWebInspector(timeout=10.0)
    inspector._service = Silent()
    assert await inspector.console_history("PID:9", 21, timeout=0.1) is None


# -- the phone: pymobiledevice3's session hands the events to handlers ------------------


async def test_a_phone_page_s_history_is_collected_and_its_handlers_put_back(monkeypatch):
    from server.device.web.phone_webinspector import PhoneWebInspector

    printed = []

    class Session:
        def __init__(self):
            self.response_methods = {"Console.messageAdded": printed.append}
            self.sent = []

        async def send_and_receive(self, message):
            self.sent.append(message["method"])
            if message["method"] == "Console.enable":
                # The receive loop dispatches the replay before the answer.
                self.response_methods["Console.messageAdded"](
                    {"method": "Console.messageAdded", "params": {"message": UNCAUGHT}})
            return {"id": 1, "result": {}}

    session = Session()
    inspector = PhoneWebInspector("USB")

    async def mine(_app_id, _page_id):
        return session
    monkeypatch.setattr(inspector, "_session", mine)
    messages = await inspector.console_history("PID:9", 2)
    assert [m["text"] for m in messages] == ["Error: boom 714"]
    assert session.sent == ["Console.enable", "Console.disable"]
    assert session.response_methods == {"Console.messageAdded": printed.append}
    assert printed == []                       # collected, not printed to quern's log


# -- the controller ---------------------------------------------------------------------


class Inspector:
    def __init__(self, messages):
        self.messages = messages

    async def connected_applications(self):
        return [{"application_id": "PID:9", "bundle_id": "com.apple.mobilesafari"}]

    async def pages(self, app_id):
        return [{"page_id": 21, "url": "a", "title": "A"}]

    async def on_screen(self, app_id, page_id):
        return True

    async def console_history(self, app_id, page_id, timeout=None):
        return self.messages


def _simulator(monkeypatch, messages) -> DeviceController:
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
        return Inspector(messages)
    ctrl._booted_simulator_count = one
    ctrl._connected_web_inspector = connected
    monkeypatch.setattr(wi, "simulator_udid_for_application", mine)
    return ctrl


async def test_levels_keep_only_those_and_read_says_how_many_there_were(monkeypatch):
    ctrl = _simulator(monkeypatch, [dict(LOG, repeat=1), dict(UNCAUGHT, repeat=1)])
    answer = await ctrl.get_web_console(udid="SIM", levels=["error"])
    assert [m["text"] for m in answer["messages"]] == ["Error: boom 714"]
    assert (answer["count"], answer["read"], answer["page_id"]) == (1, 2, 21)
    assert "note" not in answer


async def test_a_full_window_says_the_oldest_may_be_gone(monkeypatch):
    messages = [dict(LOG, repeat=1) for _ in range(console.NEAR_WINDOW)]
    answer = await _simulator(monkeypatch, messages).get_web_console(udid="SIM")
    assert "older ones may be gone" in answer["note"]
    fewer = await _simulator(monkeypatch, messages[1:]).get_web_console(udid="SIM")
    assert "note" not in fewer


async def test_a_page_that_does_not_answer_is_an_error_not_an_empty_console(monkeypatch):
    from server.models import DeviceError

    ctrl = _simulator(monkeypatch, None)
    with pytest.raises(DeviceError, match="did not answer"):
        await ctrl.get_web_console(udid="SIM")


async def test_the_route_logs_a_read(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from server.api import device_ui
    from server.models import WebConsoleRequest
    from tests.test_action_log import _capture

    controller = MagicMock()
    controller.resolve_udid = AsyncMock(return_value="SIM")
    controller._is_physical = MagicMock(return_value=False)
    controller.get_web_console = AsyncMock(return_value={"count": 1, "read": 5, "messages": []})
    monkeypatch.setattr(device_ui, "_get_controller", lambda request: controller)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    [entry] = await _capture(lambda: device_ui.get_web_console(
        request=request, body=WebConsoleRequest(levels=["error"])))
    assert entry.action == "get_web_console" and entry.category == "device.read"
    assert "1 of 5 messages" in entry.message
    assert controller.get_web_console.await_args.kwargs["levels"] == ["error"]
