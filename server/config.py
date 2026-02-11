"""Server configuration and API key management."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from pathlib import Path


CONFIG_DIR = Path.home() / ".quern"
API_KEY_FILE = CONFIG_DIR / "api-key"


@dataclass
class ServerConfig:
    """Configuration for the Quern debug log server."""

    host: str = "127.0.0.1"
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
