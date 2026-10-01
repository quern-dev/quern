"""WDA on simulators, and XCUITest's vocabulary carried through (#336).

Simulators are read through the accessibility tree by default, and XCUITest
classifies some elements differently -- a tab-bar item the accessibility tree
calls RadioButton is XCUIElementTypeButton to XCUITest. `start_driver` on a
simulator serves its UI through WebDriverAgent until `stop_driver`, and each
element carries `xcui_type`, XCUITest's own type.

Two of these tests exist because live runs found what the unit tests missed:
WDA's JSON source sends the *short* type name, so keeping "the original" kept
`Button`, not `XCUIElementTypeButton`; and the client read a connection entry a
simulator never got, which raised KeyError on the first real read.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from server.device import wda
from server.device.ui_elements import parse_elements
from server.device.wda_client import (
    WdaBackend,
    _map_wda_element,
    _map_wda_element_from_query,
)

SIM = "66EF8B35-4384-447E-84E8-4951BA26B181"


class TestXcuiTypeIsXCUITestsName:
    def test_the_short_name_json_source_sends_is_expanded(self):
        """Measured against a running WDA: not one type in a JSON /source
        carried the prefix."""
        el = _map_wda_element({"type": "Button", "label": "More"})
        assert el["type"] == "Button"
        assert el["xcui_type"] == "XCUIElementTypeButton"

    def test_the_class_name_an_element_query_sends_is_kept(self):
        el = _map_wda_element_from_query({"label": "x"}, "XCUIElementTypeTabBar")
        assert el["type"] == "TabBar"
        assert el["xcui_type"] == "XCUIElementTypeTabBar"

    def test_a_prefixed_source_type_is_not_doubled(self):
        el = _map_wda_element({"type": "XCUIElementTypeButton"})
        assert el["type"] == "Button"
        assert el["xcui_type"] == "XCUIElementTypeButton"

    def test_no_type_means_no_xcui_type(self):
        assert _map_wda_element({})["xcui_type"] is None

    def test_it_reaches_the_element_model(self):
        (el,) = parse_elements([_map_wda_element({"type": "Button", "label": "Home"})])
        assert el.xcui_type == "XCUIElementTypeButton"

    def test_the_accessibility_tree_has_none(self):
        """sim-bridge and idb never see XCUITest's vocabulary."""
        (el,) = parse_elements([{"type": "RadioButton", "AXLabel": "Home"}])
        assert el.type == "RadioButton"
        assert el.xcui_type is None


class TestTheClientReachesASimulatorOnItsOwnPort:
    async def test_it_uses_the_registered_port_and_records_a_connection(self):
        """The connection entry is what several methods read directly. Without
        it the first live read raised KeyError."""
        w = WdaBackend()
        w.register_simulator(SIM, 8200)
        assert await w._get_base_url(SIM) == "http://127.0.0.1:8200"
        assert w._connections[SIM].base_url == "http://127.0.0.1:8200"

    async def test_it_never_reaches_for_tunnels_or_auto_start(self):
        w = WdaBackend()
        w.register_simulator(SIM, 8200)
        w._try_tunneld_connection = AsyncMock(side_effect=AssertionError("tunnel"))
        w._start_usbmux_forward = AsyncMock(side_effect=AssertionError("usbmux"))
        await w._get_base_url(SIM)

    async def test_a_session_survives_repeated_lookups(self):
        """Re-creating the connection on every call would drop the session."""
        w = WdaBackend()
        w.register_simulator(SIM, 8200)
        await w._get_base_url(SIM)
        conn = w._connections[SIM]
        await w._get_base_url(SIM)
        assert w._connections[SIM] is conn

    def test_unregistering_forgets_it(self):
        w = WdaBackend()
        w.register_simulator(SIM, 8200)
        w.unregister_simulator(SIM)
        assert not w.serves_simulator(SIM)
        assert SIM not in w._connections


class TestRouting:
    @pytest.fixture
    def controller(self, monkeypatch):
        from server.device.controller import DeviceController

        c = DeviceController()
        monkeypatch.setattr(c, "_is_android", lambda udid: False)
        monkeypatch.setattr(c, "_is_physical", lambda udid: False)
        c._sim_bridge_ok = True
        return c

    def test_a_simulator_defaults_to_the_accessibility_tree(self, controller):
        assert controller._ui_backend(SIM) is controller.sim_bridge
        assert not controller._served_by_wda(SIM)

    def test_wda_mode_routes_it_to_wda(self, controller):
        controller.wda_client.register_simulator(SIM, 8200)
        assert controller._ui_backend(SIM) is controller.wda_client
        assert controller._served_by_wda(SIM)
        assert controller._backend_name(SIM) == "wda"

    def test_other_simulators_are_unaffected(self, controller):
        controller.wda_client.register_simulator(SIM, 8200)
        assert controller._ui_backend("OTHER-SIM") is controller.sim_bridge

    async def test_the_skeleton_strategy_follows_the_backend_not_the_device_kind(
        self, controller,
    ):
        """It asked `_is_physical`, so a simulator in WDA mode was sent down the
        accessibility path for a WDA-only feature."""
        controller.wda_client.register_simulator(SIM, 8200)
        controller.resolve_udid = AsyncMock(return_value=SIM)
        controller.wda_client.build_screen_skeleton = AsyncMock(return_value=[])
        await controller.get_screen_summary(udid=SIM, strategy="skeleton")
        controller.wda_client.build_screen_skeleton.assert_awaited_once()


class TestPorts:
    def test_simulators_never_get_8100(self):
        """A pre-iOS-17 phone's start polls localhost:8100 and would mistake a
        simulator's WDA there for the phone's."""
        assert wda.SIM_PORT_FIRST > 8100

    def test_a_port_another_simulator_holds_is_skipped(self, monkeypatch):
        monkeypatch.setattr(wda, "_port_is_free", lambda port: True)
        runners = {"X": {"simulator": True, "port": wda.SIM_PORT_FIRST}}
        assert wda._allocate_sim_port(runners) == wda.SIM_PORT_FIRST + 1

    def test_a_port_something_else_is_bound_to_is_skipped(self, monkeypatch):
        monkeypatch.setattr(wda, "_port_is_free", lambda port: port != wda.SIM_PORT_FIRST)
        assert wda._allocate_sim_port({}) == wda.SIM_PORT_FIRST + 1

    def test_a_full_range_is_an_error_that_says_what_to_do(self, monkeypatch):
        monkeypatch.setattr(wda, "_port_is_free", lambda port: False)
        with pytest.raises(RuntimeError, match="stop_driver"):
            wda._allocate_sim_port({})


@pytest.fixture
def state(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(wda, "read_wda_state", lambda: __import__("copy").deepcopy(store))
    monkeypatch.setattr(wda, "save_wda_state", lambda s: (store.clear(), store.update(s)))
    return store


class TestStartingOnASimulator:
    @pytest.fixture
    def env(self, monkeypatch, tmp_path, state):
        monkeypatch.setattr(wda, "build_wda_simulator", AsyncMock(return_value=False))
        monkeypatch.setattr(wda, "_find_sim_xctestrun", lambda: tmp_path / "x.xctestrun")
        monkeypatch.setattr(wda, "WDA_LOG_DIR", tmp_path)
        monkeypatch.setattr(wda, "_port_is_free", lambda port: True)
        spawned = {}

        async def fake_exec(*cmd, **kw):
            spawned["cmd"], spawned["env"] = cmd, kw.get("env") or {}
            return MagicMock(pid=4242)

        monkeypatch.setattr(wda.asyncio, "create_subprocess_exec", fake_exec)
        return spawned

    async def test_it_passes_the_port_and_targets_the_simulator(self, env, monkeypatch):
        monkeypatch.setattr(wda, "_poll_wda_status", AsyncMock(return_value=True))
        result = await wda.start_driver_simulator(SIM)

        assert result["ready"] is True and result["port"] == wda.SIM_PORT_FIRST
        assert env["env"]["TEST_RUNNER_USE_PORT"] == str(wda.SIM_PORT_FIRST)
        assert f"id={SIM}" in env["cmd"]
        assert "test-without-building" in env["cmd"]

    async def test_a_runner_that_never_answers_is_stopped_and_unrecorded(
        self, env, state, monkeypatch,
    ):
        """Left registered, every read would go to a WDA that is not there."""
        monkeypatch.setattr(wda, "_poll_wda_status", AsyncMock(return_value=False))
        monkeypatch.setattr(wda, "_is_process_alive", lambda pid: True)
        stopped = []

        async def stop(udid):
            stopped.append(udid)
            s = wda.read_wda_state()
            s.get("runners", {}).pop(udid, None)
            wda.save_wda_state(s)
            return {"status": "stopped"}

        monkeypatch.setattr(wda, "stop_driver", stop)
        result = await wda.start_driver_simulator(SIM)

        assert result["ready"] is False and result["status"] == "failed"
        assert "error" in result
        assert stopped == [SIM]
        assert wda.simulator_runner_port(SIM) is None

    async def test_a_live_runner_is_reused(self, state, monkeypatch):
        state["runners"] = {SIM: {"pid": 77, "port": 8205, "simulator": True}}
        monkeypatch.setattr(wda, "_is_process_alive", lambda pid: True)
        result = await wda.start_driver_simulator(SIM)
        assert result == {"status": "already_running", "udid": SIM, "pid": 77,
                          "port": 8205, "ready": True}


class TestTheSimulatorBuild:
    @pytest.fixture
    def env(self, monkeypatch, tmp_path, state):
        monkeypatch.setattr(wda, "WDA_REPO", tmp_path / "repo")
        (tmp_path / "repo").mkdir()
        monkeypatch.setattr(wda, "WDA_DERIVED_SIM", tmp_path / "build-sim")
        monkeypatch.setattr(wda, "_xcode_build_id", AsyncMock(return_value="27A1"))
        return tmp_path

    def _proc(self, rc):
        p = MagicMock(returncode=rc)
        p.communicate = AsyncMock(return_value=(b"", b"boom" if rc else b""))
        return p

    async def test_it_builds_for_the_simulator_unsigned(self, env, state, monkeypatch):
        seen = {}

        async def fake_exec(*cmd, **kw):
            seen["cmd"] = cmd
            products = env / "build-sim" / "Build" / "Products"
            products.mkdir(parents=True)
            (products / "W.xctestrun").write_text("x")
            return self._proc(0)

        monkeypatch.setattr(wda.asyncio, "create_subprocess_exec", fake_exec)
        assert await wda.build_wda_simulator() is True
        assert "generic/platform=iOS Simulator" in seen["cmd"]
        assert "CODE_SIGNING_ALLOWED=NO" in seen["cmd"]
        assert not any(a.startswith("DEVELOPMENT_TEAM=") for a in seen["cmd"])
        assert state["sim_build_xcode"] == "27A1"

    async def test_a_failed_build_leaves_no_fingerprint(self, env, state, monkeypatch):
        """A fingerprint must never describe an artifact that is not there --
        including one left by an earlier success."""
        state["sim_build_xcode"] = "27A1"
        state["sim_build_deployment_target"] = wda.WDA_MIN_DEPLOYMENT_TARGET
        monkeypatch.setattr(wda.asyncio, "create_subprocess_exec",
                            AsyncMock(return_value=self._proc(65)))
        with pytest.raises(RuntimeError, match="simulator"):
            await wda.build_wda_simulator(force=True)
        assert "sim_build_xcode" not in state

    async def test_a_current_build_is_reused(self, env, state, monkeypatch):
        products = env / "build-sim" / "Build" / "Products"
        products.mkdir(parents=True)
        (products / "W.xctestrun").write_text("x")
        state["sim_build_xcode"] = "27A1"
        state["sim_build_deployment_target"] = wda.WDA_MIN_DEPLOYMENT_TARGET
        monkeypatch.setattr(wda.asyncio, "create_subprocess_exec",
                            AsyncMock(side_effect=AssertionError("rebuilt")))
        assert await wda.build_wda_simulator() is False

    async def test_a_new_xcode_rebuilds(self, env, state, monkeypatch):
        products = env / "build-sim" / "Build" / "Products"
        products.mkdir(parents=True)
        (products / "W.xctestrun").write_text("x")
        state["sim_build_xcode"] = "26Z9"
        state["sim_build_deployment_target"] = wda.WDA_MIN_DEPLOYMENT_TARGET
        assert await wda._sim_build_is_current(state) is False


class TestRestartSurvival:
    def test_live_simulator_runners_are_found(self, state, monkeypatch):
        state["runners"] = {
            SIM: {"pid": 1, "port": 8200, "simulator": True},
            "DEAD": {"pid": 2, "port": 8201, "simulator": True},
            "PHONE": {"pid": 3, "hw_udid": "x"},
        }
        monkeypatch.setattr(wda, "_is_process_alive", lambda pid: pid != 2)
        assert wda.live_simulator_runners() == {SIM: 8200}

    def test_the_lifespan_restores_them(self):
        """Source, because running the lifespan reaches the real machine."""
        import inspect

        from server import main

        src = inspect.getsource(main)
        assert "live_simulator_runners()" in src
        assert "register_simulator(_udid, _port)" in src


def test_xcui_type_on_the_model_defaults_to_none():
    from server.models import UIElement

    assert UIElement(type="Button").xcui_type is None
