"""Server configuration and API key management."""

from __future__ import annotations

import json
import logging
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("quern-debug-server.config")

# Honours QUERN_STATE_DIR, so redirecting it redirects *everything* under
# ~/.quern -- the api key, config.json, the device pool, crash reports, the
# tool snapshot, the installed-by-setup manifest, all of it.
#
# It used to redirect two files. `server/lifecycle/state.py` read the variable
# itself and applied it to state.json and active-device.json only, while every
# other path was built from `Path.home()` here. The test suite's own comment
# said "QUERN_STATE_DIR redirects ~/.quern", which was the sandbox everyone
# believed in: a test calling `regenerate_api_key()` would have rewritten the
# developer's real key and left every MCP client on the machine authenticating
# with a stale one, silently.
_state_dir = os.environ.get("QUERN_STATE_DIR")
CONFIG_DIR = Path(_state_dir) if _state_dir else Path.home() / ".quern"
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


# ---------------------------------------------------------------------------
# Update channel — `stable` (default) or `beta`. Drives which branch
# `quern update` checks against and which GitHub release filter the
# tarball updater uses (#41).
# ---------------------------------------------------------------------------

VALID_UPDATE_CHANNELS = ("stable", "beta")
DEFAULT_UPDATE_CHANNEL = "stable"


def get_update_channel() -> str:
    """Return the configured update channel, defaulting to ``stable``.

    Unknown values fall back to the default rather than throwing — a
    typo in config.json shouldn't break the daemon.
    """
    raw = read_user_config().get("update_channel")
    if isinstance(raw, str) and raw in VALID_UPDATE_CHANNELS:
        return raw
    return DEFAULT_UPDATE_CHANNEL


def set_update_channel(channel: str) -> None:
    """Persist the update channel preference. Raises ValueError on an
    unknown channel name to prevent typos from silently no-op'ing."""
    if channel not in VALID_UPDATE_CHANNELS:
        raise ValueError(
            f"Unknown update channel {channel!r}. "
            f"Valid: {', '.join(VALID_UPDATE_CHANNELS)}"
        )
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config = read_user_config()
    config["update_channel"] = channel
    USER_CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n")


def get_auto_install_cert() -> bool:
    """Whether to install the mitmproxy CA automatically when capture needs it.

    Defaults to False, and deliberately: installing a MITM root CA is a larger
    and longer-lived commitment than the proxy toggle that prompts it. It
    persists across sessions, outlives the capture window, and the user has to
    know it happened in order to undo it. So the first encounter costs one
    round of asking, and this makes the second onwards free.

    Anything other than a real boolean is treated as unset -- a typo should
    read as "ask me", never as consent.
    """
    return read_user_config().get("auto_install_cert") is True


def _write_user_config(config: dict) -> None:
    """Persist the whole config, swapping it in rather than writing over it.

    The menu-bar app reads this file on a poll while the CLI writes it, and
    ``write_text`` truncates before it writes -- so a read landing in that
    window gets a partial document, and the app's parse fails. ``os.replace``
    is atomic within a filesystem, so a reader sees either the old file or the
    new one. The temporary lives in the same directory for that reason: a move
    across filesystems is a copy, and the window comes back.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = USER_CONFIG_FILE.with_name(f".{USER_CONFIG_FILE.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(config, indent=2) + "\n")
        os.replace(tmp, USER_CONFIG_FILE)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def set_auto_install_cert(enabled: bool) -> None:
    """Persist the auto-install policy.

    Surfaced in ``proxy_status`` and in the menu-bar app's Settings pane, both
    on purpose: a silent, persistent CA-install policy would be worse than the
    failure it exists to prevent.
    """
    config = read_user_config()
    config["auto_install_cert"] = bool(enabled)
    _write_user_config(config)


def get_update_check() -> bool:
    """Whether quern checks for updates on its own.

    Defaults to True: this is the "check automatically" box, and it starts
    ticked. Note the asymmetry with ``auto_install_cert``, which requires a
    literal ``true`` -- there, anything unclear must read as "ask me", because
    the cost of guessing wrong is a root CA installed without consent. Here the
    cost of guessing wrong is one HTTPS request a day, so only an explicit
    ``false`` turns it off and a typo leaves checking on.

    Governs the *automatic* check alone. ``quern check-updates`` and the menu
    bar's Check for Updates ignore it, the way every other updater leaves Check
    Now working when the box is unticked.
    """
    return read_user_config().get("update_check") is not False


def set_update_check(enabled: bool) -> None:
    """Persist whether the automatic update check runs."""
    config = read_user_config()
    config["update_check"] = bool(enabled)
    _write_user_config(config)


def channel_to_release_branch(channel: str) -> str:
    """Map a channel name to its reserved release pointer branch.

    Maintainers fast-forward these branches on each release cut —
    ``release/stable`` to the latest tagged stable release, ``release/
    beta`` to the latest prerelease commit (or main HEAD).
    """
    return f"release/{channel}"


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


# ---------------------------------------------------------------------------
# Plist watch config
# ---------------------------------------------------------------------------


def get_plist_watch_config() -> dict[str, dict]:
    """Return plist_watch config keyed by bundle_id.

    Each value has a "watches" list of {container, plist_path, ignore_prefixes} dicts.
    """
    return read_user_config().get("plist_watch", {})


def set_plist_watch_config(
    bundle_id: str,
    watches: list[dict],
) -> None:
    """Save plist watch config for a bundle_id.

    watches: list of {container, plist_path, ignore_prefixes} dicts.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config = read_user_config()
    pw = config.setdefault("plist_watch", {})
    pw[bundle_id] = {"watches": watches}
    USER_CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n")


def clear_plist_watch_config(bundle_id: str) -> bool:
    """Remove plist watch config for a bundle_id. Returns True if it existed."""
    config = read_user_config()
    pw = config.get("plist_watch", {})
    if bundle_id not in pw:
        return False
    del pw[bundle_id]
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    USER_CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n")
    return True
