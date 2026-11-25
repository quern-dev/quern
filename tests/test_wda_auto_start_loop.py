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
from server.models import DeviceError, WdaNotSetUpError, WdaRunnerBusyError
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
        assert "stop_driver, then start_driver, resets it" in str(again.value)

    async def test_the_refusal_comes_before_the_slow_probes(self, backend, monkeypatch):
        """Each probe costs seconds against a runner that takes connections
        and does not answer: only one quick look is allowed (review)."""
        monkeypatch.setattr(wda_module, "start_driver", AsyncMock(return_value=NOT_SERVING))
        with pytest.raises(DeviceError):
            await backend._get_base_url(PHONE)
        backend._try_tunneld_connection.reset_mock()
        backend._start_usbmux_forward.reset_mock()

        with pytest.raises(WdaRunnerBusyError):
            await backend._get_base_url(PHONE)

        backend._try_tunneld_connection.assert_awaited_once_with(
            PHONE, timeout=wda_client.WDA_RECOVERED_PROBE_S)
        backend._start_usbmux_forward.assert_not_called()

    async def test_a_runner_that_has_recovered_is_used_at_once(self, backend, monkeypatch):
        monkeypatch.setattr(wda_module, "start_driver", AsyncMock(return_value=NOT_SERVING))
        with pytest.raises(DeviceError):
            await backend._get_base_url(PHONE)
        backend._try_tunneld_connection = AsyncMock(return_value="http://[fd00::1]:8100")

        assert await backend._get_base_url(PHONE) == "http://[fd00::1]:8100"
        assert backend.no_auto_start_reason(PHONE) is None

    async def test_a_start_that_then_works_clears_it(self, backend, monkeypatch):
        start = AsyncMock(side_effect=[NOT_SERVING, {"status": "started", "ready": True}])
        monkeypatch.setattr(wda_module, "start_driver", start)
        monkeypatch.setattr(wda_client, "WDA_UNSERVING_S", 0.0)
        with pytest.raises(DeviceError):
            await backend._get_base_url(PHONE)
        backend._try_tunneld_connection = AsyncMock(side_effect=[None, "http://[fd00::1]:8100"])

        assert await backend._get_base_url(PHONE) == "http://[fd00::1]:8100"
        assert PHONE not in backend._unserving

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

    async def test_a_restart_behind_a_read_in_flight_is_not_made(self, backend, monkeypatch):
        """A /source timeout in a read already under way when stop_driver
        landed restarted WDA (review)."""
        start = AsyncMock(return_value={"status": "started", "ready": True})
        monkeypatch.setattr(wda_module, "start_driver", start)
        monkeypatch.setattr(wda_module, "stop_driver", AsyncMock())
        backend.note_stopped(PHONE)
        await backend._restart_wda(PHONE)
        start.assert_not_called()

    @pytest.mark.parametrize("during", ["stop_driver", "refuse_without_xcode"])
    async def test_a_stop_that_lands_during_a_restart_is_honoured(
            self, backend, monkeypatch, during):
        """Checked only on the way in, a stop_driver landing during the
        restart's own awaits -- stopping the old runner, or the Xcode check
        before the start is registered -- was followed by a start
        (CodeRabbit on #773)."""
        launched = AsyncMock(return_value={"status": "started", "ready": True})
        monkeypatch.setattr(wda_module, "_start_driver", launched)

        async def stop_lands(*args, **kwargs):
            backend.note_stopped(PHONE)

        monkeypatch.setattr(wda_module, "stop_driver",
                            AsyncMock(side_effect=stop_lands if during == "stop_driver" else None))
        monkeypatch.setattr(wda_module, "refuse_without_xcode",
                            AsyncMock(side_effect=stop_lands
                                      if during == "refuse_without_xcode" else None))
        await backend._restart_wda(PHONE)
        launched.assert_not_called()

    async def test_a_restart_with_no_stop_still_starts(self, backend, monkeypatch):
        launched = AsyncMock(return_value={"status": "started", "ready": True})
        monkeypatch.setattr(wda_module, "_start_driver", launched)
        monkeypatch.setattr(wda_module, "stop_driver", AsyncMock())
        monkeypatch.setattr(wda_module, "refuse_without_xcode", AsyncMock())
        await backend._restart_wda(PHONE)
        launched.assert_awaited_once()

    async def test_a_crash_latch_does_not_send_reads_to_a_stopped_wda(self, monkeypatch):
        """After a push crash, reads went to a WDA that refused every one, and
        the phone could not be read until start_driver (review)."""
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        from server.tooling import wda_availability
        monkeypatch.setattr(wda_availability, "known_reason", lambda: None)
        ctrl._push_crashed_apps[PHONE] = {"Demo"}
        assert ctrl._crash_latched(PHONE) == {"Demo"}
        ctrl.wda_client.note_stopped(PHONE)
        assert ctrl._crash_latched(PHONE) == set()

    async def test_a_push_read_that_fails_does_not_start_it(self, monkeypatch):
        """The read fails saying why, instead of falling back to WDA."""
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl.coredevice_ax.describe_all = AsyncMock(
            side_effect=DeviceError("the push could not read it", tool="coredevice"))
        ctrl.wda_client.note_stopped(PHONE)

        # Its own type, so its 400 survives the push's context (CodeRabbit on #773).
        with pytest.raises(WdaNotSetUpError, match="stopped with stop_driver"):
            await ctrl.get_ui_elements(PHONE, use_cache=False, whole_screen=True)
        ctrl.wda_client.describe_all.assert_not_called()

    async def test_a_busy_runner_is_not_fallen_back_to_either(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl.coredevice_ax.describe_all = AsyncMock(
            side_effect=DeviceError("the push could not read it", tool="coredevice"))
        import time
        ctrl.wda_client._unserving[PHONE] = (time.monotonic() + 60, "no /status")

        # A 503, not the plain DeviceError's 500 (CodeRabbit on #773).
        with pytest.raises(WdaRunnerBusyError, match="running but not answering") as e:
            await ctrl.get_ui_elements(PHONE, use_cache=False, whole_screen=True)
        assert "the push could not read it" in str(e.value)
        ctrl.wda_client.describe_all.assert_not_called()


def test_a_busy_runner_answers_503():
    from server.api.device import _handle_device_error

    assert _handle_device_error(WdaRunnerBusyError("busy", tool="wda")).status_code == 503


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


class TestStickyOnlyWhereThePushReads:
    """On a phone before iOS 27 WDA is the only reader, and the next read
    restarting it is how stop_driver has always reset it (review)."""

    async def _stop(self, monkeypatch, device_type, os_version, serves, busy=False):
        from httpx import ASGITransport, AsyncClient

        import server.main as m
        from server.config import ServerConfig
        from server.models import DeviceInfo, DeviceState

        app = m.create_app(ServerConfig(api_key="k" * 32))
        device = DeviceInfo(udid=PHONE, name="d", state=DeviceState.BOOTED,
                            device_type=device_type, os_version=os_version, is_available=True)
        controller = MagicMock()
        controller.list_devices = AsyncMock(return_value=[device])
        controller.wda_client = WdaBackend()             # the real one is told
        if busy:
            import time
            controller.wda_client._unserving[PHONE] = (time.monotonic() + 60, "no /status")
        controller.wda_client.delete_session = AsyncMock()
        controller._coredevice_serves = lambda udid: serves
        controller._backend_name = lambda udid: "sim-bridge"
        app.state.device_controller = controller
        monkeypatch.setattr(wda_module, "stop_driver",
                            AsyncMock(return_value={"status": "stopped", "udid": PHONE}))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/api/v1/device/wda/stop", json={"udid": PHONE},
                             headers={"Authorization": "Bearer " + "k" * 32})
        assert r.status_code == 200, r.text
        return controller.wda_client, r.json()

    async def test_a_phone_the_push_reads_stays_stopped(self, monkeypatch):
        from server.models import DeviceType
        client, body = await self._stop(monkeypatch, DeviceType.DEVICE, "27.2.1", True)
        assert PHONE in client._stopped_on_request and "auto_start" in body

    async def test_an_older_phone_does_not(self, monkeypatch):
        from server.models import DeviceType
        client, body = await self._stop(monkeypatch, DeviceType.DEVICE, "18.5", False)
        assert PHONE not in client._stopped_on_request and "auto_start" not in body

    async def test_an_older_phone_forgets_a_busy_runner(self, monkeypatch):
        """Its next read restarts WDA, as stop_driver always has: remembered
        as busy, it was refused for up to a minute instead (CodeRabbit on
        #773)."""
        from server.models import DeviceType
        client, _ = await self._stop(monkeypatch, DeviceType.DEVICE, "18.5", False, busy=True)
        assert client.no_auto_start_reason(PHONE) is None

    async def test_a_simulator_does_not(self, monkeypatch):
        from server.models import DeviceType
        client, body = await self._stop(monkeypatch, DeviceType.SIMULATOR, "18.5", True)
        assert PHONE not in client._stopped_on_request and "auto_start" not in body
