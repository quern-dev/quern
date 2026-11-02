"""What a session installed is cleaned up when its hold ends (#581).

The case this exists for: a remote machine shared by client keys kept every
app their agents installed, and each app's data -- 17 GB across 44
simulators on romulus -- and the next client to reserve a simulator found
the previous one's apps, data and keychain on it."""

from __future__ import annotations

import asyncio
import pathlib
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
    async def boot(udid, *, set_active=True):
        assert set_active is False, "a release's reboot must not move the active device"
        return udid

    controller.boot = AsyncMock(side_effect=boot)
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


async def test_a_hold_that_ends_while_the_apps_are_listed_refuses_the_install():
    """Released during the listing, with no record yet: a record made after
    it would belong to an ended opening, and nothing would clean it up."""
    s = _session()
    app, ctrl, state = _app(holder=_held_by(s))

    async def list_then_release(udid, *, set_active=True):
        app.state.sessions.held = None
        return [], udid

    ctrl.list_apps = AsyncMock(side_effect=list_then_release)
    with pytest.raises(si.BaselineUnavailable, match="hold ended"):
        await si.before_install(app, SIM, _caller(s))
    assert not getattr(app.state, "session_installs", {})


async def test_only_the_artifacts_own_app_is_taken_for_the_install():
    """A reinstall over an app already there, while something else installs
    another: the one new app is not the artifact's, so it is unclear."""
    s = _session()
    app, ctrl, state = _app(holder=_held_by(s), installed=("com.example.mine",))
    before = await si.before_install(app, SIM, _caller(s))
    state["apps"].append("com.example.other")
    await si.after_install(app, before, "com.example.mine")
    record = app.state.session_installs[SIM]
    assert (record.added, record.unclear) == (set(), 1)


async def test_a_hold_that_ends_never_removes_an_app_that_is_not_the_artifacts():
    s = _session()
    app, ctrl, state = _app(holder=_held_by(s))
    before = await si.before_install(app, SIM, _caller(s))
    app.state.sessions.held = None
    await _release(app, s)
    state["apps"].append("com.example.other")
    assert (await si.after_install(app, before, "com.example.mine"))["left"] == [
        "com.example.other"]
    ctrl.uninstall_app.assert_not_awaited()


async def test_an_install_finishing_during_the_clean_up_removes_its_own_app():
    """The release's uninstall is under way when the install finishes: added
    to the record then, the app outlived the clean-up that dropped it."""
    s = _session()
    app, ctrl, state = _app(holder=_held_by(s), installed=("com.example.first",))
    _record(app, s, added=("com.example.first",))
    late = await si.before_install(app, SIM, _caller(s))
    real = ctrl.uninstall_app.side_effect
    answers, finished = [], []

    async def slow_uninstall(bundle_id, udid, *, set_active=True):
        if not finished:
            finished.append(True)
            state["apps"].append("com.example.late")
            answers.append(await si.after_install(app, late))
        return await real(bundle_id, udid, set_active=set_active)

    ctrl.uninstall_app = AsyncMock(side_effect=slow_uninstall)
    app.state.sessions.held = None
    await _release(app, s)
    assert answers == [{"hold_ended": True, "removed": ["com.example.late"]}]
    assert state["apps"] == []


async def test_the_clean_up_does_not_drop_a_record_made_while_it_ran():
    """The key reserves the device again during the release's uninstall: the
    new opening's record is its own, and the old release must not drop it."""
    s, s2 = _session(), _session("client:a/s2", opened="o2")
    app, ctrl, state = _app(installed=("com.example.first",))
    _record(app, s, added=("com.example.first",))
    real = ctrl.uninstall_app.side_effect

    async def reserved_meanwhile(bundle_id, udid, *, set_active=True):
        app.state.session_installs[SIM] = si.InstallRecord(session=s2.id, opened="o2")
        return await real(bundle_id, udid, set_active=set_active)

    ctrl.uninstall_app = AsyncMock(side_effect=reserved_meanwhile)
    await _release(app, s)
    assert app.state.session_installs[SIM].opened == "o2"


def test_the_bundle_id_is_read_from_an_app_and_an_ipa(tmp_path):
    import plistlib
    import zipfile

    app_dir = tmp_path / "My.app"
    app_dir.mkdir()
    (app_dir / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.x.app"}))
    ipa = tmp_path / "My.ipa"
    with zipfile.ZipFile(ipa, "w") as z:
        # A watch app's bundle listed first: a nested .app, not the one installed.
        z.writestr("Payload/My.app/Watch/W.app/Info.plist",
                   plistlib.dumps({"CFBundleIdentifier": "com.x.ipa.watch"}))
        z.writestr("Payload/My.app/Info.plist", plistlib.dumps({"CFBundleIdentifier": "com.x.ipa"}))
        z.writestr("Payload/My.app/PlugIns/W.appex/Info.plist",
                   plistlib.dumps({"CFBundleIdentifier": "com.x.ipa.widget"}))
    (tmp_path / "app.apk").write_bytes(b"PK")
    assert si.bundle_id_of(app_dir) == "com.x.app"
    assert si.bundle_id_of(ipa) == "com.x.ipa"
    assert si.bundle_id_of(tmp_path / "app.apk") is None
    assert si.bundle_id_of(tmp_path / "missing.app") is None


async def test_a_late_install_on_a_device_its_key_holds_again_goes_to_that_session():
    """Released, and the key reserved it again, before the install finished:
    the device is held, so the app is not removed under the new hold but put
    on its record, for its release to remove."""
    s, s2 = _session(), _session("client:a/s2", opened="o2")
    app, ctrl, state = _app(holder=_held_by(s))
    late = await si.before_install(app, SIM, _caller(s))
    app.state.sessions.held = None
    await _release(app, s)
    app.state.sessions.held = _held_by(s2)
    await _install(app, state, s2)                     # s2's record, adding nothing
    state["apps"].append("com.example.late")
    assert await si.after_install(app, late) == {"hold_ended": True,
                                                 "handed_over": ["com.example.late"],
                                                 "to": s2.id}
    ctrl.uninstall_app.assert_not_awaited()
    assert app.state.session_installs[SIM].added == {"com.example.late"}


async def test_a_late_install_goes_to_its_keys_new_hold_even_before_it_installs():
    """The key holds the device again but has installed nothing, so has no
    record: one is made for it rather than the app being left."""
    s, s2 = _session(), _session("client:a/s2", opened="o2")
    app, ctrl, state = _app(holder=_held_by(s))
    late = await si.before_install(app, SIM, _caller(s))
    app.state.sessions.held = None
    await _release(app, s)
    app.state.sessions.held = _held_by(s2)
    state["apps"].append("com.example.late")
    assert (await si.after_install(app, late))["handed_over"] == ["com.example.late"]
    record = app.state.session_installs[SIM]
    assert (record.session, record.opened, record.added) == (s2.id, "o2", {"com.example.late"})
    app.state.sessions.held = None
    assert (await _release(app, s2))[0]["removed"] == ["com.example.late"]


async def test_a_late_install_on_a_device_another_key_holds_is_left():
    s = _session()
    app, ctrl, state = _app(holder=_held_by(s))
    late = await si.before_install(app, SIM, _caller(s))
    app.state.sessions.held = None
    await _release(app, s)
    bob = _session("client:b/x", principal="client:b")
    app.state.sessions.held = _held_by(bob)
    state["apps"].append("com.example.late")
    assert await si.after_install(app, late) == {"hold_ended": True,
                                                 "left": ["com.example.late"]}
    assert not app.state.session_installs, "no record made for another key"
    await _install(app, state, bob)                    # bob's own record
    state["apps"].append("com.example.later")
    late2 = si.Install(s.id, s.opened_at, SIM, frozenset(state["apps"][:-1]), s.principal)
    assert (await si.after_install(app, late2))["left"] == ["com.example.later"]
    ctrl.uninstall_app.assert_not_awaited()
    assert app.state.session_installs[SIM].added == set(), "not put on another key's record"


async def test_a_cancelled_xcode_install_is_still_recorded(tmp_path):
    """The request cancelled while xcrun installs: the install finishes, and
    its record with it, so the release knows the app."""
    import plistlib

    from server.api import build_app as build_api
    from server.models import BuildResult

    s = _session()
    app, ctrl, state = _app(holder=_held_by(s))
    built = tmp_path / "App.app"
    built.mkdir()
    (built / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.x.app"}))
    started, finish = asyncio.Event(), asyncio.Event()

    async def install(app_path, udid):
        started.set()
        await finish.wait()
        state["apps"].append("com.x.app")

    ctrl.install_app = AsyncMock(side_effect=install)
    ctrl.simctl.boot = AsyncMock()
    with patch.object(build_api, "_build", AsyncMock(return_value=BuildResult(succeeded=True))), \
         patch.object(build_api, "_find_app", lambda *a: built), \
         patch.object(build_api, "_record", AsyncMock(return_value=None)), \
         patch.object(build_api, "_finish_records", AsyncMock(return_value=[])), \
         patch("server.visibility.current_caller", lambda: _caller(s)):
        route = asyncio.create_task(build_api._build_and_install_xcode(
            ctrl, build_api.BuildAndInstallRequest(project_path="/p", scheme="S"), MagicMock(),
            "-project", "/p", [], [SIM], {"iphonesimulator": tmp_path / "derived"}, app=app))
        try:
            await asyncio.wait_for(started.wait(), 2)
            route.cancel()
            done, _ = await asyncio.wait({route}, timeout=2)
            assert route in done, "the cancelled route must not wait for the install"
            assert route.cancelled()
        finally:
            finish.set()
        await asyncio.wait_for(asyncio.gather(*build_api._RECORDING), 2)
    assert app.state.session_installs[SIM].added == {"com.x.app"}


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
    ctrl.boot.assert_awaited_once_with(SIM, set_active=False)
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


async def test_an_erase_retried_after_a_failed_reboot_still_boots_it():
    """The first attempt erased it and failed to boot it: the retry finds it
    shut down, and must boot it all the same."""
    s = _session()
    app, ctrl, _ = _app(client="erase")
    booted = [True]

    async def erase(udid):
        booted[0] = False                            # simctl erase shuts it down

    async def boot(udid, *, set_active=True):
        if ctrl.boot.await_count == 1:
            raise RuntimeError("boot timed out")
        booted[0] = True

    ctrl.erase = AsyncMock(side_effect=erase)
    ctrl.boot = AsyncMock(side_effect=boot)
    ctrl.list_devices = AsyncMock(side_effect=lambda: [SimpleNamespace(
        udid=SIM, state=DeviceState.BOOTED if booted[0] else DeviceState.SHUTDOWN)])
    assert (await _release(app, s))[0]["ok"] is False
    assert (await _release(app, s))[0] == {"udid": SIM, "ok": True, "action": "erased",
                                           "rebooted": True}
    assert booted[0] and not app.state.session_installs_was_booted


async def test_the_same_opening_reserving_again_before_the_sweep_keeps_its_hold():
    """release_devices for one device, then reserve it again, before the
    sweep: the hold goes on, so nothing is erased or uninstalled under it."""
    s = _session()
    app, ctrl, state = _app(client="erase", holder=_held_by(s), installed=("com.example.new",))
    _record(app, s)
    assert await _release(app, s) == []
    ctrl.erase.assert_not_awaited()
    ctrl.uninstall_app.assert_not_awaited()
    assert app.state.session_installs[SIM].added == {"com.example.new"}


async def test_a_reservation_during_the_uninstall_stops_it_and_takes_the_rest():
    s, s2 = _session(), _session("client:a/s2", opened="o2")
    app, ctrl, state = _app(installed=("com.example.a", "com.example.b"))
    _record(app, s, added=("com.example.a", "com.example.b"))
    real = ctrl.uninstall_app.side_effect

    async def reserved_after_first(bundle_id, udid, *, set_active=True):
        app.state.sessions.held = _held_by(s2)
        return await real(bundle_id, udid, set_active=set_active)

    ctrl.uninstall_app = AsyncMock(side_effect=reserved_after_first)
    actions = await _release(app, s)
    assert state["apps"] == ["com.example.b"]
    record = app.state.session_installs[SIM]
    assert (record.session, record.ending, record.attempts) == (s2.id, False, 0)
    assert [a["action"] for a in actions] == ["installs_handed_over"]


async def test_a_reservation_before_the_erase_stops_it():
    s, s2 = _session(), _session("client:a/s2", opened="o2")
    app, ctrl, _ = _app(client="erase")

    async def reserved_while_listing():
        app.state.sessions.held = _held_by(s2)
        return [SimpleNamespace(udid=SIM, state=DeviceState.BOOTED)]

    ctrl.list_devices = AsyncMock(side_effect=reserved_while_listing)
    actions = await _release(app, s)
    ctrl.erase.assert_not_awaited()
    assert not any(a.get("ok") is False for a in actions)


async def test_a_controller_boot_of_an_avd_by_name_honours_set_active():
    from server.device.controller import DeviceController

    controller = DeviceController()
    controller.adb.is_installed = lambda: True
    controller.adb.list_avds = AsyncMock(return_value=["Pixel_9"])
    controller.adb.boot_emulator = AsyncMock(return_value=EMU)
    controller._active_udid = "OWNERS"
    await controller.boot(name="Pixel_9", set_active=False)
    assert controller._active_udid == "OWNERS"


async def test_a_controller_boot_by_name_honours_set_active():
    from server.device.controller import DeviceController

    controller = DeviceController()
    controller.adb.is_installed = lambda: False
    controller.simctl.list_devices = AsyncMock(return_value=[
        SimpleNamespace(udid=SIM, name="iPhone 16")])
    controller.simctl.boot = AsyncMock()
    controller._restore_input_after_boot = AsyncMock()
    controller._active_udid = "OWNERS"
    with patch("server.device_access.check", lambda udid: None):
        await controller.boot(name="iPhone 16", set_active=False)
    assert controller._active_udid == "OWNERS"


async def test_a_controller_boot_for_a_release_leaves_the_active_device():
    from server.device.controller import DeviceController

    controller = DeviceController()
    controller._is_android = lambda udid: False
    controller._ensure_device_type_cached = AsyncMock()
    controller._require_simulator = lambda udid, what: None
    controller._restore_input_after_boot = AsyncMock()
    controller.simctl.boot = AsyncMock()
    controller._active_udid = "OWNERS"
    await controller.boot(SIM, set_active=False)
    assert controller._active_udid == "OWNERS"
    await controller.boot(SIM)
    assert controller._active_udid == SIM


def _fake_sdk(tmp_path, monkeypatch, tools: dict):
    """An SDK whose build-tools hold an aapt2 per version, and aapt2's run
    mocked: version -> (exit code, stdout), or an exception to raise. Nothing
    is executed, and the machine's own SDK and PATH are out of reach."""
    import subprocess

    from server.device.android import adb

    root = tmp_path / "sdk"
    for version in tools:
        tool = root / "build-tools" / version / "aapt2"
        tool.parent.mkdir(parents=True)
        tool.write_text("not run")
    runs = []

    def run(argv, **kw):
        runs.append((argv, kw))
        assert argv[1:3] == ["dump", "packagename"], argv
        outcome = tools[pathlib.Path(argv[0]).parent.name]
        if isinstance(outcome, BaseException):
            raise outcome
        code, out = outcome
        return subprocess.CompletedProcess(argv, code, out, "")

    monkeypatch.setattr(si.subprocess, "run", run)
    monkeypatch.setattr(si.shutil, "which", lambda name: None)
    monkeypatch.setattr(adb, "_SDK_SEARCH_PATHS", [])
    monkeypatch.setenv("ANDROID_HOME", str(root))
    monkeypatch.delenv("ANDROID_SDK_ROOT", raising=False)
    apk = tmp_path / "app.apk"
    apk.write_bytes(b"PK")
    return apk, runs


def test_an_apks_package_is_read_with_the_newest_aapt2(tmp_path, monkeypatch):
    """The newest by version, not by name: 9.0.0 sorts after 36.1.0 as text."""
    apk, runs = _fake_sdk(tmp_path, monkeypatch, {
        "9.0.0": (0, "com.old.tools\n"), "36.0.0": (0, "com.older\n"),
        "36.1.0": (0, "com.example.apk\n")})
    assert si.bundle_id_of(apk) == "com.example.apk"
    assert [pathlib.Path(a[0]).parent.name for a, _ in runs] == ["36.1.0"]


@pytest.mark.parametrize("outcome", [
    (1, ""),                                        # not an APK
    (1, "com.half.read\n"),                         # a failure is not believed
    (0, "W/ziparchive: something odd\n"),
    (0, ""),
])
def test_an_apk_aapt2_cannot_read_is_unknown(tmp_path, monkeypatch, outcome):
    apk, _ = _fake_sdk(tmp_path, monkeypatch, {"36.1.0": outcome})
    assert si.bundle_id_of(apk) is None


def test_an_apk_with_no_build_tools_is_unknown(tmp_path, monkeypatch):
    apk, runs = _fake_sdk(tmp_path, monkeypatch, {})
    assert si.bundle_id_of(apk) is None and runs == []


def test_a_stalled_aapt2_is_bounded(tmp_path, monkeypatch):
    import subprocess

    apk, runs = _fake_sdk(tmp_path, monkeypatch, {
        "36.1.0": subprocess.TimeoutExpired("aapt2", si._AAPT2_TIMEOUT_S)})
    assert si.bundle_id_of(apk) is None
    assert runs[0][1]["timeout"] == si._AAPT2_TIMEOUT_S


async def test_an_apk_install_takes_only_its_own_package(tmp_path, monkeypatch):
    """The gap this closes: an APK reinstall adding nothing while something
    else installs another app was taken for the APK's install."""
    from server.api import device as device_api
    from server.models import InstallAppRequest

    apk, _ = _fake_sdk(tmp_path, monkeypatch, {"36.1.0": (0, "com.example.mine\n")})
    s = _session()
    app, ctrl, state = _app(holder=_held_by(s), installed=("com.example.mine",))
    ctrl.resolve_udid = AsyncMock(return_value=SIM)

    async def reinstall_while_another_lands(app_path, udid):
        state["apps"].append("com.example.other")
        return udid

    ctrl.install_app = AsyncMock(side_effect=reinstall_while_another_lands)
    app.state.uploads = MagicMock()
    monkeypatch.setattr(device_api, "_get_controller", lambda r: ctrl)
    monkeypatch.setattr("server.visibility.current_caller", lambda: SimpleNamespace(
        session=s.id, principal=s.principal, is_owner=False))
    await device_api.install_app(SimpleNamespace(app=app),
                                 InstallAppRequest(app_path=str(apk), udid=SIM))
    record = app.state.session_installs[SIM]
    assert (record.added, record.unclear) == (set(), 1)


def test_an_ipa_with_an_inflated_info_plist_is_not_read(tmp_path, monkeypatch):
    import plistlib
    import zipfile

    monkeypatch.setattr(si, "_MAX_PLIST_BYTES", 1024)
    ipa = tmp_path / "Big.ipa"
    with zipfile.ZipFile(ipa, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Payload/My.app/Info.plist", plistlib.dumps(
            {"CFBundleIdentifier": "com.x.big", "Pad": "x" * 4096}))
    assert si.bundle_id_of(ipa) is None


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


async def test_a_failed_clean_up_handed_over_still_records_the_new_sessions_installs():
    """The record a failed uninstall kept is no longer ending: handed to
    another session of the key, that session's installs are recorded, not
    removed as they finish."""
    s, s2 = _session(), _session("client:a/s2", opened="o2")
    app, ctrl, state = _app(installed=("com.example.first",))
    _record(app, s, added=("com.example.first",))
    real = ctrl.uninstall_app.side_effect
    ctrl.uninstall_app = AsyncMock(side_effect=RuntimeError("busy"))
    await _release(app, s)
    ctrl.uninstall_app = AsyncMock(side_effect=real)
    app.state.sessions.held = _held_by(s2)
    await _release(app, s)                       # the retry hands it over
    assert await _install(app, state, s2, "com.example.second") == {}
    assert app.state.session_installs[SIM].added == {"com.example.first",
                                                     "com.example.second"}


async def test_a_failed_erase_never_turns_into_uninstalling_everything():
    """A failed erase is counted apart from the install records: none is made
    for it, so nothing can be uninstalled from one."""
    s = _session()
    app, ctrl, _ = _app(client="erase", installed=("com.apple.Maps", "com.example.new"))
    ctrl.erase = AsyncMock(side_effect=RuntimeError("busy"))
    await _release(app, s)
    assert not getattr(app.state, "session_installs", {})
    ctrl.uninstall_app.assert_not_awaited()


@pytest.mark.parametrize("erase_fails", [False, True])
async def test_an_erase_leaves_another_openings_record(erase_fails):
    """The same name opened again, and released too, while the old opening's
    erase is still to run: the new opening's record is its own release's."""
    s, again = _session(), _session(opened="o2")
    app, ctrl, _ = _app(client="erase")
    if erase_fails:
        ctrl.erase = AsyncMock(side_effect=RuntimeError("busy"))
    _record(app, again)
    await _release(app, s)
    record = app.state.session_installs[SIM]
    assert (record.opened, record.ending, record.attempts) == ("o2", False, 0)


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


@pytest.mark.parametrize("there, appears, record, added, unclear", [
    ((), ["com.example.built"], True, {"com.example.built"}, 0),
    # The build's package was there already, and something else appeared
    # meanwhile: not the build's, so unclear rather than taken for it.
    (("com.example.built",), ["com.other"], True, set(), 1),
    # A build that failed made nothing: nothing to record, not even unclear.
    ((), ["com.other"], False, set(), 0),
])
async def test_a_gradle_build_naming_no_device_is_recorded_on_the_one_it_installs_on(
        monkeypatch, there, appears, record, added, unclear):
    from server.api import build_android
    from server.api import build_app as build_api

    s = _session()
    app, ctrl, state = _app(holder=_held_by(s), kind=DeviceType.ANDROID_EMULATOR,
                            installed=there)
    monkeypatch.setattr(build_api, "_is_gradle", lambda p: True)
    monkeypatch.setattr(build_android, "_android_targets", AsyncMock(return_value=[EMU]))

    async def build(controller, body):
        state["apps"].extend(appears)
        return {"devices": [], "all_installed": True, "build_records": [{
            "build_id": "b", "created_at": "2026-11-01T00:00:00Z", "project_path": "/p",
            "scheme": "debug", "configuration": "debug", "platform": "android",
            "app_path": "/p/app.apk", "bundle_id": "com.example.built"}] if record else []}

    monkeypatch.setattr(build_android, "build_and_install", build)
    monkeypatch.setattr(build_api, "_android_summary", lambda r: "")
    monkeypatch.setattr("server.visibility.current_caller", lambda: SimpleNamespace(
        session=s.id, principal=s.principal, is_owner=False))
    await build_api.build_and_install(SimpleNamespace(app=app),
                                      build_api.BuildAndInstallRequest(project_path="/p"))
    got = app.state.session_installs[EMU]
    assert (got.added, got.unclear) == (added, unclear)


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


async def test_a_gradle_build_says_when_the_hold_ended_during_its_install(monkeypatch):
    """Released while the build ran: the app is removed as the install
    finishes, and the device's result says so rather than only installed."""
    from server.api import build_android
    from server.api import build_app as build_api

    s = _session()
    app, ctrl, state = _app(holder=_held_by(s), kind=DeviceType.ANDROID_EMULATOR)
    monkeypatch.setattr(build_api, "_is_gradle", lambda p: True)
    monkeypatch.setattr(build_android, "_android_targets", AsyncMock(return_value=[EMU]))

    async def build(controller, body):
        state["apps"].append("com.example.built")
        app.state.session_installs.clear()           # the release ran meanwhile
        app.state.sessions.held = None
        return {"devices": [{"udid": EMU, "installed": True}], "all_installed": True,
                "build_records": [{
                    "build_id": "b", "created_at": "2026-11-01T00:00:00Z", "project_path": "/p",
                    "scheme": "debug", "configuration": "debug", "platform": "android",
                    "app_path": "/p/app.apk", "bundle_id": "com.example.built"}]}

    monkeypatch.setattr(build_android, "build_and_install", build)
    monkeypatch.setattr("server.visibility.current_caller", lambda: SimpleNamespace(
        session=s.id, principal=s.principal, is_owner=False))
    resp = await build_api.build_and_install(SimpleNamespace(app=app),
                                             build_api.BuildAndInstallRequest(project_path="/p"))
    assert "hold ended" in resp.devices[0].note and "com.example.built" in resp.devices[0].note
    assert state["apps"] == []


async def test_an_xcode_install_says_when_the_hold_ended_during_it(tmp_path):
    import plistlib

    from server.api import build_app as build_api
    from server.models import BuildResult

    s = _session()
    app, ctrl, state = _app(holder=_held_by(s))
    built = tmp_path / "App.app"
    built.mkdir()
    (built / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.x.app"}))

    async def install(app_path, udid):
        state["apps"].append("com.x.app")
        app.state.session_installs.clear()           # the release ran meanwhile
        app.state.sessions.held = None

    ctrl.install_app = AsyncMock(side_effect=install)
    ctrl.simctl.boot = AsyncMock()
    with patch.object(build_api, "_build", AsyncMock(return_value=BuildResult(succeeded=True))), \
         patch.object(build_api, "_find_app", lambda *a: built), \
         patch.object(build_api, "_record", AsyncMock(return_value=None)), \
         patch.object(build_api, "_finish_records", AsyncMock(return_value=[])), \
         patch("server.visibility.current_caller", lambda: _caller(s)):
        resp = await build_api._build_and_install_xcode(
            ctrl, build_api.BuildAndInstallRequest(project_path="/p", scheme="S"), MagicMock(),
            "-project", "/p", [], [SIM], {"iphonesimulator": tmp_path / "derived"}, app=app)
    assert "com.x.app was removed again" in resp.devices[0].note
    assert "com.x.app was removed again" in resp.summary, "the MCP tool shows only this"
    assert state["apps"] == []


async def test_a_cancelled_direct_install_is_recorded_and_keeps_its_upload_held(
        tmp_path, monkeypatch):
    """The request cancelled while the APK installs: the install goes on, its
    upload stays held -- the sweep would delete it under the install -- and
    the app is recorded, then the upload discarded."""
    from server.api import device as device_api
    from server.models import InstallAppRequest
    from server.uploads import UploadStore

    apk, _ = _fake_sdk(tmp_path, monkeypatch, {"36.1.0": (0, "com.example.mine\n")})
    s = _session()
    app, ctrl, state = _app(holder=_held_by(s), kind=DeviceType.ANDROID_EMULATOR)
    store = UploadStore(tmp_path / "uploads", max_bytes=1 << 20)

    async def chunks():
        yield apk.read_bytes()

    upload = await store.receive(s.principal, "mine.apk", chunks(), None)
    app.state.uploads = store
    ctrl.resolve_udid = AsyncMock(return_value=EMU)
    started, finish = asyncio.Event(), asyncio.Event()

    async def install(app_path, udid):
        started.set()
        await finish.wait()
        state["apps"].append("com.example.mine")
        return udid

    ctrl.install_app = AsyncMock(side_effect=install)
    monkeypatch.setattr(device_api, "_get_controller", lambda r: ctrl)
    monkeypatch.setattr("server.visibility.current_caller", lambda: SimpleNamespace(
        session=s.id, principal=s.principal, is_owner=False))
    route = asyncio.create_task(device_api.install_app(
        SimpleNamespace(app=app), InstallAppRequest(upload_id=upload.id, udid=EMU)))
    try:
        await asyncio.wait_for(started.wait(), 2)
        route.cancel()
        done, _ = await asyncio.wait({route}, timeout=2)
        assert route in done and route.cancelled()
        assert store.busy(upload.id), "the install still holds its upload"
    finally:
        finish.set()
    await asyncio.wait_for(asyncio.gather(*device_api._RECORDING), 2)
    assert app.state.session_installs[EMU].added == {"com.example.mine"}
    assert not store.busy(upload.id)
    from server.uploads import UploadError

    with pytest.raises(UploadError):
        store.get(upload.id, s.principal)              # installed, so discarded


async def test_a_cancelled_gradle_build_is_still_recorded(monkeypatch):
    from server.api import build_android
    from server.api import build_app as build_api

    s = _session()
    app, ctrl, state = _app(holder=_held_by(s), kind=DeviceType.ANDROID_EMULATOR)
    monkeypatch.setattr(build_api, "_is_gradle", lambda p: True)
    monkeypatch.setattr(build_android, "_android_targets", AsyncMock(return_value=[EMU]))
    started, finish = asyncio.Event(), asyncio.Event()

    async def build(controller, body):
        started.set()
        await finish.wait()
        state["apps"].append("com.example.built")
        return {"devices": [{"udid": EMU, "installed": True}], "all_installed": True,
                "build_records": [{
                    "build_id": "b", "created_at": "2026-11-01T00:00:00Z", "project_path": "/p",
                    "scheme": "debug", "configuration": "debug", "platform": "android",
                    "app_path": "/p/app.apk", "bundle_id": "com.example.built"}]}

    monkeypatch.setattr(build_android, "build_and_install", build)
    monkeypatch.setattr("server.visibility.current_caller", lambda: SimpleNamespace(
        session=s.id, principal=s.principal, is_owner=False))
    route = asyncio.create_task(build_api.build_and_install(
        SimpleNamespace(app=app), build_api.BuildAndInstallRequest(project_path="/p")))
    try:
        await asyncio.wait_for(started.wait(), 2)
        route.cancel()
        done, _ = await asyncio.wait({route}, timeout=2)
        assert route in done and route.cancelled()
    finally:
        finish.set()
    await asyncio.wait_for(asyncio.gather(*build_api._RECORDING), 2)
    assert app.state.session_installs[EMU].added == {"com.example.built"}


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
