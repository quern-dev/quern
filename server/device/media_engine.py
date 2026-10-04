"""Moved to `server/device/media/media_engine.py` (#396, phase 3e). A forwarder for code
from releases before the move that may run against this tree during an
update, between the swap and the restart.

v0.24.0-beta.1 imports this name inside a function, but one that runs at
server startup, before any swap. So this is a precaution, cheaper than
proving that for every release.

Exactly those names, pinned in `tests/test_forwarders.py`. Nothing in this
tree imports from here: import from `server.device.media`. Sunset: remove once
four releases have shipped after the one containing this move (#396).
"""

from server.device.media.media_engine import build_media_engine  # noqa: F401
