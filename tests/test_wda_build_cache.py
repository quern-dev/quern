"""When a WebDriverAgent build may be reused, and when it may not.

The cache was keyed on the signing team alone, which answered one of the three
questions that matter and assumed the other two.

* **#188** -- it never checked the artifacts existed. `force` removes the
  derived data *before* building, while `build_team_id` is written only after a
  build that succeeded. So a forced rebuild that fails leaves state from the
  last good build with nothing on disk, every later run skips the build, and
  `install_wda` then raises "WDA app not found — build first". Building is what
  the skip refuses to do. Without knowing to pass `force`, there is no way out.
* **#189** -- it carried no record of the toolchain, so upgrading Xcode was
  invisible to it. Xcode 27 is the live example: machines that had built WDA
  before the upgrade kept the stale artifact and only found out at the next
  forced rebuild, which is also the moment the old one is deleted.

Order matters between them, and it is why they are one change. #189 causes more
rebuilds; #188 is what makes a failed rebuild survivable. Landing the second
alone would have made the first bug easier to hit.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from server.device import wda


@pytest.fixture
def built(tmp_path, monkeypatch):
    """A machine that has successfully built WDA: artifacts on disk, state written."""
    app = tmp_path / "Build" / "Products" / "Debug-iphoneos" / "Runner.app"
    xctestrun = tmp_path / "Build" / "Products" / "quern-driver.xctestrun"
    app.mkdir(parents=True)
    xctestrun.write_text("")
    monkeypatch.setattr(wda, "WDA_APP", app)
    monkeypatch.setattr(wda, "XCTESTRUN", xctestrun)
    return {"app": app, "xctestrun": xctestrun}


def _state(**over):
    base = {
        "build_team_id": "TEAM123",
        "build_deployment_target": wda.WDA_MIN_DEPLOYMENT_TARGET,
    }
    base.update(over)
    return base


class TestTheArtifactsHaveToBeThere:
    """#188, and it is the one that traps a user rather than merely misleading."""

    async def test_a_present_build_is_reused(self, built):
        assert await wda._build_is_current(_state(), "TEAM123") is True

    @pytest.mark.parametrize("missing", ["app", "xctestrun"])
    async def test_state_without_artifacts_is_not_current(self, built, missing):
        """The wedge. State says built, disk says otherwise, and the old check
        believed the state."""
        import shutil

        target = built[missing]
        shutil.rmtree(target) if target.is_dir() else target.unlink()

        assert await wda._build_is_current(_state(), "TEAM123") is False, (
            f"the {missing} is gone but the build reported as current, so the "
            "rebuild is skipped and install then says 'build first'"
        )

    async def test_a_different_team_still_rebuilds(self, built):
        assert await wda._build_is_current(_state(), "OTHERTEAM") is False


class TestTheToolchainIsPartOfTheKey:
    """#189. The event most likely to invalidate the artifact was invisible."""

    async def test_a_different_xcode_rebuilds(self, built):
        with patch.object(wda, "_xcode_build_id", AsyncMock(return_value="17B100")):
            current = await wda._build_is_current(_state(build_xcode="17A5241e"), "TEAM123")
        assert current is False, "an Xcode upgrade did not invalidate the build"

    async def test_the_same_xcode_is_reused(self, built):
        with patch.object(wda, "_xcode_build_id", AsyncMock(return_value="17A5241e")):
            current = await wda._build_is_current(_state(build_xcode="17A5241e"), "TEAM123")
        assert current is True

    async def test_a_toolchain_that_cannot_be_read_does_not_force_a_rebuild(self, built):
        """A probe that failed is not a toolchain that changed.

        The build below fails on its own terms if Xcode is genuinely unusable,
        and that failure says more than a rebuild triggered by a timeout.
        """
        with patch.object(wda, "_xcode_build_id", AsyncMock(return_value=None)):
            current = await wda._build_is_current(_state(build_xcode="17A5241e"), "TEAM123")
        assert current is True

    async def test_an_install_with_no_recorded_toolchain_is_left_alone(self, built):
        """Every existing install is in this state, and rebuilding WDA for all
        of them on upgrade -- minutes, plus a provisioning round trip on a free
        account -- is a poor trade for detecting staleness we cannot confirm.

        Absent means no opinion. The first build after this records it, and
        every change from then on is caught.
        """
        probe = AsyncMock(return_value="17B100")
        with patch.object(wda, "_xcode_build_id", probe):
            current = await wda._build_is_current(_state(), "TEAM123")
        assert current is True, "an upgrade rebuilt WDA on the strength of a missing key"
        assert probe.await_count == 0, "it probed the toolchain with nothing to compare"

    async def test_a_changed_deployment_target_rebuilds(self, built):
        """The input #187 added, which the cache also could not see: bumping the
        floor would otherwise leave everyone on the artifact built against the
        old one."""
        current = await wda._build_is_current(_state(build_deployment_target="13.0"), "TEAM123")
        assert current is False


class TestTheFingerprintIsRecorded:
    """A key nothing writes is a key nothing can compare.

    Worth stating because the first version of this class tested only the
    parser and called itself recorded: deleting the write survived the whole
    suite, which is exactly the shape a review caught in #194.
    """

    async def test_a_successful_build_records_what_produced_it(self, tmp_path, monkeypatch):
        repo = tmp_path / "WebDriverAgent"
        (repo / "WebDriverAgent.xcodeproj").mkdir(parents=True)
        saved: dict = {}

        async def fake_exec(*args, **kwargs):
            class P:
                returncode = 0

                async def communicate(self):
                    return b"", b""
            return P()

        monkeypatch.setattr(wda, "WDA_REPO", repo)
        monkeypatch.setattr(wda, "WDA_DERIVED", tmp_path / "build")
        monkeypatch.setattr(wda, "read_wda_state", lambda: {"cloned": True})
        monkeypatch.setattr(wda, "save_wda_state", lambda st: saved.update(st))
        monkeypatch.setattr(wda, "_post_process_runner_app", AsyncMock())
        monkeypatch.setattr(wda, "_xcode_build_id", AsyncMock(return_value="17A5241e"))
        monkeypatch.setattr(wda.asyncio, "create_subprocess_exec", fake_exec)

        await wda.build_wda("TEAM123")

        assert saved.get("build_deployment_target") == wda.WDA_MIN_DEPLOYMENT_TARGET, (
            "the deployment target was not recorded, so a future change to it "
            f"cannot be detected: {saved}"
        )
        assert saved.get("build_xcode") == "17A5241e", (
            f"the toolchain was not recorded, so an Xcode upgrade stays invisible: {saved}"
        )

    async def test_an_unreadable_toolchain_is_left_unrecorded_not_guessed(
        self, tmp_path, monkeypatch,
    ):
        """Better an absent key -- which means "no opinion" -- than a wrong one
        that would trigger a rebuild on every run afterwards."""
        repo = tmp_path / "WebDriverAgent"
        (repo / "WebDriverAgent.xcodeproj").mkdir(parents=True)
        saved: dict = {}

        async def fake_exec(*args, **kwargs):
            class P:
                returncode = 0

                async def communicate(self):
                    return b"", b""
            return P()

        monkeypatch.setattr(wda, "WDA_REPO", repo)
        monkeypatch.setattr(wda, "WDA_DERIVED", tmp_path / "build")
        monkeypatch.setattr(wda, "read_wda_state", lambda: {"cloned": True})
        monkeypatch.setattr(wda, "save_wda_state", lambda st: saved.update(st))
        monkeypatch.setattr(wda, "_post_process_runner_app", AsyncMock())
        monkeypatch.setattr(wda, "_xcode_build_id", AsyncMock(return_value=None))
        monkeypatch.setattr(wda.asyncio, "create_subprocess_exec", fake_exec)

        await wda.build_wda("TEAM123")

        assert "build_xcode" not in saved

    async def test_the_build_id_is_parsed_from_xcodebuild(self):
        output = "Xcode 27.0\nBuild version 17A5241e\n"
        with patch("server.device.tool_probe.probe_stdout",
                   AsyncMock(return_value=output)):
            assert await wda._xcode_build_id() == "17A5241e"

    async def test_an_unreadable_xcodebuild_gives_none_not_a_guess(self):
        with patch("server.device.tool_probe.probe_stdout", AsyncMock(return_value=None)):
            assert await wda._xcode_build_id() is None

    async def test_output_without_a_build_line_gives_none(self):
        with patch("server.device.tool_probe.probe_stdout",
                   AsyncMock(return_value="Xcode 27.0\n")):
            assert await wda._xcode_build_id() is None


class TestABuildFailureSaysWhatWentWrong:
    """The free-account cases especially, since a rebuild is what consumes a slot.

    The failure table was reachable only from the runner-log path, so a *build*
    that hit the app-ID limit printed twenty raw lines of xcodebuild while the
    identical condition at runner start printed an explanation. A change that
    makes rebuilds more likely has to make their failures legible.
    """

    @pytest.mark.parametrize("output,expect", [
        (
            "error: The maximum number of apps for free development profiles "
            "has been reached.",
            "at most 3 apps installed on one device at a time",
        ),
        (
            "error: The maximum number of apps for free development profiles "
            "has been reached.",
            # The remedy has to be the one that works. Waiting clears the
            # 10-App-IDs-per-7-days registration limit, which is a different
            # error; this one is cleared by deleting an app from the device.
            "Delete a free-signed app from the device",
        ),
        (
            "error: No signing certificate \"iOS Development\" found",
            "No signing certificate found",
        ),
    ])
    def test_a_known_signing_failure_is_named(self, output, expect):
        assert expect in (wda._diagnose_signing_output(output) or "")

    def test_an_unknown_failure_is_not_guessed_at(self):
        assert wda._diagnose_signing_output("error: something nobody has seen") is None

    async def test_the_build_path_uses_it(self, tmp_path, monkeypatch):
        """Not just that the table exists -- that the build consults it."""
        repo = tmp_path / "WebDriverAgent"
        (repo / "WebDriverAgent.xcodeproj").mkdir(parents=True)

        async def failing(*args, **kwargs):
            class P:
                returncode = 65

                async def communicate(self):
                    return (
                        b"error: The maximum number of apps for free "
                        b"development profiles has been reached.",
                        b"",
                    )
            return P()

        monkeypatch.setattr(wda, "WDA_REPO", repo)
        monkeypatch.setattr(wda, "WDA_DERIVED", tmp_path / "build")
        monkeypatch.setattr(wda, "read_wda_state", lambda: {"cloned": True})
        monkeypatch.setattr(wda, "save_wda_state", lambda st: None)
        monkeypatch.setattr(wda.asyncio, "create_subprocess_exec", failing)

        with pytest.raises(RuntimeError, match="Free Apple developer account limit"):
            await wda.build_wda("TEAM123")
