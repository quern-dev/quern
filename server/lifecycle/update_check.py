"""Non-blocking update check via quern.dev endpoint.

On daemon start, hits https://quern.dev/api/check-update with the local HEAD SHA.
Cloudflare analytics count the requests — no data is stored or logged server-side.
Rate-limited to once per 24 hours. Never blocks or crashes the server.

Opt out by setting "update_check": false in ~/.quern/config.json.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
import urllib.request
import urllib.error
from pathlib import Path

from server.config import CONFIG_DIR, read_user_config

logger = logging.getLogger("quern-debug-server.update-check")

LAST_CHECK_FILE = CONFIG_DIR / "last-update-check"
CHECK_INTERVAL = 86400  # 24 hours
ENDPOINT = "https://quern.dev/api/check-update"
TIMEOUT = 5  # seconds


def _find_project_root() -> Path | None:
    """Find the project root by looking for pyproject.toml."""
    path = Path(__file__).resolve().parent
    for _ in range(5):
        if (path / "pyproject.toml").exists():
            return path
        parent = path.parent
        if parent == path:
            break
        path = parent
    return None


def _get_head_sha() -> str | None:
    """Get the local HEAD commit SHA."""
    project_root = _find_project_root()
    if project_root is None or not (project_root / ".git").exists():
        return None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def check_for_updates() -> str | None:
    """Return a message if updates are available, None otherwise.

    Rate-limited to once per CHECK_INTERVAL seconds. Never blocks server
    startup — returns None on any error. Respects "update_check": false
    in ~/.quern/config.json.
    """
    try:
        # Respect opt-out
        config = read_user_config()
        if config.get("update_check") is False:
            return None

        # Check rate limit
        if LAST_CHECK_FILE.exists():
            last_check = LAST_CHECK_FILE.stat().st_mtime
            if time.time() - last_check < CHECK_INTERVAL:
                return None

        # Touch before checking (so failures don't retry rapidly)
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        LAST_CHECK_FILE.touch()

        head_sha = _get_head_sha()
        if not head_sha:
            return None

        # Hit the endpoint
        url = f"{ENDPOINT}?sha={head_sha}"
        req = urllib.request.Request(url, headers={"User-Agent": "quern-update-check/1.0"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode())

        if data.get("update_available"):
            return 'Update available \u2014 run "quern update" to get the latest version'
        return None

    except Exception:
        return None
