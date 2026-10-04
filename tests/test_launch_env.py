"""launch_app with env on every kind of device (#388).

An environment reaches only a process that is starting, and both iOS routes
bring a running app forward instead -- keeping the environment it started
with -- and report success. Measured on each: a second `simctl launch` with a
different variable kept the pid and the old value; WDA's `/wda/apps/launch`
activates a running app without applying `environment`. So with `env`, quern
restarts a running app and says so, rather than report variables it never
delivered. Android apps take no environment variables, and that is said too.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from server.device.controller import DeviceController
from server.models import DeviceError, DeviceType

SIM = "66EF8B35-4384-447E-84E8-4951BA26B181"
PHONE = "48CF8DD9-2492-5F8E-A737-49DF96422F09"
PIXEL = "R58M1234ABC"
APP = "com.example.App"
ENV = {"UITEST_DISABLE_ANIMATIONS": "YES"}


def _ctrl(udid: str, kind: DeviceType) -> DeviceController:
    ctrl = DeviceController()
    ctrl._device_type_cache[udid] = kind
    ctrl.resolve_udid = AsyncMock(return_value=udid)
    ctrl.simctl.launch_app = AsyncMock(return_value=4242)
    ctrl.simctl.running_pid = AsyncMock(return_value=None)
    ctrl._confirm_the_app_came_up = AsyncMock()
    ctrl.wda_client.app_state = AsyncMock(return_value=4)
    ctrl.wda_client.activate_app = AsyncMock()
    ctrl.wda_client.terminate_app = AsyncMock(return_value=True)
    ctrl.wda_client.launch_app = AsyncMock()
    ctrl.adb.launch_app = AsyncMock()
    ctrl._LAUNCH_FRONT_GRACE_S = 0.05
    ctrl._LAUNCH_FRONT_INTERVAL_S = 0.01
    return ctrl


class TestASimulator:
    async def test_without_env_nothing_restarts_and_nothing_is_reported(self):
        ctrl = _ctrl(SIM, DeviceType.SIMULATOR)
        udid, info = await ctrl.launch_app(APP)
        assert udid == SIM and info == {}
        ctrl.simctl.launch_app.assert_awaited_once_with(SIM, APP, env=None, restart=False)
        ctrl.simctl.running_pid.assert_not_awaited()

    async def test_env_restarts_a_running_app_and_says_so(self):
        ctrl = _ctrl(SIM, DeviceType.SIMULATOR)
        ctrl.simctl.running_pid = AsyncMock(return_value=79115)
        _, info = await ctrl.launch_app(APP, env=ENV)
        ctrl.simctl.launch_app.assert_awaited_once_with(SIM, APP, env=ENV, restart=True)
        assert info == {"env_applied": True, "restarted": True}
        ctrl._confirm_the_app_came_up.assert_awaited_once()

    async def test_env_on_a_stopped_app_is_a_plain_start(self):
        ctrl = _ctrl(SIM, DeviceType.SIMULATOR)
        _, info = await ctrl.launch_app(APP, env=ENV)
        assert info == {"env_applied": True, "restarted": False}

    async def test_the_running_check_is_made_before_the_launch(self):
        """Review: after a restart the new process is always running, so a
        check made afterwards would always answer "restarted"."""
        ctrl = _ctrl(SIM, DeviceType.SIMULATOR)
        launched: list[bool] = []
        ctrl.simctl.launch_app = AsyncMock(
            side_effect=lambda *a, **k: launched.append(True) or 4242)

        async def running_pid(udid, bundle):
            return 81847 if launched else None          # running only once launched
        ctrl.simctl.running_pid = running_pid
        _, info = await ctrl.launch_app(APP, env=ENV)
        assert info["restarted"] is False

    async def test_a_running_check_that_hangs_costs_the_report_not_the_launch(self):
        import asyncio

        ctrl = _ctrl(SIM, DeviceType.SIMULATOR)
        ctrl._LAUNCH_STATE_READ_TIMEOUT_S = 0.05

        async def stuck(udid, bundle):
            await asyncio.sleep(30)
        ctrl.simctl.running_pid = stuck
        _, info = await ctrl.launch_app(APP, env=ENV)
        assert info == {"env_applied": True, "restarted": None}
        ctrl.simctl.launch_app.assert_awaited_once()

    async def test_a_running_check_that_fails_costs_the_report_not_the_launch(self):
        ctrl = _ctrl(SIM, DeviceType.SIMULATOR)
        ctrl.simctl.running_pid = AsyncMock(side_effect=DeviceError("launchctl", tool="simctl"))
        _, info = await ctrl.launch_app(APP, env=ENV)
        ctrl.simctl.launch_app.assert_awaited_once_with(SIM, APP, env=ENV, restart=True)
        assert info == {"env_applied": True, "restarted": None}


class TestAPhysicalIPhone:
    async def test_without_env_a_running_app_is_brought_forward_as_before(self):
        ctrl = _ctrl(PHONE, DeviceType.DEVICE)
        _, info = await ctrl.launch_app(APP)
        ctrl.wda_client.activate_app.assert_awaited_once_with(PHONE, APP)
        ctrl.wda_client.launch_app.assert_not_awaited()
        ctrl.wda_client.terminate_app.assert_not_awaited()
        assert info == {}

    async def test_without_env_a_stopped_app_starts_marked_as_quern_driven(self):
        """As a simulator's does: QUERN_AUTOMATION=YES on every start."""
        ctrl = _ctrl(PHONE, DeviceType.DEVICE)
        ctrl.wda_client.app_state = AsyncMock(side_effect=[1, 4])
        await ctrl.launch_app(APP)
        ctrl.wda_client.launch_app.assert_awaited_once_with(
            PHONE, APP, {"QUERN_AUTOMATION": "YES"})
        ctrl.wda_client.activate_app.assert_not_awaited()

    async def test_env_restarts_a_running_app_so_it_applies(self):
        ctrl = _ctrl(PHONE, DeviceType.DEVICE)
        calls: list[str] = []
        ctrl.wda_client.terminate_app = AsyncMock(side_effect=lambda *a: calls.append("terminate"))
        ctrl.wda_client.launch_app = AsyncMock(side_effect=lambda *a: calls.append("launch"))
        _, info = await ctrl.launch_app(APP, env=ENV)
        assert calls == ["terminate", "launch"]
        ctrl.wda_client.launch_app.assert_awaited_once_with(
            PHONE, APP, {"QUERN_AUTOMATION": "YES", **ENV})
        assert info == {"env_applied": True, "restarted": True}

    @pytest.mark.parametrize("state", [2, 3])
    async def test_a_backgrounded_app_is_running_too(self, state):
        ctrl = _ctrl(PHONE, DeviceType.DEVICE)
        ctrl.wda_client.app_state = AsyncMock(side_effect=[state, 4])
        _, info = await ctrl.launch_app(APP, env=ENV)
        ctrl.wda_client.terminate_app.assert_awaited_once()
        assert info["restarted"] is True

    async def test_env_on_a_stopped_app_does_not_terminate(self):
        ctrl = _ctrl(PHONE, DeviceType.DEVICE)
        ctrl.wda_client.app_state = AsyncMock(side_effect=[1, 4])
        _, info = await ctrl.launch_app(APP, env=ENV)
        ctrl.wda_client.terminate_app.assert_not_awaited()
        assert info == {"env_applied": True, "restarted": False}

    async def test_an_unreadable_state_terminates_rather_than_risk_the_variables(self):
        """Terminating a stopped app is harmless; activating a running one
        drops the variables. Unknown takes the harmless side."""
        ctrl = _ctrl(PHONE, DeviceType.DEVICE)
        ctrl.wda_client.app_state = AsyncMock(side_effect=[DeviceError("x", tool="wda"), 4])
        _, info = await ctrl.launch_app(APP, env=ENV)
        ctrl.wda_client.terminate_app.assert_awaited_once()
        ctrl.wda_client.launch_app.assert_awaited_once()
        # Terminate's own answer (the fixture's: it was running) settles it.
        assert info == {"env_applied": True, "restarted": True}

    @pytest.mark.parametrize("env", [ENV, None])
    @pytest.mark.parametrize("after", [1, 2, 3])
    async def test_a_launch_that_never_reaches_the_front_fails(self, env, after):
        ctrl = _ctrl(PHONE, DeviceType.DEVICE)
        ctrl.wda_client.app_state = AsyncMock(side_effect=[1] + [after] * 50)
        with pytest.raises(DeviceError, match="not in the foreground"):
            await ctrl.launch_app(APP, env=env)

    async def test_a_background_moment_on_the_way_to_the_front_is_not_a_failure(self):
        ctrl = _ctrl(PHONE, DeviceType.DEVICE)
        ctrl._LAUNCH_FRONT_GRACE_S = 1.0
        ctrl.wda_client.app_state = AsyncMock(side_effect=[1, 3, 3, 4])
        _, info = await ctrl.launch_app(APP, env=ENV)
        assert info == {"env_applied": True, "restarted": False}

    async def test_reads_after_launch_that_fail_say_the_launch_is_unconfirmed(self):
        """Review: the launch stands, but "could not check" is said, not
        reported as confirmed."""
        ctrl = _ctrl(PHONE, DeviceType.DEVICE)
        ctrl.wda_client.app_state = AsyncMock(
            side_effect=[1] + [DeviceError("WDA gone", tool="wda")] * 50)
        _, info = await ctrl.launch_app(APP, env=ENV)
        assert info["env_applied"] is True
        assert info["launch_confirmed"] is None
        assert "WDA gone" in info["launch_check_error"]

    async def test_state_unknown_is_not_taken_for_stopped(self):
        """Review: XCUIApplication state 0 is "unknown". Read as "stopped",
        a running app was not terminated, WDA only activated it, and the
        variables never arrived while the response said they had."""
        ctrl = _ctrl(PHONE, DeviceType.DEVICE)
        ctrl.wda_client.app_state = AsyncMock(side_effect=[0, 4])
        _, info = await ctrl.launch_app(APP, env=ENV)
        ctrl.wda_client.terminate_app.assert_awaited_once()
        assert info["restarted"] is True, "WDA's terminate answered that it was running"

    @pytest.mark.parametrize("terminated,expected", [(True, True), (False, False), (None, None)])
    async def test_an_unknown_state_is_settled_by_what_terminate_answers(
        self, terminated, expected,
    ):
        ctrl = _ctrl(PHONE, DeviceType.DEVICE)
        ctrl.wda_client.app_state = AsyncMock(side_effect=[DeviceError("x", tool="wda"), 4])
        ctrl.wda_client.terminate_app = AsyncMock(return_value=terminated)
        _, info = await ctrl.launch_app(APP, env=ENV)
        assert info["restarted"] is expected

    @pytest.mark.parametrize("first", [0, DeviceError("x", tool="wda")])
    async def test_without_env_an_unknown_state_is_activated_as_before(self, first):
        ctrl = _ctrl(PHONE, DeviceType.DEVICE)
        ctrl.wda_client.app_state = AsyncMock(side_effect=[first])
        await ctrl.launch_app(APP)
        ctrl.wda_client.activate_app.assert_awaited_once()
        ctrl.wda_client.launch_app.assert_not_awaited()

    async def test_a_terminate_that_fails_says_why_it_was_terminating(self):
        ctrl = _ctrl(PHONE, DeviceType.DEVICE)
        ctrl.wda_client.terminate_app = AsyncMock(side_effect=DeviceError("WDA busy", tool="wda"))
        with pytest.raises(DeviceError, match="so env would apply: WDA busy"):
            await ctrl.launch_app(APP, env=ENV)
        ctrl.wda_client.launch_app.assert_not_awaited()

    async def test_env_can_override_quern_automation_as_on_a_simulator(self):
        ctrl = _ctrl(PHONE, DeviceType.DEVICE)
        await ctrl.launch_app(APP, env={"QUERN_AUTOMATION": "NO"})
        assert ctrl.wda_client.launch_app.call_args.args[2] == {"QUERN_AUTOMATION": "NO"}


class TestAndroid:
    async def test_env_is_reported_as_not_applied(self):
        ctrl = _ctrl(PIXEL, DeviceType.ANDROID_DEVICE)
        _, info = await ctrl.launch_app(APP, env=ENV)
        ctrl.adb.launch_app.assert_awaited_once_with(PIXEL, APP)
        assert info["env_applied"] is False
        assert "do not receive environment variables" in info["warning"]

    async def test_without_env_nothing_is_reported(self):
        ctrl = _ctrl(PIXEL, DeviceType.ANDROID_DEVICE)
        _, info = await ctrl.launch_app(APP)
        assert info == {}


class TestTheBackends:
    async def test_simctl_restart_terminates_the_running_process(self, monkeypatch):
        import server.device.simctl as simctl_mod
        from server.device.simctl import SimctlBackend

        seen: dict = {}

        async def fake_exec(*args, **kwargs):
            seen["args"], seen["env"] = args, kwargs["env"]
            proc = MagicMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(f"{APP}: 79901\n".encode(), b""))
            return proc
        monkeypatch.setattr(simctl_mod.asyncio, "create_subprocess_exec", fake_exec)
        pid = await SimctlBackend().launch_app(SIM, APP, env=ENV, restart=True)
        assert pid == 79901
        assert seen["args"] == ("xcrun", "simctl", "launch", "--terminate-running-process",
                                SIM, APP)
        assert seen["env"]["SIMCTL_CHILD_UITEST_DISABLE_ANIMATIONS"] == "YES"
        assert seen["env"]["SIMCTL_CHILD_QUERN_AUTOMATION"] == "YES"

    async def test_simctl_without_restart_has_no_terminate_flag(self, monkeypatch):
        import server.device.simctl as simctl_mod
        from server.device.simctl import SimctlBackend

        seen: dict = {}

        async def fake_exec(*args, **kwargs):
            seen["args"] = args
            proc = MagicMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(f"{APP}: 1\n".encode(), b""))
            return proc
        monkeypatch.setattr(simctl_mod.asyncio, "create_subprocess_exec", fake_exec)
        await SimctlBackend().launch_app(SIM, APP)
        assert "--terminate-running-process" not in seen["args"]

    # The line format is measured (iOS 18.6 simulator, `simctl spawn <udid>
    # launchctl list`); the Extension line is added, to test that a longer
    # bundle id sharing the prefix is not taken for the app.
    _LAUNCHCTL = (
        "PID\tStatus\tLabel\n"
        "-\t0\tcom.apple.something\n"
        f"79115\t0\tUIKitApplication:{APP}Extension[9f2c][rb-legacy]\n"
        f"79116\t0\tUIKitApplication:{APP}[acfb][rb-legacy]\n"
    )

    async def test_running_pid_finds_the_app_and_not_a_longer_bundle_id(self):
        from server.device.simctl import SimctlBackend

        simctl = SimctlBackend()
        simctl._run_simctl = AsyncMock(return_value=(self._LAUNCHCTL, ""))
        assert await simctl.running_pid(SIM, APP) == 79116
        simctl._run_simctl.assert_awaited_once_with("spawn", SIM, "launchctl", "list")

    async def test_running_pid_of_a_stopped_app_is_none(self):
        from server.device.simctl import SimctlBackend

        simctl = SimctlBackend()
        simctl._run_simctl = AsyncMock(return_value=(
            f"-\t0\tUIKitApplication:{APP}[acfb][rb-legacy]\n"
            "91159\t0\tUIKitApplication:com.apple.Spotlight[0649][rb-legacy]\n", ""))
        assert await simctl.running_pid(SIM, APP) is None

    async def test_a_list_with_no_app_jobs_is_unrecognised_not_not_running(self):
        """Review: a launchd that names app jobs differently would otherwise
        report every app as stopped. A booted simulator always runs some
        (measured on iOS 26.5: Spotlight, the widget renderer)."""
        from server.device.simctl import SimctlBackend

        simctl = SimctlBackend()
        simctl._run_simctl = AsyncMock(return_value=(
            "PID\tStatus\tLabel\n91202\t0\tApplication:com.example.App[1]\n", ""))
        with pytest.raises(DeviceError, match="recognises"):
            await simctl.running_pid(SIM, APP)

    async def test_simctl_lets_env_override_quern_automation(self, monkeypatch):
        import server.device.simctl as simctl_mod
        from server.device.simctl import SimctlBackend

        seen: dict = {}

        async def fake_exec(*args, **kwargs):
            seen["env"] = kwargs["env"]
            proc = MagicMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(f"{APP}: 1\n".encode(), b""))
            return proc
        monkeypatch.setattr(simctl_mod.asyncio, "create_subprocess_exec", fake_exec)
        await SimctlBackend().launch_app(SIM, APP, env={"QUERN_AUTOMATION": "NO"})
        assert seen["env"]["SIMCTL_CHILD_QUERN_AUTOMATION"] == "NO"

    async def test_a_timed_out_terminate_is_not_resent_and_reads_the_state_instead(self):
        """CodeRabbit on #393: re-sent after WDA had already terminated the
        app, the second answer is false, and restarted read false."""
        import httpx

        from server.device.wda_client import WdaBackend

        wda = WdaBackend()
        wda._request = AsyncMock(side_effect=httpx.ReadTimeout("slow"))
        wda.app_state = AsyncMock(return_value=1)
        assert await wda.terminate_app(PHONE, APP) is None, "stopped, but was it running?"
        assert wda._request.await_count == 1
        assert wda._request.call_args.kwargs["raise_if_maybe_delivered"] is True

    async def test_a_connection_lost_after_terminate_is_settled_the_same_way(self):
        """CodeRabbit on #393: a ReadError, like a timeout, can come after WDA
        has terminated the app, and was being re-sent."""
        import httpx

        from server.device.wda_client import WdaBackend

        wda = WdaBackend()
        wda._request = AsyncMock(side_effect=httpx.ReadError("reset"))
        wda.app_state = AsyncMock(return_value=1)
        assert await wda.terminate_app(PHONE, APP) is None
        assert wda._request.await_count == 1

    @pytest.mark.parametrize("state", [0, 2, 3, 4, 7])
    async def test_a_timed_out_terminate_not_confirmed_stopped_fails(self, state):
        """Only 1 is stopped. 0 is WDA's "unknown" (CodeRabbit on #393): read
        as stopped, a launch could go on and report env applied to a process
        that kept its old environment."""
        import httpx

        from server.device.wda_client import WdaBackend

        wda = WdaBackend()
        wda._request = AsyncMock(side_effect=httpx.ReadTimeout("slow"))
        wda.app_state = AsyncMock(return_value=state)
        with pytest.raises(DeviceError, match="cannot be confirmed stopped"):
            await wda.terminate_app(PHONE, APP)

    @pytest.mark.parametrize("payload,expected", [
        ({"value": True}, True), ({"value": False}, False), ({"value": None}, None), ([], None)])
    async def test_wda_terminate_says_whether_it_was_running(self, payload, expected):
        assert await self._wda(payload).terminate_app(PHONE, APP) is expected

    def _wda(self, payload=None, *, raises=None):
        from server.device.wda_client import WdaBackend

        wda = WdaBackend()
        resp = MagicMock()
        resp.json = MagicMock(return_value=payload)
        wda._request = AsyncMock(side_effect=raises) if raises else AsyncMock(return_value=resp)
        return wda

    async def test_wda_launch_sends_the_environment_and_is_not_resent(self):
        wda = self._wda({"value": None})
        await wda.launch_app(PHONE, APP, {"QUERN_AUTOMATION": "YES", **ENV})
        args, kwargs = wda._request.call_args
        assert args[:3] == ("post", PHONE, "/wda/apps/launch")
        assert kwargs["json"] == {"bundleId": APP,
                                  "environment": {"QUERN_AUTOMATION": "YES", **ENV}}
        assert kwargs["raise_on_timeout"] is True

    async def test_a_timed_out_wda_launch_is_reported_not_retried(self):
        import httpx

        wda = self._wda(raises=httpx.ReadTimeout("slow"))
        with pytest.raises(DeviceError, match="not launched again"):
            await wda.launch_app(PHONE, APP, {})
        assert wda._request.await_count == 1

    async def test_wda_app_state_is_an_int_or_an_error(self):
        assert await self._wda({"value": 4}).app_state(PHONE, APP) == 4
        for bad in ({"value": "running"}, {"value": None}, ["x"]):
            with pytest.raises(DeviceError):
                await self._wda(bad).app_state(PHONE, APP)


class TestTheResponse:
    @pytest.mark.parametrize("env", [{"": "x"}, {"A=B": "x"}, {"A\x00": "x"}, {"A": "x\x00"}])
    async def test_a_name_no_process_can_take_is_a_422_not_a_500(self, env):
        from httpx import ASGITransport, AsyncClient

        from server.config import ServerConfig
        from server.main import create_app

        app = create_app(config=ServerConfig(api_key="k"),
                         enable_oslog=False, enable_crash=False, enable_proxy=False)
        ctrl = DeviceController()
        ctrl.launch_app = AsyncMock()
        app.state.device_controller = ctrl
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/api/v1/device/app/launch", json={"bundle_id": APP, "env": env},
                             headers={"Authorization": "Bearer k"})
        assert r.status_code == 422, r.text
        ctrl.launch_app.assert_not_awaited()

    async def test_the_report_is_on_the_response(self):
        from httpx import ASGITransport, AsyncClient

        from server.config import ServerConfig
        from server.main import create_app

        app = create_app(config=ServerConfig(api_key="k"),
                         enable_oslog=False, enable_crash=False, enable_proxy=False)
        ctrl = DeviceController()
        ctrl.resolve_udid = AsyncMock(return_value=PHONE)
        ctrl.launch_app = AsyncMock(return_value=(PHONE, {"env_applied": True, "restarted": True}))
        app.state.device_controller = ctrl
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/api/v1/device/app/launch", json={"bundle_id": APP, "env": ENV},
                             headers={"Authorization": "Bearer k"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "launched"
        assert body["env_applied"] is True and body["restarted"] is True
        ctrl.launch_app.assert_awaited_once_with(bundle_id=APP, udid=None, env=ENV)
