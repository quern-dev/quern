"""WDA's auto-start behind a read: a runner that is not answering, and one
stopped on purpose (#769).

Measured on an iPhone 12 on a busy map: WDA's /source timed out, its runner
stayed alive and stopped answering /status, and every WDA call then asked it
again -- "auto-starting... already running... not serving", about every 7 s,
for 94 s behind one screen read. And stop_driver did not stick: the next read
the push could not make started WDA again.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from server.device.ios import wda as wda_module
from server.device.ios import wda_client
from server.device.ios.wda_client import WdaBackend
from server.models import DeviceError, WdaNotSetUpError
from tests.test_coredevice_ax import PHONE, _phone_controller

NOT_SERVING = {"status": "already_running", "udid": PHONE, "pid": 39842, "ready": False,
               "error": "WDA announced its server, but /status did not answer"}


@pytest.fixture
def backend(monkeypatch):
    """A WDA client that never reaches a WDA by itself, so every call takes
    the auto-start path."""
    b = WdaBackend()
    b._device_os_versions[PHONE] = "18.5"
    b._try_tunneld_connection = AsyncMock(return_value=None)
    b._start_usbmux_forward = AsyncMock(side_effect=DeviceError("no forward", tool="wda"))
    monkeypatch.setattr(wda_client, "_refuse_if_held", lambda udid, what: None)
    from server.tooling import wda_availability
    monkeypatch.setattr(wda_availability, "wda_unavailable_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(wda_module, "start_in_flight", lambda udid: None)
    return b


class TestARunnerThatIsNotAnswering:
    async def test_it_is_asked_once_then_not_again_for_a_while(self, backend, monkeypatch):
        start = AsyncMock(return_value=NOT_SERVING)
        monkeypatch.setattr(wda_module, "start_driver", start)

        with pytest.raises(DeviceError, match="running but not answering"):
            await backend._get_base_url(PHONE)
        with pytest.raises(DeviceError, match="not asked again for") as again:
            await backend._get_base_url(PHONE)

        assert start.await_count == 1, "the busy runner was probed again at once"
        assert "stop_driver resets it" in str(again.value)

    async def test_after_the_window_it_is_asked_again(self, backend, monkeypatch):
        start = AsyncMock(return_value=NOT_SERVING)
        monkeypatch.setattr(wda_module, "start_driver", start)
        monkeypatch.setattr(wda_client, "WDA_UNSERVING_S", 0.0)

        for _ in range(2):
            with pytest.raises(DeviceError):
                await backend._get_base_url(PHONE)
        assert start.await_count == 2

    async def test_a_runner_that_answers_is_forgotten_as_busy(self, backend, monkeypatch):
        monkeypatch.setattr(wda_module, "start_driver", AsyncMock(return_value=NOT_SERVING))
        with pytest.raises(DeviceError):
            await backend._get_base_url(PHONE)
        backend.note_started(PHONE)
        assert backend.no_auto_start_reason(PHONE) is None

    async def test_a_runner_that_fails_to_start_is_not_called_busy(self, backend, monkeypatch):
        """Only an alive runner that does not answer is remembered: a fresh
        start that fails is tried again on the next call, as before."""
        start = AsyncMock(return_value={"status": "started", "ready": False})
        monkeypatch.setattr(wda_module, "start_driver", start)
        for _ in range(2):
            with pytest.raises(DeviceError, match="did not become responsive"):
                await backend._get_base_url(PHONE)
        assert start.await_count == 2


class TestStoppedOnPurpose:
    async def test_it_is_not_auto_started_until_start_driver(self, backend, monkeypatch):
        start = AsyncMock(return_value={"status": "started", "ready": True})
        monkeypatch.setattr(wda_module, "start_driver", start)
        backend.note_stopped(PHONE)

        with pytest.raises(WdaNotSetUpError, match="stopped with stop_driver"):
            await backend._get_base_url(PHONE)
        start.assert_not_called()

        backend.note_started(PHONE)
        backend._try_tunneld_connection = AsyncMock(side_effect=[None, "http://[fd00::1]:8100"])
        assert await backend._get_base_url(PHONE) == "http://[fd00::1]:8100"
        start.assert_awaited_once()

    async def test_a_push_read_that_fails_does_not_start_it(self, monkeypatch):
        """The read fails saying why, instead of falling back to WDA."""
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl.coredevice_ax.describe_all = AsyncMock(
            side_effect=DeviceError("the push could not read it", tool="coredevice"))
        ctrl.wda_client.note_stopped(PHONE)

        with pytest.raises(DeviceError, match="stopped with stop_driver"):
            await ctrl.get_ui_elements(PHONE, use_cache=False, whole_screen=True)
        ctrl.wda_client.describe_all.assert_not_called()

    async def test_a_busy_runner_is_not_fallen_back_to_either(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl.coredevice_ax.describe_all = AsyncMock(
            side_effect=DeviceError("the push could not read it", tool="coredevice"))
        import time
        ctrl.wda_client._unserving[PHONE] = (time.monotonic() + 60, "no /status")

        with pytest.raises(DeviceError, match="running but not answering"):
            await ctrl.get_ui_elements(PHONE, use_cache=False, whole_screen=True)
        ctrl.wda_client.describe_all.assert_not_called()


class TestTheRoutes:
    @pytest.fixture
    async def post(self, monkeypatch):
        from httpx import ASGITransport, AsyncClient

        import server.main as m
        from server.config import ServerConfig
        from server.models import DeviceInfo, DeviceState, DeviceType

        app = m.create_app(ServerConfig(api_key="k" * 32))
        phone = DeviceInfo(udid=PHONE, name="iPhone", state=DeviceState.BOOTED,
                           device_type=DeviceType.DEVICE, os_version="18.5",
                           is_available=True)
        controller = MagicMock()
        controller.list_devices = AsyncMock(return_value=[phone])
        controller.wda_client.delete_session = AsyncMock()
        app.state.device_controller = controller
        monkeypatch.setattr(wda_module, "stop_driver",
                            AsyncMock(return_value={"status": "stopped", "udid": PHONE}))
        monkeypatch.setattr(wda_module, "start_driver",
                            AsyncMock(return_value={"status": "started", "ready": True}))
        self.controller = controller

        async def post(path: str):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
                return await c.post(path, json={"udid": PHONE},
                                    headers={"Authorization": "Bearer " + "k" * 32})
        return post

    async def test_stop_driver_on_a_phone_turns_auto_start_off_and_says_so(self, post):
        r = await post("/api/v1/device/wda/stop")
        assert r.status_code == 200, r.text
        self.controller.wda_client.note_stopped.assert_called_once_with(PHONE)
        assert "start_driver" in r.json()["auto_start"]

    async def test_start_driver_turns_it_back_on(self, post):
        r = await post("/api/v1/device/wda/start")
        assert r.status_code == 200, r.text
        self.controller.wda_client.note_started.assert_called_once_with(PHONE)
