"""No-WDA mode: on an iPhone the accessibility push serves (iOS 27+), quern
does not start WebDriverAgent behind a call that did not ask for it (#780).

Before this, every CoreDevice failure defaulted to "hand it to WDA": a read
the push could not make, input HID could not send. Each implicit launch was
found only after it ran -- one stayed on an iPhone 11 for six hours (#779).
Now there is one rule at the one place a runner is auto-started, and the
sweep below runs the UI tools against a phone whose CoreDevice paths all
fail, so the next implicit path is caught when it is written.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import pathlib
from unittest.mock import AsyncMock

import pytest

from server.device.controller import DeviceController
from server.device.ios import wda as wda_module
from server.device.ios import wda_client
from server.device.ios.wda_client import WdaBackend, ask_for_wda
from server.models import (
    DeviceError,
    DeviceType,
    WaitCondition,
    WdaNotAutoStartedError,
    WdaNotSetUpError,
)

PHONE = "00008030-000C59623A69802E"


def _no_wda_reachable(b: WdaBackend, monkeypatch) -> AsyncMock:
    """No WDA running anywhere, WDA available to start; returns the start spy."""
    b._try_tunneld_connection = AsyncMock(return_value=None)
    b._start_usbmux_forward = AsyncMock(side_effect=DeviceError("no forward", tool="wda"))
    monkeypatch.setattr(wda_client, "_refuse_if_held", lambda udid, what: None)
    from server.tooling import wda_availability
    monkeypatch.setattr(wda_availability, "wda_unavailable_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(wda_module, "start_in_flight", lambda udid: None)
    start = AsyncMock(return_value={"status": "started", "ready": True})
    monkeypatch.setattr(wda_module, "start_driver", start)
    return start


@pytest.fixture
def backend(monkeypatch):
    b = WdaBackend()
    b._device_os_versions[PHONE] = "iOS 27.2.1"
    b.start = _no_wda_reachable(b, monkeypatch)
    return b


class TestTheRule:
    async def test_wda_is_not_started_on_its_own(self, backend):
        with pytest.raises(WdaNotAutoStartedError) as caught:
            await backend._get_base_url(PHONE)
        backend.start.assert_not_called()
        # Discoverable: what mode this is, and both ways out of it.
        message = str(caught.value)
        assert "no-WDA mode" in message
        assert "start_driver" in message and "wda_fallback" in message

    async def test_a_call_that_asked_for_wda_starts_it(self, backend):
        async def asked():
            ask_for_wda(True)
            with contextlib.suppress(DeviceError):
                await backend._get_base_url(PHONE)

        await asyncio.create_task(asked())
        backend.start.assert_awaited_once()

    async def test_the_ask_ends_with_its_request(self, backend):
        """Each request runs in a context of its own: one that asked does not
        open the door for the next."""
        async def asked():
            ask_for_wda(True)

        await asyncio.create_task(asked())
        with pytest.raises(WdaNotAutoStartedError):
            await backend._get_base_url(PHONE)

    async def test_start_driver_opens_it_and_stop_driver_closes_it(self, backend):
        backend.note_started(PHONE)
        with contextlib.suppress(DeviceError):
            await backend._get_base_url(PHONE)
        assert backend.start.await_count == 1

        backend.note_stopped(PHONE)
        with pytest.raises(WdaNotSetUpError, match="stopped with stop_driver"):
            await backend._get_base_url(PHONE)

    async def test_the_off_switch_restores_the_fallback(self, backend, wda_fallback_on):
        with contextlib.suppress(DeviceError):
            await backend._get_base_url(PHONE)
        backend.start.assert_awaited_once()

    @pytest.mark.parametrize("os_version", ["iOS 18.5", "iOS 26.7"])
    async def test_a_phone_before_27_is_unchanged(self, backend, os_version):
        """WDA is the only reader there."""
        backend._device_os_versions[PHONE] = os_version
        with contextlib.suppress(DeviceError):
            await backend._get_base_url(PHONE)
        backend.start.assert_awaited_once()

    async def test_the_wda_reader_setting_is_unchanged(self, backend, monkeypatch):
        monkeypatch.setattr("server.config.get_device_ui_reader", lambda: "wda")
        with contextlib.suppress(DeviceError):
            await backend._get_base_url(PHONE)
        backend.start.assert_awaited_once()

    async def test_a_wda_already_running_is_used(self, backend):
        backend._try_tunneld_connection = AsyncMock(return_value="http://[fd00::1]:8100")
        assert await backend._get_base_url(PHONE) == "http://[fd00::1]:8100"
        backend.start.assert_not_called()

    async def test_a_mac_without_wda_says_that_first(self, backend, monkeypatch):
        """start_driver could not help there, so naming it would mislead."""
        from server.tooling import wda_availability
        monkeypatch.setattr(wda_availability, "wda_unavailable_reason",
                            AsyncMock(return_value="none is built"))
        with pytest.raises(WdaNotSetUpError, match="none is built") as caught:
            await backend._get_base_url(PHONE)
        assert not isinstance(caught.value, WdaNotAutoStartedError)

    def test_it_is_a_refusal_not_a_fault(self):
        from server.api.device import _handle_device_error

        assert _handle_device_error(WdaNotAutoStartedError("x", tool="wda")).status_code == 400


# MARK: - the sweep: every UI tool, every CoreDevice path failing


def _failing_phone(monkeypatch) -> tuple[DeviceController, AsyncMock]:
    """An iOS 27 phone on the push whose reads and input all fail, with the
    real WDA client behind them and its start spied on."""
    ctrl = DeviceController()
    ctrl._active_udid = PHONE
    ctrl._device_type_cache[PHONE] = DeviceType.DEVICE
    ctrl.wda_client._device_os_versions[PHONE] = "iOS 27.2.1"
    monkeypatch.setattr("server.device.controller_ui.get_device_ui_reader", lambda: "coredevice")
    monkeypatch.setattr("server.config.get_device_ui_reader", lambda: "coredevice")
    # The push read fails as an accessibility daemon that does not answer,
    # and HID cannot find the tunnel's address -- both went to WDA before.
    failure = DeviceError("the accessibility daemon did not answer", tool="pymobiledevice3")
    ctrl.coredevice_ax.describe_all = AsyncMock(side_effect=failure)
    ctrl.coredevice_ax.describe_point = AsyncMock(side_effect=failure)
    monkeypatch.setattr("server.device.performance._device_address",
                        AsyncMock(side_effect=OSError(61, "tunneld refused")))
    ctrl._mark_on_screen = AsyncMock()
    start = _no_wda_reachable(ctrl.wda_client, monkeypatch)
    # A start that answers ready, then no WDA to talk to: enough to see it
    # was started, without a WDA to emulate.
    start.return_value = {"status": "started", "ready": False}
    return ctrl, start


#: Each tool, called as its route calls it.
SWEEP = {
    "get_screen_summary": lambda c: c.get_screen_summary(udid=PHONE, whole_screen=True),
    "get_ui_tree": lambda c: c.get_ui_elements(udid=PHONE, use_cache=False, whole_screen=True),
    "get_ui_tree(children_of)": lambda c: c.get_ui_elements_children_of("General", udid=PHONE),
    "get_element": lambda c: c.get_element(label="Wi-Fi", udid=PHONE),
    "wait_for_element": lambda c: c.wait_for_element(
        WaitCondition.EXISTS, label="Wi-Fi", udid=PHONE, timeout=0.2),
    "tap_element": lambda c: c.tap_element(label="Wi-Fi", udid=PHONE),
    "tap": lambda c: c.tap(100, 200, udid=PHONE),
    "swipe": lambda c: c.swipe(100, 600, 100, 200, udid=PHONE),
    "press_button": lambda c: c.press_button("home", udid=PHONE),
    "type_text": lambda c: c.type_text("hello", udid=PHONE),
    "clear_text": lambda c: c.clear_text(udid=PHONE),
    "scroll_to_element": lambda c: c.scroll_to_element(label="Privacy", udid=PHONE, max_swipes=1),
}


@pytest.mark.parametrize("tool", sorted(SWEEP))
async def test_no_ui_tool_starts_wda_on_its_own(monkeypatch, tool):
    ctrl, start = _failing_phone(monkeypatch)
    with contextlib.suppress(DeviceError):
        await asyncio.wait_for(SWEEP[tool](ctrl), 10)
    assert start.await_count == 0, f"{tool} started WDA on a phone in no-WDA mode"


@pytest.mark.parametrize("tool", sorted(SWEEP))
async def test_the_sweep_can_fail(monkeypatch, tool, wda_fallback_on):
    """The control: with the fallback on, the same call does reach WDA. Without
    this, 'never started' could mean the sweep never got that far."""
    ctrl, start = _failing_phone(monkeypatch)
    with contextlib.suppress(DeviceError):
        await asyncio.wait_for(SWEEP[tool](ctrl), 10)
    assert start.await_count >= 1, f"{tool} never reached WDA, so the sweep says nothing"


def test_every_route_that_takes_snapshot_depth_asks_for_wda():
    """The ask is made at the route, where the caller's own value is: a new
    route that takes snapshot_depth and forgets it would be refused WDA."""
    missing = []
    for path in sorted(pathlib.Path("server/api").glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            src = ast.unparse(node)
            passes = any(f"snapshot_depth={v}" in src
                         for v in ("snapshot_depth", "body.snapshot_depth"))
            if passes and "ask_for_wda(" not in src:
                missing.append(f"{path.name}:{node.name}")
    assert missing == []


@pytest.mark.parametrize("tool, says", [
    ("get_screen_summary", "the accessibility daemon did not answer"),
    ("tap", "was not sent over CoreDevice"),
])
async def test_the_refusal_says_what_failed_first(monkeypatch, tool, says):
    """Why CoreDevice could not do it comes first, then no-WDA mode and the way
    out: the push's own failure is what to act on."""
    ctrl, _start = _failing_phone(monkeypatch)
    with pytest.raises(WdaNotAutoStartedError) as caught:
        await asyncio.wait_for(SWEEP[tool](ctrl), 10)
    message = str(caught.value)
    assert says in message and "no-WDA mode" in message
    assert message.index(says) < message.index("no-WDA mode")



@pytest.mark.parametrize("tool", sorted(SWEEP))
async def test_with_a_working_push_no_ui_tool_needs_wda(monkeypatch, tool):
    """children_of read WDA even with the push working, and the MCP's own
    instructions recommend it. Every tool is the push's now, so none is
    refused for want of WDA."""
    from server.device.ios.coredevice_ax import parse_push
    from tests.test_coredevice_ax import SETTINGS

    ctrl, start = _failing_phone(monkeypatch)
    ctrl.coredevice_ax.describe_all = AsyncMock(return_value=parse_push(SETTINGS)[1])
    ctrl.coredevice_ax.describe_point = AsyncMock(return_value=None)
    for action in ("tap", "swipe", "type_text", "select_all_and_delete", "press_button",
                   "perform_gesture"):
        setattr(ctrl.coredevice_input, action, AsyncMock(return_value="coredevice"))
    try:
        await asyncio.wait_for(SWEEP[tool](ctrl), 10)
    except WdaNotAutoStartedError as e:
        pytest.fail(f"{tool} needed WDA with the push working: {e}")
    except DeviceError:
        pass  # a miss on this screen, say: not WDA's
    start.assert_not_called()


class TestFromThePush:
    @staticmethod
    def _el(label, x, y, w, h, identifier=None):
        from server.models import UIElement

        return UIElement(type="Button", label=label, identifier=identifier,
                         frame={"x": x, "y": y, "width": w, "height": h})

    def test_children_are_the_elements_inside_the_parents_frame(self):
        from server.device.controller_ui import children_by_frame

        cell = self._el("General", 0, 100, 400, 60)
        inside = self._el("Chevron", 360, 115, 20, 30)
        overlapping = self._el("Below", 0, 150, 400, 60)
        elsewhere = self._el("Privacy", 0, 300, 400, 60)
        got = children_by_frame([cell, inside, overlapping, elsewhere], "General")
        assert got == [inside]

    def test_the_parent_is_found_by_identifier_first(self):
        from server.device.controller_ui import children_by_frame

        a = self._el("General", 0, 0, 100, 100, identifier="other")
        b = self._el("Something", 0, 200, 100, 100, identifier="General")
        child = self._el("x", 10, 210, 10, 10)
        assert children_by_frame([a, b, child], "General") == [child]

    def test_no_parent_is_no_children(self):
        from server.device.controller_ui import children_by_frame

        assert children_by_frame([self._el("General", 0, 0, 10, 10)], "Missing") == []



@pytest.mark.parametrize("value, on", [(True, True), ("true", False), (1, False), (None, False)])
def test_only_a_literal_true_turns_the_fallback_on(monkeypatch, value, on):
    """As auto_install_cert: a typo means off, never consent."""
    from server import config

    monkeypatch.setattr(config, "read_user_config",
                        lambda: {} if value is None else {"wda_fallback": value})
    assert config.get_wda_fallback() is on


class TestAfterReview:
    async def test_a_runner_already_there_is_not_refused(self, backend, monkeypatch):
        """Only a start is refused. A runner outlives the server, and
        start_driver's mark does not (CodeRabbit on #782)."""
        monkeypatch.setattr(wda_client, "_runner_alive", lambda udid: True)
        assert backend.no_auto_start_reason(PHONE) is None
        assert not backend.in_no_wda_mode(PHONE)

    def test_a_push_crash_in_no_wda_mode_keeps_the_phone_on_the_push(self, monkeypatch):
        """The crash latch sends reads to WDA; with none to send them to, every
        later read was refused, even once the push had recovered."""
        ctrl, _start = _failing_phone(monkeypatch)
        from server.tooling import wda_availability
        monkeypatch.setattr(wda_availability, "known_reason", lambda: None)
        ctrl._record_push_crash(PHONE, "Probe")
        assert ctrl._device_tree_reader(PHONE) is ctrl.coredevice_ax

    def test_with_the_fallback_on_the_crash_still_moves_it(self, monkeypatch, wda_fallback_on):
        ctrl, _start = _failing_phone(monkeypatch)
        from server.tooling import wda_availability
        monkeypatch.setattr(wda_availability, "known_reason", lambda: None)
        ctrl._record_push_crash(PHONE, "Probe")
        assert ctrl._device_tree_reader(PHONE) is None
