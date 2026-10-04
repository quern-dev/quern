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

from server.device.ios import wda
from server.device.ios.wda_client import (
    WdaBackend,
    _map_wda_element,
    _map_wda_element_from_query,
)
from server.device.ui_elements import parse_elements

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

    async def test_a_live_answering_runner_is_reused(self, env, state, monkeypatch):
        """With `env`, a regression past the reuse path fails fast on the mocked
        spawn instead of reaching a real clone and build (CodeRabbit on #362)."""
        state["runners"] = {SIM: {"pid": 77, "port": 8205, "simulator": True}}
        monkeypatch.setattr(wda, "_is_process_alive", lambda pid: True)
        monkeypatch.setattr(wda, "_poll_wda_status", AsyncMock(return_value=True))
        result = await wda.start_driver_simulator(SIM)
        assert result == {"status": "already_running", "udid": SIM, "pid": 77,
                          "port": 8205, "ready": True}

    async def test_a_live_but_silent_runner_is_restarted_not_reused(
        self, env, state, monkeypatch,
    ):
        """A live pid is not a working runner. Reporting it ready on the pid
        alone registered a hung one (review on #336)."""
        state["runners"] = {SIM: {"pid": 77, "port": 8205, "simulator": True}}
        monkeypatch.setattr(wda, "_is_process_alive", lambda pid: True)
        answers = iter([False, True])  # silent first, then the new runner
        monkeypatch.setattr(wda, "_poll_wda_status",
                            AsyncMock(side_effect=lambda *a, **k: next(answers)))
        stopped = []

        async def stop(udid):
            stopped.append(udid)
            s = wda.read_wda_state()
            s.get("runners", {}).pop(udid, None)
            wda.save_wda_state(s)
            return {"status": "stopped"}

        monkeypatch.setattr(wda, "stop_driver", stop)
        result = await wda.start_driver_simulator(SIM)
        assert stopped == [SIM]
        assert result["status"] == "started" and result["ready"] is True


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

    async def test_restore_registers_only_runners_that_answer(self, state, monkeypatch):
        """A pid check alone would register a hung runner, or a recycled pid."""
        state["runners"] = {
            SIM: {"pid": 1, "port": 8200, "simulator": True},
            "HUNG": {"pid": 2, "port": 8201, "simulator": True},
        }
        monkeypatch.setattr(wda, "_is_process_alive", lambda pid: True)
        monkeypatch.setattr(
            wda, "_poll_wda_status",
            AsyncMock(side_effect=lambda url, timeout: url.endswith(":8200")),
        )
        w = WdaBackend()
        assert await wda.restore_simulator_mode(w) == [SIM]
        assert w.serves_simulator(SIM)
        assert not w.serves_simulator("HUNG")

    def test_the_lifespan_calls_the_restore(self):
        """Source, because running the lifespan reaches the real machine; the
        behaviour is pinned above. An exact call, not a substring a comment
        or a different call could also contain."""
        import ast
        import inspect

        from server import main

        calls = [
            n for n in ast.walk(ast.parse(inspect.getsource(main)))
            if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "restore_simulator_mode"
        ]
        assert len(calls) == 1
        assert ast.unparse(calls[0].args[0]) == "device_controller.wda_client"


def test_xcui_type_on_the_model_defaults_to_none():
    from server.models import UIElement

    assert UIElement(type="Button").xcui_type is None


# ---------------------------------------------------------------------------
# From the review of 6660cd5: three bugs, and tests for the mutants that survived
# ---------------------------------------------------------------------------


class TestTwoSimulatorsStartedTogetherGetTwoPorts:
    """Choosing a port and recording it were separated by the spawn's await, so
    two simulators started together were both handed 8200 -- and whichever WDA
    won the port answered both. Reproduced by the review with this shape."""

    async def test_concurrent_starts(self, state, monkeypatch, tmp_path):
        monkeypatch.setattr(wda, "build_wda_simulator", AsyncMock(return_value=False))
        monkeypatch.setattr(wda, "_find_sim_xctestrun", lambda: tmp_path / "x.xctestrun")
        monkeypatch.setattr(wda, "WDA_LOG_DIR", tmp_path)
        monkeypatch.setattr(wda, "_port_is_free", lambda port: True)
        monkeypatch.setattr(wda, "_is_process_alive", lambda pid: True)
        monkeypatch.setattr(wda, "_poll_wda_status", AsyncMock(return_value=True))
        pids = iter(range(100, 200))

        async def fake_exec(*cmd, **kw):
            await __import__("asyncio").sleep(0)  # the await the race lived in
            return MagicMock(pid=next(pids))

        monkeypatch.setattr(wda.asyncio, "create_subprocess_exec", fake_exec)
        a, b = await __import__("asyncio").gather(
            wda.start_driver_simulator("SIM-A"), wda.start_driver_simulator("SIM-B"),
        )
        assert a["port"] != b["port"]

    async def test_concurrent_starts_of_one_simulator_spawn_once(
        self, state, monkeypatch, tmp_path,
    ):
        monkeypatch.setattr(wda, "build_wda_simulator", AsyncMock(return_value=False))
        monkeypatch.setattr(wda, "_find_sim_xctestrun", lambda: tmp_path / "x.xctestrun")
        monkeypatch.setattr(wda, "WDA_LOG_DIR", tmp_path)
        monkeypatch.setattr(wda, "_port_is_free", lambda port: True)
        monkeypatch.setattr(wda, "_is_process_alive", lambda pid: True)
        monkeypatch.setattr(wda, "_poll_wda_status", AsyncMock(return_value=True))
        spawned = []

        async def fake_exec(*cmd, **kw):
            spawned.append(cmd)
            await __import__("asyncio").sleep(0)
            return MagicMock(pid=4242)

        monkeypatch.setattr(wda.asyncio, "create_subprocess_exec", fake_exec)
        await __import__("asyncio").gather(
            wda.start_driver_simulator(SIM), wda.start_driver_simulator(SIM),
        )
        assert len(spawned) == 1


class TestTheRunnerRecord:
    async def test_it_marks_the_entry_as_a_simulator(self, state, monkeypatch, tmp_path):
        """Without the marker, restart survival, reuse and port tracking all
        silently stop seeing it -- a mutant dropping it survived every test."""
        monkeypatch.setattr(wda, "build_wda_simulator", AsyncMock(return_value=False))
        monkeypatch.setattr(wda, "_find_sim_xctestrun", lambda: tmp_path / "x.xctestrun")
        monkeypatch.setattr(wda, "WDA_LOG_DIR", tmp_path)
        monkeypatch.setattr(wda, "_port_is_free", lambda port: True)
        monkeypatch.setattr(wda, "_is_process_alive", lambda pid: True)
        monkeypatch.setattr(wda, "_poll_wda_status", AsyncMock(return_value=True))
        seen = {}

        async def fake_exec(*cmd, **kw):
            seen["cmd"] = cmd
            return MagicMock(pid=4242)

        monkeypatch.setattr(wda.asyncio, "create_subprocess_exec", fake_exec)
        result = await wda.start_driver_simulator(SIM)

        assert state["runners"][SIM]["simulator"] is True
        assert wda.live_simulator_runners() == {SIM: result["port"]}
        # The simulator artifact, never the device one.
        assert str(tmp_path / "x.xctestrun") in seen["cmd"]
        assert str(wda.XCTESTRUN) not in seen["cmd"]


class TestTheSimulatorBuildInDetail:
    @pytest.fixture
    def env(self, monkeypatch, tmp_path, state):
        monkeypatch.setattr(wda, "WDA_REPO", tmp_path / "repo")
        (tmp_path / "repo").mkdir()
        monkeypatch.setattr(wda, "WDA_DERIVED_SIM", tmp_path / "build-sim")
        monkeypatch.setattr(wda, "_xcode_build_id", AsyncMock(return_value="27A1"))
        return tmp_path

    def _ok(self, env, seen):
        async def fake_exec(*cmd, **kw):
            seen["cmd"] = cmd
            products = env / "build-sim" / "Build" / "Products"
            products.mkdir(parents=True, exist_ok=True)
            (products / "W.xctestrun").write_text("x")
            p = MagicMock(returncode=0)
            p.communicate = AsyncMock(return_value=(b"", b""))
            return p
        return fake_exec

    async def test_its_own_derived_data_and_the_deployment_floor(self, env, monkeypatch):
        seen = {}
        monkeypatch.setattr(wda.asyncio, "create_subprocess_exec", self._ok(env, seen))
        await wda.build_wda_simulator()
        cmd = list(seen["cmd"])
        assert cmd[cmd.index("-derivedDataPath") + 1] == str(env / "build-sim")
        assert str(wda.WDA_DERIVED) not in cmd, "shares the device build's derived data"
        assert f"IPHONEOS_DEPLOYMENT_TARGET={wda.WDA_MIN_DEPLOYMENT_TARGET}" in cmd

    async def test_old_derived_data_is_removed_first(self, env, monkeypatch):
        stale = env / "build-sim" / "stale.marker"
        stale.parent.mkdir(parents=True)
        stale.write_text("x")
        monkeypatch.setattr(wda.asyncio, "create_subprocess_exec", self._ok(env, {}))
        await wda.build_wda_simulator(force=True)
        assert not stale.exists()

    async def test_success_without_an_xctestrun_is_a_failure(self, env, state, monkeypatch):
        p = MagicMock(returncode=0)
        p.communicate = AsyncMock(return_value=(b"", b""))
        monkeypatch.setattr(wda.asyncio, "create_subprocess_exec", AsyncMock(return_value=p))
        with pytest.raises(RuntimeError, match="no .xctestrun"):
            await wda.build_wda_simulator(force=True)
        assert "sim_build_xcode" not in state

    async def test_a_timeout_kills_reaps_and_leaves_no_fingerprint(
        self, env, state, monkeypatch,
    ):
        state["sim_build_xcode"] = "27A1"
        p = MagicMock(returncode=None)
        p.communicate = AsyncMock(side_effect=TimeoutError)
        p.wait = AsyncMock()
        monkeypatch.setattr(wda.asyncio, "create_subprocess_exec", AsyncMock(return_value=p))
        with pytest.raises(RuntimeError, match="timed out"):
            await wda.build_wda_simulator(force=True)
        p.kill.assert_called_once()
        p.wait.assert_awaited_once()
        assert "sim_build_xcode" not in state

    async def test_a_different_deployment_target_is_stale(self, env, state):
        products = env / "build-sim" / "Build" / "Products"
        products.mkdir(parents=True)
        (products / "W.xctestrun").write_text("x")
        state["sim_build_xcode"] = "27A1"
        state["sim_build_deployment_target"] = "13.0"
        assert await wda._sim_build_is_current(state) is False


class TestPortIsFree:
    def test_a_bound_port_is_not_free(self):
        """Every other test mocks this; it has to work for real once."""
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            port = s.getsockname()[1]
            assert wda._port_is_free(port) is False

    def test_an_unbound_port_is_free(self):
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        assert wda._port_is_free(port) is True


class TestADeadRunnerSaysWhatToDo:
    def test_the_hint_names_wda_mode_and_both_ways_out(self):
        w = WdaBackend()
        w.register_simulator(SIM, 8200)
        hint = w._wda_mode_hint(SIM)
        assert "WDA mode" in hint and "start_driver" in hint and "stop_driver" in hint

    def test_no_hint_for_anything_not_in_wda_mode(self):
        assert WdaBackend()._wda_mode_hint(SIM) == ""

    async def test_a_session_that_cannot_be_created_carries_it(self, monkeypatch):
        import httpx

        from server.device.controller import DeviceError

        w = WdaBackend()
        w.register_simulator(SIM, 8200)

        class Refused:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **k):
                raise httpx.ConnectError("refused")

            async def get(self, *a, **k):
                raise httpx.ConnectError("refused")

        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: Refused())
        with pytest.raises(DeviceError, match="stop_driver"):
            await w._ensure_session(SIM)

    def test_unregistering_drops_per_session_state(self):
        w = WdaBackend()
        w.register_simulator(SIM, 8200)
        w._current_depth[SIM] = 7
        w._last_interaction[SIM] = 1.0
        w.unregister_simulator(SIM)
        assert SIM not in w._current_depth and SIM not in w._last_interaction


class TestBackendIsReportedOnEveryWdaPath:
    @pytest.fixture
    def controller(self, monkeypatch):
        from server.device.controller import DeviceController

        c = DeviceController()
        monkeypatch.setattr(c, "_is_android", lambda udid: False)
        monkeypatch.setattr(c, "_is_physical", lambda udid: False)
        c._sim_bridge_ok = True
        c.resolve_udid = AsyncMock(return_value=SIM)
        c._last_read_backend[SIM] = "sim-bridge"  # what the last full read used
        c.wda_client.register_simulator(SIM, 8200)
        return c

    async def test_a_filtered_read_goes_direct_and_says_wda(self, controller):
        """It returned before the backend was recorded, so `backend` went on
        saying sim-bridge while WDA did the work."""
        from server.models import UIElement

        controller._wda_direct_query = AsyncMock(
            return_value=([UIElement(type="Button", label="Home")], 0.1),
        )
        # The on_screen marking's own lookup is tested elsewhere.
        controller._wda_viewport = AsyncMock(return_value=None)
        elements, _ = await controller.get_ui_elements(SIM, filter_label="Home")
        controller._wda_direct_query.assert_awaited_once()
        assert controller.backend_that_served(SIM) == "wda"

    async def test_the_skeleton_says_wda(self, controller):
        controller.wda_client.build_screen_skeleton = AsyncMock(return_value=[])
        await controller.get_screen_summary(udid=SIM, strategy="skeleton")
        assert controller.backend_that_served(SIM) == "wda"


class TestWebContentInWdaMode:
    async def test_it_points_at_get_ui_tree(self, monkeypatch):
        """Its fallback hit-tests point by point, and each WDA point lookup is
        a full /source. The WDA tree already carries web content."""
        from server.device.controller import DeviceController, DeviceError

        c = DeviceController()
        monkeypatch.setattr(c, "_is_android", lambda udid: False)
        monkeypatch.setattr(c, "_is_physical", lambda udid: False)
        c.resolve_udid = AsyncMock(return_value=SIM)
        c.wda_client.register_simulator(SIM, 8200)
        with pytest.raises(DeviceError, match="get_ui_tree"):
            await c.get_web_content(udid=SIM)



# ---------------------------------------------------------------------------
# From CodeRabbit on #362
# ---------------------------------------------------------------------------


class TestACancelledBuildDoesNotLeaveXcodebuildRunning:
    async def test_cancellation_kills_and_reaps(self, state, monkeypatch, tmp_path):
        """A client that gives up on the first start_driver cancels the task.
        An xcodebuild left running goes on writing into the derived data the
        next build will rmtree."""
        import asyncio

        monkeypatch.setattr(wda, "WDA_REPO", tmp_path / "repo")
        (tmp_path / "repo").mkdir()
        monkeypatch.setattr(wda, "WDA_DERIVED_SIM", tmp_path / "build-sim")
        monkeypatch.setattr(wda, "_xcode_build_id", AsyncMock(return_value="27A1"))
        started = asyncio.Event()
        p = MagicMock(returncode=None)

        async def never():
            started.set()
            await asyncio.Event().wait()

        p.communicate = never
        p.wait = AsyncMock()
        monkeypatch.setattr(wda.asyncio, "create_subprocess_exec", AsyncMock(return_value=p))

        task = asyncio.create_task(wda.build_wda_simulator(force=True))
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)
        finally:
            task.cancel()
        p.kill.assert_called_once()
        p.wait.assert_awaited_once()


class TestARaceForOneSimulatorDoesNotStallAnother:
    async def test_the_poll_happens_outside_the_shared_lock(self, state, monkeypatch, tmp_path):
        """The re-check path polled for up to 90s inside the lock every
        simulator shares, so an unrelated start waited behind it."""
        import asyncio

        monkeypatch.setattr(wda, "build_wda_simulator", AsyncMock(return_value=False))
        monkeypatch.setattr(wda, "_find_sim_xctestrun", lambda: tmp_path / "x.xctestrun")
        monkeypatch.setattr(wda, "WDA_LOG_DIR", tmp_path)
        monkeypatch.setattr(wda, "_port_is_free", lambda port: True)
        monkeypatch.setattr(wda, "_is_process_alive", lambda pid: True)
        state["runners"] = {}

        # SIM looks free before the lock and already started inside it: the
        # shape of a concurrent start for the same simulator.
        calls = {"n": 0}
        real_port = wda.simulator_runner_port

        def runner_port(udid):
            if udid == SIM:
                calls["n"] += 1
                if calls["n"] == 1:
                    return None
                state.setdefault("runners", {})[SIM] = {"pid": 9, "port": 8299, "simulator": True}
                return 8299
            return real_port(udid)

        monkeypatch.setattr(wda, "simulator_runner_port", runner_port)
        release = asyncio.Event()

        async def poll(url, timeout):
            if url.endswith(":8299"):
                await release.wait()  # SIM's re-check poll, held open
            return True

        monkeypatch.setattr(wda, "_poll_wda_status", poll)
        monkeypatch.setattr(wda.asyncio, "create_subprocess_exec",
                            AsyncMock(return_value=MagicMock(pid=4242)))

        sim_task = asyncio.create_task(wda.start_driver_simulator(SIM))
        try:
            await asyncio.sleep(0.05)  # SIM is now polling
            other = await asyncio.wait_for(wda.start_driver_simulator("OTHER"), timeout=2)
            assert other["ready"] is True, "the other simulator waited behind SIM's poll"
            release.set()
            assert (await asyncio.wait_for(sim_task, timeout=5))["status"] == "already_running"
        finally:
            release.set()
            sim_task.cancel()


class TestSwitchingBackendForgetsTheOldOnesState:
    def test_cache_overlay_and_recorded_backend_are_cleared(self, monkeypatch):
        """A web element in the overlay from the accessibility tree has a
        different frame from the same element in WDA's tree, so it was not
        deduplicated and a tap by label came back ambiguous."""
        from server.device.controller import DeviceController
        from server.models import UIElement

        c = DeviceController()
        el = UIElement(type="Link", label="Sign in")
        c._ui_cache[SIM] = ([el], 0.0)
        c._web_overlay[SIM] = ([el], 0.0)
        c._last_read_backend[SIM] = "sim-bridge"
        c._backend_switched(SIM)
        assert SIM not in c._ui_cache
        assert SIM not in c._web_overlay
        assert SIM not in c._last_read_backend



class TestARunnerRemovedDuringThePoll:
    """A concurrent stop_driver can remove the record while a poll waits; the
    pid used to be read after the poll, which raised KeyError (CodeRabbit on
    #362)."""

    async def test_the_raced_path_reports_not_ready_instead_of_raising(
        self, state, monkeypatch, tmp_path,
    ):
        monkeypatch.setattr(wda, "build_wda_simulator", AsyncMock(return_value=False))
        monkeypatch.setattr(wda, "_find_sim_xctestrun", lambda: tmp_path / "x.xctestrun")
        monkeypatch.setattr(wda, "_is_process_alive", lambda pid: True)
        calls = {"n": 0}

        def runner_port(udid):
            calls["n"] += 1
            if calls["n"] == 1:
                return None  # free before the lock
            if calls["n"] == 2:
                state["runners"] = {SIM: {"pid": 9, "port": 8299, "simulator": True}}
                return 8299  # started by a concurrent call
            return None  # gone by the time the poll returns

        monkeypatch.setattr(wda, "simulator_runner_port", runner_port)

        async def poll(url, timeout):
            state["runners"] = {}  # the concurrent stop_driver, mid-poll
            return True

        monkeypatch.setattr(wda, "_poll_wda_status", poll)
        result = await wda.start_driver_simulator(SIM)
        assert result["status"] == "already_running"
        assert result["ready"] is False, "registering it would route to a runner being torn down"
        assert result["pid"] == 9
