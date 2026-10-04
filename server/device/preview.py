"""Moved to `server/device/media/preview.py` (#396, phase 3e). A forwarder for code
from releases before the move that may run against this tree during an
update, between the swap and the restart.

Releases before the move import these names inside functions: PreviewManager
in server startup, and build_preview_bundle in setup. Neither is reached
after a swap -- startup ran before it, and a rehearsal from v0.18.2 with no
forwarder here passed, its post-swap setup being the new tree's. So this is
a precaution, cheaper than proving that for every release.

Exactly those names, pinned in `tests/test_forwarders.py`. Nothing in this
tree imports from here: import from `server.device.media`. Sunset: remove once
four releases have shipped after the one containing this move (#396).
"""

from server.device.media.preview import PreviewManager, build_preview_bundle  # noqa: F401
