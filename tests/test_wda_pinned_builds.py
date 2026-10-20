"""Builds of the pinned WebDriverAgent (#479): a build of any other version
-- every install made before the pin -- is out of date, and a new one that
fails puts the old one back, so a device or simulator that ran WDA still does.
"""

from __future__ import annotations

import copy
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from server.device.ios import wda

PIN = wda.pinned_wda_version()


@pytest.fixture
def state(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(wda, "read_wda_state", lambda: copy.deepcopy(store))
    monkeypatch.setattr(wda, "save_wda_state", lambda s: (store.clear(), store.update(s)))
    return store


def _proc(returncode=0, out=b""):
    p = MagicMock(returncode=returncode)
    p.communicate = AsyncMock(return_value=(out, b""))
    p.wait = AsyncMock()
    return p


# ---------------------------------------------------------------------------
# The device build
# ---------------------------------------------------------------------------


@pytest.fixture
def device(tmp_path, monkeypatch, state):
    derived = tmp_path / "derived"
    app = derived / "Build" / "Products" / "Debug-iphoneos" / "Runner.app"
    xctestrun = derived / "Build" / "Products" / "quern-driver.xctestrun"
    repo = tmp_path / "repo"
    (repo / "WebDriverAgent.xcodeproj").mkdir(parents=True)
    (repo / ".quern-source").write_text(PIN + "\n")
    monkeypatch.setattr(wda, "WDA_DERIVED", derived)
    monkeypatch.setattr(wda, "WDA_APP", app)
    monkeypatch.setattr(wda, "XCTESTRUN", xctestrun)
    monkeypatch.setattr(wda, "WDA_REPO", repo)
    monkeypatch.setattr(wda, "_post_process_runner_app", AsyncMock())
    monkeypatch.setattr(wda, "_xcode_build_id", AsyncMock(return_value="27A1"))

    def old_build(version="11.4.0"):
        app.mkdir(parents=True)
        (app / "old").write_text(version)
        xctestrun.write_text("old")
        state.update({"build_team_id": "TEAM", "build_wda_version": version,
                      "build_deployment_target": wda.WDA_MIN_DEPLOYMENT_TARGET})

    def builds(ok=True):
        async def fake_exec(*cmd, **kw):
            if ok:
                app.mkdir(parents=True, exist_ok=True)
                (app / "new").write_text(PIN)
                xctestrun.write_text("new")
            return _proc(0 if ok else 65, b"" if ok else b"Signing failed")
        monkeypatch.setattr(wda.asyncio, "create_subprocess_exec", fake_exec)

    return {"app": app, "xctestrun": xctestrun, "old_build": old_build, "builds": builds}


class TestTheDeviceBuild:
    async def test_a_build_of_another_version_is_out_of_date(self, device, state):
        device["old_build"]("11.4.0")
        assert await wda._build_is_current(state, "TEAM") is False
        state["build_wda_version"] = PIN
        assert await wda._build_is_current(state, "TEAM") is True

    async def test_a_build_from_before_the_pin_is_out_of_date(self, device, state):
        """No recorded version at all: every install so far."""
        device["old_build"]()
        del state["build_wda_version"]
        assert await wda._build_is_current(state, "TEAM") is False

    async def test_a_new_build_replaces_the_old_and_records_its_version(self, device, state):
        device["old_build"]()
        device["builds"](ok=True)
        assert await wda.build_wda("TEAM") is True
        assert (device["app"] / "new").exists(), "built where the old one was"
        assert state["build_wda_version"] == PIN
        assert not list(device["app"].parents[3].parent.glob("*-previous*")), "snapshot gone"

    async def test_the_new_build_is_recorded_before_the_old_is_deleted(self, device, state,
                                                                       monkeypatch):
        """A cancel during the deletion left the new build on disk under the
        old one's version, which setup_wda would then fall back to (review)."""
        device["old_build"]()
        device["builds"](ok=True)
        seen = []

        async def spy(previous):
            seen.append(state.get("build_wda_version"))

        monkeypatch.setattr(wda, "_discard_previous", spy)
        assert await wda.build_wda("TEAM") is True
        assert seen == [PIN]

    async def test_a_new_build_that_fails_puts_the_old_one_back(self, device, state):
        """The case #479 must not get wrong: a phone that worked, stranded."""
        device["old_build"]()
        device["builds"](ok=False)
        with pytest.raises(RuntimeError):
            await wda.build_wda("TEAM")
        assert (device["app"] / "old").read_text() == "11.4.0", "the old build is back"
        assert device["xctestrun"].read_text() == "old"
        assert state["build_wda_version"] == "11.4.0", "and still described as itself"

    async def test_a_first_build_that_fails_leaves_nothing_claiming_success(self, device, state):
        device["builds"](ok=False)
        with pytest.raises(RuntimeError):
            await wda.build_wda("TEAM")
        assert not device["app"].exists() and "build_wda_version" not in state


class TestSetupWdaKeepsWhatWorked:
    async def test_a_failed_update_installs_the_old_build_and_says_so(self, device, state,
                                                                      monkeypatch):
        device["old_build"]()
        device["builds"](ok=False)
        monkeypatch.setattr(wda, "discover_signing_identities",
                            lambda: [{"team_id": "TEAM", "team_name": "T", "team_type": "Company"}])
        monkeypatch.setattr(wda, "clone_wda", AsyncMock(return_value=False))
        monkeypatch.setattr(wda, "customize_wda", lambda *a, **k: False)
        monkeypatch.setattr(wda, "_runner_app_signature_valid", AsyncMock(return_value=True))
        installed = AsyncMock()
        monkeypatch.setattr(wda, "install_wda", installed)
        result = await wda.setup_wda("UDID", "18.0")
        assert result["status"] == "ok" and result["built"] is False
        installed.assert_awaited_once()
        assert result["wda_version"] == "11.4.0" and result["wda_pinned"] == PIN
        assert "failed" in result["wda_update_failed"] and "11.4.0" in result["wda_update_failed"]
        assert "wda_update" in result, "and how to get onto the pin"

    async def test_with_no_build_to_fall_back_on_it_is_an_error(self, device, state, monkeypatch):
        device["builds"](ok=False)
        monkeypatch.setattr(wda, "discover_signing_identities",
                            lambda: [{"team_id": "TEAM", "team_name": "T", "team_type": "Company"}])
        monkeypatch.setattr(wda, "clone_wda", AsyncMock(return_value=False))
        monkeypatch.setattr(wda, "customize_wda", lambda *a, **k: False)
        with pytest.raises(RuntimeError):
            await wda.setup_wda("UDID", "18.0")


def test_the_version_report_says_how_to_catch_up():
    assert wda.wda_version_report(PIN) == {"wda_version": PIN, "wda_pinned": PIN}
    behind = wda.wda_version_report("11.4.0")
    assert behind["wda_version"] == "11.4.0" and "setup_wda" in behind["wda_update"]
    assert "before quern pinned" in wda.wda_version_report(None)["wda_version"]


# ---------------------------------------------------------------------------
# The simulator build
# ---------------------------------------------------------------------------


@pytest.fixture
def sim(tmp_path, monkeypatch, state):
    derived = tmp_path / "derived-sim"
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".quern-source").write_text(PIN + "\n")
    monkeypatch.setattr(wda, "WDA_DERIVED_SIM", derived)
    monkeypatch.setattr(wda, "WDA_REPO", repo)
    monkeypatch.setattr(wda, "clone_wda", AsyncMock(return_value=False))
    monkeypatch.setattr(wda, "customize_wda", lambda *a, **k: False)
    monkeypatch.setattr(wda, "_xcode_build_id", AsyncMock(return_value="27A1"))
    products = derived / "Build" / "Products"

    def old_build():
        products.mkdir(parents=True)
        (products / "Old.xctestrun").write_text("old")
        state.update({"sim_build_xcode": "27A1", "sim_build_wda_version": "16.15.3",
                      "sim_build_deployment_target": wda.WDA_MIN_DEPLOYMENT_TARGET})

    def builds(ok=True):
        calls = []

        async def fake_exec(*cmd, **kw):
            calls.append(cmd)
            if ok:
                products.mkdir(parents=True, exist_ok=True)
                (products / "New.xctestrun").write_text("new")
            return _proc(0 if ok else 65)
        monkeypatch.setattr(wda.asyncio, "create_subprocess_exec", fake_exec)
        return calls

    return {"products": products, "old_build": old_build, "builds": builds}


class TestTheSimulatorBuild:
    async def test_a_build_of_another_version_is_rebuilt_and_recorded(self, sim, state):
        sim["old_build"]()
        sim["builds"](ok=True)
        assert await wda.build_wda_simulator() is True
        assert (sim["products"] / "New.xctestrun").exists()
        assert state["sim_build_wda_version"] == PIN
        tmp = sim["products"].parents[2]
        assert not list(tmp.glob("derived-sim-previous*")), "the kept build is gone"

    async def test_the_new_build_is_recorded_before_the_old_is_deleted(self, sim, state,
                                                                       monkeypatch):
        """A cancel during the deletion must not leave the new build unrecorded
        (review)."""
        sim["old_build"]()
        sim["builds"](ok=True)
        seen = []

        async def spy(previous):
            seen.append(state.get("sim_build_wda_version"))

        monkeypatch.setattr(wda, "_discard_previous", spy)
        assert await wda.build_wda_simulator() is True
        assert seen == [PIN]

    async def test_a_failed_update_keeps_the_old_build_and_its_fingerprint(self, sim, state):
        sim["old_build"]()
        sim["builds"](ok=False)
        assert await wda.build_wda_simulator() is False, "not raised: the old one runs"
        assert (sim["products"] / "Old.xctestrun").read_text() == "old"
        assert state["sim_build_wda_version"] == "16.15.3" and state["sim_build_xcode"] == "27A1"
        assert state["sim_build_failed_version"] == PIN

    async def test_it_is_not_retried_on_every_start_until_the_pin_moves(self, sim, state,
                                                                       monkeypatch):
        sim["old_build"]()
        sim["builds"](ok=False)
        await wda.build_wda_simulator()
        calls = sim["builds"](ok=True)
        assert await wda.build_wda_simulator() is False and calls == [], "not rebuilt"
        state["sim_build_failed_version"] = "0.0.1"          # the pin moved since
        assert await wda.build_wda_simulator() is True and calls

    async def test_force_retries_it(self, sim, state):
        sim["old_build"]()
        sim["builds"](ok=False)
        await wda.build_wda_simulator()
        sim["builds"](ok=True)
        assert await wda.build_wda_simulator(force=True) is True
        assert "sim_build_failed_version" not in state

    async def test_a_first_build_that_fails_still_raises(self, sim, state):
        sim["builds"](ok=False)
        with pytest.raises(RuntimeError):
            await wda.build_wda_simulator()


# ---------------------------------------------------------------------------
# quern status
# ---------------------------------------------------------------------------


def test_status_names_the_builds_against_the_pin(state):
    from server.lifecycle.daemon import wda_status_line

    assert wda_status_line() is None, "never built: nothing to say"
    state.update({"build_team_id": "TEAM", "build_wda_version": PIN})
    assert wda_status_line() == f"  WebDriverAgent: device {PIN} (quern pins {PIN})"
    state.update({"sim_built_at": "x", "sim_build_wda_version": "16.15.3"})
    line = wda_status_line()
    assert f"device {PIN}, simulator 16.15.3" in line and "behind the pin" in line


def test_status_calls_a_build_from_before_the_pin_unpinned(state):
    from server.lifecycle.daemon import wda_status_line

    state.update({"build_team_id": "TEAM"})
    line = wda_status_line()
    assert "device unpinned" in line and "behind the pin" in line


async def test_the_simulator_runner_is_named_as_the_devices_is(sim, state, monkeypatch):
    """It was "WebDriverAgentRunner-Runner": the device's name comes from its
    post-processing, and the simulator build had none (#479)."""
    import plistlib

    calls = []

    async def fake_exec(*cmd, **kw):
        calls.append(cmd)
        app = sim["products"] / "Debug-iphonesimulator" / "WebDriverAgentRunner-Runner.app"
        app.mkdir(parents=True, exist_ok=True)
        (app / "Info.plist").write_bytes(plistlib.dumps(
            {"CFBundleName": "WebDriverAgentRunner-Runner"}))
        (sim["products"] / "New.xctestrun").write_text("new")
        return _proc(0)

    monkeypatch.setattr(wda.asyncio, "create_subprocess_exec", fake_exec)
    assert await wda.build_wda_simulator() is True
    app = sim["products"] / "Debug-iphonesimulator" / "WebDriverAgentRunner-Runner.app"
    info = app / "Info.plist"
    assert plistlib.loads(info.read_bytes())["CFBundleDisplayName"] == "QuernDriver"


# ---------------------------------------------------------------------------
# Review: what keeping the old build has to survive
# ---------------------------------------------------------------------------


def _identities(monkeypatch, team="TEAM"):
    monkeypatch.setattr(wda, "discover_signing_identities",
                        lambda: [{"team_id": team, "team_name": "T", "team_type": "Company"}])
    monkeypatch.setattr(wda, "_runner_app_signature_valid", AsyncMock(return_value=True))
    installed = AsyncMock()
    monkeypatch.setattr(wda, "install_wda", installed)
    return installed


class TestSetupWdaFallsBackOnlyOnAnUpdate:
    async def test_a_failed_download_keeps_the_build_there(self, device, state, monkeypatch):
        """Every install's first setup after the upgrade fetches: offline, it
        refused to reinstall a phone that had a good build (review)."""
        device["old_build"]()
        installed = _identities(monkeypatch)

        async def offline():
            raise RuntimeError("could not download WebDriverAgent from npm: offline")

        monkeypatch.setattr(wda, "clone_wda", offline)
        result = await wda.setup_wda("UDID", "18.0")
        assert result["status"] == "ok" and "offline" in result["wda_update_failed"]
        installed.assert_awaited_once()
        assert (device["app"] / "old").exists()

    @pytest.mark.parametrize("why", ["force", "already pinned", "other team", "no artifacts"])
    async def test_otherwise_the_failure_is_the_answer(self, device, state, monkeypatch, why):
        """A rebuild that was asked for, a build already on the pin, another
        team's build, or nothing on disk: installing the old would hide why."""
        device["old_build"]()
        device["builds"](ok=False)
        installed = _identities(monkeypatch, team="OTHER" if why == "other team" else "TEAM")
        monkeypatch.setattr(wda, "clone_wda", AsyncMock(return_value=False))
        monkeypatch.setattr(wda, "customize_wda", lambda *a, **k: False)
        if why == "already pinned":
            state["build_wda_version"] = PIN
            monkeypatch.setattr(wda, "_build_is_current", AsyncMock(return_value=False))
        if why == "no artifacts":
            device["xctestrun"].unlink()
        with pytest.raises(RuntimeError):
            await wda.setup_wda("UDID", "18.0", force=(why == "force"))
        installed.assert_not_awaited()

    async def test_the_build_lock_is_held_while_building(self, device, state, monkeypatch):
        """Two setups at once destroyed each other's kept build (review)."""
        held = []

        async def build(team_id, force=False):
            held.append(wda._sim_lock("build").locked())
            return True

        _identities(monkeypatch)
        monkeypatch.setattr(wda, "clone_wda", AsyncMock(return_value=False))
        monkeypatch.setattr(wda, "customize_wda", lambda *a, **k: False)
        monkeypatch.setattr(wda, "build_wda", build)
        monkeypatch.setattr(wda, "_post_process_runner_app", AsyncMock())
        await wda.setup_wda("UDID", "18.0")
        assert held == [True]


class TestDiscardingTheKeptBuild:
    """After a successful rebuild the kept build is deleted off the event loop
    -- renamed out of `-previous` first, so a deletion that outlives a cancel
    or is cut short by a crash never leaves a half-deleted directory where
    _recover_previous would restore it over the good build (#498 review)."""

    async def test_it_is_renamed_away_before_the_deletion_starts(self, tmp_path, monkeypatch):
        previous = tmp_path / "derived-previous"
        (previous / "Build").mkdir(parents=True)
        seen: list[bool] = []
        real = wda.asyncio.to_thread

        async def spy(fn, *a, **kw):
            seen.append(previous.exists())
            return await real(fn, *a, **kw)

        monkeypatch.setattr(wda.asyncio, "to_thread", spy)
        await wda._discard_previous(previous)
        assert seen == [False], "-previous must be gone before deleting begins"
        assert list(tmp_path.iterdir()) == []

    async def test_it_is_renamed_not_deleted_on_the_loop(self, tmp_path, monkeypatch):
        """Deleting -previous synchronously would also pass the test above."""
        previous = tmp_path / "derived-previous"
        (previous / "Build").mkdir(parents=True)

        async def nothing(fn, *a, **kw):
            return None

        monkeypatch.setattr(wda.asyncio, "to_thread", nothing)
        await wda._discard_previous(previous)
        assert not previous.exists()
        retired = list(tmp_path.glob("derived-previous-retired-*"))
        assert len(retired) == 1 and (retired[0] / "Build").is_dir()

    async def test_a_rename_that_fails_deletes_it_in_place(self, tmp_path, monkeypatch):
        """The build succeeded and is recorded: no failure, and no -previous
        left for the next build to restore over it (review)."""
        previous = tmp_path / "derived-previous"
        (previous / "Build").mkdir(parents=True)
        real = Path.rename

        def refuse(self, target):
            if "-retired-" in str(target):
                raise PermissionError("nope")
            return real(self, target)

        monkeypatch.setattr(Path, "rename", refuse)
        await wda._discard_previous(previous)
        assert not previous.exists()

    async def test_a_deletion_cut_short_is_not_restored_by_the_next_build(self, tmp_path,
                                                                           state):
        derived = tmp_path / "derived"
        (derived / "good").mkdir(parents=True)
        previous = tmp_path / "derived-previous"
        retired = tmp_path / "derived-previous-retired-deadbeef"
        (retired / "half").mkdir(parents=True)
        wda._recover_previous(derived, previous, "build_phase")
        assert (derived / "good").exists(), "a retired leftover is not a kept build"
        await wda._discard_previous(previous)
        assert not retired.exists(), "and the next discard sweeps it"


def test_a_snapshot_is_never_partial_where_recovery_looks(tmp_path, monkeypatch):
    """A copy cut short by a crash at `-previous` would be restored over the
    intact build (#498 review): it is copied beside it and renamed in."""
    src = tmp_path / "derived"
    (src / "Build").mkdir(parents=True)
    dst = tmp_path / "derived-previous"
    import subprocess

    def cp(cmd, **kw):
        assert not dst.exists(), "nothing at -previous while copying"
        assert cmd[-1] != str(dst)
        raise subprocess.CalledProcessError(1, cmd)   # and the fallback copy runs

    monkeypatch.setattr(subprocess, "run", cp)
    wda._snapshot(src, dst)
    assert (dst / "Build").is_dir()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["derived", "derived-previous"]


def test_recovery_settles_the_phase(tmp_path, state):
    """Restored, the build it describes is gone: left set, the next build's
    finished-and-recorded case would be read as cut short (review)."""
    derived, previous = tmp_path / "derived", tmp_path / "derived-previous"
    (derived / "partial").mkdir(parents=True)
    (previous / "old").mkdir(parents=True)
    state["build_phase"] = "building"
    wda._recover_previous(derived, previous, "build_phase")
    assert (derived / "old").is_dir() and not previous.exists()
    assert "build_phase" not in state


class TestTheKeptDeviceBuild:
    async def test_a_build_interrupted_by_a_crash_is_rolled_back_not_kept(self, device, state):
        """A -previous still there means the build that made it never ended:
        it was deleted, keeping the partial build (review, reproduced)."""
        device["old_build"]()
        derived = device["app"].parents[3]
        previous = derived.with_name(derived.name + "-previous")
        import shutil

        shutil.copytree(derived, previous)
        state["build_phase"] = "building"
        (device["app"] / "partial").write_text("half-written")
        device["builds"](ok=False)
        with pytest.raises(RuntimeError):
            await wda.build_wda("TEAM")
        assert (device["app"] / "old").exists() and not (device["app"] / "partial").exists()
        assert "build_phase" not in state

    async def test_a_build_recorded_before_the_crash_keeps_the_new_one(self, device, state):
        """Stopped after the new build was recorded but before the old one was
        deleted: restoring it would run the old build under the new record
        (#498 review)."""
        device["old_build"]()
        derived = device["app"].parents[3]
        previous = derived.with_name(derived.name + "-previous")
        import shutil

        shutil.copytree(derived, previous)
        (device["app"] / "old").write_text("the new build, recorded")
        state["build_wda_version"] = PIN          # recorded; no phase left
        wda._recover_previous(derived, previous, "build_phase")
        assert (device["app"] / "old").read_text() == "the new build, recorded"
        assert not previous.exists()
        await wda._discard_previous(previous)
        assert not list(derived.parent.glob("*-previous*"))

    async def test_the_phase_is_building_while_it_builds_and_gone_after(self, device, state,
                                                                       monkeypatch):
        device["old_build"]()
        device["builds"](ok=True)
        seen = []
        real = wda._snapshot

        def spy(src, dst):
            seen.append(state.get("build_phase"))
            real(src, dst)

        monkeypatch.setattr(wda, "_snapshot", spy)
        assert await wda.build_wda("TEAM") is True
        assert seen == ["building"], "set before the snapshot"
        assert "build_phase" not in state

    async def test_a_failed_build_clears_the_phase(self, device, state):
        device["old_build"]()
        device["builds"](ok=False)
        with pytest.raises(RuntimeError):
            await wda.build_wda("TEAM")
        assert "build_phase" not in state

    async def test_a_failed_build_s_partial_output_is_cleared_before_the_restore(
            self, device, state, monkeypatch):
        device["old_build"]()

        async def half_built(*cmd, **kw):
            (device["app"] / "partial").write_text("x")
            return _proc(65, b"Signing failed")

        monkeypatch.setattr(wda.asyncio, "create_subprocess_exec", half_built)
        with pytest.raises(RuntimeError):
            await wda.build_wda("TEAM")
        assert not (device["app"] / "partial").exists() and (device["app"] / "old").exists()

    async def test_a_cancelled_build_kills_xcodebuild_before_the_restore(self, device, state,
                                                                         monkeypatch):
        """Left running, it wrote into the build just put back (review)."""
        import asyncio

        device["old_build"]()
        proc = MagicMock(returncode=None)
        proc.communicate = AsyncMock(side_effect=asyncio.CancelledError)
        proc.wait = AsyncMock()
        monkeypatch.setattr(wda.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
        with pytest.raises(asyncio.CancelledError):
            await wda.build_wda("TEAM")
        proc.kill.assert_called_once()
        proc.wait.assert_awaited()
        assert (device["app"] / "old").exists()

    async def test_already_running_reports_the_version_too(self, state, monkeypatch):
        state.update({"build_wda_version": "11.4.0",
                      "runners": {"UDID": {"pid": 1234, "started_at": "x"}}})
        monkeypatch.setattr(wda, "_is_process_alive", lambda pid: True)
        monkeypatch.setattr(wda, "_find_xctestrun", lambda: Path("/x.xctestrun"))
        result = await wda.start_driver("UDID", "18.0")
        assert result["status"] == "already_running"
        assert result["wda_version"] == "11.4.0" and "wda_update" in result


class TestTheKeptSimulatorBuild:
    async def test_a_failed_download_keeps_the_build_and_is_remembered(self, sim, state,
                                                                       monkeypatch):
        """It raised on every start, retrying npm each time (review)."""
        sim["old_build"]()
        calls = sim["builds"](ok=True)

        async def offline():
            raise RuntimeError("could not download WebDriverAgent from npm")

        monkeypatch.setattr(wda, "clone_wda", offline)
        assert await wda.build_wda_simulator() is False
        assert calls == [] and state["sim_build_failed_version"] == PIN
        assert (sim["products"] / "Old.xctestrun").exists()

    async def test_with_no_build_a_failed_download_still_raises(self, sim, state, monkeypatch):
        async def offline():
            raise RuntimeError("could not download WebDriverAgent from npm")

        monkeypatch.setattr(wda, "clone_wda", offline)
        with pytest.raises(RuntimeError, match="download"):
            await wda.build_wda_simulator()

    async def test_the_source_is_fetched_before_building(self, sim, state, monkeypatch):
        """Removed, the simulator would build a stale source forever."""
        fetched = AsyncMock(return_value=True)
        monkeypatch.setattr(wda, "clone_wda", fetched)
        sim["builds"](ok=True)
        await wda.build_wda_simulator()
        fetched.assert_awaited_once()

    async def test_a_cancelled_build_is_not_recorded_as_a_failed_version(self, sim, state,
                                                                         monkeypatch):
        """A shutdown mid-build would otherwise stop it ever being retried."""
        import asyncio

        sim["old_build"]()
        proc = MagicMock(returncode=None)
        proc.communicate = AsyncMock(side_effect=asyncio.CancelledError)
        proc.wait = AsyncMock()
        monkeypatch.setattr(wda.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
        with pytest.raises(asyncio.CancelledError):
            await wda.build_wda_simulator()
        assert "sim_build_failed_version" not in state
        assert (sim["products"] / "Old.xctestrun").exists()

    async def test_the_memo_does_not_skip_an_xcode_change(self, sim, state):
        sim["old_build"]()
        state["sim_build_failed_version"] = PIN
        assert await wda._sim_build_is_current(state) is True
        state["sim_build_xcode"] = "26Z9"            # Xcode moved since
        assert await wda._sim_build_is_current(state) is False

    async def test_a_build_interrupted_by_a_crash_is_rolled_back(self, sim, state):
        sim["old_build"]()
        derived = sim["products"].parents[1]
        previous = derived.with_name(derived.name + "-previous")
        import shutil

        shutil.copytree(derived, previous)
        state["sim_build_phase"] = "building"
        (sim["products"] / "Partial.xctestrun").write_text("x")
        sim["builds"](ok=False)
        assert await wda.build_wda_simulator() is False
        assert (sim["products"] / "Old.xctestrun").exists()
        assert not (sim["products"] / "Partial.xctestrun").exists()

    async def test_a_crash_restored_build_gets_its_fingerprint_back(self, sim, state,
                                                                     monkeypatch):
        """Cleared before the build, the fingerprint lived only in memory: a
        crash-restored build was never current, and rebuilt at every start
        past the failed-version memo (#498 review)."""
        sim["old_build"]()
        sim["builds"](ok=False)
        real_rename = Path.rename

        def killed_before_the_restore(self, target):
            # The kill: the build fails, and the process dies before it can
            # put the old build back, leaving the directories as they were.
            if self.name.endswith("-previous") and target == wda.WDA_DERIVED_SIM:
                raise SystemExit("killed")
            return real_rename(self, target)

        monkeypatch.setattr(Path, "rename", killed_before_the_restore)
        with pytest.raises(SystemExit):
            await wda.build_wda_simulator()
        monkeypatch.setattr(Path, "rename", real_rename)
        assert "sim_build_xcode" not in state, "the crash left it cleared"
        wda._recover_previous(wda.WDA_DERIVED_SIM, wda.WDA_DERIVED_SIM.with_name(
            wda.WDA_DERIVED_SIM.name + "-previous"), "sim_build_phase", "sim_build_kept")
        assert (sim["products"] / "Old.xctestrun").read_text() == "old"
        assert state["sim_build_xcode"] == "27A1"
        assert state["sim_build_wda_version"] == "16.15.3"
        assert "sim_build_phase" not in state and "sim_build_kept" not in state
        assert await wda._sim_build_is_current(state) is False, "not the pin, so"
        state["sim_build_failed_version"] = PIN
        assert await wda._sim_build_is_current(state) is True, "the memo works again"

    async def test_a_build_recorded_before_the_crash_keeps_the_new_one(self, sim, state):
        sim["old_build"]()
        derived = sim["products"].parents[1]
        previous = derived.with_name(derived.name + "-previous")
        import shutil

        shutil.copytree(derived, previous)
        (sim["products"] / "Old.xctestrun").unlink()
        (sim["products"] / "New.xctestrun").write_text("new")
        state["sim_build_wda_version"] = PIN          # recorded; no phase left
        wda._recover_previous(derived, previous, "sim_build_phase")
        assert (sim["products"] / "New.xctestrun").exists()
        assert not (sim["products"] / "Old.xctestrun").exists()
        assert not previous.exists()

    async def test_the_phase_is_building_while_it_builds_and_gone_after(self, sim, state,
                                                                       monkeypatch):
        sim["old_build"]()
        sim["builds"](ok=True)
        seen = []
        real = wda._xcodebuild_simulator

        async def spy():
            seen.append(state.get("sim_build_phase"))
            await real()

        monkeypatch.setattr(wda, "_xcodebuild_simulator", spy)
        assert await wda.build_wda_simulator() is True
        assert seen == ["building"]
        assert "sim_build_phase" not in state and "sim_build_kept" not in state

    async def test_a_failed_build_clears_the_phase(self, sim, state):
        sim["old_build"]()
        sim["builds"](ok=False)
        assert await wda.build_wda_simulator() is False
        assert "sim_build_phase" not in state and "sim_build_kept" not in state


async def test_the_simulator_start_says_an_update_failed(state, monkeypatch, tmp_path):
    monkeypatch.setattr(wda, "build_wda_simulator", AsyncMock(return_value=False))
    monkeypatch.setattr(wda, "_find_sim_xctestrun", lambda: tmp_path / "x.xctestrun")
    monkeypatch.setattr(wda, "WDA_LOG_DIR", tmp_path)
    monkeypatch.setattr(wda, "_port_is_free", lambda port: True)
    monkeypatch.setattr(wda.asyncio, "create_subprocess_exec",
                        AsyncMock(return_value=MagicMock(pid=4242)))
    monkeypatch.setattr(wda, "_answers", AsyncMock(return_value=True))
    state.update({"sim_build_wda_version": "16.15.3", "sim_build_failed_version": PIN})
    result = await wda.start_driver_simulator("SIM-1")
    assert result["wda_version"] == "16.15.3" and "failed" in result["wda_update_failed"]


async def test_a_failed_first_build_leaves_no_partial_build_behind(device, state, monkeypatch):
    """Nothing to restore, and half a build must not be left looking like one."""
    async def half_built(*cmd, **kw):
        device["app"].mkdir(parents=True)
        (device["app"] / "partial").write_text("x")
        return _proc(65, b"Signing failed")

    monkeypatch.setattr(wda.asyncio, "create_subprocess_exec", half_built)
    with pytest.raises(RuntimeError):
        await wda.build_wda("TEAM")
    assert not device["app"].exists()
