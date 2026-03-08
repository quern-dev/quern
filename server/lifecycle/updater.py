"""./quern update — pull latest changes and rebuild.

Usage:
    ./quern update
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from server.lifecycle.update_check import ENDPOINT, TIMEOUT as CHECK_TIMEOUT


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


def _check_via_quern_dev(head_sha: str) -> bool | None:
    """Check quern.dev for updates. Returns True/False, or None on failure."""
    try:
        url = f"{ENDPOINT}?sha={head_sha}"
        req = urllib.request.Request(url, headers={"User-Agent": "quern-update/1.0"})
        with urllib.request.urlopen(req, timeout=CHECK_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
        return data.get("update_available", False)
    except Exception:
        return None


def _check_via_git(project_root: Path) -> tuple[bool, str, int] | None:
    """Fall back to git fetch to check for updates.

    Returns (has_updates, branch, behind_count) or None on failure.
    """
    result = subprocess.run(
        ["git", "fetch", "origin"],
        cwd=str(project_root),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        print(f"Error: git fetch failed: {result.stderr.strip()}")
        return None

    # Get current branch
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(project_root),
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        print("Error: could not determine current branch")
        return None
    branch = result.stdout.strip()

    # Count commits behind
    result = subprocess.run(
        ["git", "rev-list", f"HEAD..origin/{branch}", "--count"],
        cwd=str(project_root),
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        result = subprocess.run(
            ["git", "rev-list", "HEAD..origin/main", "--count"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            print("Error: could not compare with remote (no tracking branch)")
            return None
        branch = "main"

    behind_count = int(result.stdout.strip())
    return (behind_count > 0, branch, behind_count)


def run_update() -> int:
    """Pull latest changes and rebuild.

    Returns 0 on success, 1 on failure.
    """
    project_root = _find_project_root()
    if project_root is None:
        print("Error: could not find project root")
        return 1

    # Ensure we're in a git repo
    git_dir = project_root / ".git"
    if not git_dir.exists():
        print("Error: not a git repository")
        return 1

    # 1. Check for updates via quern.dev, fall back to git fetch
    print("Checking for updates...")

    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(project_root),
        capture_output=True,
        text=True,
        timeout=5,
    )
    sha = head_sha.stdout.strip() if head_sha.returncode == 0 else None

    update_available = _check_via_quern_dev(sha) if sha else None

    if update_available is False:
        print("Already up to date.")
        return 0

    # quern.dev said yes or was unreachable — need git fetch either way for the pull
    git_check = _check_via_git(project_root)
    if git_check is None:
        return 1

    has_updates, branch, behind_count = git_check
    if not has_updates:
        print("Already up to date.")
        return 0

    print(f"{behind_count} new commit{'s' if behind_count != 1 else ''} available.")

    # 3. Pull with fast-forward only
    print("Pulling changes...")
    result = subprocess.run(
        ["git", "pull", "--ff-only"],
        cwd=str(project_root),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "local changes" in stderr.lower() or "uncommitted" in stderr.lower():
            print(
                "Error: you have local changes that would be overwritten.\n"
                "Commit or stash your changes first, then try again."
            )
        elif "diverged" in stderr.lower() or "not possible to fast-forward" in stderr.lower():
            print(
                "Error: your local branch has diverged from the remote.\n"
                f"Run 'git rebase origin/{branch}' to reconcile, then try again."
            )
        else:
            print(f"Error: git pull failed: {stderr}")
        return 1

    # 4. Reinstall Python package (picks up new deps from pyproject.toml)
    venv_pip = project_root / ".venv" / "bin" / "pip"
    if venv_pip.exists():
        print("Installing Python dependencies...")
        result = subprocess.run(
            [str(venv_pip), "install", "-e", "."],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            print(f"Warning: pip install failed: {result.stderr.strip()}")
            # Continue — the pull itself succeeded

    # 5. Rebuild MCP server
    from server.__main__ import _ensure_mcp_built

    if not _ensure_mcp_built(quiet=False):
        print("Warning: MCP server build failed — MCP tools may be stale")

    # 6. Summary
    print(f"\nUpdated successfully — pulled {behind_count} commit{'s' if behind_count != 1 else ''}.\n")

    # 7. Run setup to check for new external dependencies
    from server.lifecycle.setup import run_setup

    run_setup()

    # 8. Restart the server if it was running
    from server.lifecycle.state import read_state, is_server_healthy

    state = read_state()
    if state and is_server_healthy(state.get("server_port", 9100)):
        print("Restarting server to pick up changes...")
        venv_python = project_root / ".venv" / "bin" / "python"
        subprocess.run(
            [str(venv_python), "-m", "server", "restart"],
            cwd=str(project_root),
            timeout=30,
        )
    else:
        print("Server is not running — start it with: quern start")

    return 0
