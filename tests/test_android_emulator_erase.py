"""Erasing an Android emulator is a kill and a boot with `-wipe-data` (#356).

`-wipe-data` is an `emulator` launch flag, not something a running emulator can
be told, so the erase ends the session and the device comes back running,
possibly on another console port. Nobody calls erase by accident, so that is
the point of the call rather than a side effect.

The tests that matter most are about order and refusal: nothing may be killed
before a refusal is decided, and a second instance must never be booted while
the first is still listed.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from server.config import ServerConfig
from server.device import adb as adb_module
from server.device.controller import DeviceController
from server.main import create_app
from server.models import DeviceError, DeviceState, DeviceType

OLD, NEW, AVD = "emulator-5554", "emulator-5556", "quern_probe_356"


def _listed(*serials):
    return [SimpleNamespace(udid=s, state=DeviceState.BOOTED) for s in serials]


def _emulator(monkeypatch, *, headless=False, comes_back_as=OLD, listed_after_kill=()):
    """A controller whose adb behaves like a console-attached emulator."""
    ctrl = DeviceController()
    ctrl._device_type_cache[OLD] = DeviceType.ANDROID_EMULATOR
    calls: list[str] = []

    async def run(serial, *args):
        calls.append("kill" if args == ("emu", "kill") else " ".join(args))
        return "", ""

    async def booted(serial, timeout):
        calls.append(f"boot_completed {serial}")

    async def boot(avd, timeout=60, headless=False, wipe_data=False):
        calls.append(f"boot {avd} headless={headless} wipe_data={wipe_data}")
        return comes_back_as

    monkeypatch.setattr(ctrl.adb, "avd_name", AsyncMock(return_value=AVD))
    monkeypatch.setattr(ctrl.adb, "emulator_was_headless", AsyncMock(return_value=headless))
    monkeypatch.setattr(ctrl.adb, "_run_adb_for_device", run)
    monkeypatch.setattr(ctrl.adb, "list_devices",
                        AsyncMock(return_value=_listed(*listed_after_kill)))
    monkeypatch.setattr(ctrl.adb, "boot_emulator", boot)
    monkeypatch.setattr(ctrl.adb, "wait_for_boot_completed", booted)
    return ctrl, calls


class TestAnEmulatorIsWipedByRelaunching:
    async def test_it_is_killed_then_booted_with_wipe_data(self, monkeypatch):
        ctrl, calls = _emulator(monkeypatch)

        assert await ctrl.erase(OLD) == OLD

        # And it answers only once Android has started -- measured live, adb
        # listed the device about nine seconds before it was usable.
        assert calls == ["kill", f"boot {AVD} headless=False wipe_data=True",
                         f"boot_completed {OLD}"]

    @pytest.mark.parametrize("headless", [True, False])
    async def test_it_comes_back_the_way_it_was_launched(self, monkeypatch, headless):
        """A window appearing on a desktop that had none, or vanishing from one
        somebody was watching, is a change nobody asked for."""
        ctrl, calls = _emulator(monkeypatch, headless=headless)

        await ctrl.erase(OLD)

        assert f"boot {AVD} headless={headless} wipe_data=True" in calls

    async def test_a_new_serial_takes_the_cache_and_the_active_device_with_it(
        self, monkeypatch,
    ):
        ctrl, _ = _emulator(monkeypatch, comes_back_as=NEW)
        ctrl._active_udid = OLD

        assert await ctrl.erase(OLD) == NEW

        assert ctrl._device_type_cache.get(NEW) == DeviceType.ANDROID_EMULATOR
        assert OLD not in ctrl._device_type_cache
        assert ctrl._active_udid == NEW

    async def test_a_device_that_was_not_active_does_not_become_active(self, monkeypatch):
        ctrl, _ = _emulator(monkeypatch, comes_back_as=NEW)
        ctrl._active_udid = "SOMETHING-ELSE"

        await ctrl.erase(OLD)

        assert ctrl._active_udid == "SOMETHING-ELSE"

    async def test_it_never_boots_while_the_old_one_is_still_listed(self, monkeypatch):
        """Booting the same AVD twice starts a second instance against the same
        disk images. If the kill did not take, the erase stops and says so."""
        ctrl, calls = _emulator(monkeypatch, listed_after_kill=(OLD,))
        monkeypatch.setattr(DeviceController, "_ERASE_KILL_TIMEOUT", 0.01)

        with pytest.raises(DeviceError, match="has not been wiped"):
            await ctrl.erase(OLD)

        assert not any(c.startswith("boot") for c in calls), calls


class TestRefusalsComeBeforeAnythingIsKilled:
    async def test_an_emulator_attached_over_tcp(self, monkeypatch):
        """`adb emu` only travels over the local console serial, the same
        constraint `shutdown` has, so the AVD name cannot be asked for."""
        ctrl, calls = _emulator(monkeypatch)
        ctrl._device_type_cache["127.0.0.1:5555"] = DeviceType.ANDROID_EMULATOR

        with pytest.raises(DeviceError, match="over TCP"):
            await ctrl.erase("127.0.0.1:5555")

        assert calls == []

    async def test_a_physical_phone(self, monkeypatch):
        ctrl, calls = _emulator(monkeypatch)
        ctrl._device_type_cache["8BAY0WCL7"] = DeviceType.ANDROID_DEVICE
        ctrl.simctl.erase = AsyncMock()

        with pytest.raises(DeviceError) as e:
            await ctrl.erase("8BAY0WCL7")

        assert e.value.tool == "adb"
        assert calls == []
        ctrl.simctl.erase.assert_not_awaited()


async def test_a_simulator_erase_is_unchanged():
    ctrl = DeviceController()
    sim = "F5AF3736-C05F-493F-AA52-CA883B13B18C"
    ctrl._device_type_cache[sim] = DeviceType.SIMULATOR
    ctrl._active_udid = sim
    ctrl.simctl.shutdown = AsyncMock()
    ctrl.simctl.erase = AsyncMock()

    assert await ctrl.erase(sim) == sim

    ctrl.simctl.erase.assert_awaited_once_with(sim)
    assert ctrl._active_udid is None


class TestTheAdbHelpers:
    @pytest.mark.parametrize("console_reply", [
        f"{AVD}\r\nOK\r\n", f"{AVD}\nOK\n", f"\r\n{AVD}\r\nOK",
    ])
    async def test_the_avd_name_is_read_off_the_console(self, monkeypatch, console_reply):
        backend = adb_module.AdbBackend()
        monkeypatch.setattr(backend, "_run_adb_for_device",
                            AsyncMock(return_value=(console_reply, "")))
        assert await backend.avd_name(OLD) == AVD

    async def test_a_console_that_names_nothing_is_an_error(self, monkeypatch):
        backend = adb_module.AdbBackend()
        monkeypatch.setattr(backend, "_run_adb_for_device",
                            AsyncMock(return_value=("OK\r\n", "")))
        with pytest.raises(DeviceError):
            await backend.avd_name(OLD)

    @pytest.mark.parametrize("ps_lines,expected", [
        ([f"/sdk/emulator/qemu/qemu-system-aarch64 -avd {AVD} -no-window -no-audio"], True),
        ([f"/sdk/emulator/qemu/qemu-system-aarch64 -avd {AVD} -no-audio"], False),
        # Another AVD's flag is not ours -- and a name that merely *starts*
        # with ours is another AVD.
        ([f"qemu -avd {AVD}_old -no-window", f"qemu -avd {AVD}"], False),
        (["/usr/bin/zsh", "python -m server"], False),
    ])
    async def test_headlessness_is_read_off_the_process(
        self, monkeypatch, ps_lines, expected,
    ):
        class Proc:
            async def communicate(self):
                return ("\n".join(ps_lines) + "\n").encode(), b""

        async def fake_exec(*_a, **_k):
            return Proc()

        monkeypatch.setattr(adb_module.asyncio, "create_subprocess_exec", fake_exec)
        assert await adb_module.AdbBackend().emulator_was_headless(AVD) is expected

    @pytest.mark.parametrize("wipe", [True, False])
    async def test_wipe_data_reaches_the_emulator_command_line(self, monkeypatch, wipe):
        backend = adb_module.AdbBackend()
        backend._emulator_path = "/sdk/emulator/emulator"
        monkeypatch.setattr(backend, "list_avds", AsyncMock(return_value=[AVD]))
        monkeypatch.setattr(backend, "list_devices", AsyncMock(return_value=[]))
        launched: list[tuple] = []

        class Launched(Exception):
            pass

        async def fake_exec(*args, **_k):
            launched.append(args)
            raise Launched  # stop before the wait-for-boot loop

        monkeypatch.setattr(adb_module.asyncio, "create_subprocess_exec", fake_exec)
        with pytest.raises(Launched):
            await backend.boot_emulator(AVD, wipe_data=wipe)

        assert ("-wipe-data" in launched[0]) is wipe, launched[0]


class TestTheRoute:
    @pytest.fixture
    def client(self):
        app = create_app(config=ServerConfig(api_key="k"), enable_oslog=False,
                         enable_crash=False, enable_proxy=False)
        ctrl = MagicMock()
        ctrl._active_udid = None
        ctrl.resolve_udid = AsyncMock(side_effect=lambda udid=None: udid)
        app.state.device_controller = ctrl
        app.state.proxy_adapter = None
        app.state.flow_store = None
        return TestClient(app), ctrl

    def test_a_relaunched_emulator_reports_where_it_is_now(self, client, monkeypatch):
        tc, ctrl = client
        ctrl.erase = AsyncMock(return_value=NEW)
        ctrl._device_type = lambda u: DeviceType.ANDROID_EMULATOR
        invalidated = []
        monkeypatch.setattr("server.api.device._invalidate_cert_record", invalidated.append)

        r = tc.post("/api/v1/device/erase", json={"udid": OLD},
                    headers={"Authorization": "Bearer k"})

        assert r.status_code == 200, r.text
        assert r.json() == {"status": "erased", "udid": NEW,
                            "restarted": True, "previous_udid": OLD}
        # Either serial's record would otherwise still claim the CA is trusted.
        assert sorted(invalidated) == sorted([OLD, NEW])

    def test_an_emulator_on_the_same_port_still_says_it_restarted(self, client, monkeypatch):
        tc, ctrl = client
        ctrl.erase = AsyncMock(return_value=OLD)
        ctrl._device_type = lambda u: DeviceType.ANDROID_EMULATOR
        monkeypatch.setattr("server.api.device._invalidate_cert_record", lambda u: None)

        r = tc.post("/api/v1/device/erase", json={"udid": OLD},
                    headers={"Authorization": "Bearer k"})

        assert r.json() == {"status": "erased", "udid": OLD, "restarted": True}

    def test_a_simulator_response_is_unchanged(self, client, monkeypatch):
        tc, ctrl = client
        sim = "F5AF3736-C05F-493F-AA52-CA883B13B18C"
        ctrl.erase = AsyncMock(return_value=sim)
        ctrl._device_type = lambda u: DeviceType.SIMULATOR
        monkeypatch.setattr("server.api.device._invalidate_cert_record", lambda u: None)

        r = tc.post("/api/v1/device/erase", json={"udid": sim},
                    headers={"Authorization": "Bearer k"})

        assert r.json() == {"status": "erased", "udid": sim}


class TestARefusalIsA400NotA500:
    """A device that cannot do what was asked is a refusal, not a server fault.

    The refusals `_handle_device_error` recognised were the ones whose text
    happened to contain "only supported on simulators"; an accurately worded
    one -- "not possible on a physical Android device" -- fell through to a 500
    prefixed `[adb]`, which tells the caller quern broke. Driven through a real
    controller rather than a mock, so the type has to survive the whole way.
    """

    @pytest.fixture
    def client(self):
        app = create_app(config=ServerConfig(api_key="k"), enable_oslog=False,
                         enable_crash=False, enable_proxy=False)
        ctrl = DeviceController()
        ctrl.resolve_udid = AsyncMock(side_effect=lambda udid=None, **_k: udid)
        ctrl._device_type_cache["8BAY0WCL7"] = DeviceType.ANDROID_DEVICE
        ctrl._device_type_cache["127.0.0.1:5555"] = DeviceType.ANDROID_EMULATOR
        app.state.device_controller = ctrl
        app.state.proxy_adapter = None
        app.state.flow_store = None
        return TestClient(app)

    @pytest.mark.parametrize("route,udid", [
        ("erase", "8BAY0WCL7"),
        ("erase", "127.0.0.1:5555"),
        ("shutdown", "8BAY0WCL7"),
        ("shutdown", "127.0.0.1:5555"),
    ])
    def test_the_refusal_is_a_400(self, client, route, udid):
        r = client.post(f"/api/v1/device/{route}", json={"udid": udid},
                        headers={"Authorization": "Bearer k"})
        assert r.status_code == 400, r.text
        assert not r.json()["detail"].startswith("[adb]"), r.text



class TestWaitingForBootCompleted:
    async def test_it_waits_through_an_unset_property_and_a_refused_shell(self, monkeypatch):
        """Both happen during a real boot: the property reads empty, and adb
        can refuse the shell outright while the device is still coming up."""
        backend = adb_module.AdbBackend()
        answers = [DeviceError("device offline"), ("\n", ""), ("1\n", "")]

        async def getprop(serial, *args):
            a = answers.pop(0)
            if isinstance(a, Exception):
                raise a
            return a

        monkeypatch.setattr(backend, "_run_adb_for_device", getprop)
        monkeypatch.setattr(adb_module.asyncio, "sleep", AsyncMock())

        await backend.wait_for_boot_completed(OLD, timeout=30)

        assert answers == []

    async def test_a_boot_that_never_completes_is_an_error(self, monkeypatch):
        backend = adb_module.AdbBackend()
        monkeypatch.setattr(backend, "_run_adb_for_device",
                            AsyncMock(return_value=("\n", "")))

        with pytest.raises(DeviceError, match="had not finished starting"):
            await backend.wait_for_boot_completed(OLD, timeout=0.01)
