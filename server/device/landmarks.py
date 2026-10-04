"""Moved to `server/knowledge/landmarks.py` (#396, phase 3c). A forwarder for
code from releases before the move that runs against this tree during an
update: an old server keeps running until its restart, and imports these names
inside functions (`main.py`'s app setup, `api/landmarks.py`'s validation).

Exactly those names, pinned in `tests/test_forwarders.py`. Nothing in this
tree imports from here: import from `server.knowledge`. Sunset: remove once
four releases have shipped after the one containing this move (#396).
"""

from server.knowledge.landmarks import LandmarkRegistry, detect_collisions  # noqa: F401
