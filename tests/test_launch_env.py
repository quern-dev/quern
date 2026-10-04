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
    ctrl.wda_client.terminate_app = AsyncMock()
    ctrl.wda_client.launch_app = AsyncMock()
    ctrl.adb.launch_app = AsyncMock()
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
        assert info == {"env_applied": True, "restarted": None}

    async def test_a_launch_that_did_not_come_to_the_front_fails(self):
        ctrl = _ctrl(PHONE, DeviceType.DEVICE)
        ctrl.wda_client.app_state = AsyncMock(side_effect=[1, 1])
        with pytest.raises(DeviceError, match="not in the foreground"):
            await ctrl.launch_app(APP, env=ENV)

    async def test_a_state_read_after_launch_that_fails_is_not_a_failure(self):
        ctrl = _ctrl(PHONE, DeviceType.DEVICE)
        ctrl.wda_client.app_state = AsyncMock(side_effect=[1, DeviceError("x", tool="wda")])
        _, info = await ctrl.launch_app(APP, env=ENV)
        assert info["env_applied"] is True


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
        simctl._run_simctl = AsyncMock(
            return_value=(f"-\t0\tUIKitApplication:{APP}[acfb][rb-legacy]\n", ""))
        assert await simctl.running_pid(SIM, APP) is None

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
