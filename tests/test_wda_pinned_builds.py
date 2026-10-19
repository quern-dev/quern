"""Builds of the pinned WebDriverAgent (#479): a build of any other version
-- every install made before the pin -- is out of date, and a new one that
fails puts the old one back, so a device or simulator that ran WDA still does.
"""

from __future__ import annotations

import copy
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
        assert (device["app"] / "new").exists() and not (device["app"] / "old").exists()
        assert state["build_wda_version"] == PIN
        assert not list(device["app"].parents[3].parent.glob("*-previous"))

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
