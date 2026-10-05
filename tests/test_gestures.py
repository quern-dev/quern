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
    backend = MagicMock()
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


def test_only_sim_bridge_claims_multitouch():
    """A backend that gains the attribute without the method would accept a
    gesture and fail it; one that loses it refuses a gesture it could send."""
    from server.device.android.u2_client import U2Backend
    from server.device.ios.idb import IdbBackend
    from server.device.ios.wda_client import WdaBackend

    assert SimBridgeBackend.multitouch is True
    assert callable(SimBridgeBackend.perform_gesture)
    for cls in (IdbBackend, WdaBackend, U2Backend):
        assert getattr(cls, "multitouch", False) is not True, cls.__name__


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
