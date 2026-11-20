"""One accessibility subscription per phone, and a wedged daemon recovered (#734).

Measured on an iPhone 12 (iOS 27.2.1): two subscriptions open at once failed
14% of reads, and with app switches and abandoned reads on top wedged
`axauditd` -- every new connection's DTX handshake timing out -- within
minutes, twice in two runs. It ignores SIGTERM; a SIGKILL brought it back at
once. These pin that a phone's reads never overlap, that a wedge is restarted
and the read answered, and that one that stays down is not asked again for a
while. pymobiledevice3 is faked; nothing here reaches a phone.
"""

from __future__ import annotations

import asyncio
import contextlib
import pathlib
import signal
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.device.ios import coredevice_ax
from server.device.ios.coredevice_ax import PushNotAnswering, TurnNotTaken, read_tree
from server.models import DeviceError
from tests.test_coredevice_ax import (
    PHONE,
    PUSH_SELECTOR,
    SETTINGS,
    _as,
    _phone_controller,
)

#: Before the suite's fixture names every turn file by the id given.
_REAL_LOCK_IDENTITY = coredevice_ax._lock_identity

OTHER_PHONE = "00008101-0000000000000002"
DAEMON_PID = 2039


@pytest.fixture
def phone_address(monkeypatch):
    monkeypatch.setattr("server.device.performance._device_address",
                        AsyncMock(return_value=("fd00::1", 51000)))


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    """Module state is per phone and lives for the server's life: each test
    starts from none, with bounds short enough to run in milliseconds."""
    # A fresh container of the kind production uses, so a test of how it
    # lets go of a loop tests production's choice, not one of its own.
    monkeypatch.setattr(coredevice_ax._Turn, "_locks", type(coredevice_ax._Turn._locks)())
    monkeypatch.setattr(coredevice_ax, "_restarted_at", {})
    monkeypatch.setattr(coredevice_ax, "_down_until", {})
    monkeypatch.setattr(coredevice_ax, "HANDSHAKE_S", 0.1)
    # The settle waits are the reader's, measured on phones; these tests are
    # about subscriptions, not settling.
    monkeypatch.setattr(coredevice_ax, "QUIET_S", 0.02)
    monkeypatch.setattr(coredevice_ax, "UNSETTLED_QUIET_S", 0.02)


def _lib(*, wedged=False, connect_error=None, signal_error=None, daemon_running=True,
         push_delay=0.0, kill_cures=True, wedge_after=None):
    """A pymobiledevice3 stand-in that counts open subscriptions, can hang the
    handshake until `axauditd` is killed, and records the signals sent."""
    state = SimpleNamespace(open=0, max_open=0, opened=0, wedged=wedged, signals=[],
                            connects=0)

    class RSD:
        def __init__(self, address):
            pass

        async def connect(self):
            pass

        async def close(self):
            pass

    class RemoteAX:
        def __init__(self, rsd):
            ctx: dict = {}
            self.dtx = SimpleNamespace(ctx=ctx)
            self._counted = False

            class Control:
                async def invoke(_self, selector, *args, expects_reply=True):
                    if args == (True,):
                        async def deliver():
                            await asyncio.sleep(push_delay)
                            for p in (_as("InitialDump", SETTINGS), SETTINGS):
                                ctx["control_dispatch_queue"].put_nowait((PUSH_SELECTOR, [p]))
                        asyncio.get_running_loop().create_task(deliver())

            self.dtx._ctrl_channel = Control()

        async def connect(self):
            state.connects += 1
            if connect_error:
                raise connect_error
            if wedge_after is not None and state.connects > wedge_after:
                state.wedged = True
            if state.wedged:
                await asyncio.sleep(3600)
            self._counted = True
            state.open += 1
            state.opened += 1
            state.max_open = max(state.max_open, state.open)

        async def close(self):
            if self._counted:
                self._counted = False
                state.open -= 1

    class AppService:
        def __init__(self, rsd):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def list_processes(self):
            processes = [{"processIdentifier": 617, "executableURL": {
                "relative": "file:///Applications/Preferences.app/Preferences"}}]
            if daemon_running:
                processes.append({"processIdentifier": DAEMON_PID, "executableURL": {
                    "relative": "file:///System/Library/PrivateFrameworks/"
                                "AccessibilityAudit.framework/Support/axauditd"}})
            return processes

        async def send_signal_to_process(self, pid, sig):
            if signal_error:
                raise signal_error
            state.signals.append((pid, sig))
            if pid == DAEMON_PID and sig == signal.SIGKILL and kill_cures:
                state.wedged = False      # launchd's fresh daemon answers
            return {"signal": sig}

    return (SimpleNamespace(RemoteServiceDiscoveryService=RSD, RemoteAX=RemoteAX,
                            AppService=AppService), state)


class TestOneSubscriptionAtATime:
    async def test_reads_of_one_phone_never_overlap(self, monkeypatch, phone_address):
        lib, state = _lib(push_delay=0.05)
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)

        results = await asyncio.gather(*(read_tree(PHONE, timeout=5) for _ in range(4)))

        assert all(len(r) == 14 for r in results)
        assert state.opened == 4
        assert state.max_open == 1, "two subscriptions were open on one phone at once"

    async def test_two_phones_are_read_at_once(self, monkeypatch, phone_address):
        """The turn is per phone: one phone's read does not hold up another's."""
        lib, state = _lib(push_delay=0.05)
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)

        await asyncio.gather(read_tree(PHONE, timeout=5), read_tree(OTHER_PHONE, timeout=5))

        assert state.max_open == 2

    async def test_a_launch_check_waits_its_turn_too(self, monkeypatch, phone_address):
        """front_app -- launch_app's confirmation -- subscribes as reads do,
        and was one of the overlapping subscriptions measured."""
        lib, state = _lib(push_delay=0.05)
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)

        await asyncio.gather(read_tree(PHONE, timeout=5),
                             coredevice_ax.front_app(PHONE, timeout=5))

        assert state.max_open == 1

    async def test_the_turn_is_given_back_after_a_failed_read(self, monkeypatch, phone_address):
        """A read that fails must not leave the phone locked to the next."""
        failing, _ = _lib(connect_error=ConnectionRefusedError(61, "refused"))
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: failing)
        with pytest.raises(DeviceError):
            await read_tree(PHONE, timeout=2)

        working, _ = _lib()
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: working)
        assert len(await asyncio.wait_for(read_tree(PHONE, timeout=2), 3)) == 14

    async def test_waiting_for_the_turn_counts_against_the_deadline(
            self, monkeypatch, phone_address):
        """A read queued behind a long one ends at its own deadline, never
        having connected, rather than running past it."""
        lib, state = _lib(push_delay=1.0)
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)

        long_read = asyncio.create_task(read_tree(PHONE, timeout=5))
        await asyncio.sleep(0.05)
        started = time.monotonic()
        with pytest.raises(DeviceError):
            await read_tree(PHONE, timeout=0.2)
        assert time.monotonic() - started < 0.6
        await long_read
        assert state.opened == 1


class _SlowClose:
    """Wraps a `_lib` so closing a provider takes `seconds`: a sick
    connection's close is the slow one."""

    def __init__(self, lib, seconds):
        base = lib.RemoteAX

        class RemoteAX(base):
            async def close(self_inner):
                await asyncio.sleep(seconds)
                await base.close(self_inner)

        lib.RemoteAX = RemoteAX


class TestTheTurnSurvivesACancelledClose:
    """`_close` swallows Exception, not CancelledError: a cancellation in the
    close skipped the release, and every later read of the phone failed
    until the server restarted (review)."""

    async def test_a_deadline_during_front_apps_close(self, monkeypatch, phone_address):
        lib, _ = _lib()
        _SlowClose(lib, 1.0)
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)
        monkeypatch.setattr(coredevice_ax, "CLOSE_S", 2.0)

        with contextlib.suppress(BaseException):
            await asyncio.wait_for(coredevice_ax.front_app(PHONE, timeout=5), 0.4)
        lib2, _ = _lib()
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib2)
        assert len(await asyncio.wait_for(read_tree(PHONE, timeout=2), 3)) == 14

    async def test_front_apps_own_deadline_does_not_cut_its_close(
            self, monkeypatch, phone_address):
        """The close is outside the deadline, as read_tree's is: the deadline
        firing during it left the connection half-closed."""
        lib, state = _lib(push_delay=0.25)
        _SlowClose(lib, 0.3)
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)

        assert await coredevice_ax.front_app(PHONE, timeout=0.5)
        assert state.open == 0, "the close was cut short"

    async def test_a_cancel_during_read_trees_close(self, monkeypatch, phone_address):
        lib, _ = _lib()
        _SlowClose(lib, 1.0)
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)

        with contextlib.suppress(BaseException):
            await asyncio.wait_for(read_tree(PHONE, timeout=5), 0.4)
        lib2, _ = _lib()
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib2)
        assert len(await asyncio.wait_for(read_tree(PHONE, timeout=2), 3)) == 14

    async def test_a_cancel_during_element_ats_close(self, monkeypatch, phone_address):
        lib, _ = _lib()
        _SlowClose(lib, 1.0)
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)
        monkeypatch.setattr(coredevice_ax, "_answer", AsyncMock(return_value=None))

        with contextlib.suppress(BaseException):
            await asyncio.wait_for(coredevice_ax.element_at(PHONE, 1, 1), 0.3)
        lib2, _ = _lib()
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib2)
        assert len(await asyncio.wait_for(read_tree(PHONE, timeout=2), 3)) == 14


class TestTheCloseFinishes:
    async def test_a_cancel_during_the_unsubscribe_still_closes_the_rest(self):
        """The turn is given back after the close: a step skipped by the
        cancel could leave a transport open beside the next subscription
        (CodeRabbit on #735)."""
        closed = []

        class Control:
            async def invoke(self, *_a, **_k):
                await asyncio.sleep(1)

        provider = SimpleNamespace(dtx=SimpleNamespace(_ctrl_channel=Control()),
                                   close=AsyncMock(side_effect=lambda: closed.append("provider")))
        rsd = SimpleNamespace(close=AsyncMock(side_effect=lambda: closed.append("rsd")))

        task = asyncio.create_task(coredevice_ax._close(provider, rsd))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert closed == ["provider", "rsd"]


class TestEverySiteTakesItsTurn:
    async def test_a_hit_test_waits_for_a_read_in_flight(self, monkeypatch, phone_address):
        lib, state = _lib(push_delay=0.1)
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)
        monkeypatch.setattr(coredevice_ax, "_answer", AsyncMock(return_value=None))

        await asyncio.gather(read_tree(PHONE, timeout=5), coredevice_ax.element_at(PHONE, 1, 1))

        assert state.max_open == 1

    async def test_a_hit_test_gives_the_turn_back(self, monkeypatch, phone_address):
        lib, _ = _lib()
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)
        monkeypatch.setattr(coredevice_ax, "_answer", AsyncMock(return_value=None))

        await coredevice_ax.element_at(PHONE, 1, 1)

        assert len(await asyncio.wait_for(read_tree(PHONE, timeout=2), 3)) == 14

    async def test_a_read_queued_behind_a_failed_recovery_does_not_connect(
            self, monkeypatch, phone_address):
        """It passed the down check before waiting its turn; the read ahead of
        it then marked the phone down (CodeRabbit on #735)."""
        lib, state = _lib(wedged=True, signal_error=PermissionError("not permitted"))
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)

        first = asyncio.create_task(read_tree(PHONE, timeout=5))
        await asyncio.sleep(0.01)
        queued = asyncio.create_task(read_tree(PHONE, timeout=5))
        with pytest.raises(PushNotAnswering):
            await first
        connects = state.connects
        started = time.monotonic()
        with pytest.raises(PushNotAnswering, match="not asked again"):
            await queued
        assert time.monotonic() - started < 0.05
        assert state.connects == connects, "the queued read connected to a phone marked down"

    async def test_a_launch_check_on_a_phone_that_is_down_fails_at_once(
            self, monkeypatch, phone_address):
        coredevice_ax._down_until[PHONE] = time.monotonic() + 30
        lib, state = _lib()
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)

        with pytest.raises(PushNotAnswering):
            await coredevice_ax.front_app(PHONE, timeout=5)
        assert state.connects == 0


class TestAWedgedDaemon:
    async def test_it_is_restarted_and_the_read_answers(self, monkeypatch, phone_address):
        lib, state = _lib(wedged=True)
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)

        restarts = coredevice_ax.collect_notes()

        elements = await asyncio.wait_for(read_tree(PHONE, timeout=5), 3)

        assert len(elements) == 14
        assert state.signals == [(DAEMON_PID, signal.SIGKILL)], "only axauditd, only SIGKILL"
        assert len(restarts) == 1 and "restarted" in restarts[0]

    async def test_a_restart_nobody_collects_goes_nowhere(self, monkeypatch, phone_address):
        """Kept per phone, a restart made by a call whose answer never says so
        -- typing's correction -- was claimed by the next answer on that
        phone, minutes later (review). With no list asked for, it is only
        logged."""
        lib, _ = _lib(wedged=True)
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)

        async def a_call_nobody_reports():
            # Its own task, as a request is: the list it would fill is none.
            coredevice_ax._NOTES.set(None)
            await read_tree(PHONE, timeout=5)

        await asyncio.create_task(a_call_nobody_reports())
        later = coredevice_ax.collect_notes()
        await read_tree(PHONE, timeout=5)
        assert later == []

    async def test_pymobiledevice3s_own_handshake_error_counts_as_one(
            self, monkeypatch, phone_address):
        """Its 15 s bound may fire before quern's, as it did in the field."""
        lib, state = _lib(connect_error=ConnectionError("DTX capability handshake timed out"))
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)

        with pytest.raises(PushNotAnswering):
            await read_tree(PHONE, timeout=5)

        assert state.signals == [(DAEMON_PID, signal.SIGKILL)]

    async def test_a_reader_cancelled_mid_handshake_is_not_a_wedge(
            self, monkeypatch, phone_address):
        """pymobiledevice3 says "DTX reader cancelled before handshake
        completed" for a connection that was ended, not a daemon wedged."""
        lib, state = _lib(connect_error=ConnectionResetError(
            "DTX reader cancelled before handshake completed"))
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)

        with pytest.raises(DeviceError) as caught:
            await read_tree(PHONE, timeout=5)

        assert not isinstance(caught.value, PushNotAnswering)
        assert state.signals == []

    async def test_another_connect_error_is_not_taken_for_a_wedge(
            self, monkeypatch, phone_address):
        """A refused connection is the tunnel's, not the daemon's: no kill."""
        lib, state = _lib(connect_error=ConnectionRefusedError(61, "refused"))
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)

        with pytest.raises(DeviceError) as caught:
            await read_tree(PHONE, timeout=5)

        assert not isinstance(caught.value, PushNotAnswering)
        assert state.signals == []

    async def test_one_that_stays_down_is_not_asked_again_for_a_while(
            self, monkeypatch, phone_address):
        """Each call paid ~16 s while the field's phone was wedged."""
        lib, state = _lib(wedged=True, signal_error=PermissionError("not permitted"))
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)

        with pytest.raises(PushNotAnswering):
            await read_tree(PHONE, timeout=5)
        connects = state.connects

        started = time.monotonic()
        with pytest.raises(PushNotAnswering, match="not asked again"):
            await read_tree(PHONE, timeout=5)
        assert time.monotonic() - started < 0.05
        assert state.connects == connects, "the second read connected"

    async def test_it_is_asked_again_once_the_window_is_over(self, monkeypatch, phone_address):
        monkeypatch.setattr(coredevice_ax, "DOWN_S", 0.1)
        lib, _ = _lib(wedged=True, signal_error=PermissionError("not permitted"))
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)
        with pytest.raises(PushNotAnswering):
            await read_tree(PHONE, timeout=5)

        await asyncio.sleep(0.15)
        working, _ = _lib()
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: working)
        assert len(await read_tree(PHONE, timeout=5)) == 14

    async def test_it_is_not_killed_again_within_a_minute(self, monkeypatch, phone_address):
        """A daemon that wedges again straight away is not killed in a loop."""
        lib, state = _lib(wedged=True)
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)
        await read_tree(PHONE, timeout=5)
        state.wedged = True

        with pytest.raises(PushNotAnswering):
            await read_tree(PHONE, timeout=5)

        assert state.signals == [(DAEMON_PID, signal.SIGKILL)], "killed twice"

    async def test_no_daemon_to_restart_is_not_a_recovery(self, monkeypatch, phone_address):
        lib, state = _lib(wedged=True, daemon_running=False)
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)

        with pytest.raises(PushNotAnswering):
            await read_tree(PHONE, timeout=5)

        assert state.signals == []
        assert state.connects == 1, "connected again after a restart that never happened"

    async def test_a_hit_test_on_a_phone_that_is_down_cannot_tell(
            self, monkeypatch, phone_address):
        """A tap's obstruction check does not raise: could not tell is no
        obstruction, as before."""
        coredevice_ax._down_until[PHONE] = time.monotonic() + 30
        lib, state = _lib()
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)

        assert await coredevice_ax.element_at(PHONE, 10, 10) is None
        assert state.connects == 0


class TestWhenThePhoneIsMarkedDown:
    """Only when it cannot be recovered. Marking it down over a spare
    connection skipped every read for 26 s in a soak while the daemon was
    fine (#734)."""

    async def test_a_restart_too_recent_fails_the_read_but_not_the_phone(
            self, monkeypatch, phone_address):
        lib, state = _lib(wedged=True)
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)
        await read_tree(PHONE, timeout=5)              # restarted once
        state.wedged = True

        with pytest.raises(PushNotAnswering, match="not again yet"):
            await read_tree(PHONE, timeout=5)

        state.wedged = False
        connects = state.connects
        assert len(await read_tree(PHONE, timeout=5)) == 14
        assert state.connects > connects, "the next read was skipped"

    async def test_a_fresh_daemon_that_does_not_answer_either_marks_it_down(
            self, monkeypatch, phone_address):
        lib, state = _lib(wedged=True, kill_cures=False)
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)
        restarts = coredevice_ax.collect_notes()

        with pytest.raises(PushNotAnswering, match="could not be restarted"):
            await read_tree(PHONE, timeout=5)

        assert state.signals == [(DAEMON_PID, signal.SIGKILL)]
        assert restarts == [], "a restart that did not bring it back was reported as one"
        with pytest.raises(PushNotAnswering, match="not asked again"):
            await read_tree(PHONE, timeout=5)

    async def test_a_spare_connection_that_cannot_connect_neither_kills_nor_marks(
            self, monkeypatch, phone_address):
        """The spare opens beside a first subscription that is silent (#684);
        killing the daemon would end the first, which may yet answer."""
        monkeypatch.setattr(coredevice_ax, "FIRST_PUSH_S", 0.05)
        # The first subscription answers late; every later connect hangs.
        lib, state = _lib(push_delay=0.3, wedge_after=1)
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)

        elements = await asyncio.wait_for(read_tree(PHONE, timeout=5), 3)

        assert len(elements) == 14, "the first subscription's answer was not waited for"
        assert state.signals == []
        assert PHONE not in coredevice_ax._down_until


class TestTheAnswerSaysSo:
    async def test_a_fallback_to_wda_says_why(self, monkeypatch):
        """It was said only in the log, so the answer read as the push's."""
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl.coredevice_ax.describe_all = AsyncMock(side_effect=PushNotAnswering(
            "the accessibility daemon on 48CF8DD9 did not answer", tool="coredevice"))

        elements, _ = await ctrl.get_ui_elements(PHONE, use_cache=False, whole_screen=True)

        assert [e.label for e in elements] == ["from WDA"]
        note = ctrl.read_note()
        assert note and "did not answer" in note and "WDA's" in note

    @staticmethod
    def _restarting(result=None, error=None):
        """A describe_all that restarts the daemon on its way to `result` or
        `error`, as `_connect_service` does: one sentence on the request's list."""
        async def describe_all(*_a, **_k):
            coredevice_ax._NOTES.get().append("axauditd had stopped answering; restarted")
            if error:
                raise error
            return result
        return describe_all

    async def test_a_restart_is_said_on_the_read_it_rescued(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl.coredevice_ax.describe_all = self._restarting(
            result=await ctrl.coredevice_ax.describe_all(PHONE))

        await ctrl.get_ui_elements(PHONE, use_cache=False, whole_screen=True)

        assert "restarted" in (ctrl.read_note() or "")

    async def test_a_restart_is_said_when_the_read_then_falls_back(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl.coredevice_ax.describe_all = self._restarting(
            error=PushNotAnswering("not answering", tool="coredevice"))

        await ctrl.get_ui_elements(PHONE, use_cache=False, whole_screen=True)

        note = ctrl.read_note() or ""
        assert "restarted" in note and "WDA's" in note

    async def test_a_restart_is_said_on_a_bare_tree_fallback_too(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl.coredevice_ax.describe_all = self._restarting(
            error=coredevice_ax.BareAppTree(PHONE))

        await ctrl.get_ui_elements(PHONE, use_cache=False, whole_screen=True)

        note = ctrl.read_note() or ""
        assert "restarted" in note and "empty tree" in note

    async def test_a_restart_is_added_to_a_note_already_there(self, monkeypatch):
        """A single note value: the restart overwrote the reader-overruled note."""
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl.coredevice_ax.describe_all = self._restarting(
            result=await ctrl.coredevice_ax.describe_all(PHONE))
        monkeypatch.setattr(ctrl, "_wda_overruled", lambda: "no Xcode")

        await ctrl.get_ui_elements(PHONE, use_cache=False, whole_screen=True)

        note = ctrl.read_note() or ""
        assert "no Xcode" in note and "restarted" in note

    async def test_a_restart_is_said_through_a_crash_read_again_with_no_wda(self, monkeypatch):
        """With no WDA, a read that crashed an app is read again through the
        push: the restart was lost there (CodeRabbit on #735)."""
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        elements = await ctrl.coredevice_ax.describe_all(PHONE)
        calls = []

        async def describe_all(*_a, **_k):
            calls.append(1)
            if len(calls) == 1:
                coredevice_ax._NOTES.get().append("axauditd had stopped answering; restarted")
                raise coredevice_ax.AppExitedDuringRead("Demo", 711)
            return elements

        ctrl.coredevice_ax.describe_all = describe_all
        monkeypatch.setattr(ctrl, "_no_wda_fallback", AsyncMock(return_value="no WDA built"))

        await ctrl.get_ui_elements(PHONE, use_cache=False, whole_screen=True)

        note = ctrl.read_note() or ""
        assert "restarted" in note and "exited" in note

    async def test_a_read_that_never_got_its_turn_does_not_fall_back(self, monkeypatch):
        """The push was busy, not broken: read through WDA instead, it started
        WDA beside it for every short read queued behind a long one (review
        on #736)."""
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl.coredevice_ax.describe_all = self._restarting(error=TurnNotTaken(
            "waited 1s for another accessibility read of 48CF8DD9 to finish; "
            "the phone was not asked", tool="coredevice"))

        with pytest.raises(TurnNotTaken, match="the phone was not asked") as caught:
            await ctrl.get_ui_elements(PHONE, use_cache=False, whole_screen=True)

        assert "restarted" in str(caught.value)

    async def test_a_restart_is_said_when_there_is_no_wda_to_fall_back_on(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl.coredevice_ax.describe_all = self._restarting(
            error=PushNotAnswering("not answering", tool="coredevice"))
        monkeypatch.setattr(ctrl, "_no_wda_fallback", AsyncMock(return_value="no WDA built"))

        with pytest.raises(DeviceError, match="restarted"):
            await ctrl.get_ui_elements(PHONE, use_cache=False, whole_screen=True)


# -- review on #734, after #735 ------------------------------------------------


def _hold_turn_elsewhere(lock_dir, uuid, seconds):
    """Another quern server, as far as the lock can tell: a separate process
    holding the phone's turn file for `seconds`. Returns once it holds it."""
    import subprocess
    import sys

    name = coredevice_ax.re.sub(r"[^A-Za-z0-9-]", "_", uuid)
    path = f"{lock_dir}/coredevice-ax-{name}.lock"
    code = (
        "import fcntl, os, sys, time\n"
        f"os.makedirs({str(lock_dir)!r}, exist_ok=True)\n"
        f"fd = os.open({path!r}, os.O_RDWR | os.O_CREAT, 0o600)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "print('held', flush=True)\n"
        f"time.sleep({seconds})\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    _ready(proc)
    return proc


def _ready(proc, seconds=15.0):
    """Wait, bounded, for `proc` to say it holds the turn; on a timeout or
    anything else it is killed, reaped and its pipe closed (CodeRabbit on #736)."""
    import select

    ready, _, _ = select.select([proc.stdout], [], [], seconds)
    line = proc.stdout.readline().strip() if ready else ""
    if line != "held":
        proc.kill()
        proc.wait(5)
        proc.stdout.close()
        pytest.fail(f"the other server never took the turn ({line!r})")


async def _never(*a, **k):
    await asyncio.sleep(60)


class TestATimeoutSaysWhoseItWas:
    """The turn is taken inside the deadline: a read that never got it was
    blamed on the phone, which sent an agent looking at the app (review)."""

    async def test_a_read_that_never_got_its_turn_says_the_phone_was_not_asked(
            self, monkeypatch, phone_address):
        lib, _ = _lib(push_delay=1.0)
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)

        long_read = asyncio.create_task(read_tree(PHONE, timeout=5))
        await asyncio.sleep(0.05)
        with pytest.raises(DeviceError) as caught:
            await read_tree(PHONE, timeout=0.2)
        await long_read

        assert "the phone was not asked" in str(caught.value)
        assert "sent no complete" not in str(caught.value)
        assert isinstance(caught.value, TurnNotTaken)

    async def test_a_read_that_had_its_turn_at_once_says_nothing_of_waiting(
            self, monkeypatch, phone_address):
        lib, _ = _lib()
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)
        monkeypatch.setattr(coredevice_ax, "_newest_tree", _never)

        with pytest.raises(DeviceError) as caught:
            await read_tree(PHONE, timeout=0.6)

        assert "sent no complete" in str(caught.value)
        assert "waiting" not in str(caught.value)

    async def test_a_late_turn_says_how_much_of_the_deadline_went_waiting(
            self, monkeypatch, phone_address):
        lib, _ = _lib(push_delay=0.7)
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)
        # The first read settles; the second's pushes never complete, so its
        # deadline ends it after it has waited 0.7 s for its turn.
        real, calls = coredevice_ax._newest_tree, []

        async def newest(*a, **k):
            calls.append(1)
            if len(calls) == 1:
                return await real(*a, **k)
            await asyncio.sleep(60)

        monkeypatch.setattr(coredevice_ax, "_newest_tree", newest)
        long_read = asyncio.create_task(read_tree(PHONE, timeout=5))
        await asyncio.sleep(0.05)

        with pytest.raises(DeviceError) as caught:
            await read_tree(PHONE, timeout=1.2)
        await long_read

        assert "sent no complete" in str(caught.value)
        assert "waiting for another read of this phone" in str(caught.value)
        assert not isinstance(caught.value, TurnNotTaken), "its turn came: the phone was asked"

    async def test_a_value_never_set_says_nothing_was_set(self, monkeypatch, phone_address):
        lib, _ = _lib(push_delay=1.0)
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)

        long_read = asyncio.create_task(read_tree(PHONE, timeout=5))
        await asyncio.sleep(0.05)
        with pytest.raises(DeviceError) as caught:
            await coredevice_ax.set_value(PHONE, lambda e: True, "x", timeout=0.2)
        await long_read

        assert "nothing was set" in str(caught.value)
        assert isinstance(caught.value, TurnNotTaken)
        assert "may or may not" not in str(caught.value)

    async def test_a_launch_check_that_never_got_its_turn_says_so(
            self, monkeypatch, phone_address):
        lib, _ = _lib(push_delay=1.0)
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)

        long_read = asyncio.create_task(read_tree(PHONE, timeout=5))
        await asyncio.sleep(0.05)
        with pytest.raises(DeviceError, match="the phone was not asked"):
            await coredevice_ax.front_app(PHONE, timeout=0.2)
        await long_read


class TestTheTurnAcrossServers:
    """`_Turn`'s lock was this server's alone: a scratch or branch server
    reading the same phone overlapped it, which is how live tests run."""

    async def test_a_read_waits_for_another_servers_turn(self, monkeypatch, phone_address):
        import os
        lib, _ = _lib()
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)
        other = _hold_turn_elsewhere(os.environ["QUERN_AX_LOCK_DIR"], PHONE, 0.6)
        try:
            started = time.monotonic()
            assert len(await read_tree(PHONE, timeout=5)) == 14
            assert time.monotonic() - started >= 0.4, "it did not wait for the other server"
        finally:
            other.wait(5)

    async def test_another_servers_turn_held_past_the_deadline_is_not_the_phones_fault(
            self, monkeypatch, phone_address):
        import os
        lib, state = _lib()
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)
        other = _hold_turn_elsewhere(os.environ["QUERN_AX_LOCK_DIR"], PHONE, 1.0)
        try:
            with pytest.raises(DeviceError, match="the phone was not asked"):
                await read_tree(PHONE, timeout=0.3)
            assert state.connects == 0
        finally:
            other.wait(5)

    async def test_a_read_gives_the_other_servers_turn_back(self, monkeypatch, phone_address):
        import fcntl
        import os
        lib, _ = _lib()
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)
        await read_tree(PHONE, timeout=5)

        path = coredevice_ax._turn_dir() / f"coredevice-ax-{PHONE}.lock"
        fd = os.open(path, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)   # raises if still held
        finally:
            os.close(fd)

    async def test_a_lock_it_cannot_take_does_not_stop_the_read(
            self, monkeypatch, phone_address, tmp_path):
        """Logged, and the read goes on with this server's lock alone."""
        blocker = tmp_path / "a-file"
        blocker.write_text("not a directory")
        monkeypatch.setenv("QUERN_AX_LOCK_DIR", str(blocker / "turns"))
        lib, _ = _lib()
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)

        assert len(await asyncio.wait_for(read_tree(PHONE, timeout=5), 3)) == 14

    async def test_a_turn_it_gave_up_waiting_for_is_not_kept(self, monkeypatch, phone_address):
        """A read that ran out waiting for another server must let go of this
        server's own lock too, or every read after it waits forever."""
        import os
        lib, _ = _lib()
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)
        other = _hold_turn_elsewhere(os.environ["QUERN_AX_LOCK_DIR"], PHONE, 0.5)
        try:
            with pytest.raises(DeviceError, match="the phone was not asked"):
                await read_tree(PHONE, timeout=0.2)
        finally:
            other.wait(5)
        assert len(await asyncio.wait_for(read_tree(PHONE, timeout=5), 3)) == 14

    async def test_a_lock_directory_not_made_yet_is_made(
            self, monkeypatch, phone_address, tmp_path):
        turns = tmp_path / "not" / "yet"
        monkeypatch.setenv("QUERN_AX_LOCK_DIR", str(turns))
        lib, _ = _lib()
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)
        notes = coredevice_ax.collect_notes()

        await read_tree(PHONE, timeout=5)

        assert (turns / f"coredevice-ax-{PHONE}.lock").is_file()
        assert (turns.stat().st_mode & 0o777) == 0o700
        assert notes == []

    async def test_no_descriptor_is_left_open(self, monkeypatch, phone_address):
        import os
        lib, _ = _lib(push_delay=0.3)
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)
        await read_tree(PHONE, timeout=5)                   # whatever opens once
        before = len(os.listdir("/dev/fd"))

        await asyncio.gather(*(read_tree(PHONE, timeout=5) for _ in range(3)))
        other = _hold_turn_elsewhere(os.environ["QUERN_AX_LOCK_DIR"], PHONE, 0.5)
        try:
            with pytest.raises(DeviceError):
                await read_tree(PHONE, timeout=0.2)         # gave up waiting
        finally:
            other.wait(5)
            other.stdout.close()

        assert len(os.listdir("/dev/fd")) == before

    async def test_a_lock_that_fails_otherwise_is_said_and_skipped(
            self, monkeypatch, phone_address):
        import errno
        import fcntl
        lib, _ = _lib()
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)

        def broken(fd, op):
            raise OSError(errno.ENOLCK, "No locks available")

        monkeypatch.setattr(fcntl, "flock", broken)
        notes = coredevice_ax.collect_notes()

        assert len(await asyncio.wait_for(read_tree(PHONE, timeout=5), 3)) == 14
        assert len(notes) == 1 and "another quern server may have read it" in notes[0]

    async def test_a_symlink_at_the_lock_is_not_followed(
            self, monkeypatch, phone_address, tmp_path):
        """/tmp is shared: a link another user left at the lock's name would
        have had this server create and lock a file of their choosing."""
        target = tmp_path / "theirs"
        turns = tmp_path / "turns"
        turns.mkdir(mode=0o700)
        monkeypatch.setenv("QUERN_AX_LOCK_DIR", str(turns))
        (turns / f"coredevice-ax-{PHONE}.lock").symlink_to(target)
        lib, _ = _lib()
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)
        notes = coredevice_ax.collect_notes()

        assert len(await read_tree(PHONE, timeout=5)) == 14
        assert not target.exists()
        assert len(notes) == 1

    async def test_a_shared_lock_directory_is_made_private(
            self, monkeypatch, phone_address, tmp_path):
        turns = tmp_path / "turns"
        turns.mkdir()
        turns.chmod(0o777)
        monkeypatch.setenv("QUERN_AX_LOCK_DIR", str(turns))
        lib, _ = _lib()
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)

        await read_tree(PHONE, timeout=5)

        assert (turns.stat().st_mode & 0o777) == 0o700

    async def test_a_server_reading_back_to_back_gives_another_a_turn(
            self, monkeypatch, phone_address):
        """Measured before the fix: a server handing the turn from read to
        read took it back before the other's next poll, 0 tries in 3."""
        import os
        import subprocess
        import sys
        lib, _ = _lib()
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)
        code = (
            "import asyncio, time\n"
            "import server.device.ios.coredevice_ax as ax\n"
            "from server.device.ios.coredevice_ax import _Turn\n"
            # A wide give-way, ten of this side's polls, so a loaded runner
            # still lands one in it; with none, the gap is microseconds.
            "ax.TURN_POLL_S = 0.25\n"
            # Named by the id given, as this side's are: resolving asks
            # tunneld, which is the real machine, and its time between turns
            # let this side in with no give-way at all.
            "async def as_given(uuid):\n"
            "    return uuid\n"
            "ax._lock_identity = as_given\n"
            "async def main():\n"
            "    end = time.monotonic() + 5\n"
            "    first = True\n"
            "    while time.monotonic() < end:\n"
            f"        turn = _Turn({PHONE!r})\n"
            "        await turn.take()\n"
            "        if first:\n"
            "            print('held', flush=True); first = False\n"
            "        await asyncio.sleep(0.15)\n"
            "        turn.give_back()\n"
            "asyncio.run(main())\n"
        )
        root = str(pathlib.Path(__file__).resolve().parents[1])
        other = subprocess.Popen(
            [sys.executable, "-c", code], stdout=subprocess.PIPE, text=True,
            env={**os.environ, "PYTHONPATH": root}, cwd=root)
        _ready(other)
        try:
            assert len(await read_tree(PHONE, timeout=1)) == 14
        finally:
            other.kill()
            other.wait(5)
            other.stdout.close()


    async def test_both_ids_of_a_phone_take_one_turn(self, monkeypatch, phone_address):
        """One server given the hardware UDID, another the CoreDevice UUID,
        took two files and overlapped (CodeRabbit on #736)."""
        import os

        from server.device.ios import tunneld
        monkeypatch.setattr(coredevice_ax, "_lock_identity", _REAL_LOCK_IDENTITY)
        hardware = "00008101-000A1B2C3D4E5F60"

        async def resolve(uuid):
            return hardware

        monkeypatch.setattr(tunneld, "resolve_tunnel_udid", resolve)
        lib, state = _lib()
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)
        other = _hold_turn_elsewhere(os.environ["QUERN_AX_LOCK_DIR"], hardware, 1.0)
        try:
            with pytest.raises(TurnNotTaken):
                await read_tree(PHONE, timeout=0.3)
            assert state.connects == 0
        finally:
            other.wait(5)
            other.stdout.close()

    async def test_an_id_it_cannot_resolve_is_the_id_given(self, monkeypatch, phone_address):
        from server.device.ios import tunneld

        async def broken(uuid):
            raise OSError("tunneld is not answering")

        monkeypatch.setattr(tunneld, "resolve_tunnel_udid", broken)
        assert await _REAL_LOCK_IDENTITY(PHONE) == PHONE

        async def unknown(uuid):
            return None

        monkeypatch.setattr(tunneld, "resolve_tunnel_udid", unknown)
        assert await _REAL_LOCK_IDENTITY(PHONE) == PHONE

def test_a_finished_loops_locks_are_let_go():
    """Keyed by `id()`, a loop that had gone could hand its locks to a new
    loop given its id (review)."""
    import gc
    import threading

    locks = coredevice_ax._Turn._locks       # the fixture's: production's kind
    seen, failed = [], []

    def run():
        async def make():
            # Contended: a lock that has been waited on holds its loop
            # (CodeRabbit on #736).
            first, second = coredevice_ax._Turn(PHONE), coredevice_ax._Turn(PHONE)
            await asyncio.wait_for(first.take(), 5)
            waiting = asyncio.create_task(second.take())
            await asyncio.sleep(0.05)
            first.give_back()
            await asyncio.wait_for(waiting, 5)   # bounded: a turn never given back
            second.give_back()
            seen.append(len(locks))
        try:
            asyncio.run(make())
        except BaseException as e:  # noqa: BLE001 -- said by the test, not lost in the thread
            failed.append(e)

    # A daemon thread, so one stuck past the join cannot hold the run open.
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(10)
    assert not t.is_alive(), "the loop's turns did not finish"
    if failed:
        raise failed[0]
    gc.collect()
    assert seen == [1]
    assert len(locks) == 0, "a finished loop's locks were kept"
