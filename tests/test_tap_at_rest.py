"""Android's fast path confirms a target is at rest before a tap moments after a
change (#514).

The dump path re-reads a target's position before tapping, and the iOS path
waits for the screen to settle; the selector fast path read the frame once and
pressed. A target still sliding in was pressed where it had been. Measured with
the Taps fixture's jumping slider (#511): a tap sent right after arming reached
a stale position, with `ok`, in 1 of 10 tries on an emulator.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from server.device.android import u2_client
from server.device.android.u2_client import U2Backend, _wait_for_rest
from server.device.controller import DeviceController
from server.models import DeviceType

UDID = "emulator-5554"
A = {"left": 0, "top": 0, "right": 100, "bottom": 50}
B = {"left": 184, "top": 48, "right": 284, "bottom": 98}


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(u2_client, "REST_INTERVAL_S", 0.001)
    monkeypatch.setattr(u2_client, "REST_BUDGET_S", 0.2)


def _reads(*frames):
    it = iter(frames)
    calls = []

    def read():
        calls.append(1)
        return next(it)
    read.calls = calls
    return read


class TestWaitForRest:
    def test_three_agreeing_reads_are_at_rest(self):
        read = _reads(A, A, A, A)
        rest = _wait_for_rest(read, A)
        assert rest["settled"] is True and rest["bounds"] == A
        assert len(read.calls) == 2           # the first frame, then two more

    def test_a_target_that_moved_and_stopped_is_reported_where_it_stopped(self):
        rest = _wait_for_rest(_reads(B, B, B, B), A)
        assert rest["settled"] is True and rest["bounds"] == B

    def test_two_agreeing_reads_are_not_enough(self):
        """A target jumping every 120ms reads the same twice 100ms apart."""
        frames = [A, B, B, A, A, B, B] * 200
        rest = _wait_for_rest(_reads(*frames), A)
        assert rest["settled"] is False

    def test_a_target_that_never_stops_is_given_up_on(self):
        frames = [B, A] * 1000
        started = time.monotonic()
        rest = _wait_for_rest(_reads(*frames), A)
        assert rest["settled"] is False and rest["gone"] is False
        assert time.monotonic() - started < 1.0

    def test_a_target_that_vanishes_is_gone(self):
        def read():
            raise RuntimeError("UiObjectNotFoundError")
        rest = _wait_for_rest(read, A)
        assert rest["gone"] is True and rest["settled"] is False

    def test_an_empty_read_is_gone_too(self):
        assert _wait_for_rest(_reads(None), A)["gone"] is True


def _device(frames, *, count=1):
    """A device whose one match reads `frames` in turn through `.info`."""
    obj = MagicMock()
    obj.info_list.return_value = [{"bounds": A, "className": "android.widget.Button",
                                   "text": "Slider"}] * count
    it = iter(frames)
    type(obj).info = property(lambda self: {"bounds": next(it)})

    def device(**sel):
        return obj if "text" in sel else MagicMock(**{"info_list.return_value": []})
    return device, obj


def _backend(device) -> U2Backend:
    backend = U2Backend()
    backend._connect = lambda serial: device
    return backend


class TestTapBySelector:
    async def test_without_confirm_rest_nothing_is_read_again(self):
        device, obj = _device([])             # any .info read would raise
        result = await _backend(device).tap_by_selector(UDID, label="Slider")
        obj.click.assert_called_once_with()
        assert "rest" not in result

    async def test_a_settled_target_is_pressed_and_reported_where_it_rests(self):
        device, obj = _device([B, B, B])
        result = await _backend(device).tap_by_selector(
            UDID, label="Slider", confirm_rest=True)
        obj.click.assert_called_once_with()
        assert result["rest"]["settled"] is True
        assert (result["x"], result["y"]) == (234, 73)

    async def test_a_moving_target_is_pressed_anyway_and_says_so(self):
        device, obj = _device([B, A] * 1000)
        result = await _backend(device).tap_by_selector(
            UDID, label="Slider", confirm_rest=True)
        obj.click.assert_called_once_with()
        assert result["rest"]["settled"] is False

    async def test_a_target_that_vanished_defers_without_pressing(self):
        device, obj = _device([None])
        result = await _backend(device).tap_by_selector(
            UDID, label="Slider", confirm_rest=True)
        assert result == {"defer": "vanished"}
        obj.click.assert_not_called()


class TestTapElement:
    def _ctrl(self, tapped):
        ctrl = DeviceController()
        ctrl._device_type_cache[UDID] = DeviceType.ANDROID_EMULATOR
        ctrl.resolve_udid = AsyncMock(return_value=UDID)
        ctrl._invalidate_ui_cache = MagicMock()
        backend = MagicMock()
        backend.tap_by_selector = AsyncMock(return_value=tapped)
        backend.scroll_into_view = AsyncMock()
        ctrl._ui_backend = MagicMock(return_value=backend)
        return ctrl, backend

    def _tapped(self, **rest):
        t = {"label": "Slider", "identifier": None, "type": "Button", "x": 1, "y": 2}
        if rest:
            t["rest"] = rest
        return t

    async def test_a_tap_just_after_a_change_asks_for_the_check(self):
        ctrl, backend = self._ctrl(self._tapped(waited_ms=210, settled=True))
        ctrl._last_ui_change[UDID] = time.monotonic()
        result = await ctrl.tap_element(label="Slider")
        assert backend.tap_by_selector.await_args.kwargs["confirm_rest"] is True
        assert result["waited_for_rest_ms"] == 210
        assert "target_moving" not in result and "warnings" not in result
        assert "rest" not in result["tapped"]

    async def test_a_tap_long_after_a_change_does_not(self):
        ctrl, backend = self._ctrl(self._tapped())
        ctrl._last_ui_change[UDID] = time.monotonic() - 60
        result = await ctrl.tap_element(label="Slider")
        assert backend.tap_by_selector.await_args.kwargs["confirm_rest"] is False
        assert "waited_for_rest_ms" not in result

    async def test_a_target_that_kept_moving_is_reported(self):
        ctrl, backend = self._ctrl(self._tapped(waited_ms=1000, settled=False))
        ctrl._last_ui_change[UDID] = time.monotonic()
        result = await ctrl.tap_element(label="Slider")
        assert result["status"] == "ok"
        assert result["target_moving"] is True
        assert "still moving" in result["warnings"][0]
