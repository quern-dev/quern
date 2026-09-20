"""Health checks have to prove liveness, and have to come back.

The three defects behind this file, all observed rather than imagined:

* `/tools` awaited `xcrun simctl help` with no timeout, and that command hangs
  while Xcode's first-launch tasks run — so `quern doctor` did not answer at
  all (#180).
* Several probes asked only whether a path existed, which reports a corrupt
  install as healthy (#181, #190).
* Backend selection was latched at startup, so an Xcode upgrade left the server
  routing to a backend its own health endpoint called unavailable (#179).

Most of these tests are about the *shape* of an answer rather than its value,
because every one of these bugs returned a perfectly plausible value.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from unittest.mock import AsyncMock, patch

import pytest

from server.device.tool_probe import TOOL_PROBE_TIMEOUT, probe_command, probe_stdout


class TestAProbeIsBounded:
    """A probe that can hang is the bug; the budget is the fix."""

    async def test_a_hanging_command_returns_within_the_budget(self):
        start = time.perf_counter()
        result = await asyncio.wait_for(
            probe_command("sleep", "120", timeout=1.0, tool="wedged"),
            timeout=15.0,
        )
        elapsed = time.perf_counter() - start

        assert result is False
        assert elapsed < 10.0, (
            f"a hanging probe took {elapsed:.1f}s to give up, so /tools would "
            "still be holding up quern doctor — the whole point of the budget"
        )

    async def test_a_wedged_probe_leaves_nothing_running(self):
        """Cancelling the wait does not stop the process, and killing the child
        does not stop what *it* spawned.

        `asyncio.wait_for` raises and moves on while the child keeps running --
        a probe that gives up on a wedged tool and leaks it is how a stray
        `usbmux forward` squatted a port for a week (#160). And `proc.kill()`
        alone is not enough: `xcrun` fronts two of these probes and forks a
        helper, so a marker in the *child's* argv is structurally blind to an
        orphaned grandchild. This spawns one deliberately.

        The survivor is killed before asserting rather than after. A leaked
        `sleep` outlives the test and fails every later run of this file until
        it expires, which turns one real failure into a run of spurious ones --
        and makes any mutation result measured in that window worthless.
        """
        marker = "quern-probe-leak-canary-98214"
        # Bounded from outside, because the thing under test owns the only
        # other bound. When the internal `wait_for` regresses this fails in
        # seconds instead of hanging the suite for the length of the sleep --
        # which is exactly what mutating that timeout away did.
        await asyncio.wait_for(
            probe_command(
                "/bin/sh", "-c", f"sleep 90 & echo go; wait  # {marker}",
                timeout=1.0, tool="canary",
            ),
            timeout=15.0,
        )
        await asyncio.sleep(0.4)

        def alive(pattern: str) -> list[str]:
            return subprocess.run(
                ["pgrep", "-f", pattern], capture_output=True, text=True,
                timeout=10,
            ).stdout.split()

        child = alive(marker)
        grandchild = [p for p in alive("^sleep 90") if p not in child]
        for pid in child + grandchild:
            subprocess.run(["kill", "-9", pid], capture_output=True)

        assert not child, f"the probe left its child running: {child}"
        assert not grandchild, (
            f"the probe killed its child but orphaned what the child spawned: "
            f"{grandchild} -- which a pgrep on the child's own argv cannot see"
        )

    def test_the_default_budget_is_far_above_a_healthy_probe(self):
        """Measured warm: the slowest probe (pymobiledevice3) was 0.23s.

        The budget bounds an *indefinite hang*, not a slow answer, so the only
        property that matters is that it never fires on a working machine. A
        value tuned close to the measurement would report healthy tools as
        missing -- the exact failure this module exists to prevent, and the same
        mistake that put the WDA /source budgets under water twice.
        """
        slowest_healthy_probe = 0.23
        assert TOOL_PROBE_TIMEOUT >= slowest_healthy_probe * 20, (
            f"{TOOL_PROBE_TIMEOUT}s is close enough to the {slowest_healthy_probe}s "
            "measured warm that a cold or loaded machine would trip it, and a "
            "probe that gives up early reports a healthy tool as missing"
        )

    def test_the_budget_fits_inside_the_one_the_doctor_client_allows(self):
        """The half that was missing, and it is the half that bites.

        `quern doctor` fetches /tools with its own budget. Raise the server's
        per-probe budget above it and a wedged tool makes the *client* give up
        first, so doctor reports the server unreachable instead of the tool
        wedged -- #180's symptom restored from the other end, with every test
        still green. Only the lower bound was pinned, so 600.0 survived the
        whole suite.
        """
        from server.lifecycle.state import TOOLS_FETCH_TIMEOUT

        assert TOOL_PROBE_TIMEOUT < TOOLS_FETCH_TIMEOUT, (
            f"a probe may take {TOOL_PROBE_TIMEOUT}s but doctor only waits "
            f"{TOOLS_FETCH_TIMEOUT}s for the whole /tools request"
        )


class TestAProbeProvesLiveness:
    """Existence is not health. Every one of these returned True for a path."""

    async def test_a_command_that_exits_nonzero_is_not_available(self):
        assert await probe_command(sys.executable, "-c", "raise SystemExit(3)") is False

    async def test_a_command_that_exits_zero_is_available(self):
        assert await probe_command(sys.executable, "-c", "pass") is True

    async def test_a_missing_command_is_not_available(self):
        assert await probe_command("quern-no-such-binary-31337") is False

    async def test_the_runtime_environment_reaches_the_child(self):
        """The false-failure guard, and it is not hypothetical.

        The patched `idb_companion` resolves its frameworks through
        `DYLD_FRAMEWORK_PATH`. Probed bare it dies in dyld, so the obvious
        `--version`-and-check-rc probe reports a *working* install as broken.
        Measured on a real install: False bare, True with the env. Trading a
        false pass for a false failure is the same defect facing the other way.
        """
        script = "import os, sys; sys.exit(0 if os.environ.get('QUERN_PROBE_ENV') == 'yes' else 9)"

        assert await probe_command(sys.executable, "-c", script) is False
        assert await probe_command(
            sys.executable, "-c", script, env={"QUERN_PROBE_ENV": "yes"},
        ) is True


class TestProbeStdoutSeparatesSilenceFromFailure:
    """`None` is "could not ask"; `""` is "answered with nothing"."""

    async def test_a_successful_command_returns_its_output(self):
        out = await probe_stdout(sys.executable, "-c", "print('hello')")
        assert out is not None and out.strip() == "hello"

    async def test_a_failing_command_returns_none_not_empty_string(self):
        out = await probe_stdout(sys.executable, "-c", "raise SystemExit(1)")
        assert out is None, (
            "a failed probe returned a string, so a caller doing `out.strip()` "
            "reads the failure as an empty answer — the false all-clear again"
        )

    async def test_a_command_that_succeeds_silently_returns_empty_not_none(self):
        out = await probe_stdout(sys.executable, "-c", "pass")
        assert out is not None and out.strip() == ""


class TestCheckToolsSurvivesOneBadProbe:
    """Seven probes, and any of them can fail. Six answers beat none."""

    def _controller(self):
        from server.device.controller import DeviceController
        return DeviceController()

    async def test_one_probe_raising_does_not_take_the_endpoint_down(self):
        ctrl = self._controller()
        with (
            patch.object(ctrl.simctl, "is_available", AsyncMock(side_effect=RuntimeError("boom"))),
            patch.object(ctrl.idb, "is_available", AsyncMock(return_value=True)),
            patch.object(ctrl.devicectl, "is_available", AsyncMock(return_value=False)),
            patch.object(ctrl.pmd3, "is_available", AsyncMock(return_value=True)),
            patch.object(ctrl.adb, "is_available", AsyncMock(return_value=False)),
            patch.object(ctrl.sim_bridge_manager, "is_available", AsyncMock(return_value=True)),
            patch("server.device.tunneld.is_tunneld_running", AsyncMock(return_value=False)),
        ):
            tools = await ctrl.check_tools()

        assert tools["simctl"] is False, "a raising probe must not read as available"
        assert tools["idb"] is True, "one bad probe discarded the others' answers"
        assert set(tools) == {
            "simctl", "idb", "devicectl", "pymobiledevice3",
            "tunneld", "adb", "sim_bridge",
        }

    async def test_the_probes_run_concurrently(self):
        """Sequentially the shared budget is per tool, so a machine with
        several wedged tools multiplies the wait that #180 is about."""
        ctrl = self._controller()

        async def slow():
            await asyncio.sleep(0.2)
            return True

        with (
            patch.object(ctrl.simctl, "is_available", slow),
            patch.object(ctrl.idb, "is_available", slow),
            patch.object(ctrl.devicectl, "is_available", slow),
            patch.object(ctrl.pmd3, "is_available", slow),
            patch.object(ctrl.adb, "is_available", slow),
            patch.object(ctrl.sim_bridge_manager, "is_available", slow),
            patch("server.device.tunneld.is_tunneld_running", slow),
        ):
            start = time.perf_counter()
            await ctrl.check_tools()
            elapsed = time.perf_counter() - start

        assert elapsed < 0.2 * 7 * 0.6, (
            f"seven 0.2s probes took {elapsed:.2f}s, which is close enough to "
            "their sum that they are still running one after another"
        )


class TestTheBackendIsNotLatchedAtStartup:
    """#179: Xcode moved SimulatorKit under a running server, and it kept
    routing every tap to a backend that could no longer work — while `/tools`
    reported the correct answer. The server and its health endpoint disagreed."""

    def _controller(self):
        from server.device.controller import DeviceController
        return DeviceController()

    async def _check(self, ctrl, sim_bridge: bool):
        with (
            patch.object(ctrl.simctl, "is_available", AsyncMock(return_value=True)),
            patch.object(ctrl.idb, "is_available", AsyncMock(return_value=True)),
            patch.object(ctrl.devicectl, "is_available", AsyncMock(return_value=True)),
            patch.object(ctrl.pmd3, "is_available", AsyncMock(return_value=True)),
            patch.object(ctrl.adb, "is_available", AsyncMock(return_value=True)),
            patch.object(
                ctrl.sim_bridge_manager, "is_available",
                AsyncMock(return_value=sim_bridge),
            ),
            patch("server.device.tunneld.is_tunneld_running", AsyncMock(return_value=True)),
        ):
            return await ctrl.check_tools(adopt=True)

    async def test_checking_tools_resyncs_a_server_that_has_gone_stale(self):
        ctrl = self._controller()
        await self._check(ctrl, sim_bridge=True)
        assert ctrl._sim_bridge_ok is True

        # The toolchain moves under the running server.
        tools = await self._check(ctrl, sim_bridge=False)

        assert tools["sim_bridge"] is False
        assert ctrl._sim_bridge_ok is False, (
            "/tools reported the backend unavailable while the server went on "
            "routing to it — which is #179 exactly"
        )

    async def test_establishing_the_backend_at_startup_is_not_a_warning(self, caplog):
        """Found by running the server rather than by a test.

        Every boot establishes this from False, so warning on the first answer
        put a WARNING in every startup log for the most ordinary event there
        is. A warning that always fires is one nobody reads on the day it
        means something.
        """
        ctrl = self._controller()
        with caplog.at_level("WARNING", logger="quern-debug-server.device"):
            await self._check(ctrl, sim_bridge=True)
        assert not [r for r in caplog.records if "sim-bridge backend became" in r.message], (
            "startup logged a backend-change warning for the initial answer"
        )

    async def test_a_change_under_a_running_server_is_a_warning(self, caplog):
        """The case it exists for: the toolchain moved and routing follows."""
        ctrl = self._controller()
        await self._check(ctrl, sim_bridge=True)
        with caplog.at_level("WARNING", logger="quern-debug-server.device"):
            await self._check(ctrl, sim_bridge=False)
        assert [r for r in caplog.records if "sim-bridge backend became" in r.message], (
            "the backend flipped under a running server and said nothing"
        )

    async def test_a_fresh_answer_is_not_re_probed(self):
        """The refresh has to be cheap enough to call often: it spawns
        `xcode-select`, and UI operations pick a backend on a sync path."""
        ctrl = self._controller()
        probe = AsyncMock(return_value=True)
        with patch.object(ctrl.sim_bridge_manager, "is_available", probe):
            await ctrl.refresh_sim_bridge_availability(max_age=300)
            await ctrl.refresh_sim_bridge_availability(max_age=300)
        assert probe.await_count == 1, "a cached answer was re-probed"

    async def test_a_freshly_booted_machine_does_not_serve_an_empty_cache(self):
        """`time.monotonic()` counts from boot, so it is legitimately near zero
        on a machine that just started.

        With 0.0 as the "never checked" sentinel, `monotonic() - 0.0 < max_age`
        is true for the first `max_age` seconds of uptime -- so an unpopulated
        cache reads as fresh and the backend is never probed at all. The two
        tests around this one cannot see it: they pass on any machine with more
        than five minutes of uptime, which is every developer machine and no CI
        runner. All four Python versions failed on the same commit.
        """
        ctrl = self._controller()
        probe = AsyncMock(return_value=True)
        with (
            patch.object(ctrl.sim_bridge_manager, "is_available", probe),
            patch("server.device.controller.time.monotonic", return_value=12.0),
        ):
            result = await ctrl.refresh_sim_bridge_availability(max_age=300)

        assert probe.await_count == 1, (
            "an answer that was never established was served as cached, because "
            "the machine had only just booted"
        )
        assert result is True

    async def test_a_stale_answer_is_re_probed(self):
        ctrl = self._controller()
        probe = AsyncMock(return_value=True)
        with patch.object(ctrl.sim_bridge_manager, "is_available", probe):
            await ctrl.refresh_sim_bridge_availability(max_age=300)
            await ctrl.refresh_sim_bridge_availability(max_age=0)
        assert probe.await_count == 2, (
            "the cached answer was reused past its age, which is the latch "
            "with a timestamp bolted on"
        )


class TestTheCompanionCheckAsksTheBinary:
    """#190: it returned OK for anything occupying the path."""

    def test_a_present_but_broken_binary_is_not_reported_ok(self, tmp_path, monkeypatch):
        from server.lifecycle import setup as s

        companion = tmp_path / "bin" / "idb_companion"
        companion.parent.mkdir(parents=True)
        companion.write_text("#!/bin/sh\nexit 1\n")
        companion.chmod(0o755)
        monkeypatch.setattr(s, "CONFIG_DIR", tmp_path)

        result = s.check_idb_companion()

        assert result.status is not s.CheckStatus.OK, (
            "a binary that cannot run reported as installed and healthy, and "
            "because the patched copy is preferred it would shadow a working one"
        )
        assert "not running" in result.message

    def test_a_working_binary_is_reported_ok(self, tmp_path, monkeypatch):
        from server.lifecycle import setup as s

        companion = tmp_path / "bin" / "idb_companion"
        companion.parent.mkdir(parents=True)
        companion.write_text("#!/bin/sh\nexit 0\n")
        companion.chmod(0o755)
        (tmp_path / "bin" / "idb_companion.release").write_text(
            s._IDB_COMPANION_RELEASE + "\n"
        )
        monkeypatch.setattr(s, "CONFIG_DIR", tmp_path)

        assert s.check_idb_companion().status is s.CheckStatus.OK

    def test_the_probe_supplies_the_framework_path_the_binary_needs(self, tmp_path):
        """Without this the probe reports a *working* install as broken.

        The real companion resolves its frameworks through
        `DYLD_FRAMEWORK_PATH`, exactly as `IDBController._companion_env` sets
        it. Measured on a real install: exit 1 bare, exit 0 with the env. So
        the naive `--version` probe is a false failure, not a fix.

        Asserted on the environment rather than through a fake binary on
        purpose: macOS strips `DYLD_*` when spawning a SIP-protected executable
        such as `/bin/sh`, so a shell script cannot observe it and a test built
        on one would pass whether or not the variable was ever set. The real
        companion is ad-hoc signed in `~/.quern/bin` and does receive it.
        """
        from server.device.idb import IdbBackend
        from server.lifecycle.setup import _companion_probe_env

        companion = tmp_path / "bin" / "idb_companion"
        env = _companion_probe_env(companion)
        frameworks = tmp_path / "bin" / "Frameworks"

        assert env["DYLD_FRAMEWORK_PATH"] == (
            f"{frameworks}:{frameworks / 'PackageFrameworks'}"
        )
        # And it must agree with what the runtime actually uses, or the check
        # and the thing it is checking are testing different installs.
        backend = IdbBackend()
        backend._QUERN_COMPANION = companion
        assert backend._companion_env()["DYLD_FRAMEWORK_PATH"] == env["DYLD_FRAMEWORK_PATH"]


class TestTheCompanionProbeIsWiredUpCorrectly:
    """The guard this change argues hardest for, tested where it matters.

    Comparing `_companion_probe_env()` with `IdbBackend._companion_env()` proves
    the two agree. It does not prove `check_idb_companion` *uses* it -- deleting
    `env=` from the call survived the entire suite, and the consequence is that
    every correctly-installed patched companion reports ERROR.
    """

    def _companion(self, tmp_path, body: str = "#!/bin/sh\nexit 0\n", mode: int = 0o755):
        c = tmp_path / "bin" / "idb_companion"
        c.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(body, bytes):
            c.write_bytes(body)
        else:
            c.write_text(body)
        c.chmod(mode)
        return c

    def test_the_probe_is_given_the_framework_path(self, tmp_path, monkeypatch):
        from server.lifecycle import setup as s

        self._companion(tmp_path)
        monkeypatch.setattr(s, "CONFIG_DIR", tmp_path)
        seen = {}

        def fake_run(cmd, timeout=30, env=None):
            seen["env"] = env
            return 0, "", ""

        monkeypatch.setattr(s, "_run", fake_run)
        s.check_idb_companion()

        assert seen["env"] is not None and "DYLD_FRAMEWORK_PATH" in seen["env"], (
            "check_idb_companion probed the companion bare, so a working "
            "install reports as broken"
        )

    @pytest.mark.parametrize("name,body,mode", [
        ("a truncated binary", b"\xcf\xfa\xed\xfe" + b"\x00" * 8, 0o755),
        ("one that lost its exec bit", "#!/bin/sh\nexit 0\n", 0o644),
    ])
    def test_a_binary_that_cannot_be_run_is_reported_not_raised(
        self, tmp_path, monkeypatch, name, body, mode,
    ):
        """The corruption the check exists for must not crash the check.

        `subprocess.run` raises OSError for a truncated binary and
        PermissionError for one that lost its exec bit -- neither is
        FileNotFoundError or TimeoutExpired. Uncaught, those escape
        `check_idb_companion` into `run_setup`, so `quern setup` and
        `quern update` end in a traceback instead of a diagnosis. Before this
        change the same file was merely reported OK, so a crash would be a
        regression rather than a fix.
        """
        from server.lifecycle import setup as s

        self._companion(tmp_path, body, mode)
        monkeypatch.setattr(s, "CONFIG_DIR", tmp_path)

        result = s.check_idb_companion()

        assert result.status is not s.CheckStatus.OK, f"{name} reported healthy"
        assert "not running" in result.message


class TestHotPathsDoNotSpawnSubprocesses:
    """`is_available` is a real probe now, so it is no longer free.

    Two call sites ask "should I try the Android path at all" rather than "is
    this healthy". Putting a subprocess in front of every device listing is not
    a fix, and the command they guard fails on its own terms anyway.
    """

    async def test_listing_devices_does_not_probe(self):
        from server.device.adb import AdbBackend

        backend = AdbBackend()
        backend._adb_path = "/usr/bin/adb"
        backend._run_adb = AsyncMock(return_value=("List of devices attached\n", ""))
        backend.list_avds = AsyncMock(return_value=[])
        with patch("server.device.adb.probe_command", AsyncMock()) as probe:
            await backend.list_devices()
        assert probe.await_count == 0, (
            "list_devices spawned a liveness probe, so every device listing now "
            "pays for a subprocess"
        )
        assert backend._run_adb.await_count > 0, (
            "the listing never ran, so the probe count proves nothing"
        )

    async def test_resolving_a_named_emulator_does_not_probe(self):
        from server.device.controller import DeviceController

        ctrl = DeviceController()
        ctrl.adb._adb_path = "/usr/bin/adb"
        ctrl.adb.list_avds = AsyncMock(return_value=[])
        with patch("server.device.adb.probe_command", AsyncMock()) as probe:
            ctrl.adb.is_installed()
            await ctrl.adb.list_avds()
        assert probe.await_count == 0


class TestMeasuringIsNotSelecting:
    """Adopting the measurement is opt-in, and caching keeps listings cheap."""

    def _controller(self):
        from server.device.controller import DeviceController
        return DeviceController()

    async def _probe_all(self, ctrl, sim_bridge, **kw):
        with (
            patch.object(ctrl.simctl, "is_available", AsyncMock(return_value=True)),
            patch.object(ctrl.idb, "is_available", AsyncMock(return_value=True)),
            patch.object(ctrl.devicectl, "is_available", AsyncMock(return_value=True)),
            patch.object(ctrl.pmd3, "is_available", AsyncMock(return_value=True)),
            patch.object(ctrl.adb, "is_available", AsyncMock(return_value=True)),
            patch.object(
                ctrl.sim_bridge_manager, "is_available",
                AsyncMock(return_value=sim_bridge),
            ),
            patch("server.device.tunneld.is_tunneld_running", AsyncMock(return_value=True)),
        ):
            return await ctrl.check_tools(**kw)

    async def test_listing_devices_does_not_reselect_the_backend(self):
        """It decides which backend serves every subsequent tap, and nobody
        associates *listing devices* with re-selecting one."""
        ctrl = self._controller()
        await self._probe_all(ctrl, sim_bridge=True, adopt=True)
        await self._probe_all(ctrl, sim_bridge=False)  # a listing, not a health check
        assert ctrl._sim_bridge_ok is True, (
            "a device listing silently switched the UI backend"
        )

    async def test_reporting_health_does_reselect(self):
        ctrl = self._controller()
        await self._probe_all(ctrl, sim_bridge=True, adopt=True)
        await self._probe_all(ctrl, sim_bridge=False, adopt=True)
        assert ctrl._sim_bridge_ok is False

    async def test_a_listing_can_reuse_a_recent_measurement(self):
        """Seven tools per request is six subprocesses, and `list_devices` is a
        hot path for an agent. #180 called out "no timeout, no cache"."""
        ctrl = self._controller()
        await self._probe_all(ctrl, sim_bridge=True)
        probe = AsyncMock(return_value=True)
        with patch.object(ctrl.simctl, "is_available", probe):
            await ctrl.check_tools(max_age=300)
        assert probe.await_count == 0, "a cached listing re-probed every tool"

    async def test_reporting_health_never_serves_a_cached_answer(self):
        ctrl = self._controller()
        await self._probe_all(ctrl, sim_bridge=True)
        probe = AsyncMock(return_value=False)
        with patch.object(ctrl.simctl, "is_available", probe):
            tools = await ctrl.check_tools(adopt=True)
        assert probe.await_count == 1, "/tools served a stale answer"
        assert tools["simctl"] is False


class TestAnOutdatedCompanionIsReplaced:
    """#222: v1 cannot find SimulatorKit under Xcode 27, so every HID command
    it runs fails. An install that works but is older than this quern's must
    say so, and setup must offer to replace it -- including on a sim-bridge
    machine, where idb is skipped but an old install is still the fallback.
    """

    def _install(self, tmp_path, monkeypatch, release=None, *, marker_bytes=None):
        from server.lifecycle import setup as s

        bin_dir = tmp_path / "bin"
        companion = bin_dir / "idb_companion"
        companion.parent.mkdir(parents=True, exist_ok=True)
        companion.write_text("#!/bin/sh\nexit 0\n")
        companion.chmod(0o755)
        old_fw = bin_dir / "Frameworks" / "Old.framework" / "Versions" / "A" / "Frameworks"
        old_fw.mkdir(parents=True)
        (old_fw / "stale").write_text("v1")
        marker = bin_dir / "idb_companion.release"
        if release is not None:
            marker.write_text(release + "\n")
        if marker_bytes is not None:
            marker.write_bytes(marker_bytes)
        monkeypatch.setattr(s, "CONFIG_DIR", tmp_path)
        monkeypatch.setattr(s, "_record_install", lambda *a, **k: None)
        # Pinned, not inherited: the patched build is arm64-only, and these
        # tests are about the install rather than the machine running them.
        # CI's Linux runners report x86_64, where every one of them would
        # otherwise exercise the refusal path instead.
        monkeypatch.setattr(s, "_is_apple_silicon", lambda: True)
        return s

    def _fake_download(self, monkeypatch, tmp_path, *, ok=True, layout=("bin", "Frameworks")):
        """A tarball with the real release's top-level layout."""
        import tarfile
        import urllib.request

        def retrieve(url, dest):
            if not ok:
                raise OSError("network down")
            payload = tmp_path / "payload"
            if "bin" in layout:
                (payload / "bin").mkdir(parents=True, exist_ok=True)
                (payload / "bin" / "idb_companion").write_text("#!/bin/sh\necho v2\n")
            if "Frameworks" in layout:
                fw = payload / "Frameworks" / "FBControlCore.framework"
                fw.mkdir(parents=True, exist_ok=True)
                (fw / "FBControlCore").write_text("v2")
            with tarfile.open(dest, "w:gz") as tar:
                for top in layout:
                    tar.add(payload / top, arcname=f"./{top}")

        monkeypatch.setattr(urllib.request, "urlretrieve", retrieve)

    # -- reading the marker ---------------------------------------------------

    def test_an_install_without_a_marker_is_v1_and_outdated(self, tmp_path, monkeypatch):
        s = self._install(tmp_path, monkeypatch)

        result = s.check_idb_companion()

        assert s.companion_is_outdated()
        assert result.status is s.CheckStatus.WARNING
        assert "idb-companion-v1" in result.message
        assert "Xcode 27" in result.detail
        assert result.fixable

    def test_the_current_release_is_not_outdated(self, tmp_path, monkeypatch):
        from server.lifecycle import setup as real

        s = self._install(tmp_path, monkeypatch, release=real._IDB_COMPANION_RELEASE)

        assert not s.companion_is_outdated()
        assert s.check_idb_companion().status is s.CheckStatus.OK

    def test_a_newer_release_is_not_offered_a_downgrade(self, tmp_path, monkeypatch):
        s = self._install(tmp_path, monkeypatch, release="idb-companion-v99")

        assert not s.companion_is_outdated()
        assert s.check_idb_companion().status is s.CheckStatus.OK

    @pytest.mark.parametrize("content", ["garbage", "idb-companion-vX"])
    def test_a_marker_naming_no_release_is_outdated(self, tmp_path, monkeypatch, content):
        s = self._install(tmp_path, monkeypatch, release=content)
        assert s.companion_is_outdated()

    def test_a_marker_that_is_not_text_does_not_crash_the_check(self, tmp_path, monkeypatch):
        """UnicodeDecodeError is a ValueError, not an OSError."""
        s = self._install(tmp_path, monkeypatch, marker_bytes=b"\xff\xfe\x00bad")

        assert s.companion_is_outdated()
        assert s.check_idb_companion().status is s.CheckStatus.WARNING

    def test_a_marker_that_is_a_directory_does_not_crash_anything(self, tmp_path, monkeypatch):
        s = self._install(tmp_path, monkeypatch)
        (tmp_path / "bin" / "idb_companion.release").mkdir()
        self._fake_download(monkeypatch, tmp_path)

        assert s.companion_is_outdated()
        assert s.check_idb_companion().status is s.CheckStatus.WARNING
        assert s._install_patched_companion() is False

    def test_no_install_is_not_outdated(self, tmp_path, monkeypatch):
        from server.lifecycle import setup as s

        monkeypatch.setattr(s, "CONFIG_DIR", tmp_path)
        assert not s.companion_is_outdated()

    def test_the_download_url_names_the_current_release(self):
        from server.lifecycle import setup as s

        assert s._release_number(s._IDB_COMPANION_RELEASE) >= 2
        assert f"/{s._IDB_COMPANION_RELEASE}/" in s._IDB_COMPANION_URL

    # -- installing ---------------------------------------------------------

    def test_a_successful_install_replaces_the_tree_and_records_the_release(
        self, tmp_path, monkeypatch,
    ):
        s = self._install(tmp_path, monkeypatch)
        self._fake_download(monkeypatch, tmp_path)

        assert s._install_patched_companion()

        bin_dir = tmp_path / "bin"
        assert "v2" in (bin_dir / "idb_companion").read_text()
        assert (bin_dir / "Frameworks" / "FBControlCore.framework" / "FBControlCore").is_file()
        assert not (bin_dir / "Frameworks" / "Old.framework").exists(), (
            "files only the old release had were left behind"
        )
        assert not (bin_dir / "bin").exists()
        assert not any(p.name.startswith(".idb-companion-") for p in bin_dir.iterdir())
        assert s._installed_companion_release() == s._IDB_COMPANION_RELEASE
        assert not s.companion_is_outdated()

    def test_a_failed_download_leaves_the_install_and_its_marker_alone(
        self, tmp_path, monkeypatch,
    ):
        """A current install must not start reading as outdated because a
        re-download failed."""
        s = self._install(tmp_path, monkeypatch, release="idb-companion-v2")
        self._fake_download(monkeypatch, tmp_path, ok=False)

        assert not s._install_patched_companion()
        assert s._installed_companion_release() == "idb-companion-v2"
        assert (tmp_path / "bin" / "Frameworks" / "Old.framework").exists()

    @pytest.mark.parametrize("layout", [("Frameworks",), ("bin",)])
    def test_an_incomplete_download_changes_nothing(self, tmp_path, monkeypatch, layout):
        """Nothing is touched -- the marker included -- until the payload is
        known to be whole."""
        s = self._install(tmp_path, monkeypatch, release="idb-companion-v2")
        self._fake_download(monkeypatch, tmp_path, layout=layout)

        assert not s._install_patched_companion()
        assert "v2" not in (tmp_path / "bin" / "idb_companion").read_text()
        assert (tmp_path / "bin" / "Frameworks" / "Old.framework").exists()
        assert s._installed_companion_release() == "idb-companion-v2"

    def test_a_failed_binary_swap_restores_the_old_install(self, tmp_path, monkeypatch):
        """The binary moves last, so a failure there would otherwise leave the
        new frameworks beside the old binary."""
        s = self._install(tmp_path, monkeypatch)
        self._fake_download(monkeypatch, tmp_path)
        real_replace = type(tmp_path).replace

        def replace(self, target):
            if self.name == "idb_companion":
                raise OSError("permission denied")
            return real_replace(self, target)

        monkeypatch.setattr(type(tmp_path), "replace", replace)

        assert not s._install_patched_companion()
        assert "v2" not in (tmp_path / "bin" / "idb_companion").read_text()
        assert (tmp_path / "bin" / "Frameworks" / "Old.framework").exists(), (
            "the new frameworks were left beside the old binary"
        )
        assert s.companion_is_outdated()

    def test_a_stale_staging_directory_is_cleared(self, tmp_path, monkeypatch):
        """Only a kill -9 leaves one, and each is ~17MB."""
        s = self._install(tmp_path, monkeypatch)
        stale = tmp_path / "bin" / ".idb-companion-leftover"
        (stale / "bin").mkdir(parents=True)
        self._fake_download(monkeypatch, tmp_path)

        assert s._install_patched_companion()
        assert not stale.exists()

    def test_a_failed_swap_restores_the_old_frameworks(self, tmp_path, monkeypatch):
        s = self._install(tmp_path, monkeypatch)
        self._fake_download(monkeypatch, tmp_path)
        real_rename = type(tmp_path).rename

        def rename(self, target):
            if self.name == "Frameworks" and ".idb-companion-" in str(self):
                raise OSError("no space left on device")
            return real_rename(self, target)

        monkeypatch.setattr(type(tmp_path), "rename", rename)

        assert not s._install_patched_companion()
        assert (tmp_path / "bin" / "Frameworks" / "Old.framework").exists()
        assert s.companion_is_outdated()

    def test_a_marker_that_cannot_be_written_does_not_fail_the_install(
        self, tmp_path, monkeypatch,
    ):
        s = self._install(tmp_path, monkeypatch)
        self._fake_download(monkeypatch, tmp_path)
        real_write = type(tmp_path).write_text

        def write_text(self, *a, **k):
            if self.name == "idb_companion.release":
                raise OSError("read-only")
            return real_write(self, *a, **k)

        monkeypatch.setattr(type(tmp_path), "write_text", write_text)

        assert s._install_patched_companion() is True
        assert "v2" in (tmp_path / "bin" / "idb_companion").read_text()


class TestAnIntelMacIsNotOfferedAnArm64Binary:
    """The published tarball is arm64-only, and Intel is exactly where setup
    reaches the idb path: `_sim_bridge_supported()` is False there, so
    `run_setup` takes the `sim_bridge=False` branch that offers the download.

    Installing it would put a binary that cannot execute at
    `~/.quern/bin/idb_companion`, which `IdbBackend` prefers over the system
    one -- shadowing a working Homebrew companion with a broken one.
    """

    def _intel(self, tmp_path, monkeypatch):
        from server.lifecycle import setup as s

        monkeypatch.setattr(s, "CONFIG_DIR", tmp_path)
        monkeypatch.setattr(s, "_is_apple_silicon", lambda: False)
        return s

    def test_the_install_refuses_and_downloads_nothing(self, tmp_path, monkeypatch):
        """Asserted on what was *attempted*, not on the return value.

        A stub that raises proves nothing here: the installer catches a failed
        download and returns False, so `is False` holds whether it refused up
        front or tried and fell over -- which is the same test passing against
        the bug it names. Measured: with the gate removed this assertion on
        the return value alone still passed, while the captured output read
        "Downloading patched idb_companion...".
        """
        s = self._intel(tmp_path, monkeypatch)
        attempts = []

        import urllib.request
        monkeypatch.setattr(
            urllib.request, "urlretrieve",
            lambda url, *a, **k: attempts.append(url),
        )

        assert s._install_patched_companion() is False
        assert attempts == [], f"an arm64 tarball was downloaded on Intel: {attempts}"
        assert not (tmp_path / "bin" / "idb_companion").exists()

    def test_an_install_is_not_called_outdated(self, tmp_path, monkeypatch):
        """There is nothing to update it to, so offering one is a dead end."""
        s = self._intel(tmp_path, monkeypatch)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "idb_companion").write_text("#!/bin/sh\nexit 0\n")
        (bin_dir / "idb_companion.release").write_text("idb-companion-v1\n")

        assert s.companion_is_outdated() is False

    def test_setup_does_not_ask_a_question_it_cannot_honour(self, tmp_path, monkeypatch):
        """A prompt answered yes, then refused, reported "Download failed",
        which describes the wrong problem."""
        s = self._intel(tmp_path, monkeypatch)
        prompts = []
        monkeypatch.setattr(s, "_prompt_yn", lambda msg, *a, **k: prompts.append(msg) or True)
        monkeypatch.setattr(s, "companion_is_outdated", lambda: False)
        monkeypatch.setattr(s, "check_idb_companion", lambda: s.CheckResult(
            name="idb_companion", status=s.CheckStatus.MISSING, message="not found",
        ))

        result = s._setup_idb_companion(sim_bridge=False)

        assert prompts == [], f"Intel was offered the patched build: {prompts}"
        assert result.status is s.CheckStatus.MISSING

    def test_a_system_companion_is_left_alone(self, tmp_path, monkeypatch):
        """The 'replace the system one with the patched build' offer is the
        other way an arm64 binary could land on an Intel Mac."""
        s = self._intel(tmp_path, monkeypatch)
        prompts = []
        monkeypatch.setattr(s, "_prompt_yn", lambda msg, *a, **k: prompts.append(msg) or True)
        monkeypatch.setattr(s, "companion_is_outdated", lambda: False)
        monkeypatch.setattr(s, "check_idb_companion", lambda: s.CheckResult(
            name="idb_companion", status=s.CheckStatus.OK,
            message="installed (system, /usr/local/bin/idb_companion)",
        ))

        result = s._setup_idb_companion(sim_bridge=False)

        assert prompts == [], f"Intel was offered the patched build: {prompts}"
        assert result.status is s.CheckStatus.OK


class TestSetupsCompanionStep:
    """`_setup_idb_companion`, which run_setup reports for both of its
    simulator paths. Driven for real: a check of the source text let a result
    computed and then thrown away pass."""

    def _stub(self, monkeypatch, *, outdated, answer, installs=True, status=None,
              message="installed (patched, x)"):
        from server.lifecycle import setup as s

        calls = {"prompted": 0, "installed": 0}
        state = {"outdated": outdated, "status": status, "message": message}

        def prompt(*a, **k):
            calls["prompted"] += 1
            return answer

        def install():
            calls["installed"] += 1
            if installs:
                state["outdated"] = False
                state["status"] = None
            return installs

        def check():
            if state["status"] is not None:
                return s.CheckResult(name="idb_companion", status=state["status"],
                                     message=state["message"])
            if state["outdated"]:
                return s.CheckResult(name="idb_companion", status=s.CheckStatus.WARNING,
                                     message="installed (patched, outdated: idb-companion-v1)")
            return s.CheckResult(name="idb_companion", status=s.CheckStatus.OK,
                                 message="installed (patched, x)")

        monkeypatch.setattr(s, "_is_apple_silicon", lambda: True)
        monkeypatch.setattr(s, "_prompt_yn", prompt)
        monkeypatch.setattr(s, "_install_patched_companion", install)
        monkeypatch.setattr(s, "check_idb_companion", check)
        monkeypatch.setattr(s, "companion_is_outdated", lambda: state["outdated"])
        return s, calls

    @pytest.mark.parametrize("sim_bridge", [True, False])
    def test_an_outdated_install_is_updated_when_accepted(self, monkeypatch, sim_bridge):
        s, calls = self._stub(monkeypatch, outdated=True, answer=True)

        result = s._setup_idb_companion(sim_bridge=sim_bridge)

        assert calls["installed"] == 1
        assert result.status is s.CheckStatus.OK

    @pytest.mark.parametrize("sim_bridge", [True, False])
    def test_a_declined_update_reports_the_install_as_outdated(self, monkeypatch, sim_bridge):
        s, calls = self._stub(monkeypatch, outdated=True, answer=False)

        result = s._setup_idb_companion(sim_bridge=sim_bridge)

        assert calls["installed"] == 0
        assert result.status is s.CheckStatus.WARNING
        assert "outdated" in result.message

    @pytest.mark.parametrize("sim_bridge", [True, False])
    def test_a_failed_update_is_reported(self, monkeypatch, sim_bridge):
        s, _ = self._stub(monkeypatch, outdated=True, answer=True, installs=False)

        result = s._setup_idb_companion(sim_bridge=sim_bridge)

        assert result.status is s.CheckStatus.WARNING
        assert "Update failed" in result.message

    def test_sim_bridge_with_a_current_install_asks_nothing(self, monkeypatch):
        s, calls = self._stub(monkeypatch, outdated=False, answer=True)

        result = s._setup_idb_companion(sim_bridge=True)

        assert result.status is s.CheckStatus.SKIPPED
        assert calls == {"prompted": 0, "installed": 0}

    def test_run_setup_reports_the_step_on_both_paths(self):
        """The wiring, which the tests above cannot see: both simulator paths
        hand the step's result to the report."""
        import inspect

        from server.lifecycle import setup as s

        source = inspect.getsource(s.run_setup)
        assert "report.add(_setup_idb_companion(sim_bridge=True))" in source
        assert "report.add(_setup_idb_companion(sim_bridge=False))" in source

    def test_a_missing_companion_is_offered_and_installed(self, monkeypatch):
        """The main path on a fresh machine, and the one `_setup_idb_companion`
        inherited unpinned: `if result.status == MISSING` could be removed
        entirely and every test still passed."""
        s, calls = self._stub(
            monkeypatch, outdated=False, answer=True,
            status=None, message="",
        )
        from server.lifecycle import setup as real
        state = {"missing": True}

        def check():
            if state["missing"]:
                return real.CheckResult(name="idb_companion",
                                        status=real.CheckStatus.MISSING,
                                        message="Not installed (needed for UI automation)")
            return real.CheckResult(name="idb_companion", status=real.CheckStatus.OK,
                                    message="installed (patched, x)")

        def install():
            calls["installed"] += 1
            state["missing"] = False
            return True

        monkeypatch.setattr(s, "check_idb_companion", check)
        monkeypatch.setattr(s, "_install_patched_companion", install)

        result = s._setup_idb_companion(sim_bridge=False)

        assert calls["installed"] == 1
        assert result.status is s.CheckStatus.OK

    def test_a_missing_companion_that_cannot_be_downloaded_is_reported(self, monkeypatch):
        s, _ = self._stub(monkeypatch, outdated=False, answer=True, installs=False,
                          status=None)
        monkeypatch.setattr(
            s, "check_idb_companion",
            lambda: s.CheckResult(name="idb_companion", status=s.CheckStatus.MISSING,
                                  message="Not installed (needed for UI automation)"),
        )

        result = s._setup_idb_companion(sim_bridge=False)

        assert result.status is s.CheckStatus.WARNING
        assert "Download failed" in result.message

    def test_a_system_companion_is_offered_the_patched_build(self, monkeypatch):
        """The other inherited path: a Homebrew companion is offered ours."""
        s, calls = self._stub(
            monkeypatch, outdated=False, answer=True,
            status=None, message="installed (system, /opt/homebrew/bin/idb_companion)",
        )
        from server.lifecycle import setup as real
        state = {"system": True}

        def check():
            if state["system"]:
                return real.CheckResult(
                    name="idb_companion", status=real.CheckStatus.OK,
                    message="installed (system, /opt/homebrew/bin/idb_companion)",
                )
            return real.CheckResult(name="idb_companion", status=real.CheckStatus.OK,
                                    message="installed (patched, x)")

        def install():
            calls["installed"] += 1
            state["system"] = False
            return True

        monkeypatch.setattr(s, "check_idb_companion", check)
        monkeypatch.setattr(s, "_install_patched_companion", install)

        result = s._setup_idb_companion(sim_bridge=False)

        assert calls["installed"] == 1
        assert "patched" in result.message

    def test_a_declined_patched_build_leaves_the_system_one(self, monkeypatch):
        s, calls = self._stub(
            monkeypatch, outdated=False, answer=False,
            status=None, message="installed (system, /opt/homebrew/bin/idb_companion)",
        )
        monkeypatch.setattr(
            s, "check_idb_companion",
            lambda: s.CheckResult(
                name="idb_companion", status=s.CheckStatus.OK,
                message="installed (system, /opt/homebrew/bin/idb_companion)",
            ),
        )

        result = s._setup_idb_companion(sim_bridge=False)

        assert calls["installed"] == 0
        assert "system" in result.message
