"""Server configuration and API key management."""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass, field
from pathlib import Path


logger = logging.getLogger("quern-debug-server.config")

CONFIG_DIR = Path.home() / ".quern"
API_KEY_FILE = CONFIG_DIR / "api-key"
USER_CONFIG_FILE = CONFIG_DIR / "config.json"


@dataclass
class ServerConfig:
    """Configuration for the Quern debug log server."""

    host: str = "0.0.0.0"
    port: int = 9100
    ring_buffer_size: int = 10_000
    default_device_id: str = "default"
    api_key: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if not self.api_key:
            self.api_key = self._load_or_create_api_key()

    @staticmethod
    def _load_or_create_api_key() -> str:
        """Load existing API key or generate a new one."""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)

        if API_KEY_FILE.exists():
            key = API_KEY_FILE.read_text().strip()
            if key:
                return key

        key = secrets.token_urlsafe(32)
        API_KEY_FILE.write_text(key)
        API_KEY_FILE.chmod(0o600)
        return key

    @staticmethod
    def regenerate_api_key() -> str:
        """Generate a new API key, replacing the existing one."""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        key = secrets.token_urlsafe(32)
        API_KEY_FILE.write_text(key)
        API_KEY_FILE.chmod(0o600)
        return key


def read_user_config() -> dict:
    """Read user config from ~/.quern/config.json. Returns {} if missing or invalid."""
    if not USER_CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(USER_CONFIG_FILE.read_text())
    except Exception as e:
        logger.warning("Failed to read config file %s: %s", USER_CONFIG_FILE, e)
        return {}


def get_default_device_family() -> str:
    """Return the configured default device family, defaulting to 'iPhone'."""
    return read_user_config().get("default_device_family", "iPhone")


def get_local_capture_processes() -> list[str]:
    """Return the list of process names for local capture mode.

    Returns [] if not configured (disabled).
    Handles legacy bool values: True -> default process list, False -> [].
    """
    value = read_user_config().get("local_capture")
    if value is None or value is False:
        return []
    if value is True:
        # Legacy bool: default to Safari processes
        return ["MobileSafari", "com.apple.WebKit.Networking"]
    if isinstance(value, list):
        return [str(v) for v in value if v]
    return []


def set_local_capture_processes(processes: list[str]) -> None:
    """Set the local_capture process list in ~/.quern/config.json.

    An empty list disables local capture.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config = read_user_config()
    config["local_capture"] = processes
    USER_CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n")
