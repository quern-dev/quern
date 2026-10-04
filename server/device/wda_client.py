"""Moved to `server/device/ios/wda_client.py` (#396, phase 3f). A forwarder for code
from releases before the move that may run against this tree during an
update, between the swap and the restart.

Releases before the move import these names inside functions;
ACTION_SNAPSHOT_DEPTH only in v0.24.0-beta.1.

Exactly those names, pinned in `tests/test_forwarders.py`. Nothing in this
tree imports from here: import from `server.device.ios.wda_client`. Sunset: remove once
four releases have shipped after the one containing this move (#396).
"""

from server.device.ios.wda_client import (  # noqa: F401
    ACTION_SNAPSHOT_DEPTH,
    ACTION_TIMEOUT,
    WdaBackend,
)
