"""What a session installed is cleaned up when its hold ends (#581).

The case this exists for: a remote machine shared by client keys kept every
app their agents installed, and each app's data -- 17 GB across 44
simulators on romulus -- and the next client to reserve a simulator found
the previous one's apps, data and keychain on it."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server import session_installs as si
from server.models import AppInfo, DeviceState, DeviceType

SIM = "A1B2C3D4-0000-0000-0000-000000000001"
EMU = "emulator-5554"


def _apps(*ids: str, system: tuple[str, ...] = ()) -> list[AppInfo]:
    return ([AppInfo(bundle_id=b, name=b, app_type="User") for b in ids]
            + [AppInfo(bundle_id=b, name=b, app_type="System") for b in system])


def _session(sid="client:a/s1", principal="client:a", opened="o1", reservations=None):
    return SimpleNamespace(id=sid, principal=principal, opened_at=opened,
                           reservations=reservations or {})


def _app(*, client="uninstall", owner="none", holder=None, kind=DeviceType.SIMULATOR,
         installed=()):
    controller = MagicMock()
    state = {"apps": list(installed)}
    controller.list_apps = AsyncMock(side_effect=lambda udid: (_apps(*state["apps"]), udid))

    async def uninstall(bundle_id, udid):
        state["apps"].remove(bundle_id)
        return udid

    controller.uninstall_app = AsyncMock(side_effect=uninstall)
    controller.erase = AsyncMock(side_effect=lambda udid: udid)
    controller.boot = AsyncMock(side_effect=lambda udid: udid)
    controller._device_type = lambda udid: kind
    controller.list_devices = AsyncMock(return_value=[
        SimpleNamespace(udid=SIM, state=DeviceState.BOOTED)])
    sessions = SimpleNamespace(holder_of=lambda udid: holder)
    app = SimpleNamespace(state=SimpleNamespace(
        release_policy={"client": client, "owner": owner}, device_controller=controller,
        sessions=sessions))
    return app, controller, state


def _held_by(session):
    return SimpleNamespace(session=session)


# ----------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------


@pytest.mark.parametrize("raw, client, owner, notes", [
    ({}, "uninstall", "none", 0),
    ({"client_release": "erase", "owner_release": "Uninstall"}, "erase", "uninstall", 0),
    ({"client_release": "nuke", "owner_release": 3}, "uninstall", "none", 2),
])
def test_the_settings_and_a_typo_falls_back_and_says_so(monkeypatch, raw, client, owner, notes):
    from server import config

    monkeypatch.setattr(config, "read_user_config", lambda **k: {"sessions": raw})
    s = config.sessions_settings()
    assert (s["client_release"], s["owner_release"], len(s["release_notes"])) == (
        client, owner, notes)


# ----------------------------------------------------------------------------
# Before and after an install
# ----------------------------------------------------------------------------


async def test_a_holding_clients_first_install_notes_the_baseline():
    s = _session()
    app, ctrl, state = _app(holder=_held_by(s), installed=("com.apple.Maps",))
    caller = SimpleNamespace(session=s.id, principal=s.principal)
    before = await si.before_install(app, SIM, caller)
    assert before == {"com.apple.Maps"}
    state["apps"].append("com.example.first")
    await si.before_install(app, SIM, caller)          # a second install: same baseline
    assert app.state.session_installs[SIM].baseline == {"com.apple.Maps"}


@pytest.mark.parametrize("why", ["not held", "held by another session", "owner policy none"])
async def test_nothing_is_noted_when_nothing_would_clean_up(why):
    s = _session(principal="owner" if why == "owner policy none" else "client:a")
    holder = {"not held": None, "held by another session": _held_by(_session("client:b/x")),
              "owner policy none": _held_by(s)}[why]
    app, ctrl, _ = _app(holder=holder)
    caller = SimpleNamespace(session=s.id, principal=s.principal)
    assert await si.before_install(app, SIM, caller) is None
    ctrl.list_apps.assert_not_awaited()
    assert not getattr(app.state, "session_installs", {})


async def test_an_unreadable_device_refuses_rather_than_installs_unseen():
    s = _session()
    app, ctrl, _ = _app(holder=_held_by(s))
    ctrl.list_apps = AsyncMock(side_effect=RuntimeError("simctl failed"))
    with pytest.raises(si.BaselineUnavailable, match="simctl failed"):
        await si.before_install(app, SIM, SimpleNamespace(session=s.id, principal=s.principal))


async def test_an_install_that_adds_nothing_counts_as_a_reinstall():
    s = _session()
    app, ctrl, state = _app(holder=_held_by(s), installed=("com.example.app",))
    before = await si.before_install(app, SIM, SimpleNamespace(session=s.id,
                                                               principal=s.principal))
    await si.after_install(app, SIM, before)
    assert app.state.session_installs[SIM].reinstalled == 1


# ----------------------------------------------------------------------------
# At release
# ----------------------------------------------------------------------------


def _record(app, s, baseline=("com.apple.Maps",), **kw):
    app.state.session_installs = {SIM: si.InstallRecord(
        session=s.id, opened=s.opened_at,
        baseline=None if baseline is None else frozenset(baseline), **kw)}


async def test_uninstall_removes_what_the_session_added_and_nothing_else():
    s = _session()
    app, ctrl, state = _app(installed=("com.apple.Maps", "com.example.new",
                                       "com.facebook.WebDriverAgentRunner.xctrunner"))
    _record(app, s, reinstalled=1)
    actions: list = []
    await si.end_for_release(app, app.state.sessions, s, SIM, actions)
    ctrl.uninstall_app.assert_awaited_once_with("com.example.new", SIM)
    assert actions == [{"udid": SIM, "ok": True, "action": "installs_removed",
                        "removed": ["com.example.new"], "reinstalled_kept": 1}]
    assert app.state.session_installs == {}


async def test_system_apps_are_never_uninstalled():
    s = _session()
    app, ctrl, state = _app(installed=("com.apple.Maps",))
    ctrl.list_apps = AsyncMock(return_value=(_apps("com.apple.Maps",
                                                   system=("com.apple.Preferences",)), SIM))
    _record(app, s)
    await si.end_for_release(app, app.state.sessions, s, SIM, [])
    ctrl.uninstall_app.assert_not_awaited()


async def test_another_session_of_the_key_takes_the_record():
    s, s2 = _session(), _session("client:a/s2", opened="o2")
    app, ctrl, _ = _app(holder=_held_by(s2), installed=("com.example.new",))
    _record(app, s)
    actions: list = []
    await si.end_for_release(app, app.state.sessions, s, SIM, actions)
    ctrl.uninstall_app.assert_not_awaited()
    assert (app.state.session_installs[SIM].session,
            app.state.session_installs[SIM].opened) == ("client:a/s2", "o2")
    assert actions[0]["action"] == "installs_handed_over"


async def test_another_keys_device_is_left_alone():
    s = _session()
    app, ctrl, _ = _app(holder=_held_by(_session("client:b/x", principal="client:b")),
                        installed=("com.example.new",))
    _record(app, s)
    actions: list = []
    await si.end_for_release(app, app.state.sessions, s, SIM, actions)
    ctrl.uninstall_app.assert_not_awaited()
    assert app.state.session_installs == {} and actions[0]["action"] == "installs_left"


async def test_erase_erases_a_simulator_even_with_nothing_installed_and_reboots_it():
    s = _session()
    app, ctrl, _ = _app(client="erase")
    actions: list = []
    await si.end_for_release(app, app.state.sessions, s, SIM, actions)
    ctrl.erase.assert_awaited_once_with(SIM)
    ctrl.boot.assert_awaited_once_with(SIM)
    assert actions == [{"udid": SIM, "ok": True, "action": "erased", "rebooted": True}]


async def test_erase_does_not_reboot_what_the_release_shuts_down_anyway():
    reservation = SimpleNamespace(on_release="shutdown_booted", booted_here=True)
    s = _session(reservations={SIM: reservation})
    app, ctrl, _ = _app(client="erase")
    await si.end_for_release(app, app.state.sessions, s, SIM, [])
    ctrl.erase.assert_awaited_once()
    ctrl.boot.assert_not_awaited()


async def test_erase_on_an_android_device_falls_back_to_uninstall():
    s = _session()
    app, ctrl, _ = _app(client="erase", kind=DeviceType.ANDROID_EMULATOR,
                        installed=("com.example.new",))
    app.state.session_installs = {EMU: si.InstallRecord(session=s.id, opened="o1",
                                                         baseline=frozenset())}
    await si.end_for_release(app, app.state.sessions, s, EMU, [])
    ctrl.erase.assert_not_awaited()
    ctrl.uninstall_app.assert_awaited_once_with("com.example.new", EMU)


async def test_the_owner_is_left_alone_by_default():
    s = _session(sid="ci", principal="owner")
    app, ctrl, _ = _app(installed=("com.example.new",))
    _record(app, s)
    await si.end_for_release(app, app.state.sessions, s, SIM, [])
    ctrl.uninstall_app.assert_not_awaited()


async def test_a_failure_is_retried_then_given_up_on():
    s = _session()
    app, ctrl, _ = _app(installed=("com.example.new",))
    ctrl.uninstall_app = AsyncMock(side_effect=RuntimeError("device not found"))
    _record(app, s)
    for attempt in (1, 2):
        actions: list = []
        await si.end_for_release(app, app.state.sessions, s, SIM, actions)
        assert actions[0]["ok"] is False and "gave_up" not in actions[0]
        assert app.state.session_installs[SIM].attempts == attempt
    actions = []
    await si.end_for_release(app, app.state.sessions, s, SIM, actions)
    assert actions[0]["gave_up"] is True and app.state.session_installs == {}


async def test_a_failed_erase_never_turns_into_uninstalling_everything():
    """The record a failed erase leaves counts attempts and has no baseline:
    read as 'nothing was there before', it would remove every app."""
    s = _session()
    app, ctrl, _ = _app(client="erase", installed=("com.apple.Maps", "com.example.new"))
    ctrl.erase = AsyncMock(side_effect=RuntimeError("busy"))
    await si.end_for_release(app, app.state.sessions, s, SIM, [])
    assert app.state.session_installs[SIM].baseline is None
    done = await si._uninstall(ctrl, app.state.session_installs[SIM], SIM)
    assert done["removed"] == [] and ctrl.uninstall_app.await_count == 0


async def test_a_stalled_clean_up_is_bounded(monkeypatch):
    s = _session()
    app, ctrl, _ = _app(installed=("com.example.new",))

    async def hangs(bundle_id, udid):
        await asyncio.sleep(10)

    ctrl.uninstall_app = AsyncMock(side_effect=hangs)
    monkeypatch.setattr(si, "TIMEOUT_S", 0.05)
    _record(app, s)
    actions: list = []
    await asyncio.wait_for(si.end_for_release(app, app.state.sessions, s, SIM, actions), 2)
    assert actions[0]["ok"] is False and "did not finish" in actions[0]["error"]


# ----------------------------------------------------------------------------
# The install route
# ----------------------------------------------------------------------------


async def test_the_install_route_refuses_when_the_baseline_cannot_be_read(monkeypatch):
    from fastapi import HTTPException

    from server.api import device as device_api
    from server.models import InstallAppRequest

    controller = MagicMock()
    controller.resolve_udid = AsyncMock(return_value=SIM)
    controller.install_app = AsyncMock(return_value=SIM)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        device_controller=controller, uploads=MagicMock())))
    monkeypatch.setattr(device_api, "_get_controller", lambda r: controller)
    monkeypatch.setattr("server.visibility.current_caller", lambda: SimpleNamespace(
        session="client:a/s1", principal="client:a", is_owner=False))
    with patch.object(si, "before_install", AsyncMock(side_effect=si.BaselineUnavailable("x"))), \
         pytest.raises(HTTPException) as e:
        await device_api.install_app(request, InstallAppRequest(app_path="/tmp/App.app",
                                                                udid=SIM))
    assert e.value.status_code == 503
    controller.install_app.assert_not_awaited()


async def test_the_release_loop_runs_the_policy():
    """Through _end_client_work, which every release, expiry and revocation
    runs, not only the module on its own."""
    from server.api import sessions as sessions_api

    s = _session()
    app, ctrl, _ = _app(installed=("com.apple.Maps", "com.example.new"))
    app.state.session_proxies = None
    app.state.proxy_adapter = None
    app.state.recordings = None
    app.state.mock_owners = {}
    _record(app, s)
    s.reservations = {SIM: SimpleNamespace(on_release="none", booted_here=False)}
    actions = await sessions_api._end_client_work(app, app.state.sessions, s)
    assert any(a.get("action") == "installs_removed" and a["removed"] == ["com.example.new"]
               for a in actions)


@pytest.mark.parametrize("caller", [
    SimpleNamespace(session=None, principal="owner", is_owner=True),
    SimpleNamespace(session="ci", principal="owner", is_owner=True),     # owner policy none
])
async def test_an_install_nothing_would_clean_up_is_made_as_before(monkeypatch, caller):
    """No session, or a policy of none: nothing is listed or resolved first,
    so the install is the one it always was."""
    from server.api import device as device_api
    from server.models import InstallAppRequest

    controller = MagicMock()
    controller.resolve_udid = AsyncMock(return_value=SIM)
    controller.list_apps = AsyncMock(return_value=([], SIM))
    controller.install_app = AsyncMock(return_value=SIM)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        device_controller=controller, uploads=MagicMock(),
        release_policy={"client": "uninstall", "owner": "none"})))
    monkeypatch.setattr(device_api, "_get_controller", lambda r: controller)
    monkeypatch.setattr("server.visibility.current_caller", lambda: caller)
    await device_api.install_app(request, InstallAppRequest(app_path="/tmp/App.app", udid=None))
    controller.resolve_udid.assert_not_awaited()
    controller.list_apps.assert_not_awaited()
    controller.install_app.assert_awaited_once_with(app_path="/tmp/App.app", udid=None)
