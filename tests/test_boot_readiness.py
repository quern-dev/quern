"""A booted simulator is usable when its screen can be read, not when it boots.

Found driving a remote simulator: `boot_device` answered "booted" while iOS was
still on its logo, every read and tap then failed with sim-bridge's "Failed to
query accessibility tree", and `wait_for_element` -- the call an agent makes to
wait that out -- raised on its first poll instead of waiting. Measured on
iOS 27.2: about 20 s from `bootstatus` saying booted to a readable screen.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server.device.controller import DeviceController
from server.device.controller_ui import screen_not_ready
from server.device.ios.coredevice_ax import ScreenTooBusy
from server.models import DeviceError, DeviceType, UIElement

#: The real methods, before conftest's autouse fixture stands them in.
REAL_WAIT_FOR_BOOT = DeviceController._wait_for_boot
REAL_SCREEN_READABLE = DeviceController.wait_until_screen_readable

UDID = "AAAA-1111"
NOT_READY = DeviceError("sim-bridge: Failed to query accessibility tree. Is the simulator "
                        "booted with a frontmost app?", tool="sim-bridge")


def _element(label: str) -> UIElement:
    return UIElement(type="Button", label=label, identifier=None, value=None,
                     frame={"x": 0, "y": 0, "width": 10, "height": 10}, enabled=True)


def _reads(*outcomes):
    """`get_ui_elements` answering each outcome in turn: an exception is
    raised, anything else returned as the elements."""
    seq = list(outcomes)

    async def read(*_a, **_k):
        outcome = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome, UDID
    return read


class TestTheRule:
    def test_a_not_yet_readable_screen_is_waited_for(self) -> None:
        assert screen_not_ready(NOT_READY)

    @pytest.mark.parametrize("error", [
        ScreenTooBusy(UDID, 415, 20),
        DeviceError("No such device", tool="simctl"),
        RuntimeError("Failed to query accessibility tree"),     # not a DeviceError
    ])
    def test_nothing_else_is(self, error) -> None:
        assert not screen_not_ready(error)


class TestBootWaitsForTheBoot:
    async def test_by_udid(self, monkeypatch) -> None:
        ctrl = DeviceController()
        ctrl._device_type_cache[UDID] = DeviceType.SIMULATOR
        ctrl.simctl.boot = AsyncMock()
        ctrl.simctl.wait_until_booted = AsyncMock()
        monkeypatch.setattr(DeviceController, "_wait_for_boot", REAL_WAIT_FOR_BOOT)
        ctrl._restore_input_after_boot = AsyncMock()

        await ctrl.boot(udid=UDID)

        ctrl.simctl.wait_until_booted.assert_awaited_once_with(UDID)

    async def test_by_name(self, monkeypatch) -> None:
        from server.models import DeviceState
        from tests.test_device_controller import _device

        ctrl = DeviceController()
        ctrl.adb.is_installed = MagicMock(return_value=False)
        ctrl.simctl.list_devices = AsyncMock(return_value=[
            _device(udid="found-udid", name="iPhone 17", state=DeviceState.SHUTDOWN)])
        ctrl.simctl.boot = AsyncMock()
        ctrl.simctl.wait_until_booted = AsyncMock()
        monkeypatch.setattr(DeviceController, "_wait_for_boot", REAL_WAIT_FOR_BOOT)
        ctrl._restore_input_after_boot = AsyncMock()

        await ctrl.boot(name="iPhone 17")

        ctrl.simctl.wait_until_booted.assert_awaited_once_with("found-udid")


class TestWaitingForAReadableScreen:
    async def _wait(self, *outcomes, timeout=5.0):
        ctrl = DeviceController()
        ctrl.get_ui_elements = _reads(*outcomes)
        # Bounded here too: a guard removed in the code must fail this, not hang it (CR).
        return await asyncio.wait_for(
            REAL_SCREEN_READABLE(ctrl, UDID, timeout=timeout, interval=0.01), 10)

    async def test_it_waits_through_not_ready_until_it_reads(self) -> None:
        ready, waited, error = await self._wait(NOT_READY, NOT_READY, [_element("Settings")])
        assert ready is True and error is None

    async def test_any_other_failure_is_the_answer_at_once(self) -> None:
        ready, waited, error = await self._wait(ScreenTooBusy(UDID, 415, 20), [_element("x")])
        assert ready is False and "415" in error and waited < 1

    async def test_a_screen_that_never_reads_gives_up_and_says_why(self) -> None:
        ready, waited, error = await self._wait(NOT_READY, timeout=0.05)
        assert ready is False and "accessibility tree" in error

    async def test_a_read_that_hangs_ends_at_the_budget(self) -> None:
        """Each read is inside the budget, not only the checks between them (CR)."""
        ctrl = DeviceController()

        async def hang(*_a, **_k):
            await asyncio.sleep(30)

        ctrl.get_ui_elements = hang
        ready, waited, error = await asyncio.wait_for(
            REAL_SCREEN_READABLE(ctrl, UDID, timeout=0.2, interval=0.01), 10)
        assert ready is False and waited < 2 and "within 0.2s" in error


class TestTheBootRouteSaysWhetherTheScreenIsReady:
    def _boot(self, monkeypatch, readable):
        from fastapi.testclient import TestClient

        from server.config import ServerConfig
        from server.main import create_app

        app = create_app(config=ServerConfig(api_key="k"), enable_oslog=False,
                         enable_crash=False, enable_proxy=False)
        ctrl = MagicMock()
        ctrl.boot = AsyncMock(return_value=UDID)
        ctrl._is_android = lambda _u: False
        ctrl.is_cert_installed = AsyncMock(return_value=False)
        ctrl.wait_until_screen_readable = AsyncMock(return_value=readable)
        app.state.device_controller = ctrl
        with patch("server.proxy.cert_manager.is_cert_installed", AsyncMock(return_value=False)), \
             patch("server.proxy.sim_tls.refresh_after", AsyncMock()):
            client = TestClient(app)
            r = client.post("/api/v1/device/boot", json={"udid": UDID},
                            headers={"Authorization": "Bearer k"})
        assert r.status_code == 200, r.text
        return r.json(), ctrl

    def test_a_readable_screen(self, monkeypatch) -> None:
        body, ctrl = self._boot(monkeypatch, (True, 18.4, None))
        assert body["screen_ready"] is True and body["screen_ready_after_s"] == 18.4
        assert "screen_ready_error" not in body
        ctrl.wait_until_screen_readable.assert_awaited_once_with(UDID)

    def test_one_that_never_read_says_so_and_why(self, monkeypatch) -> None:
        body, _ = self._boot(monkeypatch, (False, 60.0, str(NOT_READY)))
        assert body["status"] == "booted" and body["screen_ready"] is False
        assert "accessibility tree" in body["screen_ready_error"]
        assert "could not be read yet" in body["note"]


class TestWaitForElementWaitsThroughANotReadyScreen:
    def _ctrl(self, *outcomes) -> DeviceController:
        ctrl = DeviceController()
        ctrl._device_type_cache[UDID] = DeviceType.SIMULATOR
        ctrl.resolve_udid = AsyncMock(return_value=UDID)
        ctrl._invalidate_ui_cache = MagicMock()
        ctrl.web_page_urls = AsyncMock(return_value=None)
        ctrl.get_ui_elements = _reads(*outcomes)
        return ctrl

    async def test_it_finds_the_element_once_the_screen_reads(self) -> None:
        ctrl = self._ctrl(NOT_READY, NOT_READY, [_element("Settings")])
        payload, _ = await asyncio.wait_for(ctrl.wait_for_element(
            label="Settings", condition="exists", timeout=5, interval=0.01), 10)
        assert payload["matched"] is True and payload["polls"] == 3

    async def test_an_unreadable_screen_is_never_read_as_the_element_gone(self) -> None:
        """not_exists would pass on a screen nobody could read: it must not."""
        ctrl = self._ctrl(NOT_READY)
        with pytest.raises(DeviceError, match="accessibility tree"):
            await asyncio.wait_for(ctrl.wait_for_element(label="Spinner", condition="not_exists",
                                        timeout=0.05, interval=0.01), 10)

    async def test_a_screen_that_never_reads_raises_the_reason(self) -> None:
        ctrl = self._ctrl(NOT_READY)
        with pytest.raises(DeviceError, match="accessibility tree"):
            await asyncio.wait_for(ctrl.wait_for_element(label="Settings", condition="exists",
                                        timeout=0.05, interval=0.01), 10)

    async def test_a_slow_failed_read_past_the_timeout_buys_no_other(self) -> None:
        """Elapsed is measured after the failure, not at the poll's start (CR)."""
        calls = []

        async def slow_not_ready(*_a, **_k):
            calls.append(1)
            await asyncio.sleep(0.3)
            raise NOT_READY

        ctrl = self._ctrl([])
        ctrl.get_ui_elements = slow_not_ready
        with pytest.raises(DeviceError, match="accessibility tree"):
            await asyncio.wait_for(ctrl.wait_for_element(
                label="Settings", condition="exists", timeout=0.1, interval=0.01), 10)
        assert calls == [1], "a read that failed past the timeout was followed by another"

    async def test_any_other_failure_still_raises_at_once(self) -> None:
        calls = []

        async def busy(*_a, **_k):
            calls.append(1)
            raise ScreenTooBusy(UDID, 415, 20)

        ctrl = self._ctrl([])
        ctrl.get_ui_elements = busy
        with pytest.raises(ScreenTooBusy):
            await asyncio.wait_for(ctrl.wait_for_element(label="Settings", condition="exists",
                                        timeout=5, interval=0.01), 10)
        assert calls == [1], "a busy screen was polled again"
