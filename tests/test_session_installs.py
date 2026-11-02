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
from server.device.ios.wda import WDA_RUNNER_BUNDLE_ID
from server.models import AppInfo, DeviceState, DeviceType

SIM = "A1B2C3D4-0000-0000-0000-000000000001"
EMU = "emulator-5554"


def _apps(*ids: str, system: tuple[str, ...] = ()) -> list[AppInfo]:
    return ([AppInfo(bundle_id=b, name=b, app_type="User") for b in ids]
            + [AppInfo(bundle_id=b, name=b, app_type="System") for b in system])


def _session(sid="client:a/s1", principal="client:a", opened="o1", reservations=None):
    return SimpleNamespace(id=sid, principal=principal, opened_at=opened,
                           reservations=reservations or {})


class _Holder:
    """registry.holder_of, changeable mid-test."""

    def __init__(self, held=None):
        self.held = held

    def holder_of(self, udid):
        return self.held


def _app(*, client="uninstall", owner="none", holder=None, kind=DeviceType.SIMULATOR,
         installed=()):
    controller = MagicMock()
    state = {"apps": list(installed), "system": []}

    async def list_apps(udid, *, set_active=True):
        assert set_active is False, "bookkeeping must not move the active device"
        return _apps(*state["apps"], system=tuple(state["system"])), udid

    async def uninstall(bundle_id, udid, *, set_active=True):
        assert set_active is False, "a release must not move the active device"
        state["apps"].remove(bundle_id)
        return udid

    controller.list_apps = AsyncMock(side_effect=list_apps)
    controller.uninstall_app = AsyncMock(side_effect=uninstall)
    controller.erase = AsyncMock(side_effect=lambda udid: udid)
    controller.boot = AsyncMock(side_effect=lambda udid: udid)
    controller._device_type = lambda udid: kind
    controller.list_devices = AsyncMock(return_value=[
        SimpleNamespace(udid=SIM, state=DeviceState.BOOTED)])
    app = SimpleNamespace(state=SimpleNamespace(
        release_policy={"client": client, "owner": owner}, device_controller=controller,
        sessions=_Holder(holder)))
    return app, controller, state


def _held_by(session):
    return SimpleNamespace(session=session)


def _caller(s):
    return SimpleNamespace(session=s.id, principal=s.principal)


async def _install(app, state, s, *new, udid=SIM):
    """One install by `s`, through both hooks, adding `new`."""
    before = await si.before_install(app, udid, _caller(s))
    state["apps"].extend(new)
    return await si.after_install(app, before)


async def _release(app, s, udid=SIM):
    actions: list = []
    await si.end_for_release(app, app.state.sessions, s, udid, actions)
    return actions


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


async def test_the_server_reads_the_policy_at_start(monkeypatch):
    """Pinned against the real lifespan: the policy a release reads is the
    one config.json set."""
    from server import config
    from server.config import ServerConfig
    from server.lifecycle import network_monitor, update_check
    from server.main import create_app, lifespan

    async def _never(*a, **k):
        return None

    monkeypatch.setattr(network_monitor, "update_network_state", lambda *a: None)
    monkeypatch.setattr(network_monitor, "network_monitor_loop", _never)
    monkeypatch.setattr(update_check, "periodic_update_check", _never)
    real = config.sessions_settings
    monkeypatch.setattr(config, "sessions_settings", lambda: {
        **real(), "client_release": "erase", "owner_release": "uninstall"})
    app = create_app(config=ServerConfig(api_key="k"),
                     enable_oslog=False, enable_crash=False, enable_proxy=False)
    async with lifespan(app):
        assert app.state.release_policy == {"client": "erase", "owner": "uninstall"}


# ----------------------------------------------------------------------------
# Before and after an install
# ----------------------------------------------------------------------------


async def test_each_install_records_the_one_app_it_added():
    s = _session()
    app, ctrl, state = _app(holder=_held_by(s), installed=("com.apple.Maps",))
    await _install(app, state, s, "com.example.first")
    await _install(app, state, s, "com.example.second")
    assert app.state.session_installs[SIM].added == {"com.example.first", "com.example.second"}


async def test_an_app_installed_out_of_band_during_the_hold_is_left():
    """The owner installing at the machine while a client holds the device:
    not the client's install, so not its release's to remove."""
    s = _session()
    app, ctrl, state = _app(holder=_held_by(s))
    await _install(app, state, s, "com.example.client")
    state["apps"].append("com.example.owner")
    app.state.sessions.held = None
    actions = await _release(app, s)
    assert state["apps"] == ["com.example.owner"]
    assert actions[0]["removed"] == ["com.example.client"]


@pytest.mark.parametrize("why", ["not held", "held by another session", "owner policy none"])
async def test_nothing_is_noted_when_nothing_would_clean_up(why):
    s = _session(principal="owner" if why == "owner policy none" else "client:a")
    holder = {"not held": None, "held by another session": _held_by(_session("client:b/x")),
              "owner policy none": _held_by(s)}[why]
    app, ctrl, _ = _app(holder=holder)
    assert await si.before_install(app, SIM, _caller(s)) is None
    ctrl.list_apps.assert_not_awaited()
    assert not getattr(app.state, "session_installs", {})


async def test_an_unreadable_device_refuses_rather_than_installs_unseen():
    s = _session()
    app, ctrl, _ = _app(holder=_held_by(s))
    ctrl.list_apps = AsyncMock(side_effect=RuntimeError("simctl failed"))
    with pytest.raises(si.BaselineUnavailable, match="simctl failed"):
        await si.before_install(app, SIM, _caller(s))


async def test_an_install_that_adds_nothing_counts_as_a_reinstall():
    s = _session()
    app, ctrl, state = _app(holder=_held_by(s), installed=("com.example.app",))
    await _install(app, state, s)
    record = app.state.session_installs[SIM]
    assert (record.reinstalls, record.added, record.unclear) == (1, set(), 0)


async def test_an_install_that_seems_to_add_several_is_unclear_not_trusted():
    """A short listing before the install makes apps already there look new:
    nothing is recorded from it, and the release says so."""
    s = _session()
    app, ctrl, state = _app(holder=_held_by(s))
    await _install(app, state, s, "com.example.a", "com.example.b")
    record = app.state.session_installs[SIM]
    assert (record.added, record.unclear) == (set(), 1)
    app.state.sessions.held = None
    actions = await _release(app, s)
    ctrl.uninstall_app.assert_not_awaited()
    assert actions[0]["unclear_installs"] == 1 and "left" in actions[0]["note"]


async def test_an_install_whose_result_cannot_be_listed_is_unclear():
    s = _session()
    app, ctrl, state = _app(holder=_held_by(s))
    before = await si.before_install(app, SIM, _caller(s))
    ctrl.list_apps = AsyncMock(side_effect=RuntimeError("gone"))
    assert await si.after_install(app, before) == {}
    assert app.state.session_installs[SIM].unclear == 1


async def test_a_record_another_session_of_the_key_left_carries_over():
    """Released, its clean-up still to retry, and the key reserves again: the
    first session's app is not forgotten."""
    s, s2 = _session(), _session("client:a/s2", opened="o2")
    app, ctrl, state = _app(holder=_held_by(s))
    await _install(app, state, s, "com.example.first")
    app.state.sessions.held = _held_by(s2)
    await _install(app, state, s2, "com.example.second")
    record = app.state.session_installs[SIM]
    assert (record.session, record.added) == (s2.id, {"com.example.first",
                                                      "com.example.second"})


async def test_a_hold_that_ends_during_the_install_removes_what_it_added():
    """The release ran without this app; left, it would outlive the hold."""
    s = _session()
    app, ctrl, state = _app(holder=_held_by(s))
    before = await si.before_install(app, SIM, _caller(s))
    app.state.sessions.held = None
    assert await _release(app, s) == [{"udid": SIM, "ok": True, "action": "installs_removed",
                                       "removed": []}]
    state["apps"].append("com.example.late")
    assert await si.after_install(app, before) == {"hold_ended": True,
                                                   "removed": ["com.example.late"]}
    assert state["apps"] == []


async def test_a_hold_that_ends_during_an_unclear_install_leaves_and_says_so():
    s = _session()
    app, ctrl, state = _app(holder=_held_by(s))
    before = await si.before_install(app, SIM, _caller(s))
    app.state.sessions.held = None
    await _release(app, s)
    state["apps"].extend(["com.example.a", "com.example.b"])
    assert await si.after_install(app, before) == {
        "hold_ended": True, "left": ["com.example.a", "com.example.b"]}
    ctrl.uninstall_app.assert_not_awaited()


async def test_a_hold_that_ends_during_the_install_never_removes_querns_driver():
    """WebDriverAgent installed by quern while a reinstall ran is the one new
    app: still not the session's to remove."""
    s = _session()
    app, ctrl, state = _app(holder=_held_by(s), installed=("com.example.app",))
    before = await si.before_install(app, SIM, _caller(s))
    app.state.sessions.held = None
    await _release(app, s)
    state["apps"].append(WDA_RUNNER_BUNDLE_ID)
    assert (await si.after_install(app, before))["left"] == [WDA_RUNNER_BUNDLE_ID]
    ctrl.uninstall_app.assert_not_awaited()


# ----------------------------------------------------------------------------
# At release
# ----------------------------------------------------------------------------


def _record(app, s, added=("com.example.new",), udid=SIM, **kw):
    app.state.session_installs = {udid: si.InstallRecord(
        session=s.id, opened=s.opened_at, added=set(added), **kw)}


async def test_uninstall_removes_what_the_session_added_and_nothing_else():
    s = _session()
    app, ctrl, state = _app(installed=("com.apple.Maps", "com.example.new"))
    _record(app, s, reinstalls=1)
    actions = await _release(app, s)
    assert state["apps"] == ["com.apple.Maps"]
    assert actions == [{"udid": SIM, "ok": True, "action": "installs_removed",
                        "removed": ["com.example.new"], "installs_adding_nothing": 1}]
    assert app.state.session_installs == {}


async def test_quern_drivers_and_system_apps_are_never_uninstalled():
    s = _session()
    app, ctrl, state = _app(installed=(WDA_RUNNER_BUNDLE_ID, "com.github.uiautomator"))
    state["system"].append("com.apple.Preferences")
    _record(app, s, added=(WDA_RUNNER_BUNDLE_ID, "com.github.uiautomator",
                           "com.apple.Preferences"))
    await _release(app, s)
    ctrl.uninstall_app.assert_not_awaited()


async def test_another_session_of_the_key_takes_the_record():
    s, s2 = _session(), _session("client:a/s2", opened="o2")
    app, ctrl, _ = _app(holder=_held_by(s2), installed=("com.example.new",))
    _record(app, s)
    actions = await _release(app, s)
    ctrl.uninstall_app.assert_not_awaited()
    assert (app.state.session_installs[SIM].session,
            app.state.session_installs[SIM].opened) == ("client:a/s2", "o2")
    assert actions[0]["action"] == "installs_handed_over"


async def test_the_same_name_opened_again_takes_the_record_and_is_never_erased():
    """Same session id, a new opening: compared by id alone, the old opening's
    release erased the simulator under the new one."""
    s, again = _session(), _session(opened="o2")
    app, ctrl, _ = _app(client="erase", holder=_held_by(again), installed=("com.example.new",))
    _record(app, s)
    actions = await _release(app, s)
    ctrl.erase.assert_not_awaited()
    ctrl.uninstall_app.assert_not_awaited()
    assert app.state.session_installs[SIM].opened == "o2"
    assert actions[0]["action"] == "installs_handed_over"


async def test_another_keys_device_is_left_alone():
    s = _session()
    app, ctrl, _ = _app(holder=_held_by(_session("client:b/x", principal="client:b")),
                        installed=("com.example.new",))
    _record(app, s)
    actions = await _release(app, s)
    ctrl.uninstall_app.assert_not_awaited()
    assert app.state.session_installs == {} and actions[0]["action"] == "installs_left"


async def test_erase_erases_a_simulator_even_with_nothing_installed_and_reboots_it():
    s = _session()
    app, ctrl, _ = _app(client="erase")
    actions = await _release(app, s)
    ctrl.erase.assert_awaited_once_with(SIM)
    ctrl.boot.assert_awaited_once_with(SIM)
    assert actions == [{"udid": SIM, "ok": True, "action": "erased", "rebooted": True}]


async def test_erase_does_not_reboot_what_the_release_shuts_down_anyway():
    reservation = SimpleNamespace(on_release="shutdown_booted", booted_here=True)
    s = _session(reservations={SIM: reservation})
    app, ctrl, _ = _app(client="erase")
    await _release(app, s)
    ctrl.erase.assert_awaited_once()
    ctrl.boot.assert_not_awaited()


async def test_a_finished_erase_is_not_run_again_when_the_release_is_retried():
    """Something else on the device failed, so its release is retried: the
    erase that finished must not erase the simulator again each time."""
    s = _session()
    app, ctrl, _ = _app(client="erase")
    await _release(app, s)
    assert await _release(app, s) == []
    ctrl.erase.assert_awaited_once()


async def test_an_erase_given_up_on_is_not_tried_a_fourth_time():
    s = _session()
    app, ctrl, _ = _app(client="erase")
    ctrl.erase = AsyncMock(side_effect=RuntimeError("busy"))
    for _ in range(si.ATTEMPTS):
        actions = await _release(app, s)
    assert actions[0]["gave_up"] is True
    assert await _release(app, s) == []
    assert ctrl.erase.await_count == si.ATTEMPTS


async def test_erase_on_an_android_device_falls_back_to_uninstall():
    s = _session()
    app, ctrl, _ = _app(client="erase", kind=DeviceType.ANDROID_EMULATOR,
                        installed=("com.example.new",))
    _record(app, s, udid=EMU)
    await _release(app, s, EMU)
    ctrl.erase.assert_not_awaited()
    ctrl.uninstall_app.assert_awaited_once_with("com.example.new", EMU, set_active=False)


async def test_the_owner_is_left_alone_by_default():
    s = _session(sid="ci", principal="owner")
    app, ctrl, _ = _app(installed=("com.example.new",))
    _record(app, s)
    await _release(app, s)
    ctrl.uninstall_app.assert_not_awaited()


async def test_a_failure_is_retried_then_given_up_on():
    s = _session()
    app, ctrl, _ = _app(installed=("com.example.new",))
    ctrl.uninstall_app = AsyncMock(side_effect=RuntimeError("device not found"))
    _record(app, s)
    for attempt in (1, 2):
        actions = await _release(app, s)
        assert actions[0]["ok"] is False and "gave_up" not in actions[0]
        assert app.state.session_installs[SIM].attempts == attempt
    actions = await _release(app, s)
    assert actions[0]["gave_up"] is True and app.state.session_installs == {}


async def test_a_failed_erase_never_turns_into_uninstalling_everything():
    """The record a failed erase leaves only counts attempts: it holds no app,
    so nothing is uninstalled from it."""
    s = _session()
    app, ctrl, _ = _app(client="erase", installed=("com.apple.Maps", "com.example.new"))
    ctrl.erase = AsyncMock(side_effect=RuntimeError("busy"))
    await _release(app, s)
    record = app.state.session_installs[SIM]
    assert record.added == set() and record.attempts == 1
    done = await si._uninstall(ctrl, record, SIM)
    assert done["removed"] == [] and ctrl.uninstall_app.await_count == 0


async def test_a_stalled_clean_up_is_bounded(monkeypatch):
    s = _session()
    app, ctrl, _ = _app(installed=("com.example.new",))

    async def hangs(bundle_id, udid, *, set_active=True):
        await asyncio.sleep(10)

    ctrl.uninstall_app = AsyncMock(side_effect=hangs)
    monkeypatch.setattr(si, "TIMEOUT_S", 0.05)
    _record(app, s)
    actions: list = []
    await asyncio.wait_for(si.end_for_release(app, app.state.sessions, s, SIM, actions), 2)
    assert actions[0]["ok"] is False and "did not finish" in actions[0]["error"]


# ----------------------------------------------------------------------------
# The routes and the release sweep
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
    assert e.value.status_code == 503 and "Nothing was installed" in e.value.detail
    controller.install_app.assert_not_awaited()


async def test_the_install_route_records_the_install(monkeypatch):
    from server.api import device as device_api
    from server.models import InstallAppRequest

    s = _session()
    app, ctrl, state = _app(holder=_held_by(s))
    ctrl.resolve_udid = AsyncMock(return_value=SIM)

    async def install(app_path, udid):
        state["apps"].append("com.example.new")
        return udid

    ctrl.install_app = AsyncMock(side_effect=install)
    app.state.uploads = MagicMock()
    monkeypatch.setattr(device_api, "_get_controller", lambda r: ctrl)
    monkeypatch.setattr("server.visibility.current_caller", lambda: SimpleNamespace(
        session=s.id, principal=s.principal, is_owner=False))
    await device_api.install_app(SimpleNamespace(app=app),
                                 InstallAppRequest(app_path="/tmp/App.app", udid=SIM))
    assert app.state.session_installs[SIM].added == {"com.example.new"}


async def test_a_gradle_build_naming_no_device_is_recorded_on_the_one_it_installs_on(
        monkeypatch):
    from server.api import build_android
    from server.api import build_app as build_api

    s = _session()
    app, ctrl, state = _app(holder=_held_by(s), kind=DeviceType.ANDROID_EMULATOR)
    monkeypatch.setattr(build_api, "_is_gradle", lambda p: True)
    monkeypatch.setattr(build_android, "_android_targets", AsyncMock(return_value=[EMU]))

    async def build(controller, body):
        state["apps"].append("com.example.built")
        return {"devices": [], "all_installed": True}

    monkeypatch.setattr(build_android, "build_and_install", build)
    monkeypatch.setattr(build_api, "_android_summary", lambda r: "")
    monkeypatch.setattr("server.visibility.current_caller", lambda: SimpleNamespace(
        session=s.id, principal=s.principal, is_owner=False))
    await build_api.build_and_install(SimpleNamespace(app=app),
                                      build_api.BuildAndInstallRequest(project_path="/p"))
    assert app.state.session_installs[EMU].added == {"com.example.built"}


async def test_an_xcode_install_unseen_is_refused_on_that_device(tmp_path):
    from server.api import build_app as build_api
    from server.models import BuildResult

    s = _session()
    app, ctrl, _ = _app(holder=_held_by(s))
    ctrl.list_apps = AsyncMock(side_effect=RuntimeError("simctl failed"))
    ctrl.install_app = AsyncMock()
    ctrl.simctl.boot = AsyncMock()
    with patch.object(build_api, "_build", AsyncMock(return_value=BuildResult(succeeded=True))), \
         patch.object(build_api, "_find_app", lambda *a: tmp_path / "App.app"), \
         patch.object(build_api, "_record", AsyncMock(return_value=None)), \
         patch.object(build_api, "_finish_records", AsyncMock(return_value=[])), \
         patch("server.visibility.current_caller", lambda: _caller(s)):
        resp = await build_api._build_and_install_xcode(
            ctrl, build_api.BuildAndInstallRequest(project_path="/p", scheme="S"), MagicMock(),
            "-project", "/p", [], [SIM], {"iphonesimulator": tmp_path / "derived"}, app=app)
    result = next(d for d in resp.devices if d.udid == SIM)
    assert result.installed is False and "Nothing was installed" in result.error
    ctrl.install_app.assert_not_awaited()


async def test_the_release_loop_runs_the_policy():
    """Through _end_client_work, which every release, expiry and revocation
    runs, not only the module on its own."""
    from server.api import sessions as sessions_api

    s = _session()
    app, ctrl, state = _app(installed=("com.apple.Maps", "com.example.new"))
    app.state.proxy_adapter = None
    app.state.recordings = None
    app.state.mock_owners = {}
    _record(app, s)
    s.reservations = {SIM: SimpleNamespace(on_release="none", booted_here=False)}
    actions = await sessions_api._end_client_work(app, app.state.sessions, s)
    assert any(a.get("action") == "installs_removed" and a["removed"] == ["com.example.new"]
               for a in actions)


async def test_the_sweep_does_not_shut_down_what_an_erase_left_shut_down():
    """simctl erase leaves the simulator shut down, and the erase does not
    boot what the release shuts down: a shutdown after it fails."""
    from server.api import sessions as sessions_api

    reservation = SimpleNamespace(udid=SIM, on_release="shutdown_booted", booted_here=True)
    s = _session(reservations={SIM: reservation})
    app, ctrl, _ = _app(client="erase")
    ctrl.shutdown = AsyncMock()
    ctrl.u2 = None
    registry = app.state.sessions
    registry.take_released = lambda: [s]
    registry.drained = lambda udids: None
    registry.retry_release = lambda session, failed: None
    app.state.proxy_adapter = None
    app.state.recordings = None
    app.state.mock_owners = {}
    actions = await sessions_api.run_release_policies(app)
    ctrl.erase.assert_awaited_once()
    ctrl.shutdown.assert_not_awaited()
    assert [a["action"] for a in actions] == ["erased"]


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
