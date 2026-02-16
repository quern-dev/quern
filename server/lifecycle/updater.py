"""./quern update — pull latest changes and rebuild.

Usage:
    ./quern update
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


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

    # 1. Fetch from origin
    print("Checking for updates...")
    result = subprocess.run(
        ["git", "fetch", "origin"],
        cwd=str(project_root),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        print(f"Error: git fetch failed: {result.stderr.strip()}")
        return 1

    # 2. Check if we're behind
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
        return 1
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
        # No upstream tracking — try origin/main
        result = subprocess.run(
            ["git", "rev-list", "HEAD..origin/main", "--count"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            print("Error: could not compare with remote (no tracking branch)")
            return 1
        branch = "main"

    behind_count = int(result.stdout.strip())
    if behind_count == 0:
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
    print(f"\nUpdated successfully — pulled {behind_count} commit{'s' if behind_count != 1 else ''}.")
    return 0
