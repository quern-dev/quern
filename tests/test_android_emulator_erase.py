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
from server.models import DeviceError, DeviceState, DeviceType, EraseIncompleteError

OLD, NEW, AVD = "emulator-5554", "emulator-5556", "quern_probe_356"


def _listed(*serials):
    return [SimpleNamespace(udid=s, state=DeviceState.BOOTED) for s in serials]


def _emulator(monkeypatch, *, headless=False, comes_back_as=OLD, listed_after_kill=(),
              still_running=False, boot_fails=None, completed_fails=None):
    """A controller whose adb behaves like a console-attached emulator."""
    ctrl = DeviceController()
    ctrl._device_type_cache[OLD] = DeviceType.ANDROID_EMULATOR
    calls: list[str] = []

    async def run(serial, *args):
        calls.append("kill" if args == ("emu", "kill") else " ".join(args))
        return "", ""

    async def booted(serial, timeout):
        calls.append(f"boot_completed {serial}")
        if completed_fails:
            raise completed_fails

    async def boot(avd, timeout=60, headless=False, wipe_data=False):
        calls.append(f"boot {avd} headless={headless} wipe_data={wipe_data}")
        if boot_fails:
            raise boot_fails
        return comes_back_as

    async def precheck(avd):
        calls.append(f"precheck {avd}")

    monkeypatch.setattr(ctrl.adb, "avd_name", AsyncMock(return_value=AVD))
    monkeypatch.setattr(ctrl.adb, "emulator_was_headless", AsyncMock(return_value=headless))
    monkeypatch.setattr(ctrl.adb, "_run_adb_for_device", run)
    monkeypatch.setattr(ctrl.adb, "list_devices",
                        AsyncMock(return_value=_listed(*listed_after_kill)))
    monkeypatch.setattr(ctrl.adb, "boot_emulator", boot)
    monkeypatch.setattr(ctrl.adb, "wait_for_boot_completed", booted)
    monkeypatch.setattr(ctrl.adb, "check_can_boot", precheck)
    monkeypatch.setattr(ctrl.adb, "emulator_running",
                        AsyncMock(return_value=still_running))
    return ctrl, calls


class TestAnEmulatorIsWipedByRelaunching:
    async def test_it_is_killed_then_booted_with_wipe_data(self, monkeypatch):
        ctrl, calls = _emulator(monkeypatch)

        assert await ctrl.erase(OLD) == OLD

        # And it answers only once Android has started -- measured live, adb
        # listed the device about nine seconds before it was usable.
        assert calls == [f"precheck {AVD}", "kill",
                         f"boot {AVD} headless=False wipe_data=True",
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
        ctrl, calls = _emulator(monkeypatch, comes_back_as=NEW)
        ctrl._active_udid = OLD

        assert await ctrl.erase(OLD) == NEW

        # On a port change the old serial no longer exists; waiting on it
        # would spend the whole budget on a device that is not coming back.
        assert f"boot_completed {NEW}" in calls, calls
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

    async def test_a_process_still_running_also_blocks_the_boot(self, monkeypatch):
        """adb can stop listing a serial -- or fail outright, which
        `list_devices` reports as an empty list -- while the emulator is still
        up. The process is the second witness."""
        ctrl, calls = _emulator(monkeypatch, still_running=True)
        monkeypatch.setattr(DeviceController, "_ERASE_KILL_TIMEOUT", 0.01)

        with pytest.raises(DeviceError, match="has not been wiped"):
            await ctrl.erase(OLD)

        assert not any(c.startswith("boot") for c in calls), calls

    async def test_an_unknowable_process_state_falls_back_to_adb(self, monkeypatch):
        """No `ps` is "could not look", not "still running": adb decides."""
        ctrl, calls = _emulator(monkeypatch, still_running=None)

        await ctrl.erase(OLD)

        assert any(c.startswith("boot") for c in calls), calls

    async def test_a_shut_down_avd_is_booted_wiped_without_a_kill(self, monkeypatch):
        """Listed as `avd:NAME` because it has no serial. Previously refused as
        'attached over TCP', which was false twice over."""
        ctrl, calls = _emulator(monkeypatch, comes_back_as=NEW)

        assert await ctrl.erase(f"avd:{AVD}") == NEW

        assert "kill" not in calls
        assert f"boot {AVD} headless=False wipe_data=True" in calls

    async def test_the_old_serials_per_device_state_is_dropped(self, monkeypatch):
        """A UI tree of screens the wipe removed, and a uiautomator2 connection
        to an agent the wipe uninstalled, are both stale the moment it boots."""
        ctrl, _ = _emulator(monkeypatch)
        ctrl._ui_cache[OLD] = ([], 0.0)
        ctrl._input_checked[OLD] = True
        ctrl.u2._devices[OLD] = object()

        await ctrl.erase(OLD)

        assert OLD not in ctrl._ui_cache
        assert OLD not in ctrl._input_checked
        assert OLD not in ctrl.u2._devices


class TestNothingIsKilledUntilTheBootCanHappen:
    async def test_an_avd_this_server_cannot_launch_is_left_running(self, monkeypatch):
        """No `emulator` binary, or an AVD from a different SDK or
        `ANDROID_AVD_HOME`, used to be discovered by `boot_emulator` -- after
        the kill, leaving the emulator dead and unwiped."""
        ctrl, calls = _emulator(monkeypatch)

        async def cannot(avd):
            raise DeviceError("AVD not known here", tool="emulator")

        monkeypatch.setattr(ctrl.adb, "check_can_boot", cannot)

        with pytest.raises(DeviceError, match="not known here"):
            await ctrl.erase(OLD)

        assert "kill" not in calls

    async def test_an_avd_already_booting_or_erasing_is_refused(self, monkeypatch):
        """Two erases of one emulator both killed and both launched."""
        ctrl, calls = _emulator(monkeypatch)
        ctrl.adb._booting_avds.add(AVD)

        with pytest.raises(DeviceError, match="already being booted or erased"):
            await ctrl.erase(OLD)

        assert "kill" not in calls


class TestAFailureAfterTheKillSaysTheEmulatorIsGone:
    async def test_a_boot_that_fails(self, monkeypatch):
        ctrl, _ = _emulator(monkeypatch, boot_fails=DeviceError("timed out", tool="emulator"))
        ctrl._active_udid = OLD

        with pytest.raises(EraseIncompleteError) as e:
            await ctrl.erase(OLD)

        assert "did not come back" in str(e.value)
        assert e.value.previous_udid == OLD and e.value.udid is None
        # Not left pointing at a serial that no longer exists.
        assert ctrl._active_udid is None
        assert OLD not in ctrl._device_type_cache
        assert AVD not in ctrl.adb._booting_avds

    async def test_a_boot_that_never_completes_has_already_moved_quern(self, monkeypatch):
        ctrl, _ = _emulator(monkeypatch, comes_back_as=NEW,
                            completed_fails=DeviceError("slow", tool="adb"))
        ctrl._active_udid = OLD

        with pytest.raises(EraseIncompleteError) as e:
            await ctrl.erase(OLD)

        assert e.value.udid == NEW
        assert ctrl._active_udid == NEW
        assert ctrl._device_type_cache.get(NEW) == DeviceType.ANDROID_EMULATOR
        assert AVD not in ctrl.adb._booting_avds


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
        ([f"/sdk/emulator/qemu/qemu-system-aarch64 -avd {AVD}_old -no-window",
          f"/sdk/emulator/qemu/qemu-system-aarch64 -avd {AVD}"], False),
        (["/usr/bin/zsh", "python -m server"], False),
        # The launcher's other spelling.
        ([f"/sdk/emulator/emulator @{AVD} -no-window"], True),
        # Android Studio's embedded emulator.
        ([f"/sdk/emulator/qemu/qemu-system-aarch64 -avd {AVD} -qt-hide-window"], True),
        # The shell that launched it says `-no-window` too, and is not it.
        ([f"zsh -c /sdk/emulator/emulator -avd {AVD} -no-window",
          f"/sdk/emulator/qemu/qemu-system-aarch64 -avd {AVD}"], False),
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
        # As in production after a port change: the erase dropped the old
        # serial from the type cache, so only the new one answers.
        ctrl._device_type = lambda u: DeviceType.ANDROID_EMULATOR if u == NEW else None
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



class TestReadingTheProcessTable:
    async def test_a_launching_shell_is_not_counted_as_the_emulator(self, monkeypatch):
        """Measured: the shell that started an emulator stayed alive as a
        session leader for minutes after the emulator exited, carrying
        `-avd NAME` on its own command line. Counted, it reads as an emulator
        that will not die."""
        class Proc:
            async def communicate(self):
                return f"zsh -c /sdk/emulator/emulator -avd {AVD} -no-window\n".encode(), b""

        async def fake_exec(*_a, **_k):
            return Proc()

        monkeypatch.setattr(adb_module.asyncio, "create_subprocess_exec", fake_exec)
        assert await adb_module.AdbBackend().emulator_running(AVD) is False

    async def test_no_ps_is_unknown_not_none_running(self, monkeypatch):
        async def missing(*_a, **_k):
            raise FileNotFoundError("ps")

        monkeypatch.setattr(adb_module.asyncio, "create_subprocess_exec", missing)
        backend = adb_module.AdbBackend()
        assert await backend.emulator_running(AVD) is None
        # And the docstring's promise: "False when it cannot tell".
        assert await backend.emulator_was_headless(AVD) is False


class TestBootAdoptsOnlyItsOwnAvd:
    async def test_another_avd_booting_at_the_same_moment_is_skipped(self, monkeypatch):
        """`boot_emulator` returned the first new serial it saw. Another AVD
        booting concurrently was adopted as this one, and an erase then moved
        the active device to the wrong emulator."""
        backend = adb_module.AdbBackend()
        backend._emulator_path = "/sdk/emulator/emulator"
        monkeypatch.setattr(backend, "list_avds", AsyncMock(return_value=[AVD]))
        listings = [[], _listed("emulator-5556", "emulator-5558")]
        monkeypatch.setattr(backend, "list_devices",
                            AsyncMock(side_effect=lambda: listings.pop(0) if listings
                                      else _listed("emulator-5556", "emulator-5558")))
        names = {"emulator-5556": "SomeOtherAvd", "emulator-5558": AVD}
        monkeypatch.setattr(backend, "avd_name",
                            AsyncMock(side_effect=lambda serial: names[serial]))

        async def spawned(*_a, **_k):
            return None

        monkeypatch.setattr(adb_module.asyncio, "create_subprocess_exec", spawned)
        monkeypatch.setattr(adb_module.asyncio, "sleep", AsyncMock())

        assert await backend.boot_emulator(AVD, timeout=30) == "emulator-5558"



def test_an_incomplete_erase_still_withdraws_the_cert_record(monkeypatch):
    """Once the `-wipe-data` launch has started the data is gone, whether or
    not the boot finished -- so the record claiming the CA is trusted goes, for
    both serials. A *refused* erase keeps it; that one destroyed nothing."""
    app = create_app(config=ServerConfig(api_key="k"), enable_oslog=False,
                     enable_crash=False, enable_proxy=False)
    ctrl = MagicMock()
    ctrl._active_udid = None
    ctrl.resolve_udid = AsyncMock(side_effect=lambda udid=None: udid)
    ctrl.erase = AsyncMock(side_effect=EraseIncompleteError(
        "gone", previous_udid=OLD, udid=NEW))
    app.state.device_controller = ctrl
    app.state.proxy_adapter = None
    app.state.flow_store = None
    invalidated = []
    monkeypatch.setattr("server.api.device._invalidate_cert_record", invalidated.append)

    r = TestClient(app).post("/api/v1/device/erase", json={"udid": OLD},
                             headers={"Authorization": "Bearer k"})

    assert r.status_code >= 500, r.text
    assert sorted(invalidated) == sorted([OLD, NEW])



async def test_an_unattended_launch_never_stops_to_ask_about_a_crash(monkeypatch):
    """A windowed launch after a recorded crash showed a consent dialog and
    waited forever; quern reported only a boot timeout."""
    backend = adb_module.AdbBackend()
    backend._emulator_path = "/sdk/emulator/emulator"
    monkeypatch.setattr(backend, "list_avds", AsyncMock(return_value=[AVD]))
    monkeypatch.setattr(backend, "list_devices", AsyncMock(return_value=[]))
    launched: list[tuple] = []

    class Launched(Exception):
        pass

    async def fake_exec(*args, **_k):
        launched.append(args)
        raise Launched

    monkeypatch.setattr(adb_module.asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(Launched):
        await backend.boot_emulator(AVD)

    argv = list(launched[0])
    assert argv[argv.index("-crash-report-mode") + 1] == "never", argv


class TestTheReservationHoldsForTheWholeErase:
    async def test_it_is_still_held_while_boot_completed_is_awaited(self, monkeypatch):
        """Uses the *real* `boot_emulator`: it set the marker too and discarded
        it on the way out, dropping the erase's reservation mid-erase. The
        first test of this mocked `boot_emulator` and so could not see it."""
        ctrl, _ = _emulator(monkeypatch)
        # The real `boot_emulator` refuses before reaching the marker when no
        # emulator binary was found -- true on CI's Linux runner, which has no
        # Android SDK, so without this the test failed there for that reason.
        ctrl.adb._emulator_path = "/sdk/emulator/emulator"
        monkeypatch.setattr(ctrl.adb, "boot_emulator",
                            adb_module.AdbBackend.boot_emulator.__get__(ctrl.adb))

        async def inner(avd, timeout, headless, wipe_data):
            return OLD

        monkeypatch.setattr(ctrl.adb, "_boot_emulator_inner", inner)
        held_during_wait = []

        async def booted(serial, timeout):
            held_during_wait.append(AVD in ctrl.adb._booting_avds)

        monkeypatch.setattr(ctrl.adb, "wait_for_boot_completed", booted)

        await ctrl.erase(OLD)

        assert held_during_wait == [True]
        assert AVD not in ctrl.adb._booting_avds   # and released at the end

    async def test_a_plain_boot_still_releases_its_own_marker(self, monkeypatch):
        backend = adb_module.AdbBackend()
        backend._emulator_path = "/sdk/emulator/emulator"   # as above: no SDK on CI

        async def inner(avd, timeout, headless, wipe_data):
            return OLD

        monkeypatch.setattr(backend, "_boot_emulator_inner", inner)
        await backend.boot_emulator(AVD)
        assert AVD not in backend._booting_avds


class TestAKillThatReportsFailureIsJudgedByWhatHappened:
    """`emu kill` is a write and not retryable; a non-zero exit after the
    command went out can still mean the emulator died."""

    async def test_it_went_anyway_so_the_erase_continues(self, monkeypatch):
        ctrl, calls = _emulator(monkeypatch)

        async def run(serial, *args):
            calls.append("kill")
            raise DeviceError("adb: connection reset", tool="adb")

        monkeypatch.setattr(ctrl.adb, "_run_adb_for_device", run)

        assert await ctrl.erase(OLD) == OLD
        assert any(c.startswith("boot ") for c in calls), calls

    async def test_it_is_still_running_so_it_was_not_wiped(self, monkeypatch):
        ctrl, calls = _emulator(monkeypatch, listed_after_kill=(OLD,), still_running=True)
        monkeypatch.setattr(DeviceController, "_ERASE_KILL_TIMEOUT", 0.01)

        async def run(serial, *args):
            raise DeviceError("adb: connection reset", tool="adb")

        monkeypatch.setattr(ctrl.adb, "_run_adb_for_device", run)

        with pytest.raises(DeviceError, match="has not been wiped"):
            await ctrl.erase(OLD)
        assert not any(c.startswith("boot ") for c in calls), calls
