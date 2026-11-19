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
import signal
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.device.ios import coredevice_ax
from server.device.ios.coredevice_ax import PushNotAnswering, read_tree
from server.models import DeviceError
from tests.test_coredevice_ax import (
    PHONE,
    PUSH_SELECTOR,
    SETTINGS,
    _as,
    _phone_controller,
)

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
    monkeypatch.setattr(coredevice_ax._Turn, "_locks", {})
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

        restarts = coredevice_ax.collect_restarts()

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
            coredevice_ax._RESTARTS.set(None)
            await read_tree(PHONE, timeout=5)

        await asyncio.create_task(a_call_nobody_reports())
        later = coredevice_ax.collect_restarts()
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
        restarts = coredevice_ax.collect_restarts()

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
            coredevice_ax._RESTARTS.get().append("axauditd had stopped answering; restarted")
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

    async def test_a_restart_is_said_when_there_is_no_wda_to_fall_back_on(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl.coredevice_ax.describe_all = self._restarting(
            error=PushNotAnswering("not answering", tool="coredevice"))
        monkeypatch.setattr(ctrl, "_no_wda_fallback", AsyncMock(return_value="no WDA built"))

        with pytest.raises(DeviceError, match="restarted"):
            await ctrl.get_ui_elements(PHONE, use_cache=False, whole_screen=True)
