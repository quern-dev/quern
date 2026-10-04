"""Moved to `server/device/media/media_engine.py` (#396, phase 3e). A forwarder for code
from releases before the move that runs against this tree during an update: an
old server keeps running until its restart, and imports these names inside
functions.

Exactly those names, pinned in `tests/test_forwarders.py`. Nothing in this
tree imports from here: import from `server.device.media`. Sunset: remove once
four releases have shipped after the one containing this move (#396).
"""

from server.device.media.media_engine import build_media_engine  # noqa: F401
