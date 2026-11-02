"""A device is not reservable while its release clean-up runs (#598).

The clean-up awaits device commands -- an erase, an uninstall, a proxy clear
-- and the registry let the releasing key reserve the device again while it
drained. A hold taken during one of those awaits had the command finish under
it: its simulator erased, its app removed. Checks between commands (#597)
narrowed that to a single command; the mark closes it."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from server.session_registry import OWNER, SessionConflict, SessionRegistry

SIM = "A1B2C3D4-0000-0000-0000-000000000001"


def _registry():
    registry = SessionRegistry()
    registry.open("s1", principal="client:a")
    return registry


@pytest.mark.parametrize("principal", ["client:a", "client:b", OWNER])
def test_nobody_reserves_a_device_being_cleaned_up(principal):
    """Its own key included -- that is the case that erased a new hold -- and
    the owner too: what is running is a device command, not a stuck drain."""
    registry = _registry()
    registry.open("s2", principal=principal)
    with registry.cleaning([SIM]):
        with pytest.raises(SessionConflict, match="being cleaned up"):
            registry.reserve("s2", [SIM])
        assert SIM in registry.held_udids(), "the pool must not pick it either"
    registry.reserve("s2", [SIM])


def test_overlapping_clean_ups_keep_it_until_both_finish():
    registry = _registry()
    with registry.cleaning([SIM]):
        with registry.cleaning([SIM]):
            pass
        with pytest.raises(SessionConflict):
            registry.reserve("s1", [SIM])
    assert registry.reserve("s1", [SIM])


def test_a_clean_up_that_raises_still_lets_go():
    registry = _registry()
    with pytest.raises(RuntimeError), registry.cleaning([SIM]):
        raise RuntimeError("the sweep failed")
    assert registry.reserve("s1", [SIM])
    assert SIM not in registry.held_udids(except_session="s1")


async def test_a_reservation_during_the_sweeps_uninstall_is_refused():
    """End to end: a client's release uninstalls its app, and the same key
    reserves the device while adb runs. Refused, and free once it is done."""
    from server import session_installs as si
    from server.api import sessions as sessions_api

    registry = _registry()
    registry.reserve("s1", [SIM])
    session = registry.get("s1")
    controller = MagicMock()
    controller._device_type = lambda udid: None
    controller.u2 = None
    controller.list_apps = AsyncMock(return_value=([SimpleNamespace(
        bundle_id="com.example.app", app_type="User")], SIM))
    attempts = []

    async def uninstall(bundle_id, udid, *, set_active=True):
        try:
            registry.reserve("s1", [SIM])
            attempts.append("reserved")
        except SessionConflict as e:
            attempts.append(str(e))

    controller.uninstall_app = AsyncMock(side_effect=uninstall)
    app = SimpleNamespace(state=SimpleNamespace(
        sessions=registry, device_controller=controller, proxy_adapter=None,
        recordings=None, mock_owners={}, release_policy={"client": "uninstall"},
        session_installs={SIM: si.InstallRecord(session=session.id,
                                                opened=session.opened_at,
                                                added={"com.example.app"})}))
    registry.unreserve("s1", [SIM])
    actions = await sessions_api.run_release_policies(app)
    assert attempts and "being cleaned up" in attempts[0]
    assert any(a.get("action") == "installs_removed" for a in actions)
    assert registry.reserve("s1", [SIM]), "free once the clean-up has run"


SIM2 = "A1B2C3D4-0000-0000-0000-000000000002"


def _app(registry, controller, **state):
    return SimpleNamespace(state=SimpleNamespace(
        sessions=registry, device_controller=controller, proxy_adapter=None,
        recordings=None, mock_owners={}, release_policy={"client": "uninstall"}, **state))


async def test_every_released_sessions_devices_are_marked_from_the_start():
    """Two sessions released in one sweep: while the first's clean-up runs,
    the second's device is not reservable either (review)."""
    from server.api import sessions as sessions_api

    registry = _registry()
    registry.open("s2", principal="client:a")
    registry.reserve("s1", [SIM])
    registry.reserve("s2", [SIM2])
    attempts = []

    async def first_clean_up(app, reg, session):
        if session.id.endswith("s1"):
            try:
                reg.reserve("s1", [SIM2])
                attempts.append("reserved")
            except SessionConflict as e:
                attempts.append(str(e))
        return []

    registry.release(registry.get("s1").id)
    registry.release(registry.get("s2").id)
    registry.open("s1", principal="client:a")
    controller = MagicMock(u2=None)
    app = _app(registry, controller)
    from unittest.mock import patch
    with patch.object(sessions_api, "_end_client_work", first_clean_up):
        await sessions_api.run_release_policies(app)
    assert attempts and "being cleaned up" in attempts[0]


async def test_a_hung_shutdown_on_release_is_bounded(monkeypatch):
    """A shutdown that never returns kept the device marked, the owner's
    reservations refused with it, for as long as it hung (review)."""
    import asyncio

    from server.api import sessions as sessions_api

    monkeypatch.setattr(sessions_api, "_SHUTDOWN_TIMEOUT_S", 0.1)
    registry = _registry()
    registry.reserve("s1", [SIM], on_release="shutdown_booted", booted=[SIM])
    controller = MagicMock(u2=None)

    async def hangs(udid):
        await asyncio.sleep(30)

    controller.shutdown = AsyncMock(side_effect=hangs)
    registry.release(registry.get("s1").id)
    app = _app(registry, controller)
    actions = await asyncio.wait_for(sessions_api.run_release_policies(app), 5)
    [shutdown] = [a for a in actions if a.get("action") == "shutdown"]
    assert shutdown["ok"] is False and "did not finish" in shutdown["error"]
    registry.open("o", principal=OWNER)
    assert registry.reserve("o", [SIM]), "free once the bounded shutdown gave up"


async def test_a_late_installs_removal_is_marked():
    """An install outliving its hold removes its app after the release has
    finished, outside any sweep: marked too, or a reservation during that
    uninstall had it finish under the new hold (review)."""
    from server import session_installs as si

    registry = _registry()
    registry.reserve("s1", [SIM])
    session = registry.get("s1")
    apps = []
    controller = MagicMock()

    async def list_apps(udid, *, set_active=True):
        return [SimpleNamespace(bundle_id=b, app_type="User") for b in apps], udid

    attempts = []

    async def uninstall(bundle_id, udid, *, set_active=True):
        try:
            registry.reserve("s1", [SIM])
            attempts.append("reserved")
        except SessionConflict as e:
            attempts.append(str(e))

    controller.list_apps = AsyncMock(side_effect=list_apps)
    controller.uninstall_app = AsyncMock(side_effect=uninstall)
    app = _app(registry, controller)
    late = await si.before_install(app, SIM, SimpleNamespace(session=session.id,
                                                             principal=session.principal))
    registry.unreserve("s1", [SIM])
    app.state.session_installs.clear()                 # its release has run
    registry.drained([SIM])
    apps.append("com.example.late")
    assert (await si.after_install(app, late))["removed"] == ["com.example.late"]
    assert attempts and "being cleaned up" in attempts[0]


_SPAWN = asyncio.create_subprocess_exec                # before any test patches it


async def _sleeper():
    import asyncio
    import sys

    return await _SPAWN(
        sys.executable, "-c", "import time; time.sleep(30)",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)


@pytest.mark.parametrize("how", ["cancelled", "timed out"])
async def test_a_device_command_whose_wait_ends_early_is_killed(how):
    """A release's wait_for around simctl or adb gave up and freed the device
    while the command ran on, under the next holder (review). The child is
    killed and reaped before the wait returns."""
    import asyncio

    from server.device._subprocess import communicate

    proc = await _sleeper()
    if how == "cancelled":
        task = asyncio.ensure_future(communicate(proc))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(communicate(proc), 0.2)
    assert proc.returncode is not None, "killed and reaped, not left running"


@pytest.mark.parametrize("runner", ["simctl", "devicectl", "adb"])
async def test_each_device_tools_runner_kills_its_child_on_a_timeout(monkeypatch, runner):
    """The runners a release's clean-up goes through, each with the helper."""
    import asyncio

    started = []

    async def spawn(*args, **kw):
        started.append(await _sleeper())
        return started[-1]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    if runner == "simctl":
        from server.device.ios.simctl import SimctlBackend
        call = SimctlBackend()._run_simctl("shutdown", SIM)
    elif runner == "devicectl":
        from server.device.ios.devicectl import DevicectlBackend
        call = DevicectlBackend()._run_devicectl("device", "info")
    else:
        from server.device.android.adb import AdbBackend
        backend = AdbBackend()
        backend._adb_path = "adb"
        call = backend._run_adb("-s", "emulator-5554", "uninstall", "x")
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(call, 0.3)
    assert started and started[0].returncode is not None


async def test_a_late_installs_removal_is_bounded(monkeypatch):
    """Inside the mark a stalled uninstall kept every reservation refused,
    the owner's too, for as long as it lasted (review)."""
    import asyncio

    from server import session_installs as si

    monkeypatch.setattr(si, "TIMEOUT_S", 0.2)
    registry = _registry()
    registry.reserve("s1", [SIM])
    session = registry.get("s1")
    apps = []
    controller = MagicMock()

    async def list_apps(udid, *, set_active=True):
        return [SimpleNamespace(bundle_id=b, app_type="User") for b in apps], udid

    async def stalls(bundle_id, udid, *, set_active=True):
        await asyncio.sleep(30)

    controller.list_apps = AsyncMock(side_effect=list_apps)
    controller.uninstall_app = AsyncMock(side_effect=stalls)
    app = _app(registry, controller)
    late = await si.before_install(app, SIM, SimpleNamespace(session=session.id,
                                                             principal=session.principal))
    registry.unreserve("s1", [SIM])
    app.state.session_installs.clear()
    registry.drained([SIM])
    apps.append("com.example.late")
    ended = await asyncio.wait_for(si.after_install(app, late), 5)
    assert ended["left"] == ["com.example.late"] and "did not finish" in ended["error"]
    assert registry.reserve("s1", [SIM]), "free once the bounded uninstall gave up"


async def test_adbs_own_timeout_kills_its_child(monkeypatch):
    """adb's bounded path relies on communicate() for the kill now."""
    from server.device.android.adb import AdbBackend, AdbTimeout

    started = []

    async def spawn(*args, **kw):
        started.append(await _sleeper())
        return started[-1]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    backend = AdbBackend()
    backend._adb_path = "adb"
    with pytest.raises(AdbTimeout):
        await backend._run_adb("-s", "emulator-5554", "uninstall", "x", timeout=0.3)
    assert started[0].returncode is not None
