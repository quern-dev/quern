"""Long press on every backend, and edge swipes (#251).

Long press is `duration` on tap and tap_element, carried to each backend's own
long-press primitive: sim-bridge's `hold` (read off the wire all along, never
sent), idb's `--duration`, WDA's touchAndHold (never re-sent), uiautomator2's
long_click. Edge swipes are `edge` on swipe: a flag on every event on the
simulator, the start position on a real device, and on every backend the
swipe must start at its edge so `edge` means the same thing on each.

Measured live against the probe apps (simulator, idb, iPhone 12, emulator,
Pixel 5): a 1s hold reads as a long press and not a tap, a 0.15s one does not;
a left-edge swipe pops a navigation stack (iOS) or is the system's back
(Android); a bottom-edge flick goes home.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from server.config import ServerConfig
from server.device import gestures
from server.device.controller import DeviceController
from server.device.controller_ui import _hold
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


def _ctrl(backend, kind=DeviceType.SIMULATOR):
    ctrl = DeviceController()
    ctrl._device_type_cache[UDID] = kind
    ctrl.resolve_udid = AsyncMock(return_value=UDID)
    ctrl._invalidate_ui_cache = MagicMock()
    ctrl._ui_backend = MagicMock(return_value=backend)
    return ctrl


class TestTheDuration:
    def test_none_is_an_ordinary_tap(self):
        assert _hold(None) == {}

    def test_a_duration_is_a_hold(self):
        assert _hold(1.0) == {"hold": 1.0}

    @pytest.mark.parametrize("bad", [0, -1, 10.5, float("nan"), float("inf")])
    def test_a_duration_that_is_not_a_press_is_refused(self, bad):
        with pytest.raises(InvalidDeviceRequestError, match="duration"):
            _hold(bad)

    async def test_it_is_refused_before_any_device(self):
        backend = MagicMock(spec=["tap", "TOOL_NAME"])
        ctrl = _ctrl(backend)
        with pytest.raises(InvalidDeviceRequestError):
            await ctrl.tap(1, 2, duration=0)
        ctrl.resolve_udid.assert_not_awaited()

    async def test_tap_passes_the_hold_only_when_given(self):
        backend = MagicMock(spec=["tap", "TOOL_NAME"])
        backend.tap = AsyncMock()
        ctrl = _ctrl(backend)
        await ctrl.tap(10, 20, duration=1.5)
        assert backend.tap.await_args.kwargs == {"hold": 1.5}
        await ctrl.tap(10, 20)
        assert backend.tap.await_args.kwargs == {}       # each backend's own tap


class TestTapElementHolds:
    async def test_the_android_selector_path(self):
        backend = MagicMock(spec=["tap_by_selector", "scroll_into_view", "TOOL_NAME"])
        backend.tap_by_selector = AsyncMock(return_value={"label": "Map"})
        ctrl = _ctrl(backend, kind=DeviceType.ANDROID_EMULATOR)
        ctrl._is_android = MagicMock(return_value=True)
        await ctrl.tap_element(identifier="map", duration=1.0)
        assert backend.tap_by_selector.await_args.kwargs["hold"] == 1.0

    async def test_the_tree_path(self):
        backend = MagicMock(spec=["tap", "TOOL_NAME"])
        backend.tap = AsyncMock()
        ctrl = _ctrl(backend)
        ctrl._is_android = MagicMock(return_value=False)
        el = UIElement(type="Button", label="Map", identifier="map",
                       frame={"x": 0, "y": 0, "width": 100, "height": 40})
        ctrl.get_ui_elements = AsyncMock(return_value=([el], UDID))
        await ctrl.tap_element(identifier="map", duration=2.0, skip_stability_check=True)
        assert backend.tap.await_args.kwargs == {"hold": 2.0}


class TestEachBackendsLongPress:
    async def test_sim_bridge_sends_hold_only_when_given(self):
        mgr = SimBridgeManager()
        sent = []

        async def send(cmd):
            sent.append(cmd)
            return {"ok": True}
        mgr.send = AsyncMock(side_effect=send)  # type: ignore[method-assign]
        backend = SimBridgeBackend(mgr)
        await backend.tap(UDID, 1, 2, hold=1.0)
        await backend.tap(UDID, 1, 2)
        assert sent[0]["hold"] == 1.0 and "hold" not in sent[1]

    async def test_idb_passes_it_as_the_press_duration(self):
        from server.device.ios.idb import IdbBackend

        backend = IdbBackend()
        backend._run = AsyncMock(return_value="")
        await backend.tap(UDID, 1.4, 2.6, hold=1.25)
        args = backend._run.await_args.args
        assert args[args.index("--duration") + 1] == "1.25"
        await backend.tap(UDID, 1, 2)
        args = backend._run.await_args.args
        assert args[args.index("--duration") + 1] == "0.05"   # its ordinary tap

    @staticmethod
    def _wda(error=None):
        from server.device.ios.wda_client import WdaBackend

        backend = WdaBackend()
        calls = []

        async def request(method, udid, path, **kwargs):
            calls.append((path, kwargs))
            if path == "/window/size":
                resp = MagicMock()
                resp.json.return_value = {"value": {"width": 390, "height": 844}}
                return resp
            if error:
                raise error
            return MagicMock()
        backend._request = request
        return backend, calls

    async def test_wda_holds_through_touch_and_hold_never_resent(self):
        from server.device.ios.wda_client import ACTION_TIMEOUT

        backend, calls = self._wda()
        await backend.tap(UDID, 10, 20, hold=1.5)
        path, kwargs = calls[-1]
        assert path == "/wda/touchAndHold"
        assert kwargs["json"] == {"x": 10, "y": 20, "duration": 1.5}
        assert kwargs["raise_if_maybe_delivered"] is True
        assert kwargs["timeout"] == pytest.approx(ACTION_TIMEOUT + 1.5)
        await backend.tap(UDID, 10, 20)
        assert calls[-1][0] == "/wda/tap"

    async def test_wda_reports_an_unanswered_long_press_instead_of_resending(self):
        backend, calls = self._wda(error=httpx.ReadTimeout("slow"))
        with pytest.raises(DeviceError, match="not sent again"):
            await backend.tap(UDID, 10, 20, hold=1.0)
        assert [c[0] for c in calls].count("/wda/touchAndHold") == 1

    @staticmethod
    def _u2(device):
        from server.device.android.u2_client import U2Backend

        backend = U2Backend()
        backend._connect = lambda serial: device
        return backend

    async def test_u2_long_clicks(self):
        device = MagicMock()
        backend = self._u2(device)
        await backend.tap("dev", 10.6, 20.2, hold=1.0)
        device.long_click.assert_called_once_with(10, 20, 1.0)
        await backend.tap("dev", 10, 20)
        device.click.assert_called_once()

    async def test_u2_selector_long_clicks(self):
        obj = MagicMock()
        obj.exists = True
        obj.info = {"bounds": {"left": 0, "right": 100, "top": 0, "bottom": 50}}
        device = MagicMock(return_value=obj)
        backend = self._u2(device)
        await backend.tap_by_selector("dev", identifier="map", hold=1.0)
        obj.long_click.assert_called_once_with(1.0)
        obj.click.assert_not_called()


class TestEdgeStart:
    @pytest.mark.parametrize("edge,x,y,ok", [
        ("left", 1, 500, True), ("left", 30, 500, False),
        ("right", 399, 500, True), ("right", 370, 500, False),
        ("top", 200, 2, True), ("top", 200, 40, False),
        ("bottom", 200, 873, True), ("bottom", 200, 820, False),
    ])
    def test_it_must_start_at_its_edge(self, edge, x, y, ok):
        if ok:
            gestures.check_edge_start(edge, x, y, 402, 874, tool="t")
        else:
            with pytest.raises(InvalidDeviceRequestError, match=f"from the {edge}"):
                gestures.check_edge_start(edge, x, y, 402, 874, tool="t")

    def test_an_unknown_edge_is_refused(self):
        with pytest.raises(InvalidDeviceRequestError, match="edge must be one of"):
            gestures.check_edge("sideways")

    def test_the_bridge_uses_the_same_margin(self):
        """sim-bridge checks the start itself; the two numbers must agree."""
        from pathlib import Path

        swift = (Path(__file__).resolve().parents[1] / "tools" / "sim-bridge.swift").read_text()
        assert f"m = {gestures.EDGE_MARGIN}" in swift


class TestEdgeSwipesInTheController:
    async def test_a_bad_edge_is_refused_before_any_device(self):
        ctrl = _ctrl(MagicMock(spec=["swipe", "TOOL_NAME", "edge_swipes"]))
        with pytest.raises(InvalidDeviceRequestError):
            await ctrl.swipe(0, 0, 10, 10, edge="sideways")
        ctrl.resolve_udid.assert_not_awaited()

    async def test_a_backend_without_edge_swipes_refuses(self):
        backend = MagicMock(spec=["swipe", "TOOL_NAME"])
        backend.TOOL_NAME = "idb"
        backend.swipe = AsyncMock()
        with pytest.raises(DeviceOperationUnsupportedError, match="idb backend cannot"):
            await _ctrl(backend).swipe(1, 500, 300, 500, edge="left")
        backend.swipe.assert_not_awaited()

    async def test_the_edge_reaches_the_backend_only_when_given(self):
        backend = MagicMock(spec=["swipe", "TOOL_NAME", "edge_swipes", "edge_flag"])
        backend.edge_swipes = backend.edge_flag = True
        backend.swipe = AsyncMock()
        ctrl = _ctrl(backend)
        await ctrl.swipe(1, 500, 300, 500, edge="left")
        assert backend.swipe.await_args.kwargs == {"edge": "left"}
        await ctrl.swipe(1, 500, 300, 500)
        assert backend.swipe.await_args.kwargs == {}      # idb's swipe takes no edge


class TestEdgeSwipesPerBackend:
    async def test_sim_bridge_sends_the_edge(self):
        mgr = SimBridgeManager()
        sent = []

        async def send(cmd):
            sent.append(cmd)
            return {"ok": True}
        mgr.send = AsyncMock(side_effect=send)  # type: ignore[method-assign]
        backend = SimBridgeBackend(mgr)
        await backend.swipe(UDID, 1, 500, 300, 500, 0.3, edge="left")
        await backend.swipe(UDID, 1, 500, 300, 500, 0.3)
        assert sent[0]["edge"] == "left" and "edge" not in sent[1]

    async def test_sim_bridge_refusal_is_a_bad_request(self):
        mgr = SimBridgeManager()
        mgr.send = AsyncMock(return_value={"ok": False, "code": "bad_request",  # type: ignore
                                           "error": "an edge swipe from the left must start"})
        with pytest.raises(InvalidDeviceRequestError):
            await SimBridgeBackend(mgr).swipe(UDID, 100, 500, 300, 500, 0.3, edge="left")

    async def test_the_bridge_marks_every_event_with_the_edge(self):
        """Down, each move, the hold and the lift: an edge on touch-down only
        reads as an ordinary drag from the second sample on."""
        from pathlib import Path

        swift = (Path(__file__).resolve().parents[1] / "tools" / "sim-bridge.swift").read_text()
        start = swift.index("func doSwipe(")
        body = swift[start:swift.index("\n}\n", start)]
        assert body.count("sendDigitizerEvent(") == body.count("edgeBit: edge") == 4
        for name, bit in (("left", "0x02"), ("right", "0x04"), ("top", "0x08"),
                          ("bottom", "0x01")):
            assert f'"{name}": {bit}' in swift

    async def test_wda_checks_the_start_against_the_screen(self):
        backend, calls = TestEachBackendsLongPress._wda()
        with pytest.raises(InvalidDeviceRequestError, match="390x844"):
            await backend.swipe(UDID, 100, 500, 300, 500, 0.3, edge="left")
        assert [c[0] for c in calls] == ["/window/size"]
        await backend.swipe(UDID, 1, 500, 300, 500, 0.3, edge="left")
        assert calls[-1][0] == "/wda/dragfromtoforduration"

    async def test_u2_checks_the_start_against_the_screen(self):
        device = MagicMock()
        device.window_size.return_value = (1080, 2340)
        backend = TestEachBackendsLongPress._u2(device)
        with pytest.raises(InvalidDeviceRequestError):            # not "Swipe failed"
            await backend.swipe("dev", 200, 1300, 600, 1300, 0.3, edge="left")
        device.swipe.assert_not_called()
        await backend.swipe("dev", 1, 1300, 600, 1300, 0.3, edge="left")
        device.swipe.assert_called_once()

    def test_which_backends_claim_edge_swipes(self):
        from server.device.android.u2_client import U2Backend
        from server.device.ios.idb import IdbBackend
        from server.device.ios.wda_client import WdaBackend

        for cls in (SimBridgeBackend, WdaBackend, U2Backend):
            assert cls.edge_swipes is True, cls.__name__
        assert getattr(IdbBackend, "edge_swipes", False) is not True


class TestTheRoutes:
    @pytest.fixture
    def app(self):
        return create_app(config=ServerConfig(api_key="k"), enable_oslog=False,
                          enable_crash=False, enable_proxy=False)

    async def _post(self, app, ctrl, path, body):
        app.state.device_controller = ctrl
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            return await c.post(path, json=body, headers={"Authorization": "Bearer k"})

    async def test_a_held_tap(self, app):
        backend = MagicMock(spec=["tap", "TOOL_NAME"])
        backend.tap = AsyncMock()
        resp = await self._post(app, _ctrl(backend), "/api/v1/device/ui/tap",
                                {"x": 1, "y": 2, "duration": 1.2})
        assert resp.status_code == 200 and resp.json()["duration"] == 1.2
        assert backend.tap.await_args.kwargs == {"hold": 1.2}

    @pytest.mark.parametrize("duration", [0, 11])
    async def test_a_duration_out_of_range_is_a_422(self, app, duration):
        resp = await self._post(app, _ctrl(MagicMock()), "/api/v1/device/ui/tap",
                                {"x": 1, "y": 2, "duration": duration})
        assert resp.status_code == 422

    async def test_an_edge_swipe(self, app):
        backend = MagicMock(spec=["swipe", "TOOL_NAME", "edge_swipes", "edge_flag"])
        backend.edge_swipes, backend.edge_flag, backend.swipe = True, True, AsyncMock()
        resp = await self._post(app, _ctrl(backend), "/api/v1/device/ui/swipe",
                                {"start_x": 1, "start_y": 500, "end_x": 300, "end_y": 500,
                                 "edge": "left"})
        assert resp.status_code == 200 and resp.json()["edge"] == "left"

    async def test_an_unknown_edge_is_a_422(self, app):
        resp = await self._post(app, _ctrl(MagicMock()), "/api/v1/device/ui/swipe",
                                {"start_x": 1, "start_y": 1, "end_x": 2, "end_y": 2,
                                 "edge": "sideways"})
        assert resp.status_code == 422

    async def test_an_edge_on_idb_is_a_400(self, app):
        backend = MagicMock(spec=["swipe", "TOOL_NAME"])
        backend.TOOL_NAME = "idb"
        resp = await self._post(app, _ctrl(backend), "/api/v1/device/ui/swipe",
                                {"start_x": 1, "start_y": 500, "end_x": 300, "end_y": 500,
                                 "edge": "left"})
        assert resp.status_code == 400 and "idb backend cannot" in resp.text




# ---------------------------------------------------------------------------
# What the review's mutants showed was untested (#251, second round)
# ---------------------------------------------------------------------------


class TestEveryTapElementPathHolds:
    async def test_a_web_element(self):
        backend = MagicMock(spec=["tap", "TOOL_NAME"])
        backend.tap = AsyncMock()
        ctrl = _ctrl(backend)
        ctrl._is_android = MagicMock(return_value=False)
        el = UIElement(type="Link", label="Buy", identifier=None,
                       frame={"x": 0, "y": 0, "width": 100, "height": 40},
                       extra_attrs={"source": "web-inspector"})
        ctrl.get_ui_elements = AsyncMock(return_value=([el], UDID))
        ctrl._web_element_still_there = AsyncMock(return_value=True)
        await ctrl.tap_element(label="Buy", duration=1.5, skip_stability_check=True)
        assert backend.tap.await_args.kwargs == {"hold": 1.5}

    async def test_the_android_tap_after_scrolling_to_it(self):
        """An element off screen: scrolled to, then tapped -- and that tap held
        too, not an ordinary click that answered ok (review)."""
        backend = MagicMock(spec=["tap_by_selector", "scroll_into_view", "TOOL_NAME"])
        backend.tap_by_selector = AsyncMock(side_effect=[None, {"label": "Map"}])
        backend.scroll_into_view = AsyncMock(return_value={"label": "Map"})
        ctrl = _ctrl(backend, kind=DeviceType.ANDROID_EMULATOR)
        ctrl._is_android = MagicMock(return_value=True)
        await ctrl.tap_element(identifier="map", duration=1.0)
        assert [c.kwargs.get("hold") for c in backend.tap_by_selector.await_args_list] == [
            1.0, 1.0]

    async def test_a_bad_duration_is_refused_before_any_device(self):
        ctrl = _ctrl(MagicMock(spec=["tap", "TOOL_NAME"]))
        with pytest.raises(InvalidDeviceRequestError, match="duration"):
            await ctrl.tap_element(identifier="map", duration=0)
        ctrl.resolve_udid.assert_not_awaited()


class TestTheTapElementRoute:
    @pytest.fixture
    def app(self):
        return create_app(config=ServerConfig(api_key="k"), enable_oslog=False,
                          enable_crash=False, enable_proxy=False)

    async def test_the_duration_reaches_the_controller_and_is_said_back(self, app):
        ctrl = DeviceController()
        ctrl.resolve_udid = AsyncMock(return_value=UDID)
        ctrl.tap_element = AsyncMock(return_value={"status": "ok", "tapped": {"label": "Map"}})
        ctrl.backend_that_served = MagicMock(return_value="sim-bridge")
        app.state.device_controller = ctrl
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.post("/api/v1/device/ui/tap-element",
                                json={"identifier": "map", "duration": 1.25},
                                headers={"Authorization": "Bearer k"})
        assert resp.status_code == 200, resp.text
        assert ctrl.tap_element.await_args.kwargs["duration"] == 1.25
        assert resp.json()["duration"] == 1.25


class TestEdgeBoundariesAndAxes:
    @pytest.mark.parametrize("edge,x,y", [
        ("left", 1080 * 0.03, 1000), ("right", 1080 * 0.97, 1000),
        ("top", 500, 2340 * 0.03), ("bottom", 500, 2340 * 0.97),
    ])
    def test_exactly_at_the_margin_counts(self, edge, x, y):
        gestures.check_edge_start(edge, x, y, 1080, 2340, tool="t")

    async def test_u2_checks_the_right_edge_against_the_width(self):
        """Width and height swapped passed every other test (review)."""
        device = MagicMock()
        device.window_size.return_value = (1080, 2340)
        backend = TestEachBackendsLongPress._u2(device)
        await backend.swipe("dev", 1070, 1300, 600, 1300, 0.3, edge="right")
        device.swipe.assert_called_once()


class TestU2NeverPressesTwice:
    @staticmethod
    def _selector(**obj_kwargs):
        obj = MagicMock(**obj_kwargs)
        obj.exists = True
        obj.info = {"bounds": {"left": 0, "right": 100, "top": 0, "bottom": 50}}
        return TestEachBackendsLongPress._u2(MagicMock(return_value=obj)), obj

    async def test_a_press_that_failed_after_it_was_sent_is_not_retried(self):
        """tap_element would fall through to the tree path and press again:
        a context menu opened twice (#407)."""
        backend, obj = self._selector()
        obj.long_click.side_effect = TimeoutError("jsonrpc read timed out")
        with pytest.raises(DeviceError, match="not sent again"):
            await backend.tap_by_selector("dev", identifier="map", label="Map", hold=1.0)
        # Once: neither the next selector nor a retry pressed again. Every call
        # raises the same error, so the message alone would not tell (review).
        obj.long_click.assert_called_once_with(1.0)

    async def test_a_lookup_that_failed_still_falls_back(self):
        from server.device.android.u2_client import U2Backend

        backend = U2Backend()

        def broken(serial):
            raise ConnectionError("uiautomator not up")
        backend._connect = broken
        assert await backend.tap_by_selector("dev", identifier="map", hold=1.0) is None


class TestSimulatorEdgeSwipesNeedTheFlag:
    @staticmethod
    def _backend(*, flag):
        spec = ["swipe", "TOOL_NAME", "edge_swipes"] + (["edge_flag"] if flag else [])
        backend = MagicMock(spec=spec)
        backend.TOOL_NAME, backend.edge_swipes = ("sim-bridge" if flag else "wda"), True
        if flag:
            backend.edge_flag = True
        backend.swipe = AsyncMock()
        return backend

    async def test_wda_on_a_simulator_refuses(self):
        """It would drag from the edge, answer ok, and nothing would happen."""
        backend = self._backend(flag=False)
        with pytest.raises(DeviceOperationUnsupportedError, match="needs sim-bridge"):
            await _ctrl(backend, kind=DeviceType.SIMULATOR).swipe(
                1, 500, 300, 500, edge="left")
        backend.swipe.assert_not_awaited()

    async def test_wda_on_a_phone_needs_no_flag(self):
        backend = self._backend(flag=False)
        await _ctrl(backend, kind=DeviceType.DEVICE).swipe(1, 500, 300, 500, edge="left")
        backend.swipe.assert_awaited_once()

    async def test_sim_bridge_on_a_simulator_sends_it(self):
        backend = self._backend(flag=True)
        await _ctrl(backend, kind=DeviceType.SIMULATOR).swipe(1, 500, 300, 500, edge="left")
        backend.swipe.assert_awaited_once()

    def test_the_bridge_refuses_rather_than_skips_without_a_device(self):
        from pathlib import Path

        swift = (Path(__file__).resolve().parents[1] / "tools" / "sim-bridge.swift").read_text()
        start = swift.index('case "swipe":')
        block = swift[start:swift.index("case ", start + 10)]
        assert "guard let device = resolveDevice(udid: udid) else {" in block
        assert "if let device = resolveDevice" not in block
