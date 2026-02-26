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

    # Install node_modules if missing or stale (package.json newer than node_modules/)
    node_modules = mcp_dir / "node_modules"
    needs_install = not node_modules.exists()
    if not needs_install:
        pkg_json = mcp_dir / "package.json"
        if pkg_json.exists() and pkg_json.stat().st_mtime > node_modules.stat().st_mtime:
            needs_install = True
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

    # Build
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
    return True


def _cmd_mcp_install() -> int:
    """Add quern-debug MCP server to ~/.claude.json."""
    import json

    project_root = _find_project_root()
    if project_root is None:
        print("Error: could not find project root")
        return 1

    mcp_dir = project_root / "mcp"
    mcp_index = mcp_dir / "dist" / "index.js"

    # Build the MCP server if needed
    if not _ensure_mcp_built(quiet=False):
        return 1

    claude_config = Path.home() / ".claude.json"

    # Read existing config or start fresh
    if claude_config.exists():
        try:
            config = json.loads(claude_config.read_text())
        except (json.JSONDecodeError, ValueError):
            print(f"Error: {claude_config} contains invalid JSON")
            return 1
    else:
        config = {}

    # Add/update the MCP server entry
    if "mcpServers" not in config:
        config["mcpServers"] = {}

    existing = config["mcpServers"].get("quern-debug")
    config["mcpServers"]["quern-debug"] = {
        "command": "node",
        "args": [str(mcp_index)],
    }

    claude_config.write_text(json.dumps(config, indent=2) + "\n")

    if existing:
        print(f"Updated quern-debug MCP server in {claude_config}")
    else:
        print(f"Added quern-debug MCP server to {claude_config}")
    print(f"  command: node")
    print(f"  args: [{mcp_index}]")
    return 0


def main() -> None:
    _maybe_reexec_in_venv()

    # Lightweight commands — handle without heavy imports
    if len(sys.argv) >= 2 and sys.argv[1] == "setup":
        from server.lifecycle.setup import run_setup
        sys.exit(run_setup())

    if len(sys.argv) >= 2 and sys.argv[1] == "mcp-install":
        sys.exit(_cmd_mcp_install())

    if len(sys.argv) >= 2 and sys.argv[1] == "update":
        from server.lifecycle.updater import run_update
        sys.exit(run_update())

    if len(sys.argv) >= 2 and sys.argv[1] == "tunneld":
        from server.device.tunneld import cli_tunneld
        sys.exit(cli_tunneld(sys.argv[2:]))

    # All other commands need the full server stack
    from server.main import cli
    cli()


if __name__ == "__main__":
    main()
