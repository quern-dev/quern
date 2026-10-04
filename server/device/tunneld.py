"""Moved to `server/device/ios/tunneld.py` (#396, phase 3f). A forwarder for code
from releases before the move that may run against this tree during an
update, between the swap and the restart.

Releases before the move import these names inside functions. Whether one
runs after a swap depends on what that process had already loaded, so all of
them are forwarded.

Exactly those names, pinned in `tests/test_forwarders.py`. Nothing in this
tree imports from here: import from `server.device.ios`. Sunset: remove once
four releases have shipped after the one containing this move (#396).
"""

from server.device.ios.tunneld import (  # noqa: F401
    PLIST_PATH,
    TUNNELD_URL,
    can_recover_unattended,
    cli_tunneld,
    find_pymobiledevice3_binary,
    get_tunneld_devices,
    install_daemon,
    installed_plist_drift,
    is_tunneld_running,
    recover_wedged_tunneld,
    resolve_tunnel_udid,
    tunneld_health,
    uninstall_daemon,
)
