"""macOS system proxy snapshot, configuration, and restore.

All core functions are **sync** (subprocess.run) so they work in signal
handlers, CLI paths, AND async endpoints (via asyncio.to_thread).

The pattern is: snapshot current state -> configure -> restore on shutdown/stop.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Snapshot dataclass
# ---------------------------------------------------------------------------


@dataclass
class SystemProxySnapshot:
    """Captured state of the macOS system proxy for a network interface."""

    interface: str
    http_proxy_enabled: bool
    http_proxy_server: str
    http_proxy_port: int
    https_proxy_enabled: bool
    https_proxy_server: str
    https_proxy_port: int
    timestamp: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> SystemProxySnapshot:
        return cls(**d)


# ---------------------------------------------------------------------------
# Network interface detection (moved from server.api.proxy)
# ---------------------------------------------------------------------------


def detect_active_interface() -> str | None:
    """Detect the macOS networksetup service name for the active interface.

    Uses get_default_route_device() to get the BSD device (e.g., en0),
    then maps it to a networksetup service name (e.g., "Wi-Fi").

    Returns None if detection fails (non-macOS, no default route, etc.).
    """
    bsd_device = get_default_route_device()
    if not bsd_device:
        return None
    return bsd_device_to_service_name(bsd_device)


def get_default_route_device() -> str | None:
    """Get the BSD device name for the default route (e.g., en0, utun3)."""
    try:
        result = subprocess.run(
            ["route", "-n", "get", "default"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None

        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("interface:"):
                return line.split(":", 1)[1].strip()
        return None
    except Exception:
        return None


def bsd_device_to_service_name(bsd_device: str) -> str | None:
    """Map a BSD device name (en0) to a networksetup service name (Wi-Fi)."""
    try:
        result = subprocess.run(
            ["networksetup", "-listallhardwareports"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None

        lines = result.stdout.splitlines()
        for i, line in enumerate(lines):
            if f"Device: {bsd_device}" in line:
                for j in range(i - 1, -1, -1):
                    if lines[j].startswith("Hardware Port:"):
                        return lines[j].split(":", 1)[1].strip()
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# networksetup output parsing
# ---------------------------------------------------------------------------


def parse_networksetup_output(output: str) -> tuple[bool, str, int]:
    """Parse ``networksetup -getwebproxy`` / ``-getsecurewebproxy`` output.

    Returns (enabled, server, port).
    """
    enabled = False
    server = ""
    port = 0

    for line in output.splitlines():
        line = line.strip()
        if line.startswith("Enabled:"):
            val = line.split(":", 1)[1].strip()
            enabled = val.lower() in ("yes", "1", "true")
        elif line.startswith("Server:"):
            server = line.split(":", 1)[1].strip()
        elif line.startswith("Port:"):
            try:
                port = int(line.split(":", 1)[1].strip())
            except ValueError:
                port = 0

    return enabled, server, port


# ---------------------------------------------------------------------------
# Core operations (all sync for signal-handler safety)
# ---------------------------------------------------------------------------


def snapshot_system_proxy(interface: str) -> SystemProxySnapshot:
    """Read the current system proxy state for *interface*."""
    http_out = subprocess.run(
        ["networksetup", "-getwebproxy", interface],
        capture_output=True, text=True, timeout=5,
    )
    http_enabled, http_server, http_port = parse_networksetup_output(http_out.stdout)

    https_out = subprocess.run(
        ["networksetup", "-getsecurewebproxy", interface],
        capture_output=True, text=True, timeout=5,
    )
    https_enabled, https_server, https_port = parse_networksetup_output(https_out.stdout)

    return SystemProxySnapshot(
        interface=interface,
        http_proxy_enabled=http_enabled,
        http_proxy_server=http_server,
        http_proxy_port=http_port,
        https_proxy_enabled=https_enabled,
        https_proxy_server=https_server,
        https_proxy_port=https_port,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


def configure_system_proxy(interface: str, host: str, port: int) -> None:
    """Set both HTTP and HTTPS system proxy to *host*:*port* on *interface*."""
    subprocess.run(
        ["networksetup", "-setwebproxy", interface, host, str(port)],
        capture_output=True, text=True, timeout=5, check=True,
    )
    subprocess.run(
        ["networksetup", "-setsecurewebproxy", interface, host, str(port)],
        capture_output=True, text=True, timeout=5, check=True,
    )
    logger.info(
        "Configured system proxy on '%s' → %s:%d", interface, host, port,
    )


def restore_system_proxy(snapshot: SystemProxySnapshot) -> None:
    """Restore system proxy to the state captured in *snapshot*."""
    iface = snapshot.interface

    logger.info(
        "Restoring system proxy to pre-Quern state (interface: %s).\n"
        "       If you modified proxy settings manually while Quern was running, "
        "those changes will be lost.",
        iface,
    )

    # HTTP proxy
    if snapshot.http_proxy_enabled:
        subprocess.run(
            ["networksetup", "-setwebproxy", iface,
             snapshot.http_proxy_server, str(snapshot.http_proxy_port)],
            capture_output=True, text=True, timeout=5,
        )
    else:
        subprocess.run(
            ["networksetup", "-setwebproxystate", iface, "off"],
            capture_output=True, text=True, timeout=5,
        )

    # HTTPS proxy
    if snapshot.https_proxy_enabled:
        subprocess.run(
            ["networksetup", "-setsecurewebproxy", iface,
             snapshot.https_proxy_server, str(snapshot.https_proxy_port)],
            capture_output=True, text=True, timeout=5,
        )
    else:
        subprocess.run(
            ["networksetup", "-setsecurewebproxystate", iface, "off"],
            capture_output=True, text=True, timeout=5,
        )


# ---------------------------------------------------------------------------
# Convenience wrappers
# ---------------------------------------------------------------------------


def _sanitize_stale_snapshot(snap: SystemProxySnapshot, proxy_port: int) -> SystemProxySnapshot:
    """If the snapshot looks like a stale Quern config, treat it as disabled.

    This handles the case where a previous Quern instance configured the proxy
    but didn't clean up (crash, kill -9, etc.). Without this, the snapshot
    would capture Quern's own settings as the "original" state, and restore
    would be a no-op — leaving the proxy configured forever.
    """
    stale_http = (
        snap.http_proxy_enabled
        and snap.http_proxy_server == "127.0.0.1"
        and snap.http_proxy_port == proxy_port
    )
    stale_https = (
        snap.https_proxy_enabled
        and snap.https_proxy_server == "127.0.0.1"
        and snap.https_proxy_port == proxy_port
    )

    if stale_http or stale_https:
        logger.warning(
            "Detected stale Quern proxy config on %s (127.0.0.1:%d) — "
            "treating original state as disabled so restore will clear it.",
            snap.interface, proxy_port,
        )
        return SystemProxySnapshot(
            interface=snap.interface,
            http_proxy_enabled=False if stale_http else snap.http_proxy_enabled,
            http_proxy_server="" if stale_http else snap.http_proxy_server,
            http_proxy_port=0 if stale_http else snap.http_proxy_port,
            https_proxy_enabled=False if stale_https else snap.https_proxy_enabled,
            https_proxy_server="" if stale_https else snap.https_proxy_server,
            https_proxy_port=0 if stale_https else snap.https_proxy_port,
            timestamp=snap.timestamp,
        )

    return snap


def detect_and_configure(proxy_port: int, interface: str | None = None) -> SystemProxySnapshot | None:
    """Detect interface, snapshot, configure.  Returns None on detection failure."""
    iface = interface or detect_active_interface()
    if not iface:
        logger.warning(
            "Could not detect active network interface — skipping system proxy configuration. "
            "Use POST /proxy/configure-system with an explicit interface to configure manually.",
        )
        return None

    snap = snapshot_system_proxy(iface)
    snap = _sanitize_stale_snapshot(snap, proxy_port)
    configure_system_proxy(iface, "127.0.0.1", proxy_port)
    return snap


def restore_from_state() -> bool:
    """Read state.json and restore system proxy if configured.

    For use in signal handlers and lifespan teardown.
    Returns True if restore was performed.
    """
    from server.lifecycle.state import read_state

    state = read_state()
    if not state:
        return False
    return restore_from_state_dict(state)


def restore_from_state_dict(state: dict) -> bool:
    """Restore system proxy from a pre-read state dict.

    Returns True if restore was performed.
    """
    if not state.get("system_proxy_configured"):
        return False

    snapshot_data = state.get("system_proxy_snapshot")
    if not snapshot_data:
        logger.warning(
            "state.json has system_proxy_configured=True but no snapshot — "
            "cannot restore. You may need to manually disable the system proxy.",
        )
        return False

    try:
        snap = SystemProxySnapshot.from_dict(snapshot_data)
        restore_system_proxy(snap)
        return True
    except Exception:
        logger.exception("Failed to restore system proxy from state.json")
        return False
