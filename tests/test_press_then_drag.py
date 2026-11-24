"""A swipe that holds its finger at the start before it moves, or rests at
the end before it lifts (#769).

Drag-and-drop, reordering a row by its handle, moving an icon or a pin: each
waits for a press before it follows the finger, and an ordinary swipe moves at
once. WDA's drag always pressed first; CoreDevice HID's swipe never did, so on
an iOS 27 phone driven without WDA there was no way to send one.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from server.device import gestures
from server.device.ios import coredevice_input
from server.device.ios.coredevice_input import CoreDeviceInput
from server.models import DeviceOperationUnsupportedError, InvalidDeviceRequestError
from tests.test_coredevice_input import CONTACT, PHONE, RELEASE
from tests.test_coredevice_input import lib as lib  # noqa: F401 -- the fixture
from tests.test_coredevice_input import wda as wda  # noqa: F401 -- the fixture
from tests.test_long_press_edge import UDID, TestEachBackendsLongPress, _ctrl


class TestTheController:
    async def test_press_reaches_the_backend_only_when_given(self):
        backend = MagicMock(spec=["swipe", "TOOL_NAME", "press_drag"])
        backend.press_drag = True
        backend.swipe = AsyncMock()
        ctrl = _ctrl(backend)
        await ctrl.swipe(10, 500, 10, 200, press=0.8)
        assert backend.swipe.await_args.kwargs == {"press": 0.8}
        await ctrl.swipe(10, 500, 10, 200)
        assert backend.swipe.await_args.kwargs == {}

    async def test_a_backend_that_cannot_press_first_refuses(self):
        """Sent anyway it would arrive as a plain swipe and answer ok."""
        backend = MagicMock(spec=["swipe", "TOOL_NAME"])
        backend.TOOL_NAME = "idb"
        backend.swipe = AsyncMock()
        with pytest.raises(DeviceOperationUnsupportedError, match="idb backend cannot hold"):
            await _ctrl(backend).swipe(10, 500, 10, 200, press=0.8)
        backend.swipe.assert_not_awaited()

    def test_which_backends_can_press_first(self):
        from server.device.android.u2_client import U2Backend
        from server.device.ios.idb import IdbBackend
        from server.device.ios.sim_bridge import SimBridgeBackend
        from server.device.ios.wda_client import WdaBackend

        assert CoreDeviceInput.press_drag is True
        assert WdaBackend.press_drag is True
        assert SimBridgeBackend.press_drag is True
        assert U2Backend.press_drag is True
        assert getattr(IdbBackend, "press_drag", False) is False


class TestCoreDeviceHid:
    async def test_the_contact_is_held_still_at_the_start_then_moves(
            self, lib, wda, monkeypatch):  # noqa: F811
        slept = []
        monkeypatch.setattr(coredevice_input, "_sleep",
                            AsyncMock(side_effect=lambda s: slept.append(s)))
        assert await CoreDeviceInput(wda).swipe(
            PHONE, 207, 700, 207, 200, duration=0.5, press=0.5) == "coredevice"

        touches = lib.touches()
        start = ("touch", CONTACT, 32768, round(700 / 896 * 65535))
        # Down, 30 still frames at 60 Hz, then the move's own first sample,
        # which is the start too; only after that does it leave.
        assert touches[:32] == [start] * 32
        assert touches[32] != start
        assert touches[-1][1] == RELEASE
        # The press's waits come first and add up to it.
        assert sum(slept[1:31]) == pytest.approx(0.5)
        wda.swipe.assert_not_called()

    async def test_a_swipe_that_fails_part_way_lifts_where_it_was(
            self, lib, wda, monkeypatch):  # noqa: F811
        """Dropping the session alone may leave the finger down (CodeRabbit on #770)."""
        from server.models import DeviceError
        monkeypatch.setattr(coredevice_input, "_sleep", AsyncMock())
        lib.fail_contact = (5, ConnectionResetError("dropped after the write"))
        with pytest.raises(DeviceError):
            await CoreDeviceInput(wda).swipe(PHONE, 207, 700, 207, 200, duration=0.5)
        touches = lib.touches()
        assert touches[-1][1] == RELEASE, "the finger was not lifted"
        # Where the failed report put it: it may have landed.
        assert touches[-1][2:] != touches[0][2:]
        wda.swipe.assert_not_called()

    async def test_a_cancelled_press_lifts_the_finger(self, lib, wda, monkeypatch):  # noqa: F811
        import asyncio
        gate = asyncio.Event()
        sleeps = []

        async def sleep(s):
            sleeps.append(s)
            if len(sleeps) == 5:
                await gate.wait()           # cancelled here, mid-press

        monkeypatch.setattr(coredevice_input, "_sleep", sleep)
        task = asyncio.create_task(CoreDeviceInput(wda).swipe(
            PHONE, 207, 700, 207, 200, duration=0.5, press=1.0))
        for _ in range(100):
            if len(sleeps) >= 5:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert lib.touches()[-1][1] == RELEASE, "the finger was not lifted"

    async def test_a_plain_swipe_moves_at_once(self, lib, wda, monkeypatch):  # noqa: F811
        monkeypatch.setattr(coredevice_input, "_sleep", AsyncMock())
        await CoreDeviceInput(wda).swipe(PHONE, 207, 700, 207, 200, duration=0.5)
        assert lib.touches()[1] != lib.touches()[0]

    async def test_the_press_goes_to_wda_with_it(self, lib, wda):  # noqa: F811
        from tests.test_coredevice_input import _display
        lib.display = _display(orientation="rot90")
        await CoreDeviceInput(wda).swipe(PHONE, 1, 2, 3, 4, 0.5, press=0.7)
        wda.swipe.assert_awaited_once_with(PHONE, 1, 2, 3, 4, 0.5, hold=0.0, press=0.7)


class TestWda:
    async def test_the_press_is_the_drags_duration(self):
        backend, calls = TestEachBackendsLongPress._wda()
        await backend.swipe(UDID, 10, 500, 10, 200, 0.3, press=1.2)
        path, kwargs = calls[-1]
        assert path == "/wda/dragfromtoforduration"
        assert kwargs["json"]["duration"] == 1.2

    async def test_without_one_the_duration_is_as_before(self):
        backend, calls = TestEachBackendsLongPress._wda()
        await backend.swipe(UDID, 10, 500, 10, 200, 0.3)
        assert calls[-1][1]["json"]["duration"] == 0.3


class TestSimBridge:
    def _backend(self):
        from server.device.ios.sim_bridge import SimBridgeBackend, SimBridgeManager

        mgr = SimBridgeManager()
        sent: list[dict] = []

        async def send(cmd):
            sent.append(cmd)
            return {"ok": True}
        mgr.send = AsyncMock(side_effect=send)  # type: ignore[method-assign]
        return SimBridgeBackend(mgr), sent

    async def test_it_is_a_touch_path_that_stays_at_the_start(self):
        backend, sent = self._backend()
        await backend.swipe(UDID, 100, 700, 100, 200, 0.5, press=0.5)
        cmd = sent[-1]
        assert cmd["cmd"] == "touch-paths"
        (path,) = cmd["paths"]
        assert path[:31] == [[100, 700]] * 31
        assert path[-1] == [100, 200]
        assert cmd["duration"] == pytest.approx(1.0)

    async def test_a_plain_swipe_is_the_bridges_swipe(self):
        backend, sent = self._backend()
        await backend.swipe(UDID, 100, 700, 100, 200, 0.5)
        assert sent[-1]["cmd"] == "swipe"

    async def test_an_edge_swipe_cannot_press_first(self):
        backend, sent = self._backend()
        with pytest.raises(InvalidDeviceRequestError, match="edge"):
            await backend.swipe(UDID, 1, 500, 300, 500, 0.3, edge="left", press=0.5)
        assert sent == []


class TestAndroid:
    async def test_down_wait_move_up(self, monkeypatch):
        import server.device.android.u2_client as u2

        device = MagicMock()
        order: list[str] = []
        device.touch.down.side_effect = lambda *a: order.append("down")
        device.touch.move.side_effect = lambda *a: order.append("move")
        device.touch.up.side_effect = lambda *a: order.append("up")
        slept = []

        def wait(stop, s):
            slept.append(s)
            order.append("wait")
            return False

        monkeypatch.setattr(u2, "_interruptible_wait", wait)
        backend = TestEachBackendsLongPress._u2(device)

        await backend.swipe("dev", 100, 1300, 100, 400, 0.5, press=0.8)

        assert order[:2] == ["down", "wait"] and slept[0] == 0.8
        assert order[-1] == "up" and "move" in order
        device.touch.down.assert_called_once_with(100, 1300)
        device.touch.up.assert_called_once_with(100, 400)
        device.swipe.assert_not_called()

    async def test_a_lift_that_fails_after_a_good_drag_is_an_error(self, monkeypatch):
        """Suppressed, a finger possibly still down read as success (CodeRabbit on #770)."""
        import server.device.android.u2_client as u2
        from server.models import DeviceError

        device = MagicMock()
        device.touch.up.side_effect = RuntimeError("up never acknowledged")
        monkeypatch.setattr(u2, "_interruptible_wait", lambda stop, s: False)
        backend = TestEachBackendsLongPress._u2(device)
        with pytest.raises(DeviceError, match="up never acknowledged"):
            await backend.swipe("dev", 100, 1300, 100, 400, 0.5, press=0.8)
        device.touch.up.assert_called_once()          # not replayed
        assert device.touch.down.call_count == 1

    async def test_a_down_that_raises_is_still_lifted(self, monkeypatch):
        """It may have reached the device before raising (CodeRabbit on #770)."""
        import server.device.android.u2_client as u2

        device = MagicMock()
        device.touch.down.side_effect = RuntimeError("down, then the socket went")
        monkeypatch.setattr(u2, "_interruptible_wait", lambda stop, s: False)
        backend = TestEachBackendsLongPress._u2(device)
        with pytest.raises(Exception):
            await backend.swipe("dev", 100, 1300, 100, 400, 0.5, press=0.8)
        device.touch.up.assert_called_once_with(100, 1300)
        device.touch.move.assert_not_called()

    async def test_a_cancel_stops_the_drag_and_lifts_before_returning(self):
        """Cancelling the await did not stop the thread: it dragged and held on
        after the caller had moved on (CodeRabbit on #770). Real waits here."""
        import asyncio
        import threading
        import time

        device = MagicMock()
        pressed = threading.Event()
        lifted = threading.Event()
        device.touch.down.side_effect = lambda *a: pressed.set()

        def slow_up(*a):
            time.sleep(0.3)             # so only a cancel that waits sees it done
            lifted.set()

        device.touch.up.side_effect = slow_up
        backend = TestEachBackendsLongPress._u2(device)
        task = asyncio.create_task(
            backend.swipe("dev", 100, 1300, 100, 400, 0.5, press=10.0))
        assert await asyncio.to_thread(pressed.wait, 5), "the press never started"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        # Lifted by the time the cancel returned -- the device is not the
        # next caller's while a finger is down -- where it was pressed.
        assert lifted.is_set(), "the cancel returned before the finger was lifted"
        device.touch.up.assert_called_once_with(100, 1300)
        device.touch.move.assert_not_called()

    @pytest.mark.parametrize("press", [0.8, 0.0], ids=["press-drag", "plain swipe"])
    async def test_a_cancel_while_connecting_sends_nothing(self, press):
        """It went on to press and lift -- a tap -- after the caller had gone
        (CodeRabbit on #770)."""
        import asyncio
        import threading

        device = MagicMock()
        connecting, release = threading.Event(), threading.Event()
        backend = TestEachBackendsLongPress._u2(device)

        def connect(serial):
            connecting.set()
            release.wait(5)
            return device

        backend._connect = connect
        task = asyncio.create_task(
            backend.swipe("dev", 100, 1300, 100, 400, 0.5, press=press))
        assert await asyncio.to_thread(connecting.wait, 5)
        task.cancel()
        await asyncio.sleep(0.05)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        device.touch.down.assert_not_called()
        device.swipe.assert_not_called()

    async def test_the_finger_is_lifted_if_the_move_fails(self, monkeypatch):
        import server.device.android.u2_client as u2

        device = MagicMock()
        device.touch.move.side_effect = RuntimeError("adb went away")
        monkeypatch.setattr(u2, "_interruptible_wait", lambda stop, s: False)
        backend = TestEachBackendsLongPress._u2(device)
        with pytest.raises(Exception):
            await backend.swipe("dev", 100, 1300, 100, 400, 0.5, press=0.8)
        device.touch.up.assert_called_once()


def test_the_plan_holds_for_the_whole_press():
    plan = gestures.press_then_drag((10, 10), (10, 110), press=1.0, duration=0.5)
    (path,) = plan.paths
    assert path[:61] == [(10, 10)] * 61
    assert path[-1] == (10, 110)
    assert plan.duration == pytest.approx(1.5)


class TestTheRoute:
    def test_press_is_bounded(self):
        from pydantic import ValidationError

        from server.models import SwipeRequest

        assert SwipeRequest(start_x=0, start_y=0, end_x=1, end_y=1).press == 0
        for bad in (-1, 11, float("nan")):
            with pytest.raises(ValidationError):
                SwipeRequest(start_x=0, start_y=0, end_x=1, end_y=1, press=bad)


class TestHoldAtTheEnd:
    async def test_hold_reaches_the_backend_only_when_given(self):
        backend = MagicMock(spec=["swipe", "TOOL_NAME", "press_drag"])
        backend.press_drag = True
        backend.swipe = AsyncMock()
        ctrl = _ctrl(backend)
        await ctrl.swipe(10, 500, 10, 200, press=1.0, hold=1.2)
        assert backend.swipe.await_args.kwargs == {"press": 1.0, "hold": 1.2}
        await ctrl.swipe(10, 500, 10, 200, hold=0.4)
        assert backend.swipe.await_args.kwargs == {"hold": 0.4}

    async def test_a_backend_that_cannot_hold_refuses(self):
        backend = MagicMock(spec=["swipe", "TOOL_NAME"])
        backend.TOOL_NAME = "idb"
        backend.swipe = AsyncMock()
        with pytest.raises(DeviceOperationUnsupportedError, match="idb backend cannot hold"):
            await _ctrl(backend).swipe(10, 500, 10, 200, hold=1.0)
        backend.swipe.assert_not_awaited()

    async def test_wda_rests_with_press_and_drag_with_velocity(self):
        backend, calls = TestEachBackendsLongPress._wda()
        await backend.swipe(UDID, 60, 400, 380, 400, 0.8, press=1.0, hold=1.2)
        path, kwargs = calls[-1]
        assert path == "/wda/pressAndDragWithVelocity"
        assert kwargs["json"] == {"fromX": 60, "fromY": 400, "toX": 380, "toY": 400,
                                  "pressDuration": 1.0, "velocity": pytest.approx(400.0),
                                  "holdDuration": 1.2}

    async def test_wda_without_hold_keeps_its_drag(self):
        backend, calls = TestEachBackendsLongPress._wda()
        await backend.swipe(UDID, 60, 400, 380, 400, 0.8, press=1.0)
        assert calls[-1][0] == "/wda/dragfromtoforduration"

    async def test_sim_bridge_holds_at_the_end_of_a_press(self):
        backend, sent = TestSimBridge()._backend()
        await backend.swipe(UDID, 100, 700, 100, 200, 0.5, press=0.5, hold=1.0)
        (path,) = sent[-1]["paths"]
        assert path[-61:] == [[100, 200]] * 61
        assert sent[-1]["duration"] == pytest.approx(2.0)

    async def test_android_waits_before_lifting(self, monkeypatch):
        import server.device.android.u2_client as u2

        device = MagicMock()
        order: list[str] = []
        device.touch.move.side_effect = lambda *a: order.append("move")
        device.touch.up.side_effect = lambda *a: order.append("up")
        monkeypatch.setattr(u2, "_interruptible_wait",
                            lambda stop, s: order.append(f"wait {s}") or False)
        backend = TestEachBackendsLongPress._u2(device)
        await backend.swipe("dev", 100, 1300, 100, 400, 0.5, hold=1.5)
        assert order[-2:] == ["wait 1.5", "up"]
