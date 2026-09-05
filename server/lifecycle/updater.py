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


def _fetch_latest_release(channel: str = "stable") -> tuple[str, str] | None:
    """Fetch the latest release for the user's channel from GitHub.

    For ``stable``: hits ``/releases/latest``, which is GitHub-defined as
    the most recent release with ``prerelease: false``.

    For ``beta``: hits ``/releases`` and picks the topmost entry with
    ``prerelease: true``. Beta users get prereleases when they exist; if
    there are none, returns the stable latest so beta users never see
    older content than stable users.

    Returns ``(version, tarball_url)`` or None on failure.
    """
    try:
        if channel == "beta":
            url = f"https://api.github.com/repos/{GITHUB_REPO}/releases"
        else:
            url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
        req = urllib.request.Request(url, headers={"User-Agent": "quern-update/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())

        if channel == "beta":
            # /releases returns an array sorted newest-first. Pick the
            # first prerelease entry; fall back to stable if there are
            # no prereleases yet.
            prerelease = next(
                (r for r in data if r.get("prerelease") and not r.get("draft")),
                None,
            )
            data = prerelease if prerelease else next(
                (r for r in data if not r.get("prerelease") and not r.get("draft")),
                None,
            )
            if data is None:
                return None

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


def _get_release_branch() -> str:
    """Return the branch the user's update channel tracks.

    Reads ``update_channel`` from ``~/.quern/config.json`` (#41). Default
    is ``stable`` → ``release/stable``; opt-in to ``beta`` → ``release/
    beta`` via ``quern set-channel beta``. Maintainers fast-forward
    these branches on each release cut.
    """
    from server.config import channel_to_release_branch, get_update_channel
    return channel_to_release_branch(get_update_channel())


def _check_via_git(project_root: Path) -> tuple[bool, str, int] | None:
    """Fall back to git fetch to check for updates.

    Always compares HEAD against ``origin/<release_branch>``, where the
    release branch is determined by the user's configured channel
    (``stable`` → ``release/stable``, ``beta`` → ``release/beta``).
    Closes #40 (the original bug used the current branch's tracking ref,
    silently missing real releases for users on a feature branch) and
    introduces the channel selection from #41.

    Returns ``(has_updates, current_branch, behind_count)`` or None on
    failure. ``has_updates`` and ``behind_count`` are always measured
    against ``origin/<release_branch>``; ``current_branch`` is reported
    so the caller can decide whether to actually pull (only safe on the
    release branch) or just warn (any other branch).
    """
    release_branch = _get_release_branch()
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
    current_branch = result.stdout.strip()

    # Count commits behind origin/<release_branch> — always, regardless
    # of which branch is currently checked out.
    result = subprocess.run(
        ["git", "rev-list", f"HEAD..origin/{release_branch}", "--count"],
        cwd=str(project_root),
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        print(
            f"Error: could not compare HEAD against origin/{release_branch} "
            f"({result.stderr.strip()})"
        )
        return None

    behind_count = int(result.stdout.strip())
    return (behind_count > 0, current_branch, behind_count)


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
        return 2  # No update needed

    # quern.dev said yes or was unreachable — need git fetch either way for the pull
    git_check = _check_via_git(project_root)
    if git_check is None:
        return 1

    has_updates, branch, behind_count = git_check
    release_branch = _get_release_branch()

    # Approach (A) from #40: when the user is on a non-release branch
    # (typical for dev clones), the commit count is now truthful about
    # `origin/<release_branch>`, but pulling here would be wrong — `git
    # pull --ff-only` tracks the current branch's upstream, not the
    # release branch. So we report what's available and let the user
    # decide whether to switch.
    if branch != release_branch:
        if has_updates:
            plural = "s" if behind_count != 1 else ""
            print(
                f"You're on branch `{branch}`. "
                f"`origin/{release_branch}` is {behind_count} commit{plural} ahead. "
                f"Switch with `git checkout {release_branch}` and rerun "
                f"`quern update` to apply — or stay on `{branch}` "
                f"if you're working on Quern itself."
            )
        else:
            print(
                f"You're on branch `{branch}` "
                f"(not the release branch `{release_branch}`). "
                f"No new commits on `origin/{release_branch}`."
            )
        return 2  # Skip rebuild — current workspace isn't pull-able

    if not has_updates:
        print("Already up to date.")
        return 2  # No update needed

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
    from server.config import get_update_channel
    channel = get_update_channel()

    print(f"Checking for updates on channel '{channel}'...")

    current_version = _read_local_version(project_root)
    release = _fetch_latest_release(channel)
    if release is None:
        return 1

    latest_version, tarball_url = release

    if current_version == latest_version:
        print(f"Already up to date (v{current_version}, channel '{channel}').")
        return 2  # No update needed

    print(f"Updating v{current_version or 'unknown'} → v{latest_version} (channel '{channel}')...")

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


def _rebuild_and_restart(project_root: Path) -> list[str]:
    """Reinstall deps, rebuild MCP, run setup, restart server if running.

    Returns the steps that failed, empty when everything worked.

    Every step runs even after an earlier one fails -- an update that cannot
    build the MCP wrapper should still reconcile the venv -- but each failure is
    recorded, because only `deps` used to be. A failed MCP build was a warning,
    `run_setup`'s exit code was discarded outright, and the restart's was never
    read, so `quern update` could exit 0 having left the MCP tools stale or the
    server down.

    Named steps rather than one bool so the caller can say which part failed:
    reporting "dependencies could not be installed" after a failed restart sends
    the reader to the wrong place.
    """
    failures: list[str] = []

    # Reinstall Python deps. Delegated so there is one implementation and one
    # failure semantic — this used to be a separate pip call whose failure was
    # only a warning, so an update could report success onto a stale venv.
    from server.__main__ import _ensure_python_deps

    if not _ensure_python_deps(quiet=False, force=True, eager=True):
        failures.append("dependencies")

    # Rebuild MCP server
    from server.__main__ import _ensure_mcp_built

    try:
        if not _ensure_mcp_built(quiet=False):
            print("Warning: MCP server build failed — MCP tools may be stale")
            failures.append("MCP build")
    except (OSError, subprocess.SubprocessError) as exc:
        # _ensure_mcp_built shells out to npm twice with timeouts and catches
        # neither, so a machine without npm -- or a slow install -- raised
        # straight through this function and crashed `quern update` before it
        # could report anything, or run setup and restart.
        print(f"Warning: MCP server build failed: {exc}")
        failures.append("MCP build")

    # Run setup to check for new external dependencies
    from server.lifecycle.setup import run_setup

    if run_setup() != 0:
        print("Warning: setup reported errors — see above")
        failures.append("setup")

    # Restart the server if it was running
    from server.lifecycle.state import is_server_healthy, read_state

    state = read_state()
    if state and is_server_healthy(state.get("server_port", 9100)):
        print("Restarting server to pick up changes...")
        venv_python = project_root / ".venv" / "bin" / "python"
        try:
            restart = subprocess.run(
                [str(venv_python), "-m", "server", "restart"],
                cwd=str(project_root),
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            # A 30s timeout here left the exception uncaught, so an update that
            # hung on restart crashed rather than reporting.
            print(f"Warning: restart failed: {exc}")
            failures.append("restart")
        else:
            if restart.returncode != 0:
                print(f"Warning: restart exited {restart.returncode} — "
                      "the server may be down; check `quern status`")
                failures.append("restart")
    else:
        print("Server is not running — start it with: quern start")

    return failures


def _report_tool_updates(apply: bool = False) -> bool:
    """Show external tools that have moved on, and optionally move them.

    Runs on every `quern update`, including the "already up to date" path.
    External tools go stale on their own schedule, so gating this on quern
    having an update means learning about a two-major-old binary only when
    something unrelated happens to ship.

    Reporting is the default because these commands touch state outside the
    project -- a pipx or brew upgrade affects every other consumer on the
    machine, and doing that unasked as a side effect of updating quern is not
    the caller's decision to have made for them.

    Returns False only when `apply` was asked for and an upgrade command failed.
    Reporting alone always returns True: a stale tool is information, not a
    failed update. But `--tools` is an instruction, and printing "failed" while
    the process still exits 0 tells a script the opposite of what happened.
    """
    import asyncio

    from server.device.tool_updates import actionable, format_offer, plan_updates
    from server.device.tool_versions import collect_sites

    try:
        async def gather():
            return await plan_updates(await collect_sites())

        updates = asyncio.run(gather())
    except Exception as exc:
        # Never fail an update over a version check.
        print(f"Note: could not check external tool versions ({exc}).")
        return True

    offer = format_offer(updates)
    if not offer:
        return True
    print(offer)

    todo = actionable(updates)
    if not apply:
        print("\n  Run `quern update --tools` to apply these.")
        return True

    failures: list[str] = []
    for update in todo:
        print(f"\nUpgrading {update.name}...")
        try:
            result = subprocess.run(update.command, timeout=600)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"  failed: {exc}")
            failures.append(update.name)
            continue
        if result.returncode != 0:
            print(f"  failed: {' '.join(update.command)} exited {result.returncode}")
            failures.append(update.name)

    if failures:
        print(f"\n{len(failures)} tool upgrade(s) failed: {', '.join(failures)}")
    return not failures


def run_update(apply_tools: bool = False) -> int:
    """Pull latest changes and rebuild.

    Args:
        apply_tools: also run the external-tool upgrades, instead of only
            reporting them.

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

    if rc == 1:
        return 1  # Error
    if rc == 2:
        # Nothing to rebuild, but external tools age independently of quern.
        return 0 if _report_tool_updates(apply_tools) else 1

    failures = _rebuild_and_restart(project_root)

    # External tools live outside the project and do not depend on the rebuild
    # having worked, so they are reported either way. Returning early here meant
    # a failed rebuild also silently dropped `--tools`, which the caller asked
    # for explicitly.
    tools_ok = _report_tool_updates(apply_tools)

    if failures:
        # The source moved but part of the rebuild did not. Saying "success"
        # here is the exact failure this reporting exists to prevent, and
        # naming the step matters: "dependencies could not be installed" after
        # a failed restart sends the reader to the wrong place.
        print(f"Update incomplete: {', '.join(failures)} failed.")
        return 1

    return 0 if tools_ok else 1
