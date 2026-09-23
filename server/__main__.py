"""Lightweight CLI bootstrap for quern-debug-server.

Handles venv auto-detection and the `setup` command without importing
the full server stack, so `setup` works on a fresh clone before
dependencies are installed.

For all other commands, delegates to server.main.cli().
"""

from __future__ import annotations

import os
import shlex
import sys
from collections.abc import Callable
from pathlib import Path

from server.lifecycle.stale_modules import refresh_if_stale

# An updater from 0.18.3 or older imports this file right after swapping the
# source tree, into a process still holding the previous release's modules;
# the functions below then import from them lazily (#212). No-op otherwise.
refresh_if_stale()


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
    """Ensure ``mcp/dist/index.js`` is current, building it if it is not.

    Decides whether a build is needed *before* reaching for npm, and returns
    early when it is not. Release tarballs now ship a prebuilt ``dist/``, so for
    a tarball install the answer is normally "nothing to do" and npm is never
    invoked -- which matters because npm is frequently unreachable from the
    process that calls this. The menubar app launches the server from a GUI
    context, which inherits launchd's minimal PATH rather than a shell's, and a
    node installed by fnm or nvm lives in a directory no static PATH list can
    name: fnm's contains the pid of the shell that asked for it. `quern setup`
    records node's path as None for exactly that reason. So "just add it to the
    search path" is not available as a fix.

    Never raises. Every caller already treats a failed build as survivable --
    MCP tools go stale, the server still runs -- but that intent only worked for
    the failures this function anticipated. A *missing* npm raised instead of
    returning False, which crashed `quern start` outright, and the guard for it
    was added at one of three call sites rather than here. See #193.

    Args:
        quiet: When True, only print on actual build or failure.

    Returns:
        True if dist/ is current (or was made current), False on failure.
    """
    import subprocess

    project_root = _find_project_root()
    if project_root is None:
        if not quiet:
            print("Warning: could not find project root — skipping MCP build")
        return False

    mcp_dir = project_root / "mcp"
    src_dir = mcp_dir / "src"
    #: Both are shipped and both are used: `index.js` is the ESM entry, and
    #: `launcher.cjs` is what MCP clients are registered on. Checking only the
    #: first would let a dist/ missing the launcher read as current, and
    #: `mcp-install` would then write a config pointing at a file that is not
    #: there.
    dist_files = [mcp_dir / "dist" / "index.js", mcp_dir / "dist" / "launcher.cjs"]

    if not src_dir.exists():
        if not quiet:
            print("Warning: mcp/src/ not found — skipping MCP build")
        return False

    #: Everything the build reads. `src/` is the obvious one; the rest change
    #: the output without changing a single source file -- a new dependency, a
    #: different compile target -- and a cache keyed on `src/` alone serves a
    #: stale dist/ after any of them moves.
    build_inputs = [
        mcp_dir / "package.json",
        mcp_dir / "package-lock.json",
        mcp_dir / "tsconfig.json",
    ]

    # Is a build needed at all? Asked first, because answering "no" is what lets
    # a tarball install start on a machine with no reachable npm.
    needs_build = not all(f.exists() for f in dist_files)
    if not needs_build:
        oldest_output = min(f.stat().st_mtime for f in dist_files)
        inputs = [f for f in build_inputs if f.exists()]
        inputs += [f for f in src_dir.rglob("*") if f.is_file()]
        # `>=`, not `>`. Filesystem timestamps are coarse enough that an input
        # written in the same second as the output is a real outcome, and
        # treating that as current is how a cache serves a stale build. Erring
        # the other way now only costs a rebuild attempt, because a build that
        # cannot run is reported rather than fatal.
        needs_build = any(f.stat().st_mtime >= oldest_output for f in inputs)

    if not needs_build:
        if not quiet:
            print("MCP server up to date")
        return True

    # From here npm is required. Anything it does wrong is reported, not raised:
    # a missing binary is OSError, a slow install is TimeoutExpired, and neither
    # is a reason for the server to fail to start.
    node_modules = mcp_dir / "node_modules"
    stamp = node_modules / ".install-stamp"
    # Both manifests, not just package.json. A lockfile-only change -- the usual
    # shape of a dependency bump -- would otherwise trigger a rebuild without a
    # reinstall, and `npm run build` does not install anything, so `tsc` would
    # compile against the previous modules.
    manifests = [mcp_dir / "package.json", mcp_dir / "package-lock.json"]
    needs_install = (
        not node_modules.exists()
        or not stamp.exists()
        or any(
            f.stat().st_mtime >= stamp.stat().st_mtime
            for f in manifests if f.exists()
        )
    )

    try:
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

        if not quiet:
            print("Building MCP server...")
        result = subprocess.run(
            ["npm", "run", "build"], cwd=str(mcp_dir), timeout=60,
            capture_output=quiet,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # OSError covers the one that reached a user: npm absent from a
        # GUI-launched process's PATH, which is FileNotFoundError.
        print(f"Error: could not build the MCP server: {exc}")
        if not all(f.exists() for f in dist_files):
            print(
                "  MCP tools will be unavailable until this is built. Node 22+ "
                "and npm are needed, and a GUI launch may not see a node "
                "installed by fnm or nvm — running `quern start` from a "
                "terminal once is usually enough."
            )
        return False

    if result.returncode != 0:
        print("Error: npm run build failed for MCP server")
        return False
    if not quiet:
        print("MCP server built successfully")
    return True


def _install_json_mcpservers(config_path: Path, mcp_entry: Path) -> tuple[bool, str]:
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
        "args": [str(mcp_entry)],
    }

    config_path.write_text(json.dumps(config, indent=2) + "\n")

    verb = "Updated" if existing else "Added"
    return True, f"{verb} quern-debug in {config_path}"


def _install_opencode(mcp_entry: Path) -> tuple[bool, str]:
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
        "command": ["node", str(mcp_entry)],
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


def _install_codex(mcp_entry: Path) -> tuple[bool, str]:
    """Install quern into ~/.codex/config.toml."""
    config_path = Path.home() / ".codex" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    existing_text = config_path.read_text() if config_path.exists() else ""
    existing = "[mcp_servers.quern]" in existing_text

    fields = {
        "command": f'"{str(mcp_entry)}"',
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


def _cmd_set_auto_install_cert(args: list[str]) -> int:
    """Read or set the automatic CA-install policy.

    Usage:
        quern set-auto-install-cert on|off
        quern set-auto-install-cert          # print the current setting

    Capturing HTTPS from a device that does not trust the mitmproxy CA fails
    every request, and the symptom points nowhere near the proxy. Quern
    normally refuses to configure the proxy in that state and asks. Turning
    this on answers the question once, in advance.

    It is off by default because installing a MITM root CA is a larger and
    longer-lived commitment than the proxy toggle that prompts it.
    """
    from server.config import get_auto_install_cert, set_auto_install_cert

    if not args:
        state = "on" if get_auto_install_cert() else "off"
        print(f"Automatic certificate install: {state}")
        if state == "off":
            print("Quern will ask before installing the mitmproxy CA on a device.")
        else:
            print("Quern will install the mitmproxy CA when capture needs it.")
        return 0

    target = args[0].lower()
    if target in ("on", "true", "yes", "1"):
        set_auto_install_cert(True)
        print("Automatic certificate install: on")
        print("Quern will install the mitmproxy CA when capture needs it.")
        return 0
    if target in ("off", "false", "no", "0"):
        set_auto_install_cert(False)
        print("Automatic certificate install: off")
        return 0

    print(f"Unknown value {args[0]!r}. Use 'on' or 'off'.", file=sys.stderr)
    return 2


def _cmd_set_update_check(args: list[str]) -> int:
    """Read or set whether quern checks for updates on its own.

    Usage:
        quern set-update-check on|off
        quern set-update-check          # print the current setting

    Governs the *automatic* check alone. `quern check-updates` and the menu
    bar's Check for Updates keep working when this is off, the way every other
    updater leaves Check Now working when the box is unticked: turning off
    automatic checking says "do not call home unprompted", and asking is a
    prompt.

    On by default, unlike `set-auto-install-cert`. The asymmetry is deliberate
    -- the cost of guessing wrong there is a root CA installed without consent,
    and here it is one HTTPS request a day.
    """
    from server.config import get_update_check, set_update_check

    if not args:
        state = "on" if get_update_check() else "off"
        print(f"Automatic update check: {state}")
        if state == "off":
            print("Run `quern check-updates` to check now.")
        return 0

    target = args[0].lower()
    if target in ("on", "true", "yes", "1"):
        set_update_check(True)
        print("Automatic update check: on")
        return 0
    if target in ("off", "false", "no", "0"):
        set_update_check(False)
        print("Automatic update check: off")
        print("Run `quern check-updates` to check now.")
        return 0

    print(f"Unknown value {args[0]!r}. Use 'on' or 'off'.", file=sys.stderr)
    return 2


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
    )

    if not args:
        current = get_update_channel()
        branch = channel_to_release_branch(current)
        print(f"Current update channel: {current} (tracks origin/{branch})")
        print(f"Valid channels: {', '.join(VALID_UPDATE_CHANNELS)}")
        return 0

    target = args[0]
    # switch_channel persists the preference and drops the cached check as one
    # locked operation, so the next check is asked afresh instead of waiting
    # out the 24h rate limit, and a check already in flight cannot put the old
    # channel's answer back afterwards.
    from server.lifecycle.update_check import switch_channel

    try:
        switch_channel(target)
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

    # The launcher, not the ESM entry. `launcher.cjs` is CommonJS on purpose so
    # it parses on ancient Node, checks the major version, and prints which
    # binary it is running under and how to point the client at a newer one.
    # Registering `index.js` bypassed that gate entirely: a too-old Node gave a
    # raw ESM syntax error instead. That was survivable while everyone built
    # dist/ locally -- having built it proved a working Node -- but tarballs now
    # ship it prebuilt, so the first Node to meet this file may be the wrong one.
    mcp_entry = project_root / "mcp" / "dist" / "launcher.cjs"

    # Build the MCP server
    if not _ensure_mcp_built(quiet=False):
        return 1

    CLAUDE_DESKTOP_CONFIG = (
        Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    )

    dispatch = {
        "claude-code":    lambda: _install_json_mcpservers(Path.home() / ".claude.json", mcp_entry),
        "claude-desktop": lambda: _install_json_mcpservers(CLAUDE_DESKTOP_CONFIG, mcp_entry),
        "cursor":         lambda: _install_json_mcpservers(
            Path.home() / ".cursor" / "mcp.json", mcp_entry,
        ),
        "opencode":       lambda: _install_opencode(mcp_entry),
        "codex":          lambda: _install_codex(mcp_entry),
    }

    all_ok = True
    for target in targets:
        ok, message = dispatch[target]()
        status = "✓" if ok else "✗"
        print(f"  {status} {target}: {message}")
        if not ok:
            all_ok = False

    return 0 if all_ok else 1


def _check_args(
    command: str,
    rest: list[str],
    *,
    allowed: tuple[str, ...] = (),
    operands: int = 0,
    usage: Callable[[], None] | None = None,
) -> list[str]:
    """Answer `-h`, refuse anything else unrecognised, return the operands.

    These commands are dispatched before argparse (see the comment at the top
    of `main`), and each one used to drop `sys.argv[2:]` wholesale. So
    `quern mcp-install --help` rewrote every MCP client config, `quern update
    --help` ran a real update, and a mistyped flag did whatever the command
    does with no flag at all -- silently, while the caller believed they had
    asked for something else. `capture-env` was fixed for this first; doing it
    once here is what stops the next command being added without it.
    """
    def default_usage() -> None:
        flags = "".join(f" [{flag}]" for flag in allowed)
        args = "".join(" <value>" for _ in range(operands))
        print(f"Usage: quern {command}{flags}{args}")

    show = usage or default_usage
    if any(arg in ("-h", "--help") for arg in rest):
        show()
        sys.exit(0)
    unknown = [arg for arg in rest if arg.startswith("-") and arg not in allowed]
    if unknown:
        show()
        print(f"unrecognised option: {unknown[0]}", file=sys.stderr)
        sys.exit(2)
    # Operands too, not just flags. Returning them and leaving each caller to
    # ignore them is the same silent drop one level down: `quern update typo`
    # ran a real update, and `quern set-channel stable typo` persisted stable
    # while saying nothing about the word it did not understand. A command
    # that takes no operands says so; one that takes a value says how many.
    ops = [arg for arg in rest if not arg.startswith("-")]
    if len(ops) > operands:
        show()
        print(f"unexpected argument: {ops[operands]}", file=sys.stderr)
        sys.exit(2)
    return ops


def _valid_port(value: object) -> int | None:
    """`value` if it is a usable TCP port, else None.

    `state.json` is a file on disk and can hold anything, including the
    remains of a server that is long gone. `isinstance(v, int)` is not the
    test: `True` passes it, because bool subclasses int, and so do 0 and
    70000. A port nothing can connect to must not reach a URL a script is
    about to trust.
    """
    if type(value) is not int or not (1 <= value <= 65535):
        return None
    return value


def _server_base_url() -> str | None:
    """Where a client on this machine should talk to the running server.

    Loopback, never the bind host. The server listens on 0.0.0.0 by default,
    and a script on this machine should not be sent out to the network and
    back. The MCP wrapper builds its URL the same way.

    None when nothing is actually answering. A readable state file is not a
    running server: a crash or a SIGKILL leaves the file behind, and without
    the health check `quern url` would exit 0 and hand a script a URL that
    refuses connections -- which is worse than the hardcoded 9100 these
    commands replaced, because it looks authoritative.
    """
    from server.lifecycle.ports import _get_pid_on_port
    from server.lifecycle.state import is_server_healthy, read_state

    state = read_state()
    if not state:
        return None
    port = _valid_port(state.get("server_port"))
    if port is None or not is_server_healthy(port):
        return None

    # Answering /health is not proof of being ours, and `quern env` prints the
    # API key. Quern records the port it settled on, which is not 9100 when
    # something else had that first -- so if quern then dies, an untrusted
    # local process can take the freed port, answer 200, and be handed the key
    # by a caller that only checked for a pulse.
    #
    # The recorded pid is the thing an impostor does not control. Anyone who
    # can rewrite state.json can read ~/.quern/api-key directly, so this is
    # not the weak link.
    recorded = state.get("pid")
    if isinstance(recorded, int):
        listener = _get_pid_on_port(port)
        if listener is not None and listener != recorded:
            return None
    return f"http://127.0.0.1:{port}"


def _url_usage() -> None:
    print("Usage: quern url")
    print()
    print("Prints the running server's base URL, for scripts:")
    print()
    print("    BASE=$(quern url) || exit 1")


def _cmd_url() -> int:
    url = _server_base_url()
    if url is None:
        print("No server answering — start it with `quern start`.", file=sys.stderr)
        return 1
    print(url)
    return 0


def _env_usage() -> None:
    print("Usage: quern env")
    print()
    print("Prints shell exports for the running server, so a script never has")
    print("to hardcode a port or read ~/.quern by hand:")
    print()
    print('    eval "$(quern env)"')
    print('    curl -H "Authorization: Bearer $QUERN_API_KEY" "$QUERN_SERVER_URL/health"')


def _cmd_env() -> int:
    """Emit the server's URL and API key as shell exports.

    The `eval "$(quern env)"` shape, as `fnm env` and `docker-machine env`
    use it: computed when it is asked for, so it cannot go stale the way a
    file written at start-up would once the server moved to another port.

    Nothing goes to stdout when there is no server. A partial environment is
    worse than none -- `eval` would set half of it and the script would fail
    later, somewhere unrelated.
    """
    from server.config import API_KEY_FILE

    url = _server_base_url()
    if url is None:
        print("No server answering — start it with `quern start`.", file=sys.stderr)
        return 1

    try:
        key = API_KEY_FILE.read_text().strip()
    except OSError:
        key = ""
    if not key:
        print(f"No API key at {API_KEY_FILE} — run `quern setup`.", file=sys.stderr)
        return 1

    print(f"export QUERN_SERVER_URL={shlex.quote(url)}")
    print(f"export QUERN_API_KEY={shlex.quote(key)}")
    return 0


def _setup_usage() -> None:
    print("Usage: quern setup [-y|--yes]")
    print()
    print("Checks the environment and installs what is missing.")
    print()
    print("  -y, --yes   Answer prompts with their default, for an unattended")
    print("              run. Prompts that must be made deliberately -- ")
    print("              installing a certificate authority -- are still")
    print("              declined and listed at the end.")


def _capture_env_usage() -> None:
    print("Usage: quern capture-env [FILE]")
    print()
    print("Writes an environment report for attaching to a bug report.")
    print("With no FILE, prints to stdout.")


def main() -> None:
    # Before the re-exec, deliberately, and the ordering is the whole point:
    # this is the command people reach for when quern is broken, and
    # `_maybe_reexec_in_venv` is one of the things that can be broken. A venv
    # missing its pyvenv.cfg leaves `.venv/bin/python` executable but not
    # recognised as a venv, so that function execs itself forever -- and the
    # diagnostic would spin instead of producing a report, on exactly the
    # install it exists for. It was below this line, with a comment claiming it
    # was above.
    if len(sys.argv) >= 2 and sys.argv[1] == "capture-env":
        from server.lifecycle.capture_env import run
        # Anything after the subcommand is a destination, so a flag would be
        # taken as a filename: `capture-env --help` wrote a file called
        # "--help" and exited 0.
        rest = sys.argv[2:]
        if any(a in ("-h", "--help") for a in rest):
            # Checked before the flag rejection below: `capture-env out.json
            # --help` reported "unrecognised option: --help", which it is not.
            _capture_env_usage()
            sys.exit(0)
        if any(arg.startswith("-") for arg in rest):
            _capture_env_usage()
            bad = next(a for a in rest if a.startswith("-"))
            print(f"unrecognised option: {bad}", file=sys.stderr)
            sys.exit(2)
        if len(rest) > 1:
            # Silently writing the first and ignoring the rest is the wrong
            # kind of forgiving for a command whose output someone is about to
            # attach to a bug report.
            print(f"Expected at most one FILE, got {len(rest)}.", file=sys.stderr)
            sys.exit(2)
        sys.exit(run(rest[0] if rest else None))

    _maybe_reexec_in_venv()

    # Version flag — handle before anything else
    if len(sys.argv) >= 2 and sys.argv[1] in ("--version", "-V", "version"):
        from server import get_version
        print(f"quern {get_version()}")
        sys.exit(0)

    # Lightweight commands — handle without heavy imports
    if len(sys.argv) >= 2 and sys.argv[1] == "setup":
        # Parsed rather than ignored, for the reason `capture-env` above states
        # about its own arguments: everything after the subcommand used to be
        # dropped, so `setup --help` ran a full setup instead of printing help,
        # and a mistyped flag did the same. A flag that is silently ignored is
        # worse than one that does not exist -- the caller believes they opted
        # in. This is dispatched before argparse (see the comment at the top of
        # `main`), so the parsing has to be here.
        rest = sys.argv[2:]
        _check_args("setup", rest, allowed=("-y", "--yes"), usage=_setup_usage)
        from server.lifecycle.setup import run_setup
        sys.exit(run_setup(assume_yes=bool({"-y", "--yes"} & set(rest))))

    if len(sys.argv) >= 2 and sys.argv[1] == "url":
        _check_args("url", sys.argv[2:], usage=_url_usage)
        sys.exit(_cmd_url())

    if len(sys.argv) >= 2 and sys.argv[1] == "env":
        _check_args("env", sys.argv[2:], usage=_env_usage)
        sys.exit(_cmd_env())

    if len(sys.argv) >= 2 and sys.argv[1] == "uninstall":
        _check_args("uninstall", sys.argv[2:])
        from server.lifecycle.setup import run_uninstall
        sys.exit(run_uninstall())

    if len(sys.argv) >= 2 and sys.argv[1] == "mcp-install":
        _check_args("mcp-install", sys.argv[2:])
        sys.exit(_cmd_mcp_install())

    if len(sys.argv) >= 2 and sys.argv[1] == "grant-full-perms":
        _check_args("grant-full-perms", sys.argv[2:])
        sys.exit(_cmd_grant_full_perms())

    if len(sys.argv) >= 2 and sys.argv[1] == "menubar":
        from server.lifecycle.menubar import main as menubar_main
        # Read here, like --tools, so tests/test_readme_sync.py sees the flag.
        force = "--force" in sys.argv[2:]
        rest = [a for a in sys.argv[2:] if a != "--force"]
        sys.exit(menubar_main(rest, force=force))

    if len(sys.argv) >= 2 and sys.argv[1] == "install-precommit-hook":
        _check_args("install-precommit-hook", sys.argv[2:])
        sys.exit(_cmd_install_precommit_hook())

    if len(sys.argv) >= 2 and sys.argv[1] == "update":
        from server.lifecycle.updater import FINISH_FLAG, finish_update, run_update
        _check_args("update", sys.argv[2:], allowed=("--tools", FINISH_FLAG))
        apply_tools = "--tools" in sys.argv[2:]
        if FINISH_FLAG in sys.argv[2:]:
            sys.exit(finish_update(apply_tools=apply_tools))
        sys.exit(run_update(apply_tools=apply_tools))

    # `help` is what people type. argparse only understands -h/--help, so
    # without this the most obvious command in the tool exits 2 with an
    # "invalid choice" error.
    if len(sys.argv) >= 2 and sys.argv[1] in ("help", "--help", "-h"):
        from server.main import cli
        sys.argv = [sys.argv[0], "--help"]
        cli()
        return

    # One operand each -- the value being set. Each helper reads only its
    # first argument, so without this a second word was persisted-and-ignored.
    if len(sys.argv) >= 2 and sys.argv[1] == "set-channel":
        sys.exit(_cmd_set_channel(_check_args("set-channel", sys.argv[2:], operands=1)))

    if len(sys.argv) >= 2 and sys.argv[1] == "set-auto-install-cert":
        sys.exit(_cmd_set_auto_install_cert(
            _check_args("set-auto-install-cert", sys.argv[2:], operands=1)))

    if len(sys.argv) >= 2 and sys.argv[1] == "set-update-check":
        sys.exit(_cmd_set_update_check(
            _check_args("set-update-check", sys.argv[2:], operands=1)))

    if len(sys.argv) >= 2 and sys.argv[1] == "tunneld":
        from server.device.tunneld import cli_tunneld
        sys.exit(cli_tunneld(sys.argv[2:]))

    # All other commands need the full server stack
    from server.main import cli
    cli()


if __name__ == "__main__":
    main()
