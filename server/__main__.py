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
        "cursor":         lambda: _install_json_mcpservers(Path.home() / ".cursor" / "mcp.json", mcp_index),
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
