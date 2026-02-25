"""Persistent certificate state management.

Stores per-device cert installation state in ~/.quern/cert-state.json,
which persists across server restarts (unlike state.json which is deleted
on server stop).

Uses fcntl file locking matching the pattern in server/lifecycle/state.py.
"""

from __future__ import annotations

import fcntl
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from server.config import CONFIG_DIR

logger = logging.getLogger(__name__)

CERT_STATE_FILE = CONFIG_DIR / "cert-state.json"


def read_cert_state() -> dict[str, dict]:
    """Read cert-state.json with shared file lock.

    Returns empty dict if file doesn't exist or contains invalid JSON.
    """
    if not CERT_STATE_FILE.exists():
        return {}

    try:
        fd = CERT_STATE_FILE.open("r")
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)
            content = fd.read()
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            fd.close()

        if not content.strip():
            return {}
        return json.loads(content)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read cert state file: %s", e)
        return {}


def read_cert_state_for_device(udid: str) -> dict | None:
    """Read cert state for a specific device.

    Returns None if no state exists for the device.
    """
    state = read_cert_state()
    return state.get(udid)


def update_cert_state(udid: str, cert_data: dict[str, Any]) -> None:
    """Update cert state for a single device with exclusive lock.

    Performs read-modify-write to preserve other devices' state.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # Use a+ to create file if it doesn't exist
    fd = CERT_STATE_FILE.open("a+") if CERT_STATE_FILE.exists() else _create_and_open()
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        fd.seek(0)
        content = fd.read()

        if content.strip():
            try:
                state = json.loads(content)
            except json.JSONDecodeError:
                state = {}
        else:
            state = {}

        state[udid] = cert_data

        fd.seek(0)
        fd.truncate()
        fd.write(json.dumps(state, indent=2))
        fd.flush()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def record_device_proxy_config(
    udid: str, ssid: str, proxy_host: str, port: int, client_ip: str | None = None
) -> None:
    """Record the Wi-Fi proxy config for a specific network on a physical device."""
    existing = read_cert_state_for_device(udid) or {}
    configs: dict[str, Any] = existing.get("wifi_proxy_configs") or {}
    configs[ssid] = {
        "proxy_host": proxy_host,
        "proxy_port": port,
        "client_ip": client_ip,
        "set_at": datetime.now(timezone.utc).isoformat(),
    }
    existing["wifi_proxy_configs"] = configs
    update_cert_state(udid, existing)


def _create_and_open():
    """Create cert state file and return file handle opened for read/write."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CERT_STATE_FILE.touch()
    return CERT_STATE_FILE.open("a+")
