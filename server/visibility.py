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
from datetime import datetime

from server.principal import current_principal
from server.session import current_session
from server.session_registry import OWNER, Held, HeldSession

#: "shared" or "blind", from config.json's `sessions.visibility` at start-up
#: (server/config.py `sessions_settings`). Blind applies only to callers whose
#: principal is not the owner.
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
            "yours": session.id == caller.session,
            # Held by any session of the caller's own key: a listing asks under
            # a session of its own, so `yours` never matches it (CodeRabbit).
            "this_key": session.principal == caller.principal}


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


@dataclass(frozen=True)
class View:
    """What data a caller may see: everything, or only data attributable to
    the devices its principal holds, from the moment each was reserved.

    `since` is None for everything. Otherwise it maps each held device to its
    reservation's start, so the next holder of a device does not read the
    previous holder's logs and flows still in the buffers. Data tied to no
    device -- the host's own logs, flows quern cannot attribute -- is visible
    only with everything: "could not tell, so show less"."""

    since: dict[str, datetime] | None = None
    #: A blind client's session-id prefix (`<label>:`). An entry that names a
    #: session is the caller's only if the session is: another client's refused
    #: probe of a device is logged against that device, and must not reach the
    #: device's holder with the prober's session name in it (review).
    session_prefix: str | None = None

    @property
    def everything(self) -> bool:
        return self.since is None

    def allows(self, udid: str | None, when: datetime | None = None) -> bool:
        """Whether a record tied to `udid` (None or "": to no device), made
        at `when`, is visible. A record with no time is judged by device."""
        if self.since is None:
            return True
        if not udid:
            return False
        from server.device_access import _canonical

        start = self.since.get(_canonical(udid))
        if start is None:
            return False
        return when is None or _aware(when) >= start


def _aware(when: datetime) -> datetime:
    from datetime import UTC

    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def data_view(caller: Caller | None = None) -> View:
    """The view of the current caller's request: everything, unless blind."""
    caller = caller or current_caller()
    if not _blind(caller):
        return View()
    from server import device_access

    registry = device_access._REGISTRY
    since: dict[str, datetime] = {}
    if registry is not None:
        for session in registry.sessions():
            if session.principal != caller.principal:
                continue
            for udid, reservation in session.reservations.items():
                start = reservation.since
                if udid not in since or start < since[udid]:
                    since[udid] = start
    from server.principal import label_of

    label = label_of(caller.principal)
    return View(since=since, session_prefix=f"{label}:" if label else "\x00")


def blind_for(caller: Caller | None = None) -> bool:
    """Whether the current caller is held to blind mode."""
    return _blind(caller or current_caller())


def entry_visible(view: View, entry) -> bool:
    """Whether a log entry is visible: by the device it came from -- an action
    entry's resolved `udid`, else the capture source's `device_id` -- and when.
    The host's lines (empty) and the server's own (`device_id` "server", which
    no reservation can name) are tied to no device, so neither is shown."""
    if view.everything:
        return True
    session = getattr(entry, "session", "") or ""
    if session and view.session_prefix is not None and not session.startswith(view.session_prefix):
        return False
    udid = getattr(entry, "udid", "") or getattr(entry, "device_id", "")
    return view.allows(udid, getattr(entry, "timestamp", None))


def flow_filter(view: View):
    """A predicate for flows the view allows, or None when it allows all.

    By the device the flow came from, as recordings attribute it
    (`trace.device_of`): exactly, from the simulator or emulator its client
    process belongs to; for a physical device, by the address its Wi-Fi proxy
    was recorded with -- and only while that record is trusted. A stale or
    unreadable record is "could not tell", so the flow is hidden."""
    if view.everything:
        return None
    from server.trace import device_of

    try:
        from server.api.trace import read_ip_map

        ip_map = read_ip_map()
    except Exception:  # noqa: BLE001 -- unreadable: no physical flow is attributable
        ip_map = {}

    def visible(flow) -> bool:
        udid, firm = device_of(flow, ip_map)
        return firm and view.allows(udid, flow.timestamp)

    return visible


def listed(udid: str | None, caller: Caller | None = None) -> bool:
    """Whether a device appears in a listing: what `device_visible` decides --
    always when shared; when blind, a device nobody holds (one the caller could
    reserve) or one its own principal holds."""
    caller = caller or current_caller()
    if not _blind(caller):
        return True
    from server import device_access

    registry = device_access._REGISTRY
    holder = registry.holder_of(device_access._canonical(udid)) \
        if registry is not None and udid else None
    return device_visible(holder, caller)


class Refreshing:
    """A caller's view, rebuilt at most once a `ttl`, for streams: judging every
    entry against a freshly built view scanned the registry -- and for flows
    read the certificate state from disk -- once per entry per subscriber
    (review). A device reserved or released mid-stream counts within `ttl`."""

    def __init__(self, caller: Caller, ttl: float = 1.0, *, flows: bool = False) -> None:
        import time

        self._caller, self._ttl, self._flows, self._clock = caller, ttl, flows, time.monotonic
        self._at = float("-inf")
        self._view: View = View()
        self._flow = None

    def _refresh(self) -> None:
        now = self._clock()
        if now - self._at < self._ttl:
            return
        self._at = now
        self._view = data_view(self._caller)
        self._flow = flow_filter(self._view) if self._flows else None

    def entry(self, entry) -> bool:
        self._refresh()
        return entry_visible(self._view, entry)

    def flow(self, flow) -> bool:
        self._refresh()
        return self._flow is None or self._flow(flow)
