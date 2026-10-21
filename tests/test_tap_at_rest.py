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
    monkeypatch.setattr(u2_client, "REST_GAP_S", 0.001)
    monkeypatch.setattr(u2_client, "REST_SPAN_S", 0.01)
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
    def test_agreement_must_span_the_whole_window(self, monkeypatch):
        """Three agreeing reads taken faster than REST_SPAN_S are not enough:
        a target jumping every 120ms reads the same three times in 60ms."""
        monkeypatch.setattr(u2_client, "REST_SPAN_S", 0.05)
        reads = _reads(*([A] * 3 + [B] * 3) * 400)
        rest = _wait_for_rest(reads, A)
        assert rest["settled"] is False

    def test_a_target_still_across_the_window_settles(self, monkeypatch):
        monkeypatch.setattr(u2_client, "REST_SPAN_S", 0.02)
        rest = _wait_for_rest(_reads(*[A] * 5000), A)
        assert rest["settled"] is True

    def test_a_slow_read_starts_the_agreement_over(self, monkeypatch):
        """A target can jump and jump back inside a slow read."""
        monkeypatch.setattr(u2_client, "REST_SPAN_S", 0.02)
        monkeypatch.setattr(u2_client, "REST_MAX_GAP_S", 0.005)
        monkeypatch.setattr(u2_client, "REST_GAP_S", 0.01)   # every read is "slow"
        rest = _wait_for_rest(_reads(*[A] * 5000), A)
        assert rest["settled"] is False

    def test_reads_are_back_to_back(self, monkeypatch):
        """Spacing decides it: reads one round trip apart, not 100ms plus."""
        slept = []
        monkeypatch.setattr(u2_client.time, "sleep", slept.append)
        monkeypatch.setattr(u2_client, "REST_GAP_S", 0.02)
        monkeypatch.setattr(u2_client, "REST_BUDGET_S", 0.01)
        _wait_for_rest(_reads(*[A] * 5000), A)
        assert slept and all(t <= 0.03 for t in slept), slept

    def test_three_agreeing_reads_are_at_rest(self):
        read = _reads(*[A] * 5000)
        rest = _wait_for_rest(read, A)
        assert rest["settled"] is True and rest["bounds"] == A
        assert len(read.calls) >= 2           # the first frame, then two more

    def test_a_target_that_moved_and_stopped_is_reported_where_it_stopped(self):
        rest = _wait_for_rest(_reads(*[B] * 5000), A)
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
        """Followed by reads that would settle, so only the empty-read check
        can make this gone -- not the fake running out (review)."""
        assert _wait_for_rest(_reads(None, A, A, A, A), A)["gone"] is True


def _device(frames, counts=None):
    """A "Slider" selector: `info` answers each of `frames` in turn (the rest
    reads), and `info_list` each of `counts` -- frame lists, one frame per
    match: the count before, then the recount before the press. Running out of
    either raises, which no test may rely on by accident."""
    obj = MagicMock()
    frame_it = iter(frames)
    count_it = iter(counts if counts is not None else [[A], [A]])

    def info_list():
        return [{"bounds": f, "className": "android.widget.Button", "text": "Slider"}
                for f in next(count_it)]
    obj.info_list.side_effect = info_list
    type(obj).info = property(lambda self: {"bounds": next(frame_it)})

    def device(**sel):
        return obj if "text" in sel else MagicMock(**{"info_list.return_value": []})
    return device, obj


def _backend(device) -> U2Backend:
    backend = U2Backend()
    backend._connect = lambda serial: device
    return backend


class TestTapBySelector:
    async def test_without_confirm_rest_nothing_is_read_again(self):
        device, obj = _device([], counts=[[A]])   # any further read would raise
        result = await _backend(device).tap_by_selector(UDID, label="Slider")
        obj.click.assert_called_once_with()
        assert "rest" not in result

    async def test_a_settled_target_is_pressed_and_reported_where_it_rests(self):
        device, obj = _device([B] * 5000)
        result = await _backend(device).tap_by_selector(
            UDID, label="Slider", confirm_rest=True)
        obj.click.assert_called_once_with()
        assert result["rest"]["settled"] is True
        assert (result["x"], result["y"]) == (234, 73)

    async def test_a_moving_target_is_pressed_anyway_and_says_so(self):
        device, obj = _device([B, A] * 5000)
        result = await _backend(device).tap_by_selector(
            UDID, label="Slider", confirm_rest=True)
        obj.click.assert_called_once_with()
        assert result["rest"]["settled"] is False

    async def test_a_target_that_vanished_defers_without_pressing(self):
        device, obj = _device([B] * 5000, counts=[[A], []])
        result = await _backend(device).tap_by_selector(
            UDID, label="Slider", confirm_rest=True)
        assert result == {"defer": "vanished", "matches": 0}
        obj.click.assert_not_called()

    async def test_rest_reads_are_one_rpc_each(self):
        """A two-selector recount on every read spaced reads ~120ms apart, the
        fixture slider's half period, and it read as still 3 times in 10."""
        device, obj = _device([B] * 5000)
        await _backend(device).tap_by_selector(UDID, label="Slider", confirm_rest=True)
        assert obj.info_list.call_count == 2, "the count, and the recount before the press"

    async def test_a_recount_that_fails_defers_without_pressing(self):
        device, obj = _device([A] * 5000, counts=[[A]])     # the recount raises
        result = await _backend(device).tap_by_selector(
            UDID, label="Slider", confirm_rest=True)
        assert result == {"defer": "uncounted"}
        obj.click.assert_not_called()

    async def test_a_second_match_arriving_during_the_wait_is_ambiguous(self):
        """The incoming page of a transition has its own "Next": counted once,
        the press would go to whichever uiautomator found first (review)."""
        device, obj = _device([A] * 5000, counts=[[A], [A, B]])
        result = await _backend(device).tap_by_selector(
            UDID, label="Slider", confirm_rest=True)
        assert result == {"defer": "ambiguous", "matches": 2}
        obj.click.assert_not_called()

    async def test_a_read_that_fails_defers_without_pressing(self):
        device, obj = _device([])            # the first rest read raises
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
        assert "still moving" in result["warning"], "singular, as every other"
        assert "warnings" not in result

    async def test_skip_stability_check_skips_it(self):
        """As it skips the iOS settle: a target that never stops moving would
        otherwise cost a second on every tap (review)."""
        ctrl, backend = self._ctrl(self._tapped())
        ctrl._last_ui_change[UDID] = time.monotonic()
        await ctrl.tap_element(label="Slider", skip_stability_check=True)
        assert backend.tap_by_selector.await_args.kwargs["confirm_rest"] is False

    async def test_a_vanished_target_says_the_full_tree_was_read(self):
        from unittest.mock import patch

        from server.models import UIElement

        ctrl, backend = self._ctrl({"defer": "vanished"})
        ctrl._last_ui_change[UDID] = time.monotonic()
        backend.tap = AsyncMock()
        ctrl.get_ui_elements = AsyncMock(return_value=([UIElement(
            type="Button", label="Slider",
            frame={"x": 0, "y": 0, "width": 100, "height": 50})], UDID))
        with patch("server.device.controller_ui._capture_screenshot",
                   AsyncMock(return_value=None)):
            result = await ctrl.tap_element(label="Slider", skip_stability_check=False)
        assert "vanished" in result.get("fast_path", ""), result
        backend.scroll_into_view.assert_not_called()

    async def test_the_note_does_not_outlive_its_call(self):
        ctrl, backend = self._ctrl({"defer": "vanished"})
        from server.device.controller_ui import _FAST_PATH_NOTE

        _FAST_PATH_NOTE.set("left over")
        backend.tap_by_selector = AsyncMock(return_value=self._tapped())
        result = await ctrl.tap_element(label="Slider")
        assert "fast_path" not in result

    async def test_skip_stability_check_covers_the_tap_after_a_sweep_too(self):
        """It only covered the first call (CodeRabbit)."""
        from unittest.mock import patch

        ctrl, backend = self._ctrl(None)
        ctrl._last_ui_change[UDID] = time.monotonic()
        backend.tap_by_selector = AsyncMock(side_effect=[None, self._tapped()])
        backend.scroll_into_view = AsyncMock(return_value={"label": "Slider"})
        with patch("server.device.controller_ui.asyncio.sleep", AsyncMock()):
            await ctrl.tap_element(label="Slider", skip_stability_check=True)
        assert [c.kwargs["confirm_rest"] for c in backend.tap_by_selector.await_args_list] \
            == [False, False]

    async def test_the_tap_after_a_sweep_is_checked(self):
        from unittest.mock import patch

        ctrl, backend = self._ctrl(None)
        ctrl._last_ui_change[UDID] = time.monotonic() - 60
        backend.tap_by_selector = AsyncMock(side_effect=[None, self._tapped(
            waited_ms=220, settled=True)])
        backend.scroll_into_view = AsyncMock(return_value={"label": "Slider"})
        with patch("server.device.controller_ui.asyncio.sleep", AsyncMock()):
            result = await ctrl.tap_element(label="Slider")
        assert [c.kwargs["confirm_rest"] for c in backend.tap_by_selector.await_args_list] \
            == [False, True]
        assert result["waited_for_rest_ms"] == 220
