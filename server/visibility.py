"""Who may see what: one policy for every listing and refusal (#254).

Phase 1 has one mode, **shared**: every caller sees every device, session and
record, and a refusal names who holds the device -- today's behaviour. The
policy exists now so that **blind sharing** (#254, decided 2026-10-17) can be
switched on in phase 2 without touching the call sites: on a shared company
or commercial server, a remote caller then sees only its own principal's
devices and the data attributable to them, and a device held by someone else
reads as one that does not exist.

So every place that lists devices or sessions, or refuses access to a device,
asks this module rather than deciding for itself. The owner -- the local API
key's holder -- always sees everything.
"""

from __future__ import annotations

from dataclasses import dataclass

from server.principal import current_principal
from server.session import current_session
from server.session_registry import OWNER, Held, HeldSession

#: "shared" today. Phase 2 adds "blind", read from config, applying to
#: callers whose principal is not the owner.
MODE = "shared"


@dataclass(frozen=True)
class Caller:
    session: str
    principal: str

    @property
    def is_owner(self) -> bool:
        return self.principal == OWNER


def current_caller() -> Caller:
    """The caller of the request being handled: its session, and the principal
    its key makes it (server/principal.py). The owner outside a request."""
    return Caller(session=current_session(), principal=current_principal())


def _blind(caller: Caller) -> bool:
    return MODE == "blind" and not caller.is_owner


def holder_detail(held: Held, caller: Caller) -> dict | None:
    """What a caller may learn about who holds a device: the holder in shared
    mode, nothing in blind mode (where the device reads as nonexistent)."""
    if _blind(caller) and held.session.principal != caller.principal:
        return None
    session = held.session
    return {"held_by": session.id, "state": session.state,
            "since": session.reservations[held.udid].since.isoformat(),
            "yours": session.id == caller.session}


def visible_sessions(sessions: list[HeldSession], caller: Caller) -> list[HeldSession]:
    if not _blind(caller):
        return sessions
    return [s for s in sessions if s.principal == caller.principal]


def device_visible(holder: Held | None, caller: Caller) -> bool:
    """Whether a device appears to this caller at all: always in shared mode;
    in blind mode only if free or held by the caller's own principal."""
    if not _blind(caller) or holder is None:
        return True
    return holder.session.principal == caller.principal


def sees_unattributed(caller: Caller) -> bool:
    """Whether data tied to no device -- the host's own logs, flows quern
    cannot attribute -- is visible: always in shared mode, never to a blind
    caller, by the same "could not tell, so show less" rule as local capture."""
    return not _blind(caller)
