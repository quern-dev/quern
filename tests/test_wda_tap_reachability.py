"""Through WDA, tap only what a tap can reach (F32).

WDA reports every cell of a table, on screen or not, and elements under the
keyboard. quern tapped their coordinates and answered `ok`: measured, `row_40`
"tapped" at y=1882 on an 874-point screen with nothing moving, and on an
iPhone 11 a tab under the keyboard tapped the keyboard -- once its Dictate key.
The rule, from bajutsu PR #2119: centre on screen, and XCUITest says hittable.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from server.device.controller import DeviceController
from server.models import UIElement

SCREEN = {"x": 0.0, "y": 0.0, "width": 414.0, "height": 896.0}


def _el(identifier: str, y: float, label: str = "") -> UIElement:
    return UIElement(
        type="Button", label=label, identifier=identifier,
        frame={"x": 100.0, "y": y, "width": 80.0, "height": 40.0},
    )


@pytest.fixture(autouse=True)
def _no_screenshot(monkeypatch):
    """The not-found path captures a screenshot; never from a real device."""
    monkeypatch.setattr(
        "server.device.controller_ui._capture_screenshot", AsyncMock(return_value=None),
    )


def _controller(found: list[UIElement], hittable=True) -> tuple[DeviceController, MagicMock]:
    ctrl = DeviceController()
    ctrl.resolve_udid = AsyncMock(return_value="PHONE")
    ctrl._warn_if_input_is_suppressed = AsyncMock()
    ctrl._is_android = lambda udid: False
    ctrl._served_by_wda = lambda udid: True

    async def elements(udid=None, *args, **kwargs):
        if kwargs.get("filter_type") == "Application":
            return [UIElement(type="Application", label="App", frame=SCREEN)], "PHONE"
        return list(found), "PHONE"

    ctrl.get_ui_elements = AsyncMock(side_effect=elements)
    ctrl._all_elements_for_context = AsyncMock(return_value=(list(found), True))
    ctrl._identify_for_miss = AsyncMock(return_value={})
    ctrl.wda_client.is_hittable = AsyncMock(return_value=hittable)
    backend = MagicMock()
    backend.tap = AsyncMock()
    ctrl._ui_backend = lambda udid: backend
    return ctrl, backend


class TestTheSplit:
    async def test_on_screen_and_hittable_is_kept(self):
        ctrl, _ = _controller([])
        kept, dropped = await ctrl._wda_reachable_only("PHONE", [_el("a", 100)])
        assert [k.identifier for k in kept] == ["a"] and dropped == []

    async def test_off_screen_is_dropped_without_asking_hittable(self):
        ctrl, _ = _controller([])
        kept, dropped = await ctrl._wda_reachable_only("PHONE", [_el("row_40", 1860)])
        assert kept == [] and dropped[0]["reason"] == "off_screen"
        ctrl.wda_client.is_hittable.assert_not_awaited()

    async def test_covered_is_dropped(self):
        ctrl, _ = _controller([], hittable=False)
        kept, dropped = await ctrl._wda_reachable_only("PHONE", [_el("tab_controls", 800)])
        assert kept == [] and dropped[0]["reason"] == "not_hittable"

    async def test_an_unanswerable_hittable_question_keeps_the_match(self):
        """None is "could not ask", not "covered" -- refusing every tap when a
        second query fails would make WDA unusable."""
        ctrl, _ = _controller([], hittable=None)
        kept, dropped = await ctrl._wda_reachable_only("PHONE", [_el("a", 100)])
        assert [k.identifier for k in kept] == ["a"] and dropped == []


class TestTapElementRefusesWhatItCannotReach:
    @pytest.mark.parametrize("y, hittable, reason", [
        (1860, True, "off_screen"),
        (800, False, "not_hittable"),
    ])
    async def test_no_tap_and_a_not_found_that_says_why(self, y, hittable, reason):
        ctrl, backend = _controller([_el("target", y)], hittable=hittable)
        result = await ctrl.tap_element(
            identifier="target", udid="PHONE", scroll_to_find=False,
            skip_stability_check=True,
        )
        backend.tap.assert_not_awaited()
        assert result["status"] == "not_found", result
        assert result["unreachable"][0]["reason"] == reason
        assert "not where a tap can reach it" in result["detail"]

    async def test_a_reachable_target_is_tapped(self):
        ctrl, backend = _controller([_el("target", 100)])
        result = await ctrl.tap_element(
            identifier="target", udid="PHONE", scroll_to_find=False,
            skip_stability_check=True,
        )
        assert result["status"] == "ok", result
        backend.tap.assert_awaited_once()

    async def test_off_wda_nothing_changes(self):
        """The accessibility tree never lists off-screen rows; the rule is
        WDA's alone, and must not cost the other backends a query."""
        ctrl, backend = _controller([_el("target", 1860)])
        ctrl._served_by_wda = lambda udid: False
        await ctrl.tap_element(
            identifier="target", udid="PHONE", scroll_to_find=False,
            skip_stability_check=True,
        )
        ctrl.wda_client.is_hittable.assert_not_awaited()


class TestIsHittable:
    async def _client(self, candidates, attribute):
        from server.device.wda_client import WdaBackend

        client = WdaBackend.__new__(WdaBackend)
        client.find_elements_by_query = AsyncMock(return_value=candidates)
        response = MagicMock()
        response.json = MagicMock(return_value={"value": attribute})
        client._request = AsyncMock(return_value=response)
        return client

    async def test_asks_about_the_candidate_at_the_point(self):
        here = {"frame": {"x": 0, "y": 0, "width": 10, "height": 10}, "_wda_element_id": "near"}
        there = {"frame": {"x": 0, "y": 500, "width": 10, "height": 10}, "_wda_element_id": "far"}
        client = await self._client([there, here], True)
        assert await client.is_hittable("U", identifier="x", label=None, center=(5, 5)) is True
        path = client._request.call_args[0][2]
        assert "/element/near/attribute/hittable" in path

    async def test_a_string_answer_is_understood(self):
        here = {"frame": {"x": 0, "y": 0, "width": 10, "height": 10}, "_wda_element_id": "e"}
        client = await self._client([here], "false")
        assert await client.is_hittable("U", identifier="x", label=None, center=(5, 5)) is False

    async def test_nothing_at_the_point_is_unknown(self):
        there = {"frame": {"x": 0, "y": 500, "width": 10, "height": 10}, "_wda_element_id": "far"}
        client = await self._client([there], True)
        assert await client.is_hittable("U", identifier="x", label=None, center=(5, 5)) is None

    async def test_no_selector_is_unknown(self):
        client = await self._client([], True)
        assert await client.is_hittable("U", identifier=None, label=None, center=(5, 5)) is None


class TestClearTargetsTheFieldItWasGiven:
    """F33: WDA's clear used to take the *first* field of the class and ignore
    the coordinates, so clearing field_email emptied field_default."""

    def _client(self, by_query: dict):
        from server.device.wda_client import WdaBackend

        client = WdaBackend.__new__(WdaBackend)

        async def query(udid, using, value, **kw):
            return by_query.get((using, value), [])

        client.find_elements_by_query = AsyncMock(side_effect=query)
        client._request = AsyncMock()
        client.tap = AsyncMock()
        return client

    @staticmethod
    def _field(eid: str, y: float) -> dict:
        return {"frame": {"x": 0, "y": y, "width": 100, "height": 40}, "_wda_element_id": eid}

    def _cleared(self, client) -> list[str]:
        return [c[0][2] for c in client._request.call_args_list if c[0][2].endswith("/clear")]

    async def test_by_identifier(self):
        client = self._client({("accessibility id", "field_email"): [self._field("email", 200)]})
        await client.select_all_and_delete("U", 50, 220, "TextField", identifier="field_email")
        assert self._cleared(client) == ["/element/email/clear"]

    async def test_by_class_it_is_the_field_at_the_point_not_the_first(self):
        fields = [self._field("default", 100), self._field("email", 200)]
        client = self._client({("class name", "XCUIElementTypeTextField"): fields})
        await client.select_all_and_delete("U", 50, 220, "TextField")
        assert self._cleared(client) == ["/element/email/clear"]

    async def test_nothing_at_the_point_falls_back_to_tapping_there(self):
        fields = [self._field("default", 100)]
        client = self._client({("class name", "XCUIElementTypeTextField"): fields})
        await client.select_all_and_delete("U", 50, 220, "TextField")
        assert self._cleared(client) == []
        assert all(c[0][1:] == (50, 220) for c in client.tap.call_args_list)
        assert client.tap.await_count == 3


class TestWdaNotSetUpReachesTheCaller:
    """F34: it was a RuntimeError that left launch_app as a bare 500."""

    async def test_launch_app_answers_400_with_the_remedy(self):
        from httpx import ASGITransport, AsyncClient

        from server.config import ServerConfig
        from server.main import create_app
        from server.models import WdaNotSetUpError

        app = create_app(
            config=ServerConfig(api_key="k"),
            enable_oslog=False, enable_crash=False, enable_proxy=False,
        )
        ctrl = MagicMock()
        ctrl.resolve_udid = AsyncMock(return_value="PHONE")
        ctrl.launch_app = AsyncMock(side_effect=WdaNotSetUpError(
            "WebDriverAgent is not set up ... Run the setup_wda tool", tool="wda",
        ))
        app.state.device_controller = ctrl
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                "/api/v1/device/app/launch",
                json={"bundle_id": "com.example", "udid": "PHONE"},
                headers={"Authorization": "Bearer k"},
            )
        assert r.status_code == 400, r.text
        assert "setup_wda" in r.text


async def test_clear_text_hands_the_backend_the_fields_identifier():
    """The backend can only clear the right field if it is told which one."""
    ctrl = DeviceController()
    ctrl.resolve_udid = AsyncMock(return_value="PHONE")
    ctrl._warn_if_input_is_suppressed = AsyncMock()
    ctrl._is_android = lambda udid: False
    fields = [
        UIElement(type="TextField", identifier="field_default",
                  frame={"x": 0, "y": 100, "width": 100, "height": 40}),
        UIElement(type="TextField", identifier="field_email",
                  frame={"x": 0, "y": 200, "width": 100, "height": 40}),
    ]
    ctrl.get_ui_elements = AsyncMock(return_value=(fields, "PHONE"))
    backend = MagicMock()
    backend.select_all_and_delete = AsyncMock()
    ctrl._ui_backend = lambda udid: backend
    await ctrl.clear_text(identifier="field_email", udid="PHONE")
    kwargs = backend.select_all_and_delete.call_args.kwargs
    assert kwargs["identifier"] == "field_email"
    assert (kwargs["x"], kwargs["y"]) == (50, 220)


class TestReadsMadeForTheCallerAreShallowThroughWda:
    """F35: the full WDA walk of a 200-row table took 34.5s on an iPhone 11;
    at depth 12 it took 3.9s and still held everything a tap needs."""

    def test_default_through_wda_is_the_action_depth(self):
        from server.device.wda_client import ACTION_SNAPSHOT_DEPTH

        ctrl = DeviceController()
        ctrl._served_by_wda = lambda udid: True
        assert ctrl._read_depth("PHONE", None) == ACTION_SNAPSHOT_DEPTH == 12

    def test_the_callers_depth_wins(self):
        ctrl = DeviceController()
        ctrl._served_by_wda = lambda udid: True
        assert ctrl._read_depth("PHONE", 30) == 30

    def test_off_wda_there_is_no_depth(self):
        ctrl = DeviceController()
        ctrl._served_by_wda = lambda udid: False
        assert ctrl._read_depth("SIM", None) is None

    async def test_tap_element_reads_at_it_and_a_miss_says_so(self):
        ctrl, backend = _controller([])
        await_result = await ctrl.tap_element(
            identifier="deep_thing", udid="PHONE", scroll_to_find=False,
            skip_stability_check=True,
        )
        depths = [c.kwargs.get("snapshot_depth") for c in ctrl.get_ui_elements.await_args_list
                  if c.kwargs.get("filter_type") != "Application"]
        assert depths and all(d == 12 for d in depths), depths
        assert await_result["snapshot_depth"] == 12
        assert "retry with a larger snapshot_depth" in await_result["detail"]

    async def test_the_route_passes_the_callers_depth(self):
        from httpx import ASGITransport, AsyncClient

        from server.config import ServerConfig
        from server.main import create_app

        app = create_app(
            config=ServerConfig(api_key="k"),
            enable_oslog=False, enable_crash=False, enable_proxy=False,
        )
        ctrl = MagicMock(spec=DeviceController)
        ctrl.resolve_udid = AsyncMock(return_value="PHONE")
        ctrl.tap_element = AsyncMock(return_value={"status": "ok", "tapped": {}})
        ctrl.backend_that_served = MagicMock(return_value="wda")
        app.state.device_controller = ctrl
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                "/api/v1/device/ui/tap-element",
                json={"identifier": "x", "udid": "PHONE", "snapshot_depth": 30},
                headers={"Authorization": "Bearer k"},
            )
        assert r.status_code == 200, r.text
        assert ctrl.tap_element.call_args.kwargs["snapshot_depth"] == 30
