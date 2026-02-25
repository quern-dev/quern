"""State file management for Quern Debug Server.

The state file (~/.quern/state.json) is the single source of truth
for discovering a running server instance. Every consumer (CLI, MCP, shell
scripts, CI) reads this file to find the server.
"""

from __future__ import annotations

import fcntl
import json
import logging
import socket
import urllib.request
from pathlib import Path
from typing import Any, TypedDict

from server.config import CONFIG_DIR

logger = logging.getLogger(__name__)

STATE_FILE = CONFIG_DIR / "state.json"


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
    active_devices: list[str]
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
