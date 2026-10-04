"""Moved to `server/device/web/web_content.py` (#396, phase 3d). A forwarder for code
from releases before the move that may run against this tree during an
update, between the swap and the restart.

Releases before the move import these names only inside functions, so an old
server that has not read web content since it started loads this module for
the first time from the new tree -- here.

Exactly those names, pinned in `tests/test_forwarders.py`. Nothing in this
tree imports from here: import from `server.device.web.web_content`. Sunset: remove once
four releases have shipped after the one containing this move (#396).
"""

from server.device.web.web_content import (  # noqa: F401
    _is_app,
    _texts_correspond,
    collect_web_content,
    from_probe,
    normalise,
)
