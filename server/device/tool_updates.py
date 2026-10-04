"""Moved to `server/tooling/tool_updates.py` (#396). This forwarder is for code
from releases before the move that runs against this tree during an update.

`quern update` swaps the source tree before anything restarts. Until the
restart, older code is still running -- the server, and in 0.18.1-0.18.3 the
updater itself, which keeps going in the same process -- and any import it
makes inside a function reads the *new* tree. Without these forwarders those
imports fail mid-update: the 0.18.x updater's tool report after the swap (the
#212 failure, reproduced with release-rehearsal.sh from v0.18.2), or an old
server answering `/tools/sites` or a WDA setup in that window.

The names forwarded are exactly those imported inside functions by any release
from v0.18.0 to v0.24.0-beta.1, found by parsing each tag's `server/`; they are
pinned in `tests/test_forwarders.py`. Nothing in this tree imports from here:
import from `server.tooling.tool_updates`. Sunset: remove once four releases have shipped
after the one containing this move (#396).
"""

from server.tooling.tool_updates import (  # noqa: F401
    actionable,
    format_offer,
    format_report,
    plan_updates,
)
