"""Non-blocking update check for server startup.

Rate-limited to once per 24 hours. Never blocks or crashes the server.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from server.config import CONFIG_DIR

LAST_CHECK_FILE = CONFIG_DIR / "last-update-check"
CHECK_INTERVAL = 86400  # 24 hours


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


def check_for_updates() -> str | None:
    """Return a message if updates are available, None otherwise.

    Rate-limited to once per CHECK_INTERVAL seconds. Never blocks server
    startup — returns None on any error.
    """
    try:
        # Check rate limit
        if LAST_CHECK_FILE.exists():
            last_check = LAST_CHECK_FILE.stat().st_mtime
            if time.time() - last_check < CHECK_INTERVAL:
                return None

        # Touch the file before checking (so failures don't retry rapidly)
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        LAST_CHECK_FILE.touch()

        project_root = _find_project_root()
        if project_root is None or not (project_root / ".git").exists():
            return None

        # Fetch quietly with a very short timeout — must never delay startup
        result = subprocess.run(
            ["git", "fetch", "origin", "--quiet"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode != 0:
            return None

        # Count commits behind on current branch
        branch_result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if branch_result.returncode != 0:
            return None
        branch = branch_result.stdout.strip()

        result = subprocess.run(
            ["git", "rev-list", f"HEAD..origin/{branch}", "--count"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            # Try origin/main as fallback
            result = subprocess.run(
                ["git", "rev-list", "HEAD..origin/main", "--count"],
                cwd=str(project_root),
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return None

        behind = int(result.stdout.strip())
        if behind > 0:
            return f"{behind} update{'s' if behind != 1 else ''} available — run './quern update'"
        return None

    except Exception:
        return None
