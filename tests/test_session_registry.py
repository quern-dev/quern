"""The held-session registry (#254, phase 1): lifecycle, grace, reservations.

An injected clock, so grace is tested by moving time rather than waiting.
"""

from __future__ import annotations

import pytest

from server.session_registry import (
    DEFAULT_GRACE_SECONDS,
    BadToken,
    SessionConflict,
    SessionError,
    SessionRegistry,
    UnknownSession,
    clamp_grace,
)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def reg(clock):
    return SessionRegistry(grace_seconds=180, clock=clock)


def _held(reg, name="claude-a"):
    session, token = reg.open(name)
    reg.attach(session.id, token)
    return session, token


class TestGraceSetting:
    @pytest.mark.parametrize("value,expected", [
        (180, 180), (60, 120), (900, 300), (250.7, 250),
        ("300", DEFAULT_GRACE_SECONDS), (True, DEFAULT_GRACE_SECONDS),
        (None, DEFAULT_GRACE_SECONDS),
    ])
    def test_clamped_to_two_to_five_minutes_and_a_typo_is_the_default(self, value, expected):
        assert clamp_grace(value) == expected


class TestLifecycle:
    def test_an_open_that_is_never_attached_does_not_hold_for_ever(self, reg, clock):
        """Opened in grace: no connection, so nothing keeps it alive."""
        session, _ = reg.open("claude-a")
        reg.reserve(session.id, ["SIM-1"])
        clock.t += 181
        assert reg.holder_of("SIM-1") is None
        assert reg.get("claude-a") is None

    def test_attached_it_is_live_until_the_connection_ends(self, reg, clock):
        session, _ = _held(reg)
        reg.reserve(session.id, ["SIM-1"])
        clock.t += 10_000
        assert reg.holder_of("SIM-1").session.id == "claude-a", "live sessions do not time out"

    def test_a_clean_close_releases_at_once(self, reg):
        session, _ = _held(reg)
        reg.reserve(session.id, ["SIM-1"])
        reg.detach(session.id, session.generation, clean=True)
        assert reg.holder_of("SIM-1") is None
        assert [s.id for s in reg.take_released()] == ["claude-a"]

    def test_an_abrupt_drop_holds_for_the_grace_period_then_releases(self, reg, clock):
        session, _ = _held(reg)
        reg.reserve(session.id, ["SIM-1"])
        reg.detach(session.id, session.generation, clean=False)
        assert reg.get("claude-a").state == "grace"
        clock.t += 179
        assert reg.holder_of("SIM-1") is not None, "still held inside grace"
        clock.t += 2
        assert reg.holder_of("SIM-1") is None, "released when grace ran out"
        assert [s.id for s in reg.take_released()] == ["claude-a"]

    def test_reconnecting_inside_grace_keeps_the_reservations(self, reg, clock):
        session, token = _held(reg)
        reg.reserve(session.id, ["SIM-1"])
        reg.detach(session.id, session.generation, clean=False)
        clock.t += 170
        reg.attach(session.id, token)
        clock.t += 10_000
        assert reg.holder_of("SIM-1").session.state == "live"

    def test_reconnecting_needs_the_right_token(self, reg):
        session, _ = _held(reg)
        reg.detach(session.id, session.generation, clean=False)
        with pytest.raises(BadToken):
            reg.attach(session.id, "not-it")
        with pytest.raises(BadToken):
            reg.attach(session.id, "")

    def test_the_newest_connection_wins_and_an_old_ones_end_changes_nothing(self, reg):
        """A reconnect often arrives before the server notices the old socket
        died. Two sockets on one token used to mean a clean close of either
        released the session under the other (review)."""
        session, token = _held(reg)
        old = session.generation
        _, new = reg.attach(session.id, token)        # the reconnect
        assert new != old
        assert reg.detach(session.id, old, clean=True) is False, "the old socket's close"
        assert reg.get(session.id).state == "live"
        assert reg.detach(session.id, new, clean=False) is True
        assert reg.get(session.id).state == "grace"

    def test_a_stale_socket_cannot_touch_a_newer_session_of_the_same_name(self, reg):
        """A forced release, then the name opened again: the first session's
        socket closing late used to release the new one (review)."""
        first, _ = _held(reg)
        stale = first.generation
        reg.release("claude-a")
        second, _ = _held(reg)
        reg.reserve("claude-a", ["SIM-1"])
        assert reg.detach("claude-a", stale, clean=True) is False
        assert reg.detach("claude-a", stale, clean=False) is False
        assert reg.holder_of("SIM-1").session is second
        assert second.state == "live"

    def test_a_takeover_restarts_the_grace_clock(self, reg, clock):
        """Taken over at 179s of 180, it used to leave one second to connect."""
        session, _ = _held(reg)
        reg.detach(session.id, session.generation, clean=False)
        clock.t += 179
        _, token = reg.open("claude-a", local=True)
        clock.t += 100
        assert reg.get("claude-a") is not None, "still in its fresh grace period"
        reg.attach("claude-a", token)
        assert reg.get("claude-a").state == "live"

    def test_a_live_session_cannot_be_opened_twice(self, reg):
        _held(reg)
        with pytest.raises(SessionConflict):
            reg.open("claude-a")

    def test_locally_the_same_name_takes_over_a_session_in_grace(self, reg, clock):
        """An agent whose MCP restarted keeps its devices (decision 5)."""
        session, old_token = _held(reg)
        reg.reserve(session.id, ["SIM-1"])
        reg.detach(session.id, session.generation, clean=False)
        again, new_token = reg.open("claude-a", local=True)
        assert again is session and new_token != old_token
        reg.attach("claude-a", new_token)
        with pytest.raises(BadToken):
            reg.attach("claude-a", old_token)         # the old token is spent
        assert reg.holder_of("SIM-1").session.id == "claude-a"

    def test_remotely_a_name_does_not_take_over_without_the_token(self, reg):
        session, _ = _held(reg)
        reg.detach(session.id, session.generation, clean=False)
        with pytest.raises(SessionConflict):
            reg.open("claude-a", local=False)

    def test_another_principal_cannot_take_over_even_locally(self, reg):
        session, _ = _held(reg)
        reg.detach(session.id, session.generation, clean=False)
        with pytest.raises(SessionConflict):
            reg.open("claude-a", principal="client-7")

    def test_an_unknown_session_says_to_reserve_again(self, reg):
        with pytest.raises(UnknownSession, match="reserve again"):
            reg.attach("gone", "x")

    def test_only_a_hash_of_the_token_is_kept(self, reg):
        session, token = reg.open("claude-a")
        assert token not in repr(session)


class TestReservations:
    def test_all_or_nothing_and_the_refusal_names_the_holder(self, reg):
        a, _ = _held(reg, "claude-a")
        b, _ = _held(reg, "claude-b")
        reg.reserve(a.id, ["SIM-1"])
        with pytest.raises(SessionConflict, match="claude-a"):
            reg.reserve(b.id, ["SIM-2", "SIM-1"])
        assert reg.holder_of("SIM-2") is None, "nothing was reserved"

    def test_re_reserving_your_own_device_is_a_no_op(self, reg):
        a, _ = _held(reg)
        first = reg.reserve(a.id, ["SIM-1"], on_release="shutdown_booted", booted=["SIM-1"])[0]
        again = reg.reserve(a.id, ["SIM-1"])[0]
        assert again is first and again.booted_here

    def test_unreserve_frees_some_and_queues_their_policies(self, reg):
        a, _ = _held(reg)
        reg.reserve(a.id, ["SIM-1", "SIM-2"], on_release="shutdown_booted", booted=["SIM-2"])
        freed = reg.unreserve(a.id, ["SIM-2"])
        assert [r.udid for r in freed] == ["SIM-2"]
        assert reg.holder_of("SIM-1") is not None and reg.holder_of("SIM-2") is None
        released = reg.take_released()
        assert [list(s.reservations) for s in released] == [["SIM-2"]]
        assert released[0].reservations["SIM-2"].booted_here

    def test_a_bad_policy_or_an_empty_request_is_refused(self, reg):
        a, _ = _held(reg)
        with pytest.raises(SessionError):
            reg.reserve(a.id, ["SIM-1"], on_release="erase")
        with pytest.raises(SessionError):
            reg.reserve(a.id, [])

    def test_held_udids_excludes_the_callers_own(self, reg):
        a, _ = _held(reg, "claude-a")
        b, _ = _held(reg, "claude-b")
        reg.reserve(a.id, ["SIM-1"])
        reg.reserve(b.id, ["SIM-2"])
        assert reg.held_udids(except_session="claude-a") == {"SIM-2"}
        assert reg.held_udids() == {"SIM-1", "SIM-2"}

    def test_a_released_session_frees_its_devices_for_others(self, reg, clock):
        a, _ = _held(reg, "claude-a")
        b, _ = _held(reg, "claude-b")
        reg.reserve(a.id, ["SIM-1"])
        reg.detach(a.id, a.generation, clean=False)
        clock.t += 181
        assert reg.reserve(b.id, ["SIM-1"])[0].udid == "SIM-1"


def test_rekey_moves_a_reservation_to_the_id_its_device_booted_as():
    # #582: an AVD reserved as avd:<name> is emulator-5554 once booted.
    from server.session_registry import SessionConflict, SessionRegistry

    reg = SessionRegistry()
    a, ta = reg.open("a")
    reg.attach(a.id, ta)
    reg.reserve("a", ["avd:Pixel"], booted=["avd:Pixel"], on_release="shutdown_booted")
    reg.rekey("a", "avd:Pixel", "emulator-5554")
    held = reg.holder_of("emulator-5554")
    assert held.session.id == "a" and reg.holder_of("avd:Pixel") is None
    r = a.reservations["emulator-5554"]
    assert (r.udid, r.booted_here, r.on_release) == ("emulator-5554", True, "shutdown_booted")

    b, tb = reg.open("b")
    reg.attach(b.id, tb)
    reg.reserve("b", ["avd:Other"])
    with pytest.raises(SessionConflict):
        reg.rekey("b", "avd:Other", "emulator-5554")
    assert "avd:Other" in b.reservations, "a refused rekey changes nothing"
