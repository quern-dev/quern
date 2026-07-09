"""State file management for Quern.

The state file (~/.quern/state.json) is the single source of truth
for discovering a running server instance. Every consumer (CLI, MCP, shell
scripts, CI) reads this file to find the server.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import socket
import urllib.request
from pathlib import Path
from typing import Any, TypedDict

from server.config import CONFIG_DIR

logger = logging.getLogger(__name__)

# Allow tests to override the state file path via env var to avoid
# clobbering a running server's state.json.
_state_dir = os.environ.get("QUERN_STATE_DIR")
STATE_FILE = Path(_state_dir) / "state.json" if _state_dir else CONFIG_DIR / "state.json"

# Active device persistence is intentionally separate from STATE_FILE.
# state.json is server-runtime data and gets deleted on `quern stop`; the
# active device is user preference and must survive stop/start cycles.
ACTIVE_DEVICE_FILE = (
    Path(_state_dir) / "active-device.json" if _state_dir
    else CONFIG_DIR / "active-device.json"
)


class ServerState(TypedDict, total=False):
    """Schema for state.json."""

    pid: int
    server_host: str
    local_ip: str | None
    server_port: int
    proxy_port: int
    proxy_enabled: bool
    proxy_status: str  # "running", "stopped", "crashed", "disabled"
    started_at: str  # ISO 8601
    api_key: str
    system_proxy_configured: bool
    system_proxy_interface: str | None
    system_proxy_snapshot: dict | None


def read_state() -> ServerState | None:
    """Read state.json with shared file lock.

    Returns None if the file doesn't exist or contains invalid JSON.
    """
    if not STATE_FILE.exists():
        return None

    try:
        fd = STATE_FILE.open("r")
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)
            content = fd.read()
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            fd.close()

        if not content.strip():
            return None
        return json.loads(content)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read state file: %s", e)
        return None


def write_state(state: ServerState) -> None:
    """Write state.json with exclusive file lock."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    fd = STATE_FILE.open("w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        fd.write(json.dumps(state, indent=2))
        fd.flush()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def remove_state() -> None:
    """Remove state.json."""
    STATE_FILE.unlink(missing_ok=True)


def read_active_udid() -> str | None:
    """Read the persisted active-device UDID from its sidecar file.

    Lives in a separate file from state.json so it survives server
    stop/start cycles — `quern stop` deletes state.json, but the active
    device is user preference, not server runtime.

    Returns None if the file is missing, empty, or holds no UDID.
    """
    if not ACTIVE_DEVICE_FILE.exists():
        return None
    try:
        fd = ACTIVE_DEVICE_FILE.open("r")
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)
            content = fd.read()
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            fd.close()
        if not content.strip():
            return None
        data = json.loads(content)
        if not isinstance(data, dict):
            return None
        udid = data.get("udid")
        return udid if isinstance(udid, str) and udid else None
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read active-device.json: %s", e)
        return None


def write_active_udid(udid: str | None) -> None:
    """Persist the active-device UDID to its sidecar file.

    Pass None (or empty string) to clear. Survives `quern stop` —
    `remove_state()` deletes state.json but does not touch this file.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"udid": udid} if udid else {}
    fd = ACTIVE_DEVICE_FILE.open("w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        fd.write(json.dumps(payload, indent=2))
        fd.flush()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def update_state(**updates: Any) -> None:
    """Read-modify-write state.json with exclusive lock.

    No-op if state.json doesn't exist (e.g., running in test mode).
    """
    if not STATE_FILE.exists():
        return

    try:
        fd = STATE_FILE.open("a+")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            fd.seek(0)
            content = fd.read()
            if not content.strip():
                return
            state = json.loads(content)
            state.update(updates)
            fd.seek(0)
            fd.truncate()
            fd.write(json.dumps(state, indent=2))
            fd.flush()
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            fd.close()
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to update state file: %s", e)


def detect_local_ip() -> str | None:
    """Detect the machine's outward-facing LAN IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def _get_all_interface_ips() -> list[str]:
    """Return all IPv4 addresses currently assigned to local interfaces."""
    import subprocess
    try:
        result = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5)
        ips = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("inet ") and not line.startswith("inet6"):
                parts = line.split()
                if len(parts) >= 2:
                    ip = parts[1]
                    if not ip.startswith("127."):
                        ips.append(ip)
        return ips
    except Exception:
        return []


def detect_host_ip_for_subnet(device_ip: str) -> str | None:
    """Find the Mac interface IP on the same /24 subnet as device_ip."""
    import ipaddress
    try:
        device_net = ipaddress.ip_network(f"{device_ip}/24", strict=False)
    except ValueError:
        return None
    for ip in _get_all_interface_ips():
        try:
            if ipaddress.ip_network(f"{ip}/24", strict=False) == device_net:
                return ip
        except ValueError:
            continue
    return None


def enumerate_local_interfaces() -> list[dict]:
    """Enumerate active Mac network interfaces with IPv4 addresses, the
    default-route flag, and the Wi-Fi SSID when applicable.

    Each entry: ``{"interface", "ip", "subnet", "is_default_route", "ssid"}``.
    Loopback (127.0.0.0/8) is excluded. ``subnet`` is a /24 string; ``ssid``
    is set only for Wi-Fi interfaces that are currently associated.

    Used by ``proxy_status`` and ``proxy_setup_guide`` to disambiguate which
    Mac IP to advertise when multiple interfaces are active on different
    subnets — the single ``local_ip`` field (default-route only) is wrong
    in that scenario for any device not on the default-route subnet.
    """
    import ipaddress
    import subprocess

    entries: list[dict] = []
    try:
        result = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5)
    except Exception:
        return []

    current_iface: str | None = None
    for raw in result.stdout.splitlines():
        # Interface header lines start at column 0 ("en0: flags=...");
        # `inet` lines are indented. Track the most-recent header so we can
        # attach each address to the right BSD device.
        if raw and not raw[0].isspace():
            if ":" in raw:
                current_iface = raw.split(":", 1)[0]
            continue
        stripped = raw.strip()
        if not current_iface or not stripped.startswith("inet ") or stripped.startswith("inet6"):
            continue
        parts = stripped.split()
        if len(parts) < 2:
            continue
        ip = parts[1]
        # Skip loopback (127.0.0.0/8) and link-local autoconfig
        # (APIPA, 169.254.0.0/16). Link-local addresses appear on
        # interfaces that never got a DHCP lease — they're not reachable
        # from devices on any real subnet, so they'd only add noise to
        # the multi-interface ambiguity check.
        if ip.startswith("127.") or ip.startswith("169.254."):
            continue
        try:
            subnet = str(ipaddress.ip_network(f"{ip}/24", strict=False))
        except ValueError:
            subnet = None
        entries.append({
            "interface": current_iface,
            "ip": ip,
            "subnet": subnet,
            "is_default_route": False,
            "ssid": None,
        })

    if not entries:
        return entries

    # Annotate default-route interface. Import locally to avoid a module
    # cycle with server.proxy.system_proxy (which already imports state).
    try:
        from server.proxy.system_proxy import get_default_route_device
        default_iface = get_default_route_device()
    except Exception:
        default_iface = None
    if default_iface:
        for entry in entries:
            if entry["interface"] == default_iface:
                entry["is_default_route"] = True

    # Best-effort per-interface SSID lookup. networksetup -getairportnetwork
    # errors out on non-Wi-Fi interfaces; we treat any non-match as None.
    for entry in entries:
        entry["ssid"] = _get_interface_ssid(entry["interface"])

    return entries


def _get_interface_ssid(interface: str) -> str | None:
    """Return the SSID for a Wi-Fi interface, or None if not Wi-Fi or not connected."""
    import subprocess
    try:
        result = subprocess.run(
            ["networksetup", "-getairportnetwork", interface],
            capture_output=True, text=True, timeout=3,
        )
    except Exception:
        return None
    if "Current Wi-Fi Network:" in result.stdout:
        return result.stdout.split("Current Wi-Fi Network:", 1)[1].strip()
    return None


def detect_current_ssid() -> str | None:
    """Return the current Wi-Fi SSID, or None if not connected / not detectable."""
    import subprocess
    for iface in ("en0", "en1", "en2"):
        try:
            result = subprocess.run(
                ["networksetup", "-getairportnetwork", iface],
                capture_output=True, text=True, timeout=3,
            )
            if "Current Wi-Fi Network:" in result.stdout:
                return result.stdout.split("Current Wi-Fi Network:", 1)[1].strip()
        except Exception:
            continue
    return None


def is_server_healthy(port: int, host: str = "127.0.0.1", timeout: float = 2.0) -> bool:
    """Check if a server is responding on the given port.

    Uses stdlib urllib only — no FastAPI/httpx dependency.
    """
    try:
        url = f"http://{host}:{port}/health"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def fetch_tools(
    port: int, host: str = "127.0.0.1", timeout: float = 15.0
) -> dict | None:
    """Fetch device-tool availability from a running server's /tools endpoint.

    Separate from is_server_healthy() because tool probing (e.g. `idb
    list-targets`) can take several seconds — which is exactly why it no longer
    lives on the fast /health path. Returns the parsed JSON dict, or None if the
    server is unreachable. Uses stdlib urllib only.
    """
    try:
        url = f"http://{host}:{port}/tools"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode())
    except Exception:
        return None
