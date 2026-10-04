"""Moved to `server/knowledge/landmarks.py` (#396, phase 3c). A forwarder for
code from releases before the move that may run against this tree during an
update, between the swap and the restart.

Those releases import these two names inside functions. In practice a running
server loaded this module at startup, so its imports resolve from the copy in
`sys.modules` rather than this file; the forwarder is a precaution, because
two names are cheaper to forward than that is to prove for every release.

Exactly those names, pinned in `tests/test_forwarders.py`. Nothing in this
tree imports from here: import from `server.knowledge`. Sunset: remove once
four releases have shipped after the one containing this move (#396).
"""

from server.knowledge.landmarks import LandmarkRegistry, detect_collisions  # noqa: F401
