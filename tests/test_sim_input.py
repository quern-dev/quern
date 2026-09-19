"""Detecting and repairing a simulator whose input services were taken.

Xcode 27's Device Hub attaches a guest HID daemon; the guest answers by
disconnecting the legacy touch, button and keyboard services quern drives, and
every tap and keystroke is then accepted and discarded. See
server/device/sim_input.py for the mechanism and the sources.

These tests pin the parts a live run cannot: what each `notifyutil` answer
means, the order of the repair, and that a boot repairs while an input call
only warns.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from server.device import sim_input
from server.models import DeviceError


def _spawn_returning(*results):
    """Stub `_spawn`, recording the argv it was given."""
    calls: list[tuple[str, ...]] = []
    answers = list(results)

    async def spawn(udid, *argv):
        calls.append(argv)
        return answers.pop(0) if answers else (0, "")

    return spawn, calls


class TestReadingTheState:
    @pytest.mark.parametrize("output,expected", [
        ("com.apple.coredevice.dtuhidd.active 1", True),
        ("com.apple.coredevice.dtuhidd.active 0", False),
    ])
    async def test_the_two_answers_that_mean_something(self, output, expected):
        spawn, _ = _spawn_returning((0, output))
        with patch.object(sim_input, "_spawn", spawn):
            assert await sim_input.legacy_input_is_suppressed("SIM") is expected

    @pytest.mark.parametrize("returncode,output", [
        (1, ""),                                  # not booted, or no such key
        (0, ""),                                  # answered nothing
        (0, "some other key 1"),                  # answered about something else
        (0, "com.apple.coredevice.dtuhidd.active"),   # no value
    ])
    async def test_an_unanswerable_question_is_none_not_false(self, returncode, output):
        """"Could not ask" must not read as "the services are fine".

        False sends a caller down the everything-is-normal path; None says the
        check did not run, which is a different thing and the distinction this
        repo has been bitten by before.
        """
        spawn, _ = _spawn_returning((returncode, output))
        with patch.object(sim_input, "_spawn", spawn):
            assert await sim_input.legacy_input_is_suppressed("SIM") is None

    async def test_a_hung_notifyutil_does_not_hang_the_caller(self):
        """`simctl spawn` against a wedged simulator can never return."""
        async def never_returns(*a, **k):
            import asyncio
            await asyncio.sleep(3600)

        proc = AsyncMock()
        proc.communicate = never_returns
        proc.kill = lambda: None
        proc.wait = AsyncMock(return_value=0)

        with (
            patch.object(sim_input, "_SPAWN_TIMEOUT_S", 0.05),
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
        ):
            assert await sim_input.legacy_input_is_suppressed("SIM") is None


class TestTheRepair:
    async def test_the_state_is_cleared_before_backboardd_restarts(self):
        """The order is the repair.

        Clearing the state alone reconnects nothing; clearing it *after* the
        restart hands the new backboardd the active-to-inactive edge that
        causes the disconnect in the first place.
        """
        spawn, calls = _spawn_returning((0, ""), (0, ""))
        with (
            patch.object(sim_input, "_spawn", spawn),
            patch.object(sim_input, "_BACKBOARDD_RESTART_S", 0),
        ):
            await sim_input.restore_legacy_input("SIM")

        assert calls[0][:2] == ("notifyutil", "-s")
        assert calls[0][2] == sim_input.DTUHID_ACTIVE_KEY
        assert calls[0][3] == "0"
        assert calls[1][:2] == ("launchctl", "kickstart")
        assert "backboardd" in calls[1][-1]

    @pytest.mark.parametrize("failing_step", [0, 1])
    async def test_a_failed_step_is_reported_not_swallowed(self, failing_step):
        results = [(0, ""), (0, "")]
        results[failing_step] = (1, "")
        spawn, _ = _spawn_returning(*results)

        with (
            patch.object(sim_input, "_spawn", spawn),
            patch.object(sim_input, "_BACKBOARDD_RESTART_S", 0),
            pytest.raises(DeviceError, match="cannot be restored"),
        ):
            await sim_input.restore_legacy_input("SIM")

    async def test_backboardd_is_not_restarted_when_the_state_will_not_clear(self):
        """Restarting SpringBoard kills the user's apps. Doing that and *then*
        failing would cost them the session for nothing."""
        spawn, calls = _spawn_returning((1, ""))

        with (
            patch.object(sim_input, "_spawn", spawn),
            patch.object(sim_input, "_BACKBOARDD_RESTART_S", 0),
            pytest.raises(DeviceError),
        ):
            await sim_input.restore_legacy_input("SIM")

        assert len(calls) == 1, "SpringBoard was restarted after the clear failed"


class TestTheWarningText:
    def test_it_names_the_symptom_and_the_way_out(self):
        text = sim_input.suppressed_input_warning("040CC54B-0939-4D98")

        assert "040CC54B" in text
        assert "restore-input" in text
        assert "nothing on screen changes" in text


class TestWhereTheRepairIsAutomaticAndWhereItIsNot:
    """A boot quern performed runs nothing, so the SpringBoard restart is free.
    On a device that was already booted the same repair kills whatever the user
    has open, so there the answer is a warning and an endpoint."""

    def _controller(self):
        from server.device.controller import DeviceController

        controller = DeviceController()
        controller._is_android = lambda udid: False
        controller._is_physical = lambda udid: False
        return controller

    async def test_a_boot_takes_the_services_back(self):
        controller = self._controller()
        controller.simctl.boot = AsyncMock()
        controller._require_simulator = lambda udid, what: None

        with (
            patch.object(sim_input, "device_hub_is_running", AsyncMock(return_value=False)),
            patch.object(sim_input, "legacy_input_is_suppressed", AsyncMock(return_value=True)),
            patch.object(sim_input, "restore_legacy_input", AsyncMock()) as restore,
        ):
            await controller.boot(udid="SIM")

        restore.assert_awaited_once_with("SIM")

    async def test_a_boot_waits_for_device_hub_before_repairing(self):
        """Device Hub attaches a few seconds after the boot returns.

        Measured: repairing the moment boot returned was undone by the
        attachment that had not happened yet -- the state went back to 1 and
        typing was dead. Repairing after it holds.
        """
        controller = self._controller()
        controller.simctl.boot = AsyncMock()
        controller._require_simulator = lambda udid, what: None
        order: list[str] = []

        async def waited(udid, **kwargs):
            order.append("waited")
            return True

        async def repaired(udid):
            order.append("repaired")

        with (
            patch.object(sim_input, "device_hub_is_running", AsyncMock(return_value=True)),
            patch.object(sim_input, "wait_for_device_hub_to_attach", waited),
            patch.object(sim_input, "restore_legacy_input", repaired),
            patch.object(
                sim_input, "legacy_input_is_suppressed",
                AsyncMock(side_effect=AssertionError("waited for nothing")),
            ),
        ):
            await controller.boot(udid="SIM")

        assert order == ["waited", "repaired"]

    async def test_a_boot_without_device_hub_does_not_wait(self):
        """With nothing to attach, waiting would add seconds to every boot."""
        controller = self._controller()
        controller.simctl.boot = AsyncMock()
        controller._require_simulator = lambda udid, what: None

        with (
            patch.object(sim_input, "device_hub_is_running", AsyncMock(return_value=False)),
            patch.object(sim_input, "legacy_input_is_suppressed", AsyncMock(return_value=False)),
            patch.object(
                sim_input, "wait_for_device_hub_to_attach",
                AsyncMock(side_effect=AssertionError("waited with no Device Hub")),
            ),
        ):
            await controller.boot(udid="SIM")

    async def test_a_boot_of_a_healthy_simulator_restarts_nothing(self):
        controller = self._controller()
        controller.simctl.boot = AsyncMock()
        controller._require_simulator = lambda udid, what: None

        with (
            patch.object(sim_input, "device_hub_is_running", AsyncMock(return_value=False)),
            patch.object(sim_input, "legacy_input_is_suppressed", AsyncMock(return_value=False)),
            patch.object(sim_input, "restore_legacy_input", AsyncMock()) as restore,
        ):
            await controller.boot(udid="SIM")

        restore.assert_not_awaited()

    async def test_a_boot_survives_a_repair_that_fails(self):
        """A simulator that cannot take input is worth more than no simulator,
        and the next input call says what is wrong."""
        controller = self._controller()
        controller.simctl.boot = AsyncMock()
        controller._require_simulator = lambda udid, what: None

        with (
            patch.object(sim_input, "device_hub_is_running", AsyncMock(return_value=False)),
            patch.object(sim_input, "legacy_input_is_suppressed", AsyncMock(return_value=True)),
            patch.object(
                sim_input, "restore_legacy_input",
                AsyncMock(side_effect=DeviceError("no", tool="simctl")),
            ),
        ):
            assert await controller.boot(udid="SIM") == "SIM"

    async def test_an_input_call_warns_and_proceeds(self, caplog):
        """Not a refusal: measured, a simulator booted before Device Hub
        started kept working with the state set, so refusing on the state alone
        would refuse input that works."""
        import logging

        controller = self._controller()
        controller.resolve_udid = AsyncMock(return_value="SIM")
        controller._input_checked = {}
        tapped = []
        tap = AsyncMock(side_effect=lambda *a: tapped.append(a))
        backend = type("B", (), {"tap": staticmethod(tap)})()
        controller._ui_backend = lambda udid: backend
        controller._invalidate_ui_cache = lambda udid: None

        with (
            patch.object(sim_input, "legacy_input_is_suppressed", AsyncMock(return_value=True)),
            caplog.at_level(logging.WARNING, logger="quern-debug-server.device"),
        ):
            await controller.tap(1.0, 2.0, udid="SIM")

        assert tapped, "the tap was refused rather than warned about"
        assert any("restore-input" in r.message for r in caplog.records)

    async def test_the_state_is_read_once_per_device(self):
        """The check costs a `simctl spawn` (~0.5s); paying it per tap would
        be felt on every sweep."""
        controller = self._controller()
        controller.resolve_udid = AsyncMock(return_value="SIM")
        controller._input_checked = {}
        controller._ui_backend = lambda udid: type("B", (), {"tap": staticmethod(AsyncMock())})()
        controller._invalidate_ui_cache = lambda udid: None

        probe = AsyncMock(return_value=False)
        with patch.object(sim_input, "legacy_input_is_suppressed", probe):
            for _ in range(3):
                await controller.tap(1.0, 2.0, udid="SIM")

        assert probe.await_count == 1

    async def test_a_physical_device_is_never_asked(self):
        controller = self._controller()
        controller._is_physical = lambda udid: True
        controller.resolve_udid = AsyncMock(return_value="PHONE")
        controller._input_checked = {}
        controller._ui_backend = lambda udid: type("B", (), {"tap": staticmethod(AsyncMock())})()
        controller._invalidate_ui_cache = lambda udid: None

        probe = AsyncMock(return_value=True)
        with patch.object(sim_input, "legacy_input_is_suppressed", probe):
            await controller.tap(1.0, 2.0, udid="PHONE")

        probe.assert_not_awaited()


class TestWaitingForTheAttachment:
    async def test_it_returns_as_soon_as_the_state_flips(self):
        answers = [False, False, True]

        async def state(udid):
            return answers.pop(0)

        with patch.object(sim_input, "legacy_input_is_suppressed", state):
            assert await sim_input.wait_for_device_hub_to_attach(
                "SIM", timeout=5, interval=0,
            ) is True
        assert answers == []

    async def test_it_gives_up_and_reports_what_it_last_saw(self):
        with patch.object(
            sim_input, "legacy_input_is_suppressed", AsyncMock(return_value=False),
        ):
            assert await sim_input.wait_for_device_hub_to_attach(
                "SIM", timeout=0, interval=0,
            ) is False

    async def test_an_unreadable_state_stays_none(self):
        """None is "could not ask", and waiting does not turn it into an answer."""
        with patch.object(
            sim_input, "legacy_input_is_suppressed", AsyncMock(return_value=None),
        ):
            assert await sim_input.wait_for_device_hub_to_attach(
                "SIM", timeout=0, interval=0,
            ) is None


class TestAHalfAppliedRepairStaysVisible:
    """F-1 from the review of #234: the one combination nothing can detect.

    The clear lands, the restart fails, and the services are still
    disconnected -- but the state now says they are not, so every later read
    reports the device healthy and no tap ever warns again. Quieter than
    before the repair was attempted.
    """

    async def test_the_state_goes_back_when_the_restart_fails(self):
        spawn, calls = _spawn_returning((0, ""), (1, "kickstart: no such service"))

        with (
            patch.object(sim_input, "_spawn", spawn),
            patch.object(sim_input, "_BACKBOARDD_RESTART_S", 0),
            pytest.raises(DeviceError, match="kickstart"),
        ):
            await sim_input.restore_legacy_input("SIM")

        assert calls[-1] == ("notifyutil", "-s", sim_input.DTUHID_ACTIVE_KEY, "1"), (
            "the state was left clear while the services were still taken, "
            "which reads as a healthy simulator that ignores every tap"
        )

    async def test_the_failure_names_what_went_wrong(self):
        """simctl puts the reason on stderr -- a shut-down device says "device
        is not booted", which is not "Device Hub holds it"."""
        spawn, _ = _spawn_returning((1, "Unable to spawn: device is not booted"))

        with (
            patch.object(sim_input, "_spawn", spawn),
            patch.object(sim_input, "_BACKBOARDD_RESTART_S", 0),
            pytest.raises(DeviceError, match="not booted"),
        ):
            await sim_input.restore_legacy_input("SIM")


class TestATransientFailureDoesNotDisableTheCheck:
    """F-2: `not None` is True, so one unreadable answer used to record the
    device as healthy for the rest of the session."""

    async def test_an_unreadable_state_is_not_cached(self):
        from server.device.controller import DeviceController

        controller = DeviceController()
        controller._is_android = lambda udid: False
        controller._is_physical = lambda udid: False
        controller.resolve_udid = AsyncMock(return_value="SIM")
        controller._input_checked = {}
        controller._ui_backend = lambda udid: type("B", (), {"tap": staticmethod(AsyncMock())})()
        controller._invalidate_ui_cache = lambda udid: None

        answers = [None, True]

        async def state(udid):
            return answers.pop(0)

        with patch.object(sim_input, "legacy_input_is_suppressed", state):
            await controller.tap(1.0, 2.0, udid="SIM")
            assert controller._input_checked == {}, "a failed read was cached"
            # Rate-limited rather than cached, so the next ask is due only
            # after the cooldown -- cleared here to make it due now.
            controller._input_probe_cooldown.clear()
            await controller.tap(1.0, 2.0, udid="SIM")

        assert controller._input_checked == {"SIM": False}
        assert answers == [], "the second call did not re-read the state"


class TestTheWaitIsBoundedAndReported:
    """F-3: with the daemon crashed at boot it never attaches, and the wait was
    20s of silence followed by a verdict of "healthy"."""

    def test_the_default_wait_is_short(self):
        import inspect

        signature = inspect.signature(sim_input.wait_for_device_hub_to_attach)
        assert signature.parameters["timeout"].default <= 10

    async def test_a_boot_where_nothing_attaches_says_so(self, caplog):
        import logging

        from server.device.controller import DeviceController

        controller = DeviceController()
        controller._is_android = lambda udid: False
        controller._is_physical = lambda udid: False
        controller.simctl.boot = AsyncMock()
        controller._require_simulator = lambda udid, what: None
        controller._input_checked = {}

        with (
            patch.object(sim_input, "device_hub_is_running", AsyncMock(return_value=True)),
            patch.object(sim_input, "wait_for_device_hub_to_attach", AsyncMock(return_value=False)),
            patch.object(sim_input, "restore_legacy_input", AsyncMock()) as restore,
            caplog.at_level(logging.INFO, logger="quern-debug-server.device"),
        ):
            await controller.boot(udid="SIM")

        restore.assert_not_awaited()
        assert any("never claimed the input services" in r.message for r in caplog.records)


class TestEveryInputPathIsCovered:
    """CodeRabbit on #234: four of the seven input paths carried the check.

    `tap_element`, `scroll_to_element` and `clear_text` resolved a device and
    then wrote to it -- taps, sweeps, select-all-and-delete -- without ever
    asking whether the guest would receive any of it. Asserted on the source
    rather than by driving seven methods, because what matters is that none is
    forgotten when an eighth arrives.
    """

    @pytest.mark.parametrize("method", [
        "tap", "tap_element", "swipe", "type_text", "clear_text",
        "press_button", "scroll_to_element",
    ])
    def test_the_method_asks_before_it_writes(self, method):
        import inspect

        from server.device.controller_ui import DeviceControllerUI

        source = inspect.getsource(getattr(DeviceControllerUI, method))
        assert "_warn_if_input_is_suppressed" in source, (
            f"{method} sends input without checking whether it can land"
        )

    def test_tap_element_covers_both_of_its_paths(self):
        """It resolves twice: a fast path for known static coordinates and the
        ordinary one. Checking only the first leaves the common case bare."""
        import inspect

        from server.device.controller_ui import DeviceControllerUI

        source = inspect.getsource(DeviceControllerUI.tap_element)
        assert source.count("_warn_if_input_is_suppressed") >= 2


class TestOnlySettledAnswersAreCached:
    """CodeRabbit on #234: a wait that timed out was recorded as healthy.

    `_warn_if_input_is_suppressed` skips its probe whenever the cache holds
    anything, so a verdict written after a wait that never got an answer
    disables the check for the rest of the session.
    """

    def _controller(self):
        from server.device.controller import DeviceController

        controller = DeviceController()
        controller._is_android = lambda udid: False
        controller._is_physical = lambda udid: False
        controller.simctl.boot = AsyncMock()
        controller._require_simulator = lambda udid, what: None
        controller._input_checked = {}
        return controller

    async def test_a_wait_that_timed_out_is_not_cached(self):
        """Device Hub running and no attachment inside the wait: the daemon
        may attach a moment later, or may have crashed. Either way nothing
        was settled."""
        controller = self._controller()

        with (
            patch.object(sim_input, "device_hub_is_running", AsyncMock(return_value=True)),
            patch.object(sim_input, "wait_for_device_hub_to_attach", AsyncMock(return_value=False)),
        ):
            await controller.boot(udid="SIM")

        assert controller._input_checked == {}

    async def test_an_unreadable_state_is_not_cached(self):
        controller = self._controller()

        with (
            patch.object(sim_input, "device_hub_is_running", AsyncMock(return_value=False)),
            patch.object(sim_input, "legacy_input_is_suppressed", AsyncMock(return_value=None)),
        ):
            await controller.boot(udid="SIM")

        assert controller._input_checked == {}

    async def test_a_healthy_simulator_with_no_device_hub_is_cached(self):
        """The one case that is settled: nothing to attach, nothing claimed."""
        controller = self._controller()

        with (
            patch.object(sim_input, "device_hub_is_running", AsyncMock(return_value=False)),
            patch.object(sim_input, "legacy_input_is_suppressed", AsyncMock(return_value=False)),
        ):
            await controller.boot(udid="SIM")

        assert controller._input_checked == {"SIM": True}

    async def test_a_successful_repair_is_cached(self):
        controller = self._controller()

        with (
            patch.object(sim_input, "device_hub_is_running", AsyncMock(return_value=True)),
            patch.object(sim_input, "wait_for_device_hub_to_attach", AsyncMock(return_value=True)),
            patch.object(sim_input, "restore_legacy_input", AsyncMock()),
        ):
            await controller.boot(udid="SIM")

        assert controller._input_checked == {"SIM": True}


class TestAnAmbiguousTimeoutIsNotReadAsNoChange:
    """CodeRabbit on #234: `notifyutil -s` may write the state and then be
    killed by the timeout. Treating that as "nothing happened" leaves a
    cleared state over disconnected services -- undetectable."""

    async def test_a_timed_out_clear_puts_the_state_back(self):
        spawn, calls = _spawn_returning((sim_input._TIMED_OUT, "timed out after 15s"))

        with (
            patch.object(sim_input, "_spawn", spawn),
            patch.object(sim_input, "_BACKBOARDD_RESTART_S", 0),
            pytest.raises(DeviceError, match="timed out"),
        ):
            await sim_input.restore_legacy_input("SIM")

        assert calls[-1] == ("notifyutil", "-s", sim_input.DTUHID_ACTIVE_KEY, "1")

    async def test_an_ordinary_failure_does_not(self):
        """A command that refused did not write anything, so putting the state
        back would be a second write for no reason."""
        spawn, calls = _spawn_returning((1, "device is not booted"))

        with (
            patch.object(sim_input, "_spawn", spawn),
            patch.object(sim_input, "_BACKBOARDD_RESTART_S", 0),
            pytest.raises(DeviceError, match="not booted"),
        ):
            await sim_input.restore_legacy_input("SIM")

        assert len(calls) == 1


class TestRepairsDoNotInterleave:
    """CodeRabbit on #234: the endpoint and a boot can repair the same
    simulator at once, and their clear/restart/rollback steps interleave. The
    sequence that matters ends with the loser's rollback setting the state
    back to 1 after the winner has reported success."""

    async def test_a_second_repair_waits_for_the_first(self):
        import asyncio

        order: list[str] = []

        async def spawn(udid, *argv):
            order.append(f"{argv[0]}:{argv[-1]}")
            await asyncio.sleep(0.01)       # let the other task run if it can
            return 0, ""

        sim_input._REPAIR_LOCKS.clear()
        with (
            patch.object(sim_input, "_spawn", spawn),
            patch.object(sim_input, "_BACKBOARDD_RESTART_S", 0),
        ):
            await asyncio.gather(
                sim_input.restore_legacy_input("SIM"),
                sim_input.restore_legacy_input("SIM"),
            )

        # Each repair is a clear then a restart; interleaved they would read
        # clear, clear, restart, restart.
        assert order == [
            "notifyutil:0", "launchctl:system/com.apple.backboardd",
            "notifyutil:0", "launchctl:system/com.apple.backboardd",
        ], order

    async def test_two_simulators_are_not_serialised_against_each_other(self):
        import asyncio

        started: list[str] = []

        async def spawn(udid, *argv):
            started.append(udid)
            await asyncio.sleep(0.02)
            return 0, ""

        sim_input._REPAIR_LOCKS.clear()
        with (
            patch.object(sim_input, "_spawn", spawn),
            patch.object(sim_input, "_BACKBOARDD_RESTART_S", 0),
        ):
            await asyncio.gather(
                sim_input.restore_legacy_input("SIM-A"),
                sim_input.restore_legacy_input("SIM-B"),
            )

        assert started[:2] == ["SIM-A", "SIM-B"], (
            f"one simulator's repair waited for another's: {started}"
        )


class TestAnUnreadableStateIsNotProbedOnEveryKeystroke:
    """Found from dev-d6's note about unit tests spawning subprocesses.

    An unreadable state is deliberately not cached, so that a device which
    becomes readable is noticed. Without a cooldown that means one
    `xcrun simctl spawn` per tap, swipe and keystroke, forever, for a device
    that never answers -- measured at ~0.11s each against an unknown udid, and
    in the unit suite it was a real subprocess per call.
    """

    def _controller(self):
        from server.device.controller import DeviceController

        controller = DeviceController()
        controller._is_android = lambda udid: False
        controller._is_physical = lambda udid: False
        controller.resolve_udid = AsyncMock(return_value="SIM")
        controller._ui_backend = lambda udid: type(
            "B", (), {"tap": staticmethod(AsyncMock())},
        )()
        controller._invalidate_ui_cache = lambda udid: None
        return controller

    async def test_an_unreadable_state_is_asked_once_within_the_cooldown(self):
        controller = self._controller()
        probe = AsyncMock(return_value=None)

        with patch.object(sim_input, "legacy_input_is_suppressed", probe):
            for _ in range(5):
                await controller.tap(1.0, 2.0, udid="SIM")

        assert probe.await_count == 1, (
            f"{probe.await_count} probes for five taps; each one is a subprocess"
        )
        assert controller._input_checked == {}, "an unreadable state was cached"

    async def test_it_is_asked_again_once_the_cooldown_passes(self):
        """Not cached, only rate-limited: a device that starts answering is
        noticed rather than written off for the session."""
        import time as time_mod

        controller = self._controller()
        probe = AsyncMock(side_effect=[None, True])

        with patch.object(sim_input, "legacy_input_is_suppressed", probe):
            await controller.tap(1.0, 2.0, udid="SIM")
            controller._input_probe_cooldown["SIM"] = (
                time_mod.monotonic() - controller._INPUT_PROBE_COOLDOWN_S - 1
            )
            await controller.tap(1.0, 2.0, udid="SIM")

        assert probe.await_count == 2
        assert controller._input_checked == {"SIM": False}

    async def test_a_readable_state_is_not_rate_limited_into_silence(self):
        """The cooldown must not swallow the first real answer."""
        controller = self._controller()

        with patch.object(
            sim_input, "legacy_input_is_suppressed", AsyncMock(return_value=True),
        ):
            await controller.tap(1.0, 2.0, udid="SIM")

        assert controller._input_checked == {"SIM": False}
