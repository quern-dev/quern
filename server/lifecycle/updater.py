"""./quern update — pull latest changes and rebuild.

Supports two install modes:
  - Git clone (developers): updates via git pull --ff-only
  - Release tarball (users): downloads latest GitHub release

Usage:
    ./quern update
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from server.lifecycle.update_check import ENDPOINT
from server.lifecycle.update_check import TIMEOUT as CHECK_TIMEOUT

GITHUB_REPO = "quern-dev/quern"


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


def _read_local_version(project_root: Path) -> str | None:
    """Read version from pyproject.toml."""
    pyproject = project_root / "pyproject.toml"
    if not pyproject.exists():
        return None
    for line in pyproject.read_text().splitlines():
        if line.startswith("version"):
            return line.split('"')[1]
    return None


def _fetch_latest_release() -> tuple[str, str] | None:
    """Fetch latest release version and tarball URL from GitHub.

    Returns (version, tarball_url) or None on failure.
    """
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
        req = urllib.request.Request(url, headers={"User-Agent": "quern-update/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        tag = data.get("tag_name", "")
        version = tag.lstrip("v")
        tarball_url = data.get("tarball_url", "")
        if version and tarball_url:
            return version, tarball_url
    except Exception as e:
        print(f"Error: could not fetch release info: {e}")
    return None


def _is_git_install(project_root: Path) -> bool:
    """Check if this is a git-based install."""
    return (project_root / ".git").exists()


# ---------------------------------------------------------------------------
# Git-based update (for developers)
# ---------------------------------------------------------------------------


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


def _update_via_git(project_root: Path) -> int:
    """Update a git-based install via git pull."""
    # Check for updates via quern.dev, fall back to git fetch
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

    # Pull with fast-forward only
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

    new_version = _read_local_version(project_root) or "unknown"
    print(f"\nUpdated successfully to v{new_version} "
          f"({behind_count} commit{'s' if behind_count != 1 else ''}).\n")
    return 0


# ---------------------------------------------------------------------------
# Tarball-based update (for release installs)
# ---------------------------------------------------------------------------


def _update_via_tarball(project_root: Path) -> int:
    """Update a tarball-based install by downloading the latest release."""
    print("Checking for updates...")

    current_version = _read_local_version(project_root)
    release = _fetch_latest_release()
    if release is None:
        return 1

    latest_version, tarball_url = release

    if current_version == latest_version:
        print(f"Already up to date (v{current_version}).")
        return 0

    print(f"Updating v{current_version or 'unknown'} → v{latest_version}...")

    # Download tarball
    tmpdir = tempfile.mkdtemp()
    try:
        tarball_path = Path(tmpdir) / "quern.tar.gz"
        print("Downloading...")
        req = urllib.request.Request(tarball_url, headers={"User-Agent": "quern-update/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            tarball_path.write_bytes(resp.read())

        # Extract
        result = subprocess.run(
            ["tar", "-xzf", str(tarball_path), "-C", tmpdir],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            print(f"Error: failed to extract tarball: {result.stderr.strip()}")
            return 1

        # Find extracted directory
        extracted = [p for p in Path(tmpdir).iterdir() if p.is_dir() and p.name != "__MACOSX"]
        if not extracted:
            print("Error: tarball extracted but no directory found")
            return 1
        extracted_dir = extracted[0]

        # Preserve venv
        venv_dir = project_root / ".venv"
        venv_tmp = Path(tmpdir) / ".venv-preserve"
        if venv_dir.exists():
            shutil.move(str(venv_dir), str(venv_tmp))

        # Replace project files (keep .quern config dir if somehow present)
        for item in project_root.iterdir():
            if item.name == ".venv":
                continue
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()

        # Move new files in
        for item in extracted_dir.iterdir():
            shutil.move(str(item), str(project_root / item.name))

        # Restore venv
        if venv_tmp.exists():
            shutil.move(str(venv_tmp), str(venv_dir))

        print(f"\nUpdated successfully to v{latest_version}.\n")
        return 0

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def _rebuild_and_restart(project_root: Path) -> None:
    """Reinstall deps, rebuild MCP, run setup, restart server if running."""
    # Reinstall Python package (picks up new deps from pyproject.toml)
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

    # Rebuild MCP server
    from server.__main__ import _ensure_mcp_built

    if not _ensure_mcp_built(quiet=False):
        print("Warning: MCP server build failed — MCP tools may be stale")

    # Run setup to check for new external dependencies
    from server.lifecycle.setup import run_setup

    run_setup()

    # Restart the server if it was running
    from server.lifecycle.state import is_server_healthy, read_state

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


def run_update() -> int:
    """Pull latest changes and rebuild.

    Returns 0 on success, 1 on failure.
    """
    project_root = _find_project_root()
    if project_root is None:
        print("Error: could not find project root")
        return 1

    if _is_git_install(project_root):
        rc = _update_via_git(project_root)
    else:
        rc = _update_via_tarball(project_root)

    if rc != 0:
        return rc

    _rebuild_and_restart(project_root)
    return 0
