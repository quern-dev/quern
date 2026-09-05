"""Lightweight CLI bootstrap for quern-debug-server.

Handles venv auto-detection and the `setup` command without importing
the full server stack, so `setup` works on a fresh clone before
dependencies are installed.

For all other commands, delegates to server.main.cli().
"""

from __future__ import annotations

import os
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


def _maybe_reexec_in_venv() -> None:
    """If not running inside the project venv, re-exec using it.

    This lets users run `quern-debug-server start` without activating
    the venv — the CLI finds .venv and re-launches itself inside it.
    """
    if sys.prefix != sys.base_prefix:
        return  # already in a venv

    project_root = _find_project_root()
    if project_root is None:
        return

    venv_python = project_root / ".venv" / "bin" / "python"
    if not venv_python.exists():
        return

    # Replace this process with the venv Python running the same command
    os.execv(str(venv_python), [str(venv_python), "-m", "server"] + sys.argv[1:])


DEPS_STAMP_NAME = ".deps-stamp"


def python_deps_state(project_root: Path | None = None) -> dict:
    """Report whether the venv is in sync with pyproject.toml. Read-only.

    Mirrors how _ensure_mcp_built decides whether node_modules is stale: an
    mtime stamp, not a resolver run. Two stat() calls, no network, no pip.

    The stamp is written only after a successful install, so a failed install
    is never remembered as done — the next start retries by itself. That is
    what makes recovery automatic rather than something a user has to trigger.

    Returns a dict with:
        applicable: False when there is no venv to reconcile (tarball/system install)
        in_sync:    stamp exists and is at least as new as pyproject.toml
        reason:     short human-readable explanation
    """
    if project_root is None:
        project_root = _find_project_root()
    if project_root is None:
        return {"applicable": False, "in_sync": True, "reason": "project root not found"}

    venv_pip = project_root / ".venv" / "bin" / "pip"
    if not venv_pip.exists():
        return {"applicable": False, "in_sync": True, "reason": "no venv — nothing to reconcile"}

    pyproject = project_root / "pyproject.toml"
    stamp = project_root / ".venv" / DEPS_STAMP_NAME

    if not pyproject.exists():
        return {"applicable": False, "in_sync": True, "reason": "no pyproject.toml"}
    if not stamp.exists():
        return {"applicable": True, "in_sync": False,
                "reason": "dependencies have never been reconciled"}
    if pyproject.stat().st_mtime > stamp.stat().st_mtime:
        return {"applicable": True, "in_sync": False,
                "reason": "pyproject.toml is newer than the last successful install"}
    return {"applicable": True, "in_sync": True, "reason": "up to date"}


def _ensure_python_deps(
    quiet: bool = False, force: bool = False, eager: bool = False,
) -> bool:
    """Install declared Python dependencies when the venv has fallen behind.

    Covers the cases `quern update` cannot reach — a manual `git pull`, a branch
    switch, an update that returned early because the workspace was not
    pullable, and an earlier install that failed. Runs on every start; when
    nothing has changed it costs two stat() calls.

    Args:
        quiet: only print on an actual install or failure.
        force: install regardless of the stamp (used by `quern doctor --fix`).
        eager: also pull transitive dependencies up to their newest compatible
            release, rather than leaving any already-satisfying version alone.
            Only `quern update` passes this -- see the note below.

    Returns:
        True if the venv is in sync, False if an install was needed and failed.
    """
    import subprocess

    project_root = _find_project_root()
    state = python_deps_state(project_root)
    if not state["applicable"]:
        return True
    if state["in_sync"] and not force:
        return True

    venv_pip = project_root / ".venv" / "bin" / "pip"
    if not quiet:
        print(f"Installing Python dependencies ({state['reason']})...")

    # pip's default (--upgrade-strategy only-if-needed) leaves any version that
    # already satisfies the constraint alone, so a venv drifts arbitrarily far
    # behind while every declared floor stays satisfied. Our floors are all `>=`
    # and none is near what ships (see pyproject.toml), so "satisfies" is a very
    # weak statement about how current the venv is.
    #
    # Eager is therefore right for `quern update` -- an explicit "bring me
    # forward" -- and wrong for the start path, which runs on every launch and
    # must stay a cheap constraint check rather than a network-bound upgrade.
    # Doctor --fix stays non-eager too: it repairs a broken venv, and pulling
    # every transitive dep forward mid-repair changes more than the fault.
    cmd = [str(venv_pip), "install", "-e", "."]
    if eager:
        cmd += ["--upgrade", "--upgrade-strategy", "eager"]

    # Every failure path below has to go through this. Not writing the stamp is
    # enough only when it is absent or older than pyproject.toml; a *forced*
    # install runs regardless of the stamp, so it can fail against one that is
    # already current from an earlier success, and leaving it alone records the
    # failure as done. The next start then reads "up to date" and skips, which
    # also makes the "starting Quern again will retry automatically" message
    # printed below false.
    #
    # It matters most for the eager path, which moves the whole transitive tree:
    # a failure part-way through can leave a partially upgraded venv marked
    # complete. A timeout is the likeliest way to get there, which is exactly
    # the branch the first version of this fix missed -- hence one helper rather
    # than the same unlink repeated per path.
    def failed() -> bool:
        (project_root / ".venv" / DEPS_STAMP_NAME).unlink(missing_ok=True)
        return False

    try:
        result = subprocess.run(
            cmd,
            cwd=str(project_root), capture_output=True, text=True, timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # The two base classes, not a list of the ones seen so far. Naming
        # concrete exceptions cost three rounds here: FileNotFoundError alone
        # missed TimeoutExpired, and adding that missed PermissionError -- which
        # a non-executable .venv/bin/pip raises, and which propagated uncaught
        # into the start path rather than merely leaving a stale stamp.
        # OSError covers the whole errno family; SubprocessError covers
        # TimeoutExpired and CalledProcessError.
        print(f"Error: dependency install failed: {exc}")
        return failed()

    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        print("Error: dependency install failed. Quern will start, but features "
              "needing the missing packages will fail.")
        if err:
            print(f"  {err.splitlines()[-1][:200]}")
        print("  This is usually a network problem. Once it is reachable, "
              "starting Quern again will retry automatically.")
        return failed()

    # Only on success — a failed install must not look done to the next start.
    (project_root / ".venv" / DEPS_STAMP_NAME).touch()
    if not quiet:
        print("Python dependencies up to date.")
    return True


def _ensure_mcp_built(quiet: bool = False) -> bool:
    """Build the MCP TypeScript server.

    Always runs ``npm run build`` to ensure dist/ is up to date.
    Runs ``npm install`` first when node_modules/ is missing or
    package.json is newer than node_modules/.

    Args:
        quiet: When True, only print on actual build or failure.

    Returns:
        True if the build succeeded, False on failure.
    """
    import subprocess

    project_root = _find_project_root()
    if project_root is None:
        if not quiet:
            print("Warning: could not find project root — skipping MCP build")
        return False

    mcp_dir = project_root / "mcp"
    src_dir = mcp_dir / "src"
    dist_file = mcp_dir / "dist" / "index.js"

    if not src_dir.exists():
        if not quiet:
            print("Warning: mcp/src/ not found — skipping MCP build")
        return False

    # Install node_modules if missing or stale (package.json newer than sentinel file)
    node_modules = mcp_dir / "node_modules"
    stamp = node_modules / ".install-stamp"
    pkg_json = mcp_dir / "package.json"
    needs_install = (
        not node_modules.exists()
        or not stamp.exists()
        or (pkg_json.exists() and pkg_json.stat().st_mtime > stamp.stat().st_mtime)
    )
    if needs_install:
        if not quiet:
            print("Installing MCP server dependencies...")
        result = subprocess.run(
            ["npm", "install", "--prefer-offline"], cwd=str(mcp_dir), timeout=120,
            capture_output=quiet,
        )
        if result.returncode != 0:
            print("Error: npm install failed for MCP server")
            return False
        stamp.touch()

    # Build only if dist is missing or any source file is newer than dist/index.js
    needs_build = not dist_file.exists()
    if not needs_build:
        dist_mtime = dist_file.stat().st_mtime
        for src_file in src_dir.rglob("*"):
            if src_file.is_file() and src_file.stat().st_mtime > dist_mtime:
                needs_build = True
                break

    if needs_build:
        if not quiet:
            print("Building MCP server...")
        result = subprocess.run(
            ["npm", "run", "build"], cwd=str(mcp_dir), timeout=60,
            capture_output=quiet,
        )
        if result.returncode != 0:
            print("Error: npm run build failed for MCP server")
            return False
        if not quiet:
            print("MCP server built successfully")
    elif not quiet:
        print("MCP server up to date")

    return True


def _install_json_mcpservers(config_path: Path, mcp_index: Path) -> tuple[bool, str]:
    """Install quern-debug into a config file that uses the mcpServers JSON format.

    Used by claude-code, claude-desktop, and cursor.
    Deep-merges — all other keys in the config are preserved.
    Creates the file (and parent dirs) if missing.
    """
    import json

    config_path.parent.mkdir(parents=True, exist_ok=True)

    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
        except (json.JSONDecodeError, ValueError):
            return False, f"Error: {config_path} contains invalid JSON"
    else:
        config = {}

    if "mcpServers" not in config:
        config["mcpServers"] = {}

    existing = config["mcpServers"].get("quern-debug")
    config["mcpServers"]["quern-debug"] = {
        "command": "node",
        "args": [str(mcp_index)],
    }

    config_path.write_text(json.dumps(config, indent=2) + "\n")

    verb = "Updated" if existing else "Added"
    return True, f"{verb} quern-debug in {config_path}"


def _install_opencode(mcp_index: Path) -> tuple[bool, str]:
    """Install quern into ~/.config/opencode/opencode.json."""
    import json

    config_path = Path.home() / ".config" / "opencode" / "opencode.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
        except (json.JSONDecodeError, ValueError):
            return False, f"Error: {config_path} contains invalid JSON"
    else:
        config = {}

    if "mcp" not in config:
        config["mcp"] = {}

    existing = config["mcp"].get("quern")
    config["mcp"]["quern"] = {
        "type": "local",
        "command": ["node", str(mcp_index)],
    }

    config_path.write_text(json.dumps(config, indent=2) + "\n")

    verb = "Updated" if existing else "Added"
    return True, f"{verb} quern in {config_path}"


def _toml_upsert_section(text: str, section: str, fields: dict) -> str:
    """Insert or replace a TOML section using text manipulation.

    If the section header exists, replaces content from that line until
    the next section header (or EOF). If not found, appends at end.
    """
    header = f"[{section}]"
    lines = text.splitlines(keepends=True)

    # Build replacement block
    field_lines = [f"{k} = {v}\n" for k, v in fields.items()]
    block = [header + "\n"] + field_lines

    # Find the section
    start = None
    for i, line in enumerate(lines):
        if line.strip() == header:
            start = i
            break

    if start is None:
        # Append — ensure there's a blank line separator
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        if lines and lines[-1].strip():
            lines.append("\n")
        lines.extend(block)
        return "".join(lines)

    # Find end of existing section (next header or EOF)
    end = len(lines)
    for i in range(start + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped.startswith("[") and not stripped.startswith("[["):
            end = i
            break

    lines[start:end] = block + ["\n"]
    return "".join(lines)


def _install_codex(mcp_index: Path) -> tuple[bool, str]:
    """Install quern into ~/.codex/config.toml."""
    config_path = Path.home() / ".codex" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    existing_text = config_path.read_text() if config_path.exists() else ""
    existing = "[mcp_servers.quern]" in existing_text

    fields = {
        "command": f'"{str(mcp_index)}"',
        "args": "[]",
        "enabled": "true",
    }
    new_text = _toml_upsert_section(existing_text, "mcp_servers.quern", fields)
    config_path.write_text(new_text)

    verb = "Updated" if existing else "Added"
    return True, f"{verb} quern in {config_path}"


def _cmd_grant_full_perms() -> int:
    """Add a wildcard allow permission for quern-debug tools to Claude Code user settings."""
    import json

    settings_path = Path.home() / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    if settings_path.exists():
        try:
            config = json.loads(settings_path.read_text())
        except (json.JSONDecodeError, ValueError):
            print(f"Error: {settings_path} contains invalid JSON")
            return 1
    else:
        config = {}

    if "permissions" not in config:
        config["permissions"] = {}
    if "allow" not in config["permissions"]:
        config["permissions"]["allow"] = []

    rule = "mcp__quern-debug"
    allow_list: list[str] = config["permissions"]["allow"]
    if rule in allow_list:
        print(f"  Already granted: {rule} is in {settings_path}")
        return 0

    allow_list.append(rule)
    settings_path.write_text(json.dumps(config, indent=2) + "\n")
    print(f"  ✓ Added '{rule}' to permissions.allow in {settings_path}")
    print("  All quern-debug MCP tools will now run without prompting.")
    return 0


def _cmd_set_channel(args: list[str]) -> int:
    """Persist the update channel preference (``stable`` or ``beta``).

    Usage:
        quern set-channel <name>
        quern set-channel            # print the current channel

    Setting the channel only updates ``~/.quern/config.json``; it does
    not switch git branches or apply an update. Run ``quern update``
    afterwards (and, on a dev clone, switch branches manually) to pick
    up the new channel's content.
    """
    from server.config import (
        VALID_UPDATE_CHANNELS,
        channel_to_release_branch,
        get_update_channel,
        set_update_channel,
    )

    if not args:
        current = get_update_channel()
        branch = channel_to_release_branch(current)
        print(f"Current update channel: {current} (tracks origin/{branch})")
        print(f"Valid channels: {', '.join(VALID_UPDATE_CHANNELS)}")
        return 0

    target = args[0]
    try:
        set_update_channel(target)
    except ValueError as e:
        print(f"Error: {e}")
        return 1

    branch = channel_to_release_branch(target)
    print(f"Update channel set to: {target} (tracks origin/{branch})")
    print("Run `quern update` to apply changes from this channel.")
    return 0


def _cmd_install_precommit_hook() -> int:
    """Install the pre-commit checklist hook into ~/.claude/settings.json.

    Standalone subcommand for re-running the install (e.g. after updating
    Quern, or to refresh the script content). The same install runs
    automatically as part of `quern setup`.
    """
    project_root = _find_project_root()
    if project_root is None:
        print("Error: could not find project root (no pyproject.toml in ancestors)")
        return 1

    from server.lifecycle.setup import CheckStatus, _install_precommit_hook

    result = _install_precommit_hook(project_root)
    icon = {
        CheckStatus.OK: "✓",
        CheckStatus.WARNING: "⚠",
        CheckStatus.ERROR: "✗",
        CheckStatus.MISSING: "?",
        CheckStatus.SKIPPED: "—",
    }.get(result.status, "?")
    print(f"  {icon} {result.message}")
    if result.detail:
        print(f"    {result.detail}")
    return 0 if result.status != CheckStatus.ERROR else 1


def _cmd_mcp_install() -> int:
    """Add quern-debug MCP server to one or more AI tool configs."""
    import argparse

    ALL_TARGETS = ["claude-code", "claude-desktop", "opencode", "codex", "cursor"]

    parser = argparse.ArgumentParser(
        prog="quern mcp-install",
        description="Install the Quern MCP server into AI coding tools.",
    )
    parser.add_argument(
        "targets",
        nargs="*",
        default=["claude-code"],
        metavar="TARGET",
        help=f"Targets to install into: {', '.join(ALL_TARGETS)}, all (default: claude-code)",
    )
    args = parser.parse_args(sys.argv[2:])

    # Expand "all"
    targets: list[str] = []
    for t in args.targets:
        if t == "all":
            targets.extend(ALL_TARGETS)
        elif t in ALL_TARGETS:
            targets.append(t)
        else:
            print(f"Error: unknown target {t!r}. Valid targets: {', '.join(ALL_TARGETS)}, all")
            return 1
    # Deduplicate while preserving order
    seen: set[str] = set()
    targets = [t for t in targets if not (t in seen or seen.add(t))]  # type: ignore[func-returns-value]

    project_root = _find_project_root()
    if project_root is None:
        print("Error: could not find project root")
        return 1

    mcp_index = project_root / "mcp" / "dist" / "index.js"

    # Build the MCP server
    if not _ensure_mcp_built(quiet=False):
        return 1

    CLAUDE_DESKTOP_CONFIG = (
        Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    )

    dispatch = {
        "claude-code":    lambda: _install_json_mcpservers(Path.home() / ".claude.json", mcp_index),
        "claude-desktop": lambda: _install_json_mcpservers(CLAUDE_DESKTOP_CONFIG, mcp_index),
        "cursor":         lambda: _install_json_mcpservers(
            Path.home() / ".cursor" / "mcp.json", mcp_index,
        ),
        "opencode":       lambda: _install_opencode(mcp_index),
        "codex":          lambda: _install_codex(mcp_index),
    }

    all_ok = True
    for target in targets:
        ok, message = dispatch[target]()
        status = "✓" if ok else "✗"
        print(f"  {status} {target}: {message}")
        if not ok:
            all_ok = False

    return 0 if all_ok else 1


def main() -> None:
    _maybe_reexec_in_venv()

    # Version flag — handle before anything else
    if len(sys.argv) >= 2 and sys.argv[1] in ("--version", "-V", "version"):
        from server import get_version
        print(f"quern {get_version()}")
        sys.exit(0)

    # Lightweight commands — handle without heavy imports
    if len(sys.argv) >= 2 and sys.argv[1] == "setup":
        from server.lifecycle.setup import run_setup
        sys.exit(run_setup())

    if len(sys.argv) >= 2 and sys.argv[1] == "uninstall":
        from server.lifecycle.setup import run_uninstall
        sys.exit(run_uninstall())

    if len(sys.argv) >= 2 and sys.argv[1] == "mcp-install":
        sys.exit(_cmd_mcp_install())

    if len(sys.argv) >= 2 and sys.argv[1] == "grant-full-perms":
        sys.exit(_cmd_grant_full_perms())

    if len(sys.argv) >= 2 and sys.argv[1] == "install-precommit-hook":
        sys.exit(_cmd_install_precommit_hook())

    if len(sys.argv) >= 2 and sys.argv[1] == "update":
        from server.lifecycle.updater import run_update
        sys.exit(run_update(apply_tools="--tools" in sys.argv[2:]))

    if len(sys.argv) >= 2 and sys.argv[1] == "set-channel":
        sys.exit(_cmd_set_channel(sys.argv[2:]))

    if len(sys.argv) >= 2 and sys.argv[1] == "tunneld":
        from server.device.tunneld import cli_tunneld
        sys.exit(cli_tunneld(sys.argv[2:]))

    # All other commands need the full server stack
    from server.main import cli
    cli()


if __name__ == "__main__":
    main()
