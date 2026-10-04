"""Moved to `server/device/ios/sim_input.py` (#396, phase 3f). A forwarder for
code from releases before the move that may run against this tree during an
update, between the swap and the restart.

Releases from v0.20.0 import the module itself inside functions (`from
server.device import sim_input`) and then read attributes off it, so this
forwards the module rather than names: importing it puts the real module in
its place, and every attribute -- and every patch -- lands on the one copy.

Pinned in `tests/test_forwarders.py`. Nothing in this tree imports from here:
import from `server.device.ios.sim_input`. Sunset: remove once four releases have
shipped after the one containing this move (#396).
"""

import sys

from server.device.ios import sim_input as _moved

sys.modules[__name__] = _moved
