"""Moved to `server/device/web/web_probing.py` (#396, phase 3d). A forwarder for code
from releases before the move that may run against this tree during an
update, between the swap and the restart.

Releases before the move import these names inside functions, but also import
this module at startup (`controller_ui`), so a running server resolves them
from `sys.modules`. The forwarder is a precaution, cheaper than proving that
for every release.

Exactly those names, pinned in `tests/test_forwarders.py`. Nothing in this
tree imports from here: import from `server.device.web.web_probing`. Sunset: remove once
four releases have shipped after the one containing this move (#396).
"""

from server.device.web.web_probing import app_frame, hit_contains, sweep_web_content  # noqa: F401
