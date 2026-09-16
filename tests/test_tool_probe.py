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
