"""Moved to `server/device/web/webinspector.py` (#396, phase 3d). A forwarder for code
from releases before the move that runs against this tree during an update: an
old server keeps running until its restart, and imports these names inside
functions.

Exactly those names, pinned in `tests/test_forwarders.py`. Nothing in this
tree imports from here: import from `server.device.web`. Sunset: remove once
four releases have shipped after the one containing this move (#396).
"""

from server.device.web.webinspector import (  # noqa: F401
    SimulatorWebInspector,
    WebInspectorError,
    simulator_udid_for_application,
)
