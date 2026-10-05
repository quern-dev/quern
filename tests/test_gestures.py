"""Multi-finger gestures and timed taps (#252).

The geometry is pure and checked exactly: a pinch's separation ratio is the
scale, a rotation's waypoints stay on the circle, and the signs match what a
recogniser reports (positive rotation is clockwise on screen). Then the layers
that carry it: the controller refuses before touching a device, and refuses a
backend that cannot move several fingers rather than sending one; the
sim-bridge client sends what the Swift side parses and turns a refused request
into a 400; the route maps all of it.
"""

from __future__ import annotations

import math
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from server.config import ServerConfig
from server.device import gestures
from server.device.controller import DeviceController
from server.device.ios.sim_bridge import SimBridgeBackend, SimBridgeManager
from server.main import create_app
from server.models import (
    DeviceError,
    DeviceOperationUnsupportedError,
    DeviceType,
    InvalidDeviceRequestError,
    UIElement,
)

UDID = "AAAA-1111"


def _sep(pair):
    (ax, ay), (bx, by) = pair
    return math.hypot(bx - ax, by - ay)


def _ends(plan):
    starts = [p[0] for p in plan.paths]
    ends = [p[-1] for p in plan.paths]
    return starts, ends


class TestPinch:
    @pytest.mark.parametrize("scale", [3.0, 0.5, 1.5, 0.2])
    def test_the_separation_ratio_is_the_scale(self, scale):
        starts, ends = _ends(gestures.plan("pinch", 200, 400, scale=scale))
        assert _sep(ends) / _sep(starts) == pytest.approx(scale)

    def test_the_narrow_end_is_the_distance(self):
        out = gestures.plan("pinch", 200, 400, scale=4, distance=50)
        assert _sep(_ends(out)[0]) == pytest.approx(50)     # spread starts narrow
        squeeze = gestures.plan("pinch", 200, 400, scale=0.25, distance=50)
        assert _sep(_ends(squeeze)[1]) == pytest.approx(50)  # squeeze ends narrow

    def test_it_stays_centred_and_on_its_line(self):
        plan = gestures.plan("pinch", 200, 400, scale=2, angle=90)
        for a, b in zip(*plan.paths, strict=True):
            assert ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2) == pytest.approx((200, 400))
            assert a[0] == pytest.approx(200) and b[0] == pytest.approx(200)  # vertical

    @pytest.mark.parametrize("scale", [None, 1, 0, -2, float("nan"), float("inf")])
    def test_a_scale_that_is_not_a_pinch_is_refused(self, scale):
        with pytest.raises(InvalidDeviceRequestError, match="scale"):
            gestures.plan("pinch", 200, 400, scale=scale)


class TestRotate:
    def test_the_fingers_stay_on_the_circle(self):
        # Not the default radius, so a plan that ignored `distance` fails.
        plan = gestures.plan("rotate", 200, 400, degrees=270, distance=50)
        for path in plan.paths:
            for x, y in path:
                assert math.hypot(x - 200, y - 400) == pytest.approx(50)

    def test_it_starts_where_angle_says(self):
        plan = gestures.plan("rotate", 200, 400, degrees=45, angle=90, distance=80)
        assert plan.paths[0][0] == pytest.approx((200, 320))   # above, not left
        assert plan.paths[1][0] == pytest.approx((200, 480))

    @pytest.mark.parametrize("degrees", [3601, -3601, 1e5])
    def test_a_turn_beyond_ten_circles_is_refused(self, degrees):
        """Unbounded, 1e5 degrees was 10,001 waypoints and at least 80s on the
        bridge, past its 30s timeout -- killed mid-gesture, fingers down."""
        with pytest.raises(InvalidDeviceRequestError, match="3600"):
            gestures.plan("rotate", 200, 400, degrees=degrees)

    def test_the_duration_said_is_the_one_the_bridge_takes(self):
        """It never steps faster than 8ms, so ten circles take ~2.9s whatever
        was asked; the response used to say 0.6."""
        plan = gestures.plan("rotate", 200, 400, degrees=3600, duration=0.6)
        assert plan.duration == pytest.approx(360 * gestures.MIN_STEP_SECONDS)
        assert plan.geometry()["duration"] == plan.duration

    def test_positive_is_clockwise_on_screen(self):
        """y grows downward, so the left finger rising is clockwise -- the
        sign UIRotationGestureRecognizer reports (+85 measured for +90)."""
        plan = gestures.plan("rotate", 200, 400, degrees=90, distance=80)
        assert plan.paths[0][0] == pytest.approx((120, 400))   # starts left
        assert plan.paths[0][-1] == pytest.approx((200, 320))  # ends above

    def test_a_large_turn_moves_along_the_arc(self):
        """Straight between the ends, a half turn sends both fingers through
        the centre; -200 was measured reading as -195 and -205 on iOS 18.6."""
        plan = gestures.plan("rotate", 200, 400, degrees=-200)
        assert len(plan.paths[0]) >= 21
        step = math.radians(200) / (len(plan.paths[0]) - 1)
        chord = 2 * 80 * math.sin(step / 2)
        for path in plan.paths:
            for a, b in zip(path, path[1:]):
                assert math.dist(a, b) == pytest.approx(chord)

    @pytest.mark.parametrize("degrees", [None, 0, float("nan"), float("inf")])
    def test_no_turn_is_refused(self, degrees):
        with pytest.raises(InvalidDeviceRequestError, match="degrees"):
            gestures.plan("rotate", 200, 400, degrees=degrees)


class TestPanAndTaps:
    def test_pan_moves_both_fingers_together(self):
        starts, ends = _ends(gestures.plan("pan", 200, 400, dx=80, dy=-40, distance=40))
        assert starts == pytest.approx([(180, 400), (220, 400)])
        assert ends == pytest.approx([(260, 360), (300, 360)])

    def test_a_pan_that_goes_nowhere_is_refused(self):
        with pytest.raises(InvalidDeviceRequestError, match="dx or dy"):
            gestures.plan("pan", 200, 400)

    def test_double_tap(self):
        plan = gestures.plan("double_tap", 200, 400)
        assert (plan.points, plan.count, plan.interval) == ([(200, 400)], 2, 0.08)
        assert gestures.plan("double_tap", 200, 400, count=3).count == 3
        with pytest.raises(InvalidDeviceRequestError, match="single tap"):
            gestures.plan("double_tap", 200, 400, count=1)

    def test_two_finger_tap(self):
        plan = gestures.plan("two_finger_tap", 200, 400, distance=50)
        assert plan.points == [(175, 400), (225, 400)] and plan.count == 1

    def test_the_timing_arguments_are_used(self):
        for kind, extra in (("pinch", {"scale": 2}), ("rotate", {"degrees": 30}),
                            ("pan", {"dx": 5})):
            assert gestures.plan(kind, 200, 400, duration=1.7, **extra).duration == 1.7
        assert gestures.plan("double_tap", 200, 400, interval=0.2).interval == 0.2
        tft = gestures.plan("two_finger_tap", 200, 400, count=3, interval=0.15)
        assert (tft.count, tft.interval) == (3, 0.15)

    @pytest.mark.parametrize("kind,extra", [
        ("pinch", {"scale": 1e308}),                     # 60pt * 1e308
        ("pan", {"dx": 1.7e308, "distance": 1e308}),     # 0.5e308 + 1.7e308
    ])
    def test_arithmetic_that_overflows_is_refused(self, kind, extra):
        """Every argument finite, the fingers at infinity: it reached the
        bridge as a token its JSON parser rejects, and came back as a 500."""
        with pytest.raises(InvalidDeviceRequestError, match="infinity"):
            gestures.plan(kind, 200, 400, **extra)

    @pytest.mark.parametrize("angle", [float("nan"), float("inf")])
    def test_an_angle_that_is_not_a_number_is_refused(self, angle):
        """NaN reached the bridge as a bare `NaN`, which its JSON parser
        rejects, and came back as a 500."""
        with pytest.raises(InvalidDeviceRequestError, match="angle"):
            gestures.plan("pinch", 200, 400, scale=2, angle=angle)

    def test_an_unknown_gesture_is_refused(self):
        with pytest.raises(InvalidDeviceRequestError, match="unknown gesture"):
            gestures.plan("wiggle", 200, 400)

    def test_the_geometry_said_back_is_where_the_fingers_went(self):
        geo = gestures.plan("pan", 200, 400, dx=10).geometry()
        assert geo["fingers"][0] == {"from": [180.0, 400.0], "to": [190.0, 400.0]}
        assert gestures.plan("two_finger_tap", 200, 400).geometry()["fingers"] == [
            {"at": [180.0, 400.0]}, {"at": [220.0, 400.0]}]


def _ctrl(backend):
    ctrl = DeviceController()
    ctrl._device_type_cache[UDID] = DeviceType.SIMULATOR
    ctrl.resolve_udid = AsyncMock(return_value=UDID)
    ctrl._invalidate_ui_cache = MagicMock()
    ctrl._ui_backend = MagicMock(return_value=backend)
    return ctrl


def _multitouch_backend():
    # Specced: a bare MagicMock has every attribute, `gesture_defaults` among
    # them, and the controller would await it.
    backend = MagicMock(spec=["TOOL_NAME", "multitouch", "perform_gesture"])
    backend.TOOL_NAME = "sim-bridge"
    backend.multitouch = True
    backend.perform_gesture = AsyncMock()
    return backend


class TestTheController:
    async def test_a_bad_argument_is_refused_before_any_device(self):
        backend = _multitouch_backend()
        ctrl = _ctrl(backend)
        with pytest.raises(InvalidDeviceRequestError):
            await ctrl.gesture("pinch", x=1, y=2, scale=1)
        ctrl.resolve_udid.assert_not_awaited()

    @pytest.mark.parametrize("where", [{}, {"x": 1, "y": 2, "identifier": "pad"}])
    async def test_it_needs_a_point_or_an_element_not_both(self, where):
        ctrl = _ctrl(_multitouch_backend())
        with pytest.raises(InvalidDeviceRequestError, match="one of the two"):
            await ctrl.gesture("double_tap", **where)

    async def test_half_a_point_is_refused_not_ignored(self):
        """`x` with a label used to be dropped, and the gesture centred on the
        element without a word."""
        ctrl = _ctrl(_multitouch_backend())
        with pytest.raises(InvalidDeviceRequestError, match="both x and y"):
            await ctrl.gesture("double_tap", x=10, label="Map")

    async def test_an_element_without_a_frame_is_refused(self):
        """A frameless match raised a TypeError: a bare 500."""
        ctrl = _ctrl(_multitouch_backend())
        ctrl.get_element = AsyncMock(return_value=({"label": "Map", "frame": None}, UDID))
        with pytest.raises(InvalidDeviceRequestError, match="no frame"):
            await ctrl.gesture("double_tap", label="Map")

    async def test_the_element_query_is_passed_on(self):
        ctrl = _ctrl(_multitouch_backend())
        ctrl.get_element = AsyncMock(return_value=(
            {"frame": {"x": 0, "y": 0, "width": 100, "height": 100}}, UDID))
        await ctrl.gesture("double_tap", label="Map", element_type="Image")
        assert ctrl.get_element.await_args.kwargs["element_type"] == "Image"

    async def test_a_backend_without_multitouch_refuses_rather_than_sending_one_finger(self):
        backend = MagicMock(spec=["TOOL_NAME", "swipe", "tap"])
        backend.TOOL_NAME = "wda"
        ctrl = _ctrl(backend)
        ctrl.get_element = AsyncMock()
        with pytest.raises(DeviceOperationUnsupportedError, match="wda backend cannot"):
            await ctrl.gesture("pinch", identifier="map", scale=2)
        ctrl.get_element.assert_not_awaited()    # refused before reading the tree

    async def test_it_sends_the_plan_and_says_where_the_fingers_went(self):
        backend = _multitouch_backend()
        ctrl = _ctrl(backend)
        result = await ctrl.gesture("pinch", x=200, y=400, scale=2)
        sent_udid, plan = backend.perform_gesture.await_args.args
        assert sent_udid == UDID and plan.kind == "pinch"
        assert result["center"] == [200.0, 400.0] and result["backend"] == "sim-bridge"
        assert len(result["fingers"]) == 2
        ctrl._invalidate_ui_cache.assert_called_once_with(UDID)

    async def test_a_backends_defaults_are_used(self):
        backend = MagicMock(spec=["TOOL_NAME", "multitouch", "perform_gesture",
                                  "gesture_defaults"])
        backend.TOOL_NAME, backend.multitouch = "u2", True
        backend.perform_gesture = AsyncMock()
        backend.gesture_defaults = AsyncMock(return_value={"unit": 2.0})
        await _ctrl(backend).gesture("two_finger_tap", x=500, y=1000)
        plan = backend.perform_gesture.await_args.args[1]
        assert plan.points == [(460, 1000), (540, 1000)]     # 40 * 2.0 apart

    async def test_it_centres_on_an_element(self):
        backend = _multitouch_backend()
        ctrl = _ctrl(backend)
        ctrl.get_element = AsyncMock(return_value=(
            {"label": "Map", "identifier": "map", "type": "Other", "match_count": 2,
             "frame": {"x": 20, "y": 100, "width": 360, "height": 400}}, UDID))
        result = await ctrl.gesture("double_tap", identifier="map")
        plan = backend.perform_gesture.await_args.args[1]
        assert plan.points == [(200, 300)]
        assert result["element"]["match_count"] == 2


class TestTheSimBridgeClient:
    @staticmethod
    def _backend(reply):
        mgr = SimBridgeManager()
        sent = []

        async def send(cmd):
            sent.append(cmd)
            return reply
        mgr.send = AsyncMock(side_effect=send)  # type: ignore[method-assign]
        return SimBridgeBackend(mgr), sent

    async def test_a_moving_gesture_is_touch_paths(self):
        backend, sent = self._backend({"ok": True})
        await backend.perform_gesture(UDID, gestures.plan("pan", 200, 400, dx=10))
        cmd = sent[0]
        assert cmd["cmd"] == "touch-paths" and cmd["udid"] == UDID
        assert len(cmd["paths"]) == 2 and cmd["paths"][0][0] == [180.0, 400.0]
        assert cmd["duration"] == 0.6

    async def test_a_tap_is_multi_tap(self):
        backend, sent = self._backend({"ok": True})
        await backend.perform_gesture(
            UDID, gestures.plan("double_tap", 200, 400, count=3, interval=0.2))
        assert sent[0] == {"cmd": "multi-tap", "udid": UDID, "count": 3,
                           "interval": 0.2, "points": [[200, 400]]}

    async def test_the_duration_reaches_the_wire(self):
        backend, sent = self._backend({"ok": True})
        await backend.perform_gesture(
            UDID, gestures.plan("pinch", 200, 400, scale=2, duration=1.4))
        assert sent[0]["duration"] == 1.4

    async def test_a_request_the_bridge_turned_down_is_invalid(self):
        backend, _ = self._backend({"ok": False, "code": "bad_request",
                                    "error": "point (-36, 546) is off the 402x874 screen"})
        with pytest.raises(InvalidDeviceRequestError, match="off the"):
            await backend.perform_gesture(UDID, gestures.plan("pinch", 200, 400, scale=20))

    async def test_a_bridge_failure_is_not(self):
        backend, _ = self._backend({"ok": False, "error": "no HID client"})
        with pytest.raises(DeviceError) as exc:
            await backend.perform_gesture(UDID, gestures.plan("pinch", 200, 400, scale=2))
        assert not isinstance(exc.value, InvalidDeviceRequestError)


class TestTheRoute:
    @pytest.fixture
    def app(self):
        return create_app(config=ServerConfig(api_key="k"), enable_oslog=False,
                          enable_crash=False, enable_proxy=False)

    async def _post(self, app, ctrl, body):
        app.state.device_controller = ctrl
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            return await c.post("/api/v1/device/ui/gesture", json=body,
                                headers={"Authorization": "Bearer k"})

    async def test_ok(self, app):
        ctrl = _ctrl(_multitouch_backend())
        resp = await self._post(app, ctrl, {"type": "rotate", "x": 200, "y": 400, "degrees": 45})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "ok" and body["gesture"] == "rotate"

    @pytest.mark.parametrize("body", [
        {"type": "pinch", "x": 1, "y": 2, "scale": 1},
        {"type": "pinch", "scale": 2},
        {"type": "pan", "x": 1, "y": 2},
    ])
    async def test_a_bad_request_is_a_400(self, app, body):
        resp = await self._post(app, _ctrl(_multitouch_backend()), body)
        assert resp.status_code == 400, resp.text

    async def test_an_unsupported_backend_is_a_400(self, app):
        backend = MagicMock(spec=["TOOL_NAME"])
        backend.TOOL_NAME = "u2"
        resp = await self._post(app, _ctrl(backend), {"type": "two_finger_tap", "x": 1, "y": 2})
        assert resp.status_code == 400 and "u2 backend cannot" in resp.text

    async def test_every_argument_reaches_the_plan(self, app):
        backend = _multitouch_backend()
        resp = await self._post(app, _ctrl(backend), {
            "type": "two_finger_tap", "x": 200, "y": 400,
            "distance": 50, "count": 3, "interval": 0.2})
        assert resp.status_code == 200, resp.text
        plan = backend.perform_gesture.await_args.args[1]
        assert (plan.points, plan.count, plan.interval) == (
            [(175, 400), (225, 400)], 3, 0.2)

    async def test_a_non_finite_number_is_a_422(self, app):
        app.state.device_controller = _ctrl(_multitouch_backend())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.post(
                "/api/v1/device/ui/gesture",
                content=b'{"type": "pinch", "x": 1, "y": 2, "scale": 2, "angle": NaN}',
                headers={"Authorization": "Bearer k", "content-type": "application/json"})
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"][0]["input"] == "nan"

    async def test_a_huge_turn_is_a_422(self, app):
        body = {"type": "rotate", "x": 1, "y": 2, "degrees": 1e5}
        resp = await self._post(app, _ctrl(_multitouch_backend()), body)
        assert resp.status_code == 422

    async def test_the_schema_refuses_an_unknown_type(self, app):
        body = {"type": "wiggle", "x": 1, "y": 2}
        resp = await self._post(app, _ctrl(_multitouch_backend()), body)
        assert resp.status_code == 422


def test_the_backends_that_claim_multitouch_can_send_it():
    """A backend that gains the attribute without the method would accept a
    gesture and fail it; one that loses it refuses a gesture it could send."""
    from server.device.android.u2_client import U2Backend
    from server.device.ios.idb import IdbBackend
    from server.device.ios.wda_client import WdaBackend

    for cls in (SimBridgeBackend, WdaBackend, U2Backend):
        assert cls.multitouch is True and callable(cls.perform_gesture), cls.__name__
    assert getattr(IdbBackend, "multitouch", False) is not True


class TestW3cActions:
    """What WDA's /actions is sent: one touch source per finger, in one
    timeline. Measured on an iPhone 12 (iOS 26.5): pinch x3 read 2.65, x0.5
    read 0.54, rotate 90 read 85, -200 read -195 -- the simulator's numbers."""

    def test_a_moving_gesture(self):
        plan = gestures.plan("pinch", 200, 400, scale=2, duration=1.1)
        sources = gestures.w3c_actions(plan)
        assert [s["id"] for s in sources] == ["finger1", "finger2"]
        assert all(s["parameters"] == {"pointerType": "touch"} for s in sources)
        for source, path in zip(sources, plan.paths, strict=True):
            steps = source["actions"]
            assert steps[0] == {"type": "pointerMove", "duration": 0, "origin": "viewport",
                                "x": round(path[0][0], 2), "y": round(path[0][1], 2)}
            assert steps[1] == {"type": "pointerDown", "button": 0}
            moves = steps[2:-1]
            assert len(moves) == len(path) - 1
            assert {m["duration"] for m in moves} == {round(1100 / (len(path) - 1))}
            assert (moves[-1]["x"], moves[-1]["y"]) == (round(path[-1][0], 2),
                                                        round(path[-1][1], 2))
            assert steps[-1] == {"type": "pointerUp", "button": 0}

    def test_taps_are_timed_on_the_device(self):
        """The interval is a pause inside one action sequence, so it is the
        device's clock that keeps it, not a round trip per tap."""
        plan = gestures.plan("double_tap", 200, 400, count=3, interval=0.2)
        (source,) = gestures.w3c_actions(plan)
        kinds = [(a["type"], a.get("duration")) for a in source["actions"][1:]]
        tap = [("pointerDown", None), ("pause", gestures.TAP_HOLD_MS), ("pointerUp", None)]
        assert kinds == tap + [("pause", 200)] + tap + [("pause", 200)] + tap

    def test_a_two_finger_tap_is_two_sources(self):
        sources = gestures.w3c_actions(gestures.plan("two_finger_tap", 200, 400, distance=50))
        assert [(s["actions"][0]["x"], s["actions"][0]["y"]) for s in sources] == [
            (175, 400), (225, 400)]


class TestTheWdaBackend:
    @staticmethod
    def _backend(actions_reply=None, size=(390, 844)):
        from server.device.ios.wda_client import WdaBackend

        backend = WdaBackend()
        calls = []

        async def request(method, udid, path, **kwargs):
            calls.append((method, path, kwargs))
            if path == "/window/size":
                resp = MagicMock()
                resp.json.return_value = {"value": {"width": size[0], "height": size[1]}}
                return resp
            if isinstance(actions_reply, Exception):
                raise actions_reply
            return MagicMock()
        backend._request = request
        return backend, calls

    async def test_it_sends_the_actions_and_never_resends(self):
        backend, calls = self._backend()
        await backend.perform_gesture(UDID, gestures.plan("rotate", 195, 531, degrees=90))
        method, path, kwargs = calls[-1]
        assert (method, path) == ("post", "/actions")
        assert kwargs["raise_if_maybe_delivered"] is True     # a second turn is twice as far
        assert kwargs["use_session"] is True
        assert len(kwargs["json"]["actions"]) == 2

    async def test_a_point_off_the_screen_is_refused_before_anything_is_sent(self):
        backend, calls = self._backend()
        with pytest.raises(InvalidDeviceRequestError, match="off the 390x844 screen"):
            await backend.perform_gesture(UDID, gestures.plan("pinch", 195, 531, scale=20))
        assert [c[1] for c in calls] == ["/window/size"]

    async def test_no_answer_is_reported_not_retried(self):
        import httpx

        backend, calls = self._backend(actions_reply=httpx.ReadTimeout("slow"))
        with pytest.raises(DeviceError, match="not sent again"):
            await backend.perform_gesture(UDID, gestures.plan("double_tap", 195, 531))
        assert [c[1] for c in calls].count("/actions") == 1

    async def test_an_unreadable_window_size_is_a_failure(self):
        from server.device.ios.wda_client import WdaBackend

        backend = WdaBackend()
        resp = MagicMock()
        resp.json.return_value = {"value": None}
        backend._request = AsyncMock(return_value=resp)
        with pytest.raises(DeviceError, match="window/size"):
            await backend.perform_gesture(UDID, gestures.plan("double_tap", 195, 531))


def test_element_frames_carry_through_ui_element():
    """`gesture` centres on `frame`, as UIElement dumps it."""
    el = UIElement(type="Other", label="Map", frame={"x": 0, "y": 0, "width": 10, "height": 20})
    assert el.model_dump()["frame"] == {"x": 0, "y": 0, "width": 10, "height": 20}


def test_the_bridge_handles_what_the_client_sends():
    """No test drives the Swift side, so a renamed command or code would pass
    every test here and fail the first live gesture."""
    from pathlib import Path

    swift = (Path(__file__).resolve().parents[1] / "tools" / "sim-bridge.swift").read_text()
    for command in ("touch-paths", "multi-tap"):
        assert f'case "{command}":' in swift, command
    assert '"code": "bad_request"' in swift



class TestDeviceDefaults:
    def test_unit_scales_the_defaults_and_only_the_defaults(self):
        tft = gestures.plan("two_finger_tap", 500, 1000, unit=2.5)
        assert tft.points == [(450, 1000), (550, 1000)]           # 40pt * 2.5
        given = gestures.plan("two_finger_tap", 500, 1000, unit=2.5, distance=40)
        assert given.points == [(480, 1000), (520, 1000)]         # the caller's, as given

    def test_a_backend_pinch_default_and_axis(self):
        plan = gestures.plan("pinch", 540, 1200, scale=2, unit=2.75,
                             pinch_distance=519, pinch_angle=90)
        starts, ends = _ends(plan)
        assert _sep(starts) == pytest.approx(519) and _sep(ends) == pytest.approx(1038)
        assert all(p[0] == pytest.approx(540) for p in starts + ends)   # vertical
        across = gestures.plan("pinch", 540, 1200, scale=2, pinch_distance=519,
                               pinch_angle=90, angle=0)
        assert all(p[1] == pytest.approx(1200) for p in _ends(across)[1])  # caller's angle wins

    def test_a_pinch_axis_does_not_turn_a_rotation(self):
        plan = gestures.plan("rotate", 540, 1200, degrees=30, pinch_angle=90, distance=100)
        assert plan.paths[0][0] == pytest.approx((440, 1200))

    @pytest.mark.parametrize("unit", [0, -1, float("nan")])
    def test_a_unit_that_is_not_a_scale_is_refused(self, unit):
        with pytest.raises(InvalidDeviceRequestError, match="unit"):
            gestures.plan("double_tap", 1, 2, unit=unit)


class TestScrcpyMessages:
    def test_a_touch_message_is_scrcpys_layout(self):
        import struct

        from server.device.android import scrcpy_input as sc

        msg = sc.touch_message(sc._DOWN, 1, 100.4, 200.6, 1080, 2340, 1.0)
        assert len(msg) == 32
        assert struct.unpack(">BBqiiHHHII", msg) == (
            2, 0, sc._POINTER_BASE + 1, 100, 201, 1080, 2340, 0xFFFF, 0, 0)
        up = sc.touch_message(sc._UP, 0, 1, 2, 1080, 2340, 0.0)
        assert struct.unpack(">BBqiiHHHII", up)[7] == 0

    def test_a_moving_gesture_is_each_finger_in_turn(self):
        from server.device.android import scrcpy_input as sc

        plan = gestures.plan("pan", 500, 1000, dx=110, distance=100, duration=1.1)
        out = sc.gesture_messages(plan, 1080, 2340)
        actions = [(m[1], (int.from_bytes(m[2:10], "big", signed=True) - sc._POINTER_BASE))
                   for m, _ in out]
        n = len(plan.paths[0])
        assert actions[:2] == [(sc._DOWN, 0), (sc._DOWN, 1)]
        assert actions[2:-2] == [(sc._MOVE, f) for _ in range(n - 1) for f in (0, 1)]
        assert actions[-2:] == [(sc._UP, 0), (sc._UP, 1)]
        step = 1.1 / (n - 1)
        waits = [w for _, w in out]
        # one step's wait after each batch, before the next waypoint
        assert waits[1] == pytest.approx(step) and waits[3] == pytest.approx(step)
        assert waits[0] == waits[2] == 0
        assert sum(waits) == pytest.approx(step * (n - 1) + 0.016)

    def test_taps_hold_then_wait_the_interval(self):
        from server.device.android import scrcpy_input as sc

        out = sc.gesture_messages(gestures.plan("double_tap", 500, 1000, interval=0.2),
                                  1080, 2340)
        assert [m[1] for m, _ in out] == [sc._DOWN, sc._UP, sc._DOWN, sc._UP]
        assert [w for _, w in out] == [gestures.TAP_HOLD_MS / 1000, 0.2,
                                       gestures.TAP_HOLD_MS / 1000, 0.0]


class TestFindingScrcpy:
    def _run(self, monkeypatch, tmp_path, version_out="scrcpy 4.1 <https://...>", env=None):
        from server.device.android import scrcpy_input as sc

        cellar = tmp_path / "Cellar" / "scrcpy" / "4.1"
        (cellar / "bin").mkdir(parents=True)
        (cellar / "share" / "scrcpy").mkdir(parents=True)
        binary = cellar / "bin" / "scrcpy"
        binary.write_text("")
        (cellar / "share" / "scrcpy" / "scrcpy-server").write_text("jar")
        monkeypatch.setattr(sc.shutil, "which", lambda name: str(binary))
        monkeypatch.setattr(sc.subprocess, "run", lambda *a, **k: MagicMock(stdout=version_out))
        if env:
            monkeypatch.setenv("SCRCPY_SERVER_PATH", env)
        else:
            monkeypatch.delenv("SCRCPY_SERVER_PATH", raising=False)
        return sc.find_server(), cellar

    def test_the_jar_beside_the_binary_and_its_version(self, monkeypatch, tmp_path):
        found, cellar = self._run(monkeypatch, tmp_path)
        assert found.version == "4.1"
        assert found.jar == cellar / "share" / "scrcpy" / "scrcpy-server"

    def test_scrcpy_server_path_wins(self, monkeypatch, tmp_path):
        mine = tmp_path / "mine.jar"
        mine.write_text("jar")
        found, _ = self._run(monkeypatch, tmp_path, env=str(mine))
        assert found.jar == mine

    def test_an_unreadable_version_is_no_server(self, monkeypatch, tmp_path):
        """The server refuses any version but its own, so a jar of unknown
        version cannot be started at all."""
        found, _ = self._run(monkeypatch, tmp_path, version_out="")
        assert found is None

    def test_no_scrcpy_is_no_server(self, monkeypatch):
        from server.device.android import scrcpy_input as sc

        monkeypatch.setattr(sc.shutil, "which", lambda name: None)
        assert sc.find_server() is None


class TestScrcpySessions:
    @staticmethod
    def _input(monkeypatch, *, send_error=None):
        from server.device.android import scrcpy_input as sc

        server = sc.ScrcpyServer(Path("/x.jar"), "4.1")
        monkeypatch.setattr(sc, "find_server", lambda: server)
        started, sent = [], []

        class FakeSession:
            def __init__(self, adb, serial, srv):
                self.server = srv
                self.alive = False

            async def start(self):
                started.append(1)
                self.alive = True

            async def send(self, messages):
                if send_error:
                    raise send_error
                sent.append(len(messages))

            async def close(self):
                self.alive = False

        monkeypatch.setattr(sc, "_Session", FakeSession)
        return sc.ScrcpyInput("/usr/bin/adb"), started, sent

    async def test_one_server_per_device_kept_for_the_next_gesture(self, monkeypatch):
        touch, started, sent = self._input(monkeypatch)
        plan = gestures.plan("double_tap", 10, 10)
        await touch.perform("dev", plan, 1080, 2340)
        await touch.perform("dev", plan, 1080, 2340)
        assert started == [1] and sent == [4, 4]

    async def test_without_scrcpy_the_refusal_says_how_to_get_it(self, monkeypatch):
        from server.device.android import scrcpy_input as sc

        monkeypatch.setattr(sc, "find_server", lambda: None)
        with pytest.raises(DeviceOperationUnsupportedError, match="brew install scrcpy"):
            await sc.ScrcpyInput("/usr/bin/adb").perform(
                "dev", gestures.plan("double_tap", 1, 1), 1080, 2340)

    async def test_an_interrupted_gesture_is_not_sent_again(self, monkeypatch):
        touch, started, _ = self._input(monkeypatch, send_error=ConnectionResetError("gone"))
        with pytest.raises(DeviceError, match="not sent again"):
            await touch.perform("dev", gestures.plan("double_tap", 1, 1), 1080, 2340)
        assert started == [1]
        assert "dev" not in touch._sessions          # the next gesture starts a new server


class TestTheU2Backend:
    @staticmethod
    def _backend(size=(1080, 2340), info=None):
        from server.device.android.u2_client import U2Backend

        backend = U2Backend()
        device = MagicMock()
        device.window_size.return_value = size
        device.info = info or {"displayWidth": 1080, "displaySizeDpX": 393}
        backend._connect = lambda serial: device
        backend._touch = MagicMock()
        backend._touch.perform = AsyncMock()
        return backend

    async def test_it_hands_the_screen_size_on(self):
        backend = self._backend()
        await backend.perform_gesture("dev", gestures.plan("double_tap", 540, 1170))
        args = backend._touch.perform.await_args.args
        assert args[0] == "dev" and args[2:] == (1080, 2340)

    async def test_a_point_off_the_screen_is_refused(self):
        backend = self._backend()
        with pytest.raises(InvalidDeviceRequestError, match="off the 1080x2340 screen"):
            await backend.perform_gesture("dev", gestures.plan("double_tap", 1100, 1170))
        backend._touch.perform.assert_not_awaited()

    async def test_the_defaults_come_from_the_density(self):
        backend = self._backend()
        d = await backend.gesture_defaults("dev")
        assert d["unit"] == pytest.approx(1080 / 393)
        assert d["pinch_distance"] == pytest.approx(30 / 25.4 * 160 * 1080 / 393)
        assert d["pinch_angle"] == 90

    async def test_an_unreadable_density_leaves_the_defaults_alone(self):
        backend = self._backend(info={"displayWidth": 1080})   # no dp width
        assert await backend.gesture_defaults("dev") == {}
