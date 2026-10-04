"""Moved to `server/device/ios/simctl.py` (#396, phase 3f). A forwarder for code
from releases before the move that may run against this tree during an
update, between the swap and the restart.

Releases from v0.22.0 import this name inside a function.

Exactly those names, pinned in `tests/test_forwarders.py`. Nothing in this
tree imports from here: import from `server.device.ios.simctl`. Sunset: remove once
four releases have shipped after the one containing this move (#396).
"""

from server.device.ios.simctl import SimctlBackend  # noqa: F401
