"""Moved to `server/device/ios/wda.py` (#396, phase 3f). A forwarder for code
from releases before the move that may run against this tree during an
update, between the swap and the restart.

Releases before the move import these names inside functions. Whether one
runs after a swap depends on what that process had already loaded, so all of
them are forwarded.

Exactly those names, pinned in `tests/test_forwarders.py`. Nothing in this
tree imports from here: import from `server.device.ios`. Sunset: remove once
four releases have shipped after the one containing this move (#396).
"""

from server.device.ios.wda import (  # noqa: F401
    build_wda_simulator,
    restore_simulator_mode,
    setup_wda,
    start_driver,
    start_driver_simulator,
    stop_driver,
)
