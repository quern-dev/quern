"""Held sessions and the devices they reserve (#254, phase 1).

A session is *held* while a client keeps its WebSocket open (`/sessions/{id}/
live`). A clean close releases it at once. A connection that drops -- a closed
lid, a dead Wi-Fi, a killed process -- puts it in **grace** for
`grace_seconds`, during which reconnecting reclaims it with its reservations
intact; when grace runs out it is released. Nothing here survives a server
restart, deliberately: nobody holding a reservation survived it either, and a
claim file outliving its process is how stale claims became permanent the last
time this was built.

A *claimed* session -- a name in `X-Quern-Session` with no socket behind it --
is not in this registry at all. It still gets attribution and its own active
device (#453, #460); it just cannot reserve.

Pure state, no I/O: the API layer turns devices into udids, runs release
policies, and talks HTTP. Expiry is a sweep (`expire_due`) driven by a
background task and by every lookup, so a lookup never trusts a grace period
that has already run out because the sweeper was late.

Every session and reservation has an owning *principal*. In phase 1 it is
always `OWNER` -- the local API key's holder -- and phase 2's per-client keys
become principals. Blind sharing filters by principal, not by session, so one
client's own sessions still see each other (see server/visibility.py).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

#: The principal behind the local API key: the server's owner.
OWNER = "owner"

#: Grace after an abrupt drop: the maintainer's 2-5 minutes, default 3.
DEFAULT_GRACE_SECONDS = 180
GRACE_MIN, GRACE_MAX = 120, 300

#: Release policies a reservation can carry. `shutdown_booted` shuts down
#: only simulators this session booted for the reservation; erasing is
#: deferred to its own design.
ON_RELEASE = ("none", "shutdown_booted")


def clamp_grace(value: object) -> int:
    """`sessions.grace_seconds` from config, clamped; anything that is not a
    plain number is the default, so a typo never means "no grace"."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULT_GRACE_SECONDS
    return int(min(max(value, GRACE_MIN), GRACE_MAX))


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8", "replace")).hexdigest()


@dataclass
class Reservation:
    udid: str
    on_release: str = "none"
    #: True when this session booted the device to reserve it, which is what
    #: `shutdown_booted` acts on: a device that was already running is left so.
    booted_here: bool = False
    since: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class HeldSession:
    id: str
    principal: str
    token_digest: str
    opened_at: datetime
    #: "live" while a connection is held, "grace" after an abrupt drop.
    state: str = "live"
    #: The current connection's generation, from a counter shared by every
    #: session in the registry. A connection that has been replaced, or that
    #: belonged to an earlier session of the same name, carries an older one,
    #: and its end changes nothing (review: a stale socket closing used to
    #: release, or put in grace, the newer session that reused its name).
    generation: int = 0
    #: Monotonic time grace ends, while in grace.
    grace_until: float | None = None
    disconnected_at: datetime | None = None
    last_seen: datetime = field(default_factory=lambda: datetime.now(UTC))
    reservations: dict[str, Reservation] = field(default_factory=dict)

    def summary(self) -> dict:
        return {
            "id": self.id, "state": self.state, "principal": self.principal,
            "opened_at": self.opened_at.isoformat(),
            "last_seen": self.last_seen.isoformat(),
            "disconnected_at": self.disconnected_at.isoformat() if self.disconnected_at else None,
            "reservations": [
                {"udid": r.udid, "on_release": r.on_release, "booted_here": r.booted_here,
                 "since": r.since.isoformat()}
                for r in self.reservations.values()
            ],
        }


class SessionError(Exception):
    """A refusal, with the HTTP status the API should answer with."""

    status = 400


class SessionConflict(SessionError):
    status = 409


class UnknownSession(SessionError):
    status = 404


class BadToken(SessionError):
    status = 403


@dataclass
class Held:
    """A device another session holds: what a refusal is built from."""

    udid: str
    session: HeldSession


class SessionRegistry:
    """Every held session, in memory, and who holds which device."""

    def __init__(self, *, grace_seconds: int = DEFAULT_GRACE_SECONDS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.grace_seconds = clamp_grace(grace_seconds)
        self._clock = clock
        self._sessions: dict[str, HeldSession] = {}
        #: Released sessions waiting for their release policies to be run by
        #: whoever drains them (`take_released`): grace that ran out in a
        #: sweep has nobody waiting on it to act.
        self._released: list[HeldSession] = []
        self._generations = 0

    # -- lifecycle ---------------------------------------------------------

    def open(self, name: str, *, principal: str = OWNER,
             local: bool = True) -> tuple[HeldSession, str]:
        """Open (or, locally, take over) the session `name`; returns it and
        a fresh resume token, which is shown once and kept only as a hash.

        A session that is live -- a connection holds it -- is refused: two
        clients cannot hold one session. One in grace may be taken over by
        the same principal on the owner's machine (`local`), which is how an
        agent whose MCP restarted keeps its devices (#254 decision 5);
        remotely only the resume token reclaims it (`attach`). A takeover
        restarts the grace clock: the new holder needs time to connect, and a
        takeover at 179s of 180 left it one second (review)."""
        self.expire_due()
        token = secrets.token_urlsafe(32)
        existing = self._sessions.get(name)
        if existing is not None:
            if existing.state == "live":
                raise SessionConflict(
                    f"session {name} is held by another connection; close it first, or "
                    f"open a session under another name")
            if not (local and existing.principal == principal):
                raise SessionConflict(
                    f"session {name} is in its grace period; reconnect with its resume token")
            existing.token_digest = _digest(token)
            existing.grace_until = self._clock() + self.grace_seconds
            return existing, token
        session = HeldSession(id=name, principal=principal, token_digest=_digest(token),
                              opened_at=datetime.now(UTC), state="grace",
                              grace_until=self._clock() + self.grace_seconds)
        # Opened in grace, not live: live means a connection is held, and an
        # open that is never followed by one must not hold devices for ever.
        self._sessions[name] = session
        return session, token

    def attach(self, session_id: str, token: str) -> tuple[HeldSession, int]:
        """A connection for the session begins; returns it and its generation.

        One connection holds a session at a time, and the newest wins: a
        reconnect often arrives before the server has noticed the old socket
        died (that takes up to 30s of missed pings), so the caller closes the
        previous connection, whose end then changes nothing. Two sockets on
        one token used to mean a clean close of either released the session
        under the other (review)."""
        self.expire_due()
        session = self._get(session_id)
        if not token or not hmac.compare_digest(_digest(token), session.token_digest):
            raise BadToken(f"wrong resume token for session {session_id}")
        self._generations += 1
        session.generation = self._generations
        session.state = "live"
        session.grace_until = None
        session.disconnected_at = None
        session.last_seen = datetime.now(UTC)
        return session, session.generation

    def detach(self, session_id: str, generation: int, *, clean: bool) -> bool:
        """The connection of `generation` ended. Clean: the holder is done, so
        release now. Abrupt: grace. A connection that is no longer the
        session's current one -- replaced, or from an earlier session of the
        same name -- changes nothing. True if it acted."""
        session = self._sessions.get(session_id)
        if session is None or session.generation != generation:
            return False
        session.last_seen = datetime.now(UTC)
        if clean:
            self.release(session_id)
        else:
            session.state = "grace"
            session.grace_until = self._clock() + self.grace_seconds
            session.disconnected_at = datetime.now(UTC)
        return True

    def release(self, session_id: str) -> HeldSession | None:
        """End a session and free its devices. Its release policies are
        queued for `take_released`."""
        session = self._sessions.pop(session_id, None)
        if session is not None:
            self._released.append(session)
        return session

    def expire_due(self) -> list[HeldSession]:
        """Release every session whose grace has run out."""
        now = self._clock()
        due = [s.id for s in self._sessions.values()
               if s.state == "grace" and s.grace_until is not None and now >= s.grace_until]
        return [s for s in (self.release(i) for i in due) if s is not None]

    def take_released(self) -> list[HeldSession]:
        """Sessions released since the last call, for their policies to run."""
        out, self._released = self._released, []
        return out

    def touch(self, session_id: str) -> None:
        session = self._sessions.get(session_id)
        if session is not None:
            session.last_seen = datetime.now(UTC)

    # -- reservations ------------------------------------------------------

    def reserve(self, session_id: str, udids: Iterable[str], *, on_release: str = "none",
                booted: Iterable[str] = ()) -> list[Reservation]:
        """Reserve every one of `udids`, or none: a device another session
        holds refuses the whole request, naming it. Re-reserving a device the
        session already holds is a no-op that keeps its original record."""
        self.expire_due()
        session = self._get(session_id)
        if on_release not in ON_RELEASE:
            raise SessionError(f"on_release must be one of {', '.join(ON_RELEASE)}")
        udids = list(dict.fromkeys(udids))
        if not udids:
            raise SessionError("nothing to reserve")
        for udid in udids:
            held = self.holder_of(udid)
            if held is not None and held.session.id != session_id:
                raise SessionConflict(f"{udid} is reserved by session {held.session.id}")
        booted = set(booted)
        for udid in udids:
            session.reservations.setdefault(
                udid, Reservation(udid=udid, on_release=on_release, booted_here=udid in booted))
        return [session.reservations[u] for u in udids]

    def unreserve(self, session_id: str, udids: Iterable[str] | None = None) -> list[Reservation]:
        """Free some (or all) of a session's devices; the session stays."""
        session = self._get(session_id)
        targets = list(session.reservations) if udids is None else list(udids)
        freed = [session.reservations.pop(u) for u in targets if u in session.reservations]
        if freed:
            self._released.append(HeldSession(
                id=session.id, principal=session.principal, token_digest="",
                opened_at=session.opened_at, reservations={r.udid: r for r in freed}))
        return freed

    def holder_of(self, udid: str) -> Held | None:
        """The session holding `udid`, after expiring stale grace."""
        self.expire_due()
        for session in self._sessions.values():
            if udid in session.reservations:
                return Held(udid=udid, session=session)
        return None

    def held_udids(self, *, except_session: str = "") -> set[str]:
        """Every device some *other* session holds."""
        self.expire_due()
        return {u for s in self._sessions.values() if s.id != except_session
                for u in s.reservations}

    # -- reading -----------------------------------------------------------

    def get(self, session_id: str) -> HeldSession | None:
        self.expire_due()
        return self._sessions.get(session_id)

    def sessions(self) -> list[HeldSession]:
        self.expire_due()
        return list(self._sessions.values())

    def grace_remaining(self, session: HeldSession) -> float | None:
        if session.grace_until is None:
            return None
        return max(0.0, session.grace_until - self._clock())

    def _get(self, session_id: str) -> HeldSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise UnknownSession(
                f"no held session {session_id}: it ended, its grace ran out, or the server "
                f"restarted -- open a session and reserve again")
        return session
