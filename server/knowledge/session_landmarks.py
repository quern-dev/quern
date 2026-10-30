"""Knowledge bases loaded for one held session (#560).

Landmark sets are server-wide, keyed by app, which is why loading one was the
owner's: a set loaded by one client changed `identify_screen` for every other
client of that app. A session's own set is loaded from an upload, belongs to
that session alone, and is dropped when the session ends.

`app.state.landmark_registry` is a `SessionLandmarks`, which answers every read
as the *current* session sees the sets -- the server-wide ones, with the
session's own in place of them app by app -- so nothing that reads landmarks
needs to know sessions exist. For an app the session uploaded it sees only its
own screens: merging would let the server's copy, often another checkout or
version, match screens the session's build changed or removed. Writes through
it (`load`, `unload`, ...) are the server-wide registry's, as before; a
session's own set is written through `own()`.
"""

from __future__ import annotations

import types
from typing import Any

from server.knowledge.landmarks import LandmarkRegistry
from server.visibility import current_caller

#: The registry's per-app state: an app's entry in each comes from one place.
_PER_APP = ("_sets", "_web_content", "_conventions", "_edge_issues",
            "_global_declared", "_sources")
#: Writes, which mean the server-wide registry when made through the proxy.
_SHARED_WRITES = frozenset({"load", "load_scan", "load_from_path", "unload"})


class SessionLandmarks:
    """The server-wide landmark registry, as the current session sees it."""

    def __init__(self, shared: LandmarkRegistry) -> None:
        self.shared = shared
        self._own: dict[str, LandmarkRegistry] = {}

    # -- the session's own sets ------------------------------------------------

    def own(self, session: str) -> LandmarkRegistry:
        """`session`'s own registry, made on first use."""
        return self._own.setdefault(session, LandmarkRegistry())

    def own_apps(self, session: str) -> list[str]:
        own = self._own.get(session)
        return sorted(own.list_sets()) if own is not None else []

    def drop(self, session: str) -> list[str]:
        """Forget `session`'s own sets: it ended. Returns the apps dropped."""
        own = self._own.pop(session, None)
        return sorted(own.list_sets()) if own is not None else []

    # -- reading ---------------------------------------------------------------

    def view(self, session: str | None = None) -> LandmarkRegistry:
        """The registry as `session` (default: the current caller's) sees it."""
        if session is None:
            session = current_caller().session
        own = self._own.get(session) if session else None
        if own is None or own.is_empty:
            return self.shared
        composed = LandmarkRegistry()
        mine = set(own._sets)
        for attr in _PER_APP:
            # Everything about an app the session loaded comes from its own
            # set -- an inline load records no web content, and the server's
            # for that app must not show through (review).
            merged = {k: v for k, v in getattr(self.shared, attr).items() if k not in mine}
            merged.update(getattr(own, attr))
            setattr(composed, attr, merged)
        return composed

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        if name in _SHARED_WRITES:
            return getattr(self.shared, name)
        if callable(getattr(LandmarkRegistry, name, None)):
            # Resolved at call time, not now: the controller is handed
            # `scrollable_for` once at start-up, and must see the session that
            # is calling when it is called. Bound to this object, so anything
            # checking what a lookup is bound to finds the registry the API
            # layer uses.
            def late(owner: SessionLandmarks, *args: Any, **kwargs: Any) -> Any:
                return getattr(owner.view(), name)(*args, **kwargs)
            late.__name__ = name
            return types.MethodType(late, self)
        return getattr(self.view(), name)
