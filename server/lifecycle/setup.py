"""./quern setup — interactive environment checker and installer.

Validates the Python virtual environment, system dependencies, installs
missing tools via Homebrew, and optionally configures simulators for proxy use.

Usage:
    ./quern setup
    ./quern uninstall
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

# ── Result types ──────────────────────────────────────────────────────────

class CheckStatus(Enum):
    OK = "ok"
    WARNING = "warning"
    MISSING = "missing"
    ERROR = "error"
    SKIPPED = "skipped"


@dataclass
class CheckResult:
    name: str
    status: CheckStatus
    message: str
    detail: str = ""
    fixable: bool = False  # can we auto-fix this?

    @property
    def icon(self) -> str:
        return {
            CheckStatus.OK: "✓",
            CheckStatus.WARNING: "⚠",
            CheckStatus.MISSING: "✗",
            CheckStatus.ERROR: "✗",
            CheckStatus.SKIPPED: "–",
        }[self.status]


@dataclass
class SetupReport:
    results: list[CheckResult] = field(default_factory=list)

    def add(self, result: CheckResult) -> None:
        self.results.append(result)

    @property
    def has_errors(self) -> bool:
        return any(r.status in (CheckStatus.MISSING, CheckStatus.ERROR) for r in self.results)

    @property
    def has_warnings(self) -> bool:
        return any(r.status == CheckStatus.WARNING for r in self.results)

    def print_summary(self) -> None:
        print()
        print("─" * 50)
        print("  Quern Setup Summary")
        print("─" * 50)
        for r in self.results:
            line = f"  {r.icon} {r.name}: {r.message}"
            print(line)
            if r.detail:
                for detail_line in r.detail.splitlines():
                    print(f"      {detail_line}")
        print("─" * 50)
        if self.has_errors:
            print("  Some required dependencies are missing.")
            print("  Re-run './quern setup' after resolving them.")
        elif self.has_warnings:
            print("  Setup complete with warnings (see above).")
        else:
            print("  All checks passed — ready to go!")
        if not self.has_errors:
            print()
            print("  Tip: Run 'quern grant-full-perms' to allow all quern")
            print("  tools in Claude Code without per-tool approval prompts.")
        print()


# ── Helpers ───────────────────────────────────────────────────────────────

def _run(cmd: list[str], timeout: int = 30) -> tuple[int, str, str]:
    """Run a command and return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except FileNotFoundError:
        return -1, "", f"Command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return -1, "", f"Command timed out: {' '.join(cmd)}"


def _which(name: str) -> str | None:
    """Find a command on PATH, returning its full path or None."""
    return shutil.which(name)


def _is_apple_silicon() -> bool:
    """True on arm64 Macs (Apple Silicon). Sim-bridge requires it."""
    import platform
    return platform.machine() == "arm64"


def _xcode_major_version() -> int | None:
    """Return Xcode's major version (e.g. 26 for Xcode 26.0), or None.

    Parses the first line of `xcodebuild -version`, which reads
    `Xcode <major>.<minor>`. Returns None if xcodebuild is missing,
    fails, or the output is unrecognized.
    """
    rc, stdout, _ = _run(["xcodebuild", "-version"], timeout=5)
    if rc != 0 or not stdout:
        return None
    first_line = stdout.splitlines()[0]
    parts = first_line.split()
    if len(parts) >= 2 and parts[0] == "Xcode":
        try:
            return int(parts[1].split(".")[0])
        except ValueError:
            return None
    return None


def _sim_bridge_supported() -> bool:
    """True when sim-bridge can run — i.e. idb is not required.

    Sim-bridge needs Apple Silicon and Xcode 26+ (for the SimulatorKit /
    CoreSimulator private symbols it dlopens). When both hold, simulator
    UI automation runs natively and idb is redundant.
    """
    if not _is_apple_silicon():
        return False
    major = _xcode_major_version()
    return major is not None and major >= 26


def _fix_developer_dir_for_setup() -> str | None:
    """Auto-fix DEVELOPER_DIR if xcode-select doesn't provide simctl.

    Sets the DEVELOPER_DIR env var for this process so subsequent xcrun
    calls work. Returns a message describing the fix, or None if not needed.
    """
    if os.environ.get("DEVELOPER_DIR"):
        return None

    # Check if simctl already works
    rc, _, _ = _run(["xcrun", "simctl", "help"])
    if rc == 0:
        return None

    # simctl broken — find a working Xcode
    rc, current_dir, _ = _run(["xcode-select", "-p"])
    current_dir = current_dir.strip() if rc == 0 else "(unknown)"

    for xcode_app in sorted(Path("/Applications").glob("Xcode*.app")):
        candidate = xcode_app / "Contents" / "Developer"
        if candidate.exists():
            os.environ["DEVELOPER_DIR"] = str(candidate)
            rc, _, _ = _run(["xcrun", "simctl", "help"])
            if rc == 0:
                return (
                    f"Xcode developer tools not found at default location ({current_dir}).\n"
                    f"Using {xcode_app} instead.\n"
                    f"To make this permanent: sudo xcode-select -s '{candidate}'"
                )
            del os.environ["DEVELOPER_DIR"]

    return None


def _get_version(cmd: list[str]) -> str | None:
    """Run a version command and extract the version string."""
    rc, stdout, stderr = _run(cmd)
    if rc != 0:
        return None
    # Return first non-empty line (version output varies widely)
    output = stdout or stderr
    for line in output.splitlines():
        line = line.strip()
        if line:
            return line
    return None


INSTALL_MANIFEST = Path.home() / ".quern" / "installed-by-setup.json"


def _read_manifest() -> dict:
    """Read the install manifest (what quern setup has installed)."""
    import json
    try:
        return json.loads(INSTALL_MANIFEST.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"brew": [], "pip": [], "pipx": [], "pipx_global": []}


def _maybe_remove_user_pmd3(pipx_bin: str) -> None:
    """After a successful global install, offer to remove the per-user copy.

    pipx's `ensurepath` puts ~/.local/bin ahead of /usr/local/bin in the
    user's PATH, so a per-user `pymobiledevice3` keeps shadowing the global
    one we just installed. Removing the per-user copy clears the shadow;
    LaunchDaemons run as root and don't inherit the user's PATH, so they
    would already see the global one — but `shutil.which` from a user
    shell wouldn't, which is what `check_pymobiledevice3()` calls.
    """
    user_venv = Path.home() / ".local" / "pipx" / "venvs" / "pymobiledevice3"
    if not user_venv.exists():
        return
    if not _prompt_yn(
        "    Per-user pymobiledevice3 still installed at "
        f"{user_venv}. Remove it so the system-wide copy is used?",
    ):
        return
    try:
        subprocess.run(
            [pipx_bin, "uninstall", "pymobiledevice3"],
            stdin=subprocess.DEVNULL, timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        print("    Warning: pipx uninstall pymobiledevice3 failed")
        return
    # Clean up the manifest entry if setup originally tracked the per-user
    # install. Safe to skip if the user installed it themselves outside setup.
    manifest = _read_manifest()
    if "pymobiledevice3" in manifest.get("pipx", []):
        manifest["pipx"].remove("pymobiledevice3")
        _write_manifest(manifest)


def _home_is_on_external() -> bool:
    """True iff the current user's home resolves under /Volumes/.

    Detects the common "moved my home folder to an external drive" setup,
    where per-user pipx installs end up in /Volumes/<vol>/<user>/.local/pipx/
    — a path that doesn't exist pre-login. We use this to prefer
    `sudo pipx install --global` for tools that need to be reachable by
    LaunchDaemons (currently just pymobiledevice3 for tunneld).
    """
    return str(Path.home().resolve()).startswith("/Volumes/")


def _write_manifest(data: dict) -> None:
    """Write the install manifest."""
    import json
    INSTALL_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    INSTALL_MANIFEST.write_text(json.dumps(data, indent=2) + "\n")


def _record_install(category: str, name: str) -> None:
    """Record that setup installed something."""
    manifest = _read_manifest()
    items = manifest.setdefault(category, [])
    if name not in items:
        items.append(name)
    _write_manifest(manifest)


def _find_brew_binary(name: str) -> str | None:
    """Find a binary by name, checking PATH then Homebrew prefix.

    After a fresh ``brew install`` the binary may not be on the running
    process's PATH yet. This falls back to ``brew --prefix`` to locate it.
    """
    found = _which(name)
    if found:
        return found
    # Ask Homebrew for its top-level prefix (e.g. /opt/homebrew)
    rc, prefix, _ = _run(["brew", "--prefix"])
    if rc == 0 and prefix:
        candidate = Path(prefix.strip()) / "bin" / name
        if candidate.exists():
            return str(candidate)
    return None


def _brew_install(formula: str) -> bool:
    """Install a Homebrew formula. Returns True on success."""
    print(f"    Installing {formula} via Homebrew...")
    try:
        result = subprocess.run(
            ["brew", "install", formula],
            stdin=subprocess.DEVNULL,
            timeout=300,  # 5 min timeout for installs
        )
        if result.returncode == 0:
            _record_install("brew", formula)
            return True
        return False
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _prompt_yn(question: str, default: bool = True) -> bool:
    """Prompt the user for yes/no confirmation.

    When stdin is not a TTY (e.g. ``curl | bash``), reopens /dev/tty so
    interactive prompts still work.
    """
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        if sys.stdin.isatty():
            answer = input(question + suffix).strip().lower()
        else:
            # stdin is a pipe (curl | bash) — read from the real terminal
            tty = open("/dev/tty")
            print(question + suffix, end="", flush=True)
            answer = tty.readline().strip().lower()
            tty.close()
    except (EOFError, KeyboardInterrupt, OSError):
        print()
        return False
    if not answer:
        return default
    return answer in ("y", "yes")


def _detect_shell_rc() -> Path | None:
    """Detect the user's shell config file based on $SHELL."""
    shell = os.environ.get("SHELL", "")
    home = Path.home()

    if "zsh" in shell:
        return home / ".zshrc"
    elif "bash" in shell:
        # Check for .bash_profile first (macOS default), then .bashrc
        bash_profile = home / ".bash_profile"
        if bash_profile.exists():
            return bash_profile
        return home / ".bashrc"
    elif "fish" in shell:
        return home / ".config" / "fish" / "config.fish"

    # Fallback to .zshrc on macOS (most common)
    if platform.system() == "Darwin":
        return home / ".zshrc"

    return None


def _add_to_path(shell_rc: Path, directory: Path) -> bool:
    """Add directory to PATH in shell config file.

    Returns True if added, False if already present or error.
    """
    path_export = 'export PATH="$HOME/.local/bin:$PATH"'

    try:
        # Create parent directory if needed (e.g., ~/.config/fish)
        shell_rc.parent.mkdir(parents=True, exist_ok=True)

        # Check if PATH export already exists
        if shell_rc.exists():
            content = shell_rc.read_text()
            if ".local/bin" in content and "PATH" in content:
                return False  # Already configured

        # Append PATH export with a comment
        with shell_rc.open("a") as f:
            f.write(f"\n# Added by Quern setup\n{path_export}\n")

        return True
    except Exception:
        return False


def _build_mcp(project_root: Path) -> CheckResult:
    """Build the MCP TypeScript server (npm install + npm run build)."""
    mcp_dir = project_root / "mcp"
    src_dir = mcp_dir / "src"
    dist_file = mcp_dir / "dist" / "index.js"

    if not src_dir.exists():
        return CheckResult(
            name="MCP server",
            status=CheckStatus.WARNING,
            message="mcp/src/ not found — skipped",
        )

    node_modules = mcp_dir / "node_modules"
    stamp = node_modules / ".install-stamp"
    pkg_json = mcp_dir / "package.json"
    needs_install = (
        not node_modules.exists()
        or not stamp.exists()
        or (pkg_json.exists() and pkg_json.stat().st_mtime > stamp.stat().st_mtime)
    )

    if needs_install:
        print("    Installing MCP server dependencies...")
        result = subprocess.run(
            ["npm", "install", "--prefer-offline"], cwd=str(mcp_dir),
            stdin=subprocess.DEVNULL, timeout=120,
        )
        if result.returncode != 0:
            return CheckResult(
                name="MCP server",
                status=CheckStatus.ERROR,
                message="npm install failed",
                detail="Try manually: cd mcp && npm install",
            )
        stamp.touch()

    needs_build = not dist_file.exists()
    if not needs_build:
        dist_mtime = dist_file.stat().st_mtime
        for src_file in src_dir.rglob("*"):
            if src_file.is_file() and src_file.stat().st_mtime > dist_mtime:
                needs_build = True
                break

    if needs_build:
        print("    Building MCP server...")
        result = subprocess.run(
            ["npm", "run", "build"], cwd=str(mcp_dir),
            stdin=subprocess.DEVNULL, timeout=60,
        )
        if result.returncode != 0:
            return CheckResult(
                name="MCP server",
                status=CheckStatus.ERROR,
                message="npm run build failed",
                detail="Try manually: cd mcp && npm run build",
            )
        return CheckResult(
            name="MCP server",
            status=CheckStatus.OK,
            message="Built successfully",
        )

    return CheckResult(
        name="MCP server",
        status=CheckStatus.OK,
        message="Up to date",
    )


def install_wrapper_script() -> CheckResult:
    """Install quern wrapper script to ~/.local/bin."""
    local_bin = Path.home() / ".local" / "bin"
    wrapper_path = local_bin / "quern"

    # Find project root (works regardless of folder name)
    project_root = _find_project_root()
    if not project_root:
        return CheckResult(
            name="Wrapper script",
            status=CheckStatus.ERROR,
            message="Could not find project root",
            detail="server/main.py not found in any parent directory",
        )

    venv_python = project_root / ".venv" / "bin" / "python"
    if not venv_python.exists():
        return CheckResult(
            name="Wrapper script",
            status=CheckStatus.WARNING,
            message="Skipped (venv not found)",
            detail="Create venv first, then re-run setup",
        )

    # Create ~/.local/bin if it doesn't exist
    local_bin.mkdir(parents=True, exist_ok=True)

    # Write wrapper script
    wrapper_content = f"""#!/usr/bin/env bash
# Quern wrapper — auto-generated by ./quern setup
# Points to: {project_root}
exec "{venv_python}" -m server "$@"
"""

    try:
        wrapper_path.write_text(wrapper_content)
        wrapper_path.chmod(0o755)  # Make executable

        # Check if ~/.local/bin is in PATH
        path_env = os.environ.get("PATH", "")
        if str(local_bin) not in path_env.split(":"):
            # Offer to add it automatically
            shell_rc = _detect_shell_rc()
            if shell_rc:
                print("\n~/.local/bin is not in your PATH.")
                if _prompt_yn("Add it to your PATH automatically?", default=True):
                    if _add_to_path(shell_rc, local_bin):
                        return CheckResult(
                            name="Wrapper script",
                            status=CheckStatus.OK,
                            message=f"Installed to {wrapper_path}",
                            detail=(
                                f"✓ Added to PATH in {shell_rc}\n"
                                f"  Run: source {shell_rc}\n"
                                f"  Or restart your shell to use 'quern' command globally"
                            ),
                        )
                    else:
                        return CheckResult(
                            name="Wrapper script",
                            status=CheckStatus.OK,
                            message=f"Installed to {wrapper_path}",
                            detail=f"PATH already configured in {shell_rc}",
                        )

            # User declined or shell detection failed — show manual instructions
            return CheckResult(
                name="Wrapper script",
                status=CheckStatus.OK,
                message=f"Installed to {wrapper_path}",
                detail=(
                    "⚠ Add ~/.local/bin to PATH manually:\n"
                    "    echo 'export PATH=\"$HOME/.local/bin:$PATH\"' >> ~/.zshrc\n"
                    "    source ~/.zshrc"
                ),
            )

        return CheckResult(
            name="Wrapper script",
            status=CheckStatus.OK,
            message=f"Installed to {wrapper_path}",
        )
    except Exception as e:
        return CheckResult(
            name="Wrapper script",
            status=CheckStatus.ERROR,
            message="Installation failed",
            detail=str(e),
        )


def _install_skills(project_root: Path) -> CheckResult:
    """Symlink quern skills into ~/.claude/skills/ for Claude Code."""
    skills_src = project_root / "skills"
    if not skills_src.exists() or not any(skills_src.iterdir()):
        return CheckResult(
            name="Claude Code skills",
            status=CheckStatus.OK,
            message="No skills to install",
        )

    claude_dir = Path.home() / ".claude"
    if not claude_dir.exists():
        return CheckResult(
            name="Claude Code skills",
            status=CheckStatus.OK,
            message="Skipped (no ~/.claude directory)",
        )

    skills_dest = claude_dir / "skills"
    skills_dest.mkdir(parents=True, exist_ok=True)

    installed = []
    for skill_dir in sorted(skills_src.iterdir()):
        if not skill_dir.is_dir() or skill_dir.name.startswith("."):
            continue
        link_path = skills_dest / skill_dir.name
        if link_path.is_symlink():
            if link_path.resolve() == skill_dir.resolve():
                installed.append(f"{skill_dir.name} (already linked)")
                continue
            link_path.unlink()
        elif link_path.exists():
            installed.append(f"{skill_dir.name} (skipped — non-symlink exists)")
            continue
        link_path.symlink_to(skill_dir)
        installed.append(skill_dir.name)

    if not installed:
        return CheckResult(
            name="Claude Code skills",
            status=CheckStatus.OK,
            message="No skills to install",
        )

    return CheckResult(
        name="Claude Code skills",
        status=CheckStatus.OK,
        message=f"Linked {len(installed)} skill(s) to ~/.claude/skills/",
        detail=", ".join(installed),
    )


# Marker we use to identify our PreToolUse hook entry inside
# ~/.claude/settings.json. Stable string included in the command path so
# re-running setup updates rather than duplicates the entry.
_PRECOMMIT_HOOK_MARKER = "agent-precommit-checklist.sh"


def _install_precommit_hook(project_root: Path) -> CheckResult:
    """Install a Claude Code pre-commit checklist hook into ~/.claude.

    Copies the checklist script + content out of the Quern source tree
    into ~/.quern/ (so they survive a source-tree move/uninstall), then
    deep-merges a PreToolUse:Bash hook into ~/.claude/settings.json that
    runs the script. The script self-gates on a `.quern/` directory
    being present in the agent's cwd, so the hook stays silent in
    projects that don't use Quern.

    Idempotent — re-running setup updates an existing entry in place
    rather than duplicating it.
    """
    import json
    import shutil

    src_script = project_root / "scripts" / "agent-precommit-checklist.sh"
    src_checklist = project_root / "docs" / "agent-precommit-checklist.md"
    if not src_script.exists() or not src_checklist.exists():
        return CheckResult(
            name="Claude Code pre-commit hook",
            status=CheckStatus.OK,
            message="Source files not found — skipped",
        )

    claude_dir = Path.home() / ".claude"
    if not claude_dir.exists():
        return CheckResult(
            name="Claude Code pre-commit hook",
            status=CheckStatus.OK,
            message="Skipped (no ~/.claude directory)",
        )

    # Install the script and checklist content into ~/.quern/. The script
    # resolves the checklist path relative to its own location ($0/..),
    # so the layout is: ~/.quern/bin/agent-precommit-checklist.sh and
    # ~/.quern/agent-precommit-checklist.md.
    quern_dir = Path.home() / ".quern"
    bin_dir = quern_dir / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    dest_script = bin_dir / "agent-precommit-checklist.sh"
    dest_checklist = quern_dir / "agent-precommit-checklist.md"
    shutil.copy2(src_script, dest_script)
    shutil.copy2(src_checklist, dest_checklist)
    dest_script.chmod(0o755)

    # Deep-merge the hook config into ~/.claude/settings.json.
    settings_path = claude_dir / "settings.json"
    if settings_path.exists():
        try:
            config = json.loads(settings_path.read_text())
        except (json.JSONDecodeError, ValueError):
            return CheckResult(
                name="Claude Code pre-commit hook",
                status=CheckStatus.WARNING,
                message=f"Skipped: {settings_path} contains invalid JSON",
            )
    else:
        config = {}

    hooks_root = config.setdefault("hooks", {})
    pre_tool_use = hooks_root.setdefault("PreToolUse", [])
    new_command = str(dest_script)

    # If our hook is already installed (identified by the marker string
    # in the command path), update its command in place. Otherwise add a
    # new top-level entry. Preserves any other hooks the user may have.
    found = False
    for entry in pre_tool_use:
        for h in entry.get("hooks", []):
            if _PRECOMMIT_HOOK_MARKER in h.get("command", ""):
                h["command"] = new_command
                found = True
                break
        if found:
            break

    if not found:
        pre_tool_use.append({
            "matcher": "Bash",
            "hooks": [{"type": "command", "command": new_command}],
        })

    settings_path.write_text(json.dumps(config, indent=2) + "\n")

    verb = "Updated" if found else "Installed"
    return CheckResult(
        name="Claude Code pre-commit hook",
        status=CheckStatus.OK,
        message=f"{verb} pre-commit checklist hook in {settings_path}",
        detail=f"Script: {dest_script}",
    )


# ── Individual checks ─────────────────────────────────────────────────────

def _find_project_root() -> Path | None:
    """Find the project root by looking for pyproject.toml above this file."""
    path = Path(__file__).resolve().parent
    for _ in range(5):  # don't walk too far up
        if (path / "pyproject.toml").exists():
            return path
        parent = path.parent
        if parent == path:
            break
        path = parent
    return None


def check_venv() -> CheckResult:
    """Check if running inside a virtual environment."""
    in_venv = sys.prefix != sys.base_prefix
    if in_venv:
        return CheckResult(
            name="Virtual env",
            status=CheckStatus.OK,
            message=sys.prefix,
        )

    project_root = _find_project_root()
    venv_path = project_root / ".venv" if project_root else None

    if venv_path and venv_path.exists():
        return CheckResult(
            name="Virtual env",
            status=CheckStatus.WARNING,
            message="Not activated",
            detail=f"A venv exists at {venv_path}\n"
                   f"Activate it: source {venv_path}/bin/activate",
        )

    return CheckResult(
        name="Virtual env",
        status=CheckStatus.WARNING,
        message="Not using a virtual environment",
        fixable=True,
    )


def _find_best_python() -> str:
    """Find the best available Python interpreter (prefer supported versions)."""
    # Try specific supported versions first (newest to oldest)
    for ver in ("3.13", "3.12", "3.11"):
        path = _which(f"python{ver}")
        if path:
            return path
    # Fall back to whatever python3 is
    return sys.executable


def create_venv(project_root: Path) -> bool:
    """Create a .venv and install the project into it. Returns True on success."""
    venv_path = project_root / ".venv"
    python = _find_best_python()
    print(f"    Creating virtual environment at {venv_path} (using {python})...")

    rc, _, stderr = _run(
        [python, "-m", "venv", str(venv_path)], timeout=60,
    )
    if rc != 0:
        print(f"    Failed to create venv: {stderr}")
        return False

    pip = str(venv_path / "bin" / "pip")
    print("    Installing quern-debug-server into venv...")
    try:
        result = subprocess.run(
            [pip, "install", "-e", f"{project_root}[dev]"],
            stdin=subprocess.DEVNULL, timeout=300,
        )
        if result.returncode != 0:
            print("    pip install failed")
            return False
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"    pip install failed: {exc}")
        return False

    return True


def check_platform() -> CheckResult:
    """Verify we're on macOS."""
    system = platform.system()
    if system == "Darwin":
        mac_ver = platform.mac_ver()[0]
        return CheckResult(
            name="Platform",
            status=CheckStatus.OK,
            message=f"macOS {mac_ver}",
        )
    return CheckResult(
        name="Platform",
        status=CheckStatus.WARNING,
        message=f"{system} (some features require macOS)",
        detail="iOS device log capture and simulator control require macOS.\n"
               "Proxy/network capture will work on any platform.",
    )


PYTHON_MIN = (3, 11)
PYTHON_MAX = (3, 13)


def check_python() -> CheckResult:
    """Verify Python version is within the supported 3.11–3.13 range."""
    version = sys.version_info
    version_str = f"{version[0]}.{version[1]}.{version[2]}"

    if PYTHON_MIN <= (version[0], version[1]) <= PYTHON_MAX:
        return CheckResult(
            name="Python",
            status=CheckStatus.OK,
            message=version_str,
        )

    if (version[0], version[1]) < PYTHON_MIN:
        return CheckResult(
            name="Python",
            status=CheckStatus.ERROR,
            message=f"{version_str} (requires >= 3.11)",
            fixable=True,
        )

    # Above max — check if a supported version is already installed
    for ver in ("3.13", "3.12", "3.11"):
        if _which(f"python{ver}"):
            return CheckResult(
                name="Python",
                status=CheckStatus.OK,
                message=f"{version_str} (will use python{ver} for venv)",
            )

    # No supported version found
    return CheckResult(
        name="Python",
        status=CheckStatus.WARNING,
        message=f"{version_str} (tested with 3.11–3.13)",
        fixable=True,
    )


def check_homebrew() -> CheckResult:
    """Check if Homebrew is installed."""
    path = _which("brew")
    if path:
        version = _get_version(["brew", "--version"])
        short = version.split("\n")[0] if version else "installed"
        return CheckResult(
            name="Homebrew",
            status=CheckStatus.OK,
            message=short,
        )
    return CheckResult(
        name="Homebrew",
        status=CheckStatus.MISSING,
        message="Not installed",
        detail="Install from https://brew.sh\n"
               '  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"',
    )


def check_libimobiledevice() -> CheckResult:
    """Check for libimobiledevice (provides idevicesyslog, idevicecrashreport, etc.)."""
    tool = _which("idevicesyslog")
    if tool:
        version = _get_version(["idevicesyslog", "--version"])
        msg = version or "installed"
        return CheckResult(
            name="libimobiledevice",
            status=CheckStatus.OK,
            message=msg,
        )
    return CheckResult(
        name="libimobiledevice",
        status=CheckStatus.MISSING,
        message="Not installed (needed for iOS device log capture)",
        fixable=True,
    )


def check_ideviceinstaller() -> CheckResult:
    """Check for ideviceinstaller (needed to install apps on pre-iOS 17 devices)."""
    tool = _which("ideviceinstaller")
    if tool:
        version = _get_version(["ideviceinstaller", "--version"])
        msg = version or "installed"
        return CheckResult(
            name="ideviceinstaller",
            status=CheckStatus.OK,
            message=msg,
        )
    return CheckResult(
        name="ideviceinstaller",
        status=CheckStatus.MISSING,
        message="Not installed (needed to install apps on pre-iOS 17 devices)",
        fixable=True,
    )


def _diagnose_developer_dir() -> str | None:
    """Check if xcode-select developer dir provides simctl; suggest fix if not.

    Returns a diagnostic string if the developer dir is stale/invalid or
    points to CommandLineTools (which lacks simctl), or None if fine.
    """
    rc, dev_dir, _ = _run(["xcode-select", "-p"])
    if rc != 0:
        return None
    dev_dir = dev_dir.strip()

    # Check two failure modes:
    # 1. Path doesn't exist (renamed/moved Xcode)
    # 2. Path exists but is CommandLineTools (no simctl)
    path_exists = Path(dev_dir).exists()
    is_clt = "CommandLineTools" in dev_dir

    if path_exists and not is_clt:
        return None  # Looks fine — pointing to an Xcode.app

    if is_clt:
        reason = (
            f"xcode-select points to '{dev_dir}' (Command Line Tools), "
            f"which does not include simctl."
        )
    else:
        reason = f"xcode-select points to '{dev_dir}' which does not exist."

    # Search for Xcode installations to suggest a fix
    xcode_apps = sorted(Path("/Applications").glob("Xcode*.app"))
    if xcode_apps:
        best = xcode_apps[0]
        candidate = best / "Contents" / "Developer"
        if candidate.exists():
            return (
                f"{reason}\n"
                f"Found Xcode at '{best}'.\n"
                f"Fix with: sudo xcode-select -s '{candidate}'"
            )
        return (
            f"{reason}\n"
            f"Found '{best}' but it has no Contents/Developer.\n"
            f"Reinstall Xcode or run: sudo xcode-select -s /path/to/Xcode.app/Contents/Developer"
        )
    return (
        f"{reason}\n"
        f"No Xcode found in /Applications. Install Xcode from the App Store,\n"
        f"or if you renamed it, run: sudo xcode-select -s /path/to/YourXcode.app/Contents/Developer"
    )


def check_xcode_cli_tools() -> CheckResult:
    """Check for Xcode command line tools (provides xcrun, simctl)."""
    xcrun = _which("xcrun")
    if not xcrun:
        return CheckResult(
            name="Xcode CLI Tools",
            status=CheckStatus.MISSING,
            message="Not installed",
            detail="Install with: xcode-select --install",
        )
    # Verify simctl works
    rc, stdout, _ = _run(["xcrun", "simctl", "help"])
    if rc == 0:
        return CheckResult(
            name="Xcode CLI Tools",
            status=CheckStatus.OK,
            message="Installed (simctl available)",
        )

    # simctl failed — check if it's a stale developer dir (renamed Xcode)
    diagnosis = _diagnose_developer_dir()
    if diagnosis:
        return CheckResult(
            name="Xcode CLI Tools",
            status=CheckStatus.ERROR,
            message="xcrun found but simctl unavailable (developer dir mismatch)",
            detail=diagnosis,
        )
    return CheckResult(
        name="Xcode CLI Tools",
        status=CheckStatus.WARNING,
        message="xcrun found but simctl unavailable",
        detail="Xcode may not be fully installed. Try: xcode-select --install",
    )


def check_mitmdump() -> CheckResult:
    """Check for mitmdump (mitmproxy CLI tool for network capture)."""
    tool = _which("mitmdump")
    if tool:
        version = _get_version(["mitmdump", "--version"])
        # mitmdump --version outputs multi-line; grab the version number
        if version:
            for part in version.split():
                if part[0].isdigit():
                    version = part
                    break
        msg = version or "installed"
        return CheckResult(
            name="mitmdump",
            status=CheckStatus.OK,
            message=msg,
        )
    # mitmdump should come with our pip install (mitmproxy is a dependency),
    # but it could be missing if installed in a weird way
    return CheckResult(
        name="mitmdump",
        status=CheckStatus.MISSING,
        message="Not found on PATH",
        detail="This should be installed as part of the mitmproxy pip dependency.\n"
               "Try: pip install mitmproxy",
    )


def check_node() -> CheckResult:
    """Check for Node.js (needed to run the MCP server)."""
    node = _which("node")
    if node:
        version = _get_version(["node", "--version"])
        msg = version or "installed"
        return CheckResult(
            name="Node.js",
            status=CheckStatus.OK,
            message=msg,
        )
    return CheckResult(
        name="Node.js",
        status=CheckStatus.MISSING,
        message="Not installed (needed for MCP server)",
        fixable=True,
    )


def check_idb() -> CheckResult:
    """Check for idb CLI tool (needed for UI automation)."""
    tool = _which("idb")
    if tool:
        # idb doesn't have --version, but we can check if it runs
        rc, stdout, _ = _run(["idb", "list-targets"], timeout=5)
        if rc == 0 or "usage:" in stdout.lower():
            return CheckResult(
                name="idb (fb-idb)",
                status=CheckStatus.OK,
                message="installed",
            )
    return CheckResult(
        name="idb (fb-idb)",
        status=CheckStatus.MISSING,
        message="Not installed (needed for simulator UI automation)",
        detail="Install with: pip install fb-idb\n"
               "Also requires: brew install idb-companion\n"
               "Then run: pyenv rehash (if using pyenv)",
        fixable=True,
    )


def check_idb_companion() -> CheckResult:
    """Check for idb_companion, preferring the patched build in ~/.quern/bin/."""
    quern_companion = Path.home() / ".quern" / "bin" / "idb_companion"
    if quern_companion.is_file():
        return CheckResult(
            name="idb_companion",
            status=CheckStatus.OK,
            message=f"installed (patched, {quern_companion})",
        )
    system_companion = _which("idb_companion")
    if system_companion:
        return CheckResult(
            name="idb_companion",
            status=CheckStatus.OK,
            message=f"installed (system, {system_companion})",
            detail="Patched build available with improved Group element detection: ./quern setup",
        )
    return CheckResult(
        name="idb_companion",
        status=CheckStatus.MISSING,
        message="Not installed (needed for UI automation)",
        fixable=True,
    )


_IDB_COMPANION_URL = (
    "https://github.com/quern-dev/idb/releases/download/"
    "idb-companion-v1/idb-companion-patched-arm64.tar.gz"
)


def _install_patched_companion() -> bool:
    """Download and install the patched idb_companion to ~/.quern/bin/."""
    import urllib.request

    dest = Path.home() / ".quern" / "bin"
    dest.mkdir(parents=True, exist_ok=True)
    tarball = dest / "idb-companion.tar.gz"

    try:
        print("    Downloading patched idb_companion...")
        urllib.request.urlretrieve(_IDB_COMPANION_URL, tarball)
        print("    Extracting...")
        subprocess.run(
            ["tar", "xzf", str(tarball), "-C", str(dest)],
            check=True, stdin=subprocess.DEVNULL,
        )
        tarball.unlink(missing_ok=True)
        # Tarball extracts bin/idb_companion — move it up to dest/
        nested = dest / "bin" / "idb_companion"
        companion = dest / "idb_companion"
        if nested.exists():
            nested.rename(companion)
            (dest / "bin").rmdir()
        if companion.exists():
            companion.chmod(0o755)
            _record_install("quern", "idb_companion")
            return True
        return False
    except Exception as exc:
        print(f"    Download failed: {exc}")
        tarball.unlink(missing_ok=True)
        return False


def check_vpn() -> CheckResult:
    """Detect active VPN connections that may interfere with the proxy."""
    if platform.system() != "Darwin":
        return CheckResult(
            name="VPN Detection",
            status=CheckStatus.SKIPPED,
            message="macOS only",
        )

    # Check scutil for VPN connections
    rc, stdout, _ = _run(["scutil", "--nc", "list"])
    connected_vpns: list[str] = []
    if rc == 0:
        for line in stdout.splitlines():
            if "(Connected)" in line:
                # Extract VPN name from between quotes
                parts = line.split('"')
                if len(parts) >= 2:
                    connected_vpns.append(parts[1])

    # Check default route for tunnel interface
    rc, stdout, _ = _run(["route", "-n", "get", "default"])
    tunnel_iface = False
    if rc == 0:
        for line in stdout.splitlines():
            if "interface:" in line:
                iface = line.split(":")[-1].strip()
                if iface.startswith("utun"):
                    tunnel_iface = True
                break

    if not connected_vpns and not tunnel_iface:
        return CheckResult(
            name="VPN Detection",
            status=CheckStatus.OK,
            message="No active VPN detected",
        )

    warnings = []
    if connected_vpns:
        names = ", ".join(connected_vpns)
        warnings.append(f"Active VPN: {names}")
    if tunnel_iface:
        warnings.append("Default route uses a tunnel interface")

    return CheckResult(
        name="VPN Detection",
        status=CheckStatus.WARNING,
        message="; ".join(warnings),
        detail="VPNs can intercept traffic before it reaches the proxy.\n"
               "Consider disconnecting VPN or configuring split tunneling\n"
               "when using proxy capture.",
    )


def check_mitmproxy_cert() -> CheckResult:
    """Check if the mitmproxy CA certificate exists."""
    cert_path = Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.pem"
    if cert_path.exists():
        return CheckResult(
            name="mitmproxy CA Cert",
            status=CheckStatus.OK,
            message=str(cert_path),
        )
    return CheckResult(
        name="mitmproxy CA Cert",
        status=CheckStatus.WARNING,
        message="Not generated yet",
        detail="The CA certificate is auto-generated on first proxy start.\n"
               "Run './quern start -f --no-crash' to generate it,\n"
               "then Ctrl+C to stop.",
    )


def check_pymobiledevice3() -> CheckResult:
    """Check if pymobiledevice3 is installed (needed for physical device screenshots).

    Also flags installs that live under an external home volume — those work
    while the user is logged in but won't be reachable at boot, which means
    the tunneld LaunchDaemon can't start until login completes.
    """
    from server.device.tunneld import find_pymobiledevice3_binary

    binary = find_pymobiledevice3_binary()
    if not binary:
        return CheckResult(
            name="pymobiledevice3",
            status=CheckStatus.WARNING,
            message="Not installed (needed for physical device screenshots)",
            detail="Install with: pipx install pymobiledevice3",
        )

    rc, stdout, _ = _run([str(binary), "version"])
    version = stdout.strip() if rc == 0 else "installed"

    if _home_is_on_external() and str(binary).startswith("/Volumes/"):
        return CheckResult(
            name="pymobiledevice3",
            status=CheckStatus.WARNING,
            message=f"{version} — installed under external home ({binary})",
            detail="Binary lives on an external volume, so the tunneld "
                   "LaunchDaemon can't reach it pre-login. Reinstall "
                   "system-wide with: sudo pipx install --global pymobiledevice3",
        )

    return CheckResult(
        name="pymobiledevice3",
        status=CheckStatus.OK,
        message=version,
    )


def check_tunneld() -> CheckResult:
    """Check if the tunneld LaunchDaemon is installed and running."""
    from server.device.tunneld import (
        LOG_PATH,
        PLIST_PATH,
        TUNNELD_URL,
        installed_plist_is_current,
        installed_plist_log_path,
    )

    if not PLIST_PATH.exists():
        return CheckResult(
            name="tunneld",
            status=CheckStatus.WARNING,
            message="Not installed",
            detail="Install with: ./quern tunneld install\n"
                   "Required for physical device screenshots.",
        )

    plist_outdated = not installed_plist_is_current()

    # Check if running
    running = False
    try:
        import urllib.request
        req = urllib.request.Request(TUNNELD_URL, method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            running = resp.status == 200
    except Exception:
        pass

    if plist_outdated:
        old = installed_plist_log_path()
        return CheckResult(
            name="tunneld",
            status=CheckStatus.WARNING,
            message=f"Plist outdated (log path: {old})",
            detail=f"Log path moved to {LOG_PATH}. The old location under the "
                   "user home caused boot-time races for home-on-external-volume "
                   "setups. Reinstall with: ./quern tunneld install",
        )

    if running:
        return CheckResult(
            name="tunneld",
            status=CheckStatus.OK,
            message=f"Running on {TUNNELD_URL}",
        )

    return CheckResult(
        name="tunneld",
        status=CheckStatus.WARNING,
        message="Installed but not running",
        detail="Try: ./quern tunneld restart",
    )


def configure_crash_reporter_dialog() -> CheckResult:
    """Check and optionally disable the macOS crash reporter dialog.

    When set to 'none', crash reports are still written to
    ~/Library/Logs/DiagnosticReports/ but no modal dialog appears.
    This is especially useful on headless CI machines where hundreds
    of dialogs can accumulate.
    """
    if platform.system() != "Darwin":
        return CheckResult(
            name="Crash dialog",
            status=CheckStatus.SKIPPED,
            message="macOS only",
        )

    rc, stdout, _ = _run(["defaults", "read", "com.apple.CrashReporter", "DialogType"])
    current = stdout.strip() if rc == 0 else ""

    if current == "none":
        return CheckResult(
            name="Crash dialog",
            status=CheckStatus.OK,
            message="Disabled (crash reports still saved to disk)",
        )

    desc = f"Currently: '{current}'" if current else "Currently: default (shows dialog)"
    if _prompt_yn(f"    Disable macOS crash reporter dialog? ({desc})"):
        rc, _, stderr = _run([
            "defaults", "write", "com.apple.CrashReporter", "DialogType", "none",
        ])
        if rc == 0:
            return CheckResult(
                name="Crash dialog",
                status=CheckStatus.OK,
                message="Disabled (crash reports still saved to disk)",
            )
        return CheckResult(
            name="Crash dialog",
            status=CheckStatus.ERROR,
            message="Failed to set defaults",
            detail=stderr,
        )

    return CheckResult(
        name="Crash dialog",
        status=CheckStatus.WARNING,
        message=desc,
        detail="Disable manually: defaults write com.apple.CrashReporter DialogType none",
    )


def check_booted_simulators() -> list[dict[str, str]]:
    """Return a list of booted simulators [{name, udid}]."""
    rc, stdout, _ = _run(["xcrun", "simctl", "list", "devices", "--json"])
    if rc != 0:
        return []

    import json
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return []

    booted = []
    for runtime, devices in data.get("devices", {}).items():
        for dev in devices:
            if dev.get("state") == "Booted":
                booted.append({
                    "name": dev.get("name", "Unknown"),
                    "udid": dev.get("udid", ""),
                })
    return booted


def _is_cert_installed(udid: str) -> bool:
    """Check if mitmproxy CA cert is already installed on a simulator."""
    import asyncio

    from server.device.controller import DeviceController
    from server.proxy import cert_manager

    controller = DeviceController()
    try:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                cert_manager.is_cert_installed(controller, udid, verify=True)
            )
        finally:
            loop.close()
    except Exception:
        return False


def install_cert_simulator(udid: str, name: str) -> CheckResult:
    """Install mitmproxy CA cert into a booted simulator.

    This function is synchronous but calls async cert_manager functions internally.
    """
    import asyncio

    from server.device.controller import DeviceController
    from server.proxy import cert_manager

    cert_path = cert_manager.get_cert_path()
    if not cert_path.exists():
        return CheckResult(
            name=f"Cert → {name}",
            status=CheckStatus.SKIPPED,
            message="No CA cert yet (start proxy first)",
        )

    # Create a controller for cert_manager (it needs it for device name lookup)
    controller = DeviceController()

    try:
        # Run async cert installation in sync context
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # First verify if cert is already installed (via SQLite)
            is_installed = loop.run_until_complete(
                cert_manager.is_cert_installed(controller, udid, verify=True)
            )

            if is_installed:
                return CheckResult(
                    name=f"Cert → {name}",
                    status=CheckStatus.OK,
                    message="CA certificate already trusted (verified via TrustStore)",
                )

            # Install the cert
            was_installed = loop.run_until_complete(
                cert_manager.install_cert(controller, udid, force=False)
            )

            if was_installed:
                return CheckResult(
                    name=f"Cert → {name}",
                    status=CheckStatus.OK,
                    message="CA certificate installed and verified",
                )
            else:
                # This shouldn't happen (is_installed was False but install returned False)
                return CheckResult(
                    name=f"Cert → {name}",
                    status=CheckStatus.OK,
                    message="CA certificate trusted",
                )
        finally:
            loop.close()
    except Exception as e:
        return CheckResult(
            name=f"Cert → {name}",
            status=CheckStatus.ERROR,
            message=f"Failed to install cert: {e}",
        )


# ── Main setup flow ──────────────────────────────────────────────────────

def _reexec_in_venv(venv_path: Path) -> int:
    """Re-execute setup inside the venv so all checks run in the right environment."""
    venv_python = venv_path / "bin" / "python"
    if not venv_python.exists():
        return -1
    print(f"    Continuing setup inside {venv_path}...")
    print()
    # Put the venv's bin dir on PATH so which() finds venv-installed tools
    env = os.environ.copy()
    venv_bin = str(venv_path / "bin")
    env["PATH"] = venv_bin + ":" + env.get("PATH", "")
    env["VIRTUAL_ENV"] = str(venv_path)
    # When running under `curl | bash`, stdin is the pipe — pass /dev/tty
    # so the re-exec'd process can prompt interactively.
    stdin_arg: int | None = None
    if not sys.stdin.isatty():
        try:
            stdin_arg = os.open("/dev/tty", os.O_RDONLY)
        except OSError:
            pass
    result = subprocess.run(
        [str(venv_python), "-m", "server.main", "setup"],
        cwd=str(venv_path.parent),
        stdin=stdin_arg,
        env=env,
    )
    if stdin_arg is not None:
        os.close(stdin_arg)
    return result.returncode


def run_setup() -> int:
    """Run the interactive setup. Returns 0 on success, 1 on errors."""
    # Ensure venv bin dir is on PATH so which() finds venv-installed tools
    if sys.prefix != sys.base_prefix:
        venv_bin = str(Path(sys.prefix) / "bin")
        path = os.environ.get("PATH", "")
        if venv_bin not in path.split(":"):
            os.environ["PATH"] = venv_bin + ":" + path

    print()
    print("  Quern — Setup")
    print("  Checking your environment...")
    print()

    report = SetupReport()
    project_root = _find_project_root()

    # ── Platform (informational) ──

    report.add(check_platform())

    # ── Homebrew (required — halt if missing) ──

    brew_result = check_homebrew()
    report.add(brew_result)
    if brew_result.status == CheckStatus.MISSING:
        report.print_summary()
        print("  Homebrew is required to install system dependencies.")
        print("  Install it first, then re-run: ./quern setup")
        print()
        return 1

    # ── Python (halt after brew install so user re-runs under new interpreter) ──

    python_result = check_python()
    if python_result.fixable and python_result.status in (CheckStatus.ERROR, CheckStatus.WARNING):
        too_old = python_result.status == CheckStatus.ERROR
        prompt = (
            "    Python version is not supported. Install Python 3.12 via Homebrew?"
            if too_old else
            "    Python version is untested. Install Python 3.12 via Homebrew?"
        )
        if _prompt_yn(prompt, default=too_old):
            if _brew_install("python@3.12"):
                report.add(CheckResult(
                    name="Python",
                    status=CheckStatus.OK,
                    message="Python 3.12 installed via Homebrew",
                ))
                report.print_summary()
                print("  Python 3.12 was installed. Restart your shell, then re-run:")
                print("    ./quern setup")
                print()
                return 0
            else:
                python_result = CheckResult(
                    name="Python",
                    status=CheckStatus.ERROR,
                    message="Homebrew install failed",
                    detail="Try manually: brew install python@3.12",
                )
    report.add(python_result)

    # ── Virtual environment (create + re-exec if needed) ──

    in_venv = sys.prefix != sys.base_prefix
    if not in_venv and project_root:
        venv_path = project_root / ".venv"
        if venv_path.exists():
            # Check if the venv was created with an unsupported Python
            venv_python = venv_path / "bin" / "python"
            if venv_python.exists():
                rc, stdout, _ = _run([str(venv_python), "--version"])
                if rc == 0:
                    # e.g. "Python 3.14.0" → (3, 14)
                    parts = stdout.split()[-1].split(".")
                    venv_ver = (int(parts[0]), int(parts[1]))
                    best = _find_best_python()
                    best_rc, best_out, _ = _run([best, "--version"])
                    best_ver = None
                    if best_rc == 0:
                        bp = best_out.split()[-1].split(".")
                        best_ver = (int(bp[0]), int(bp[1]))
                    if venv_ver > PYTHON_MAX and best_ver and best_ver != venv_ver:
                        print(f"    Existing venv uses Python {parts[0]}.{parts[1]}"
                              f" (unsupported). A better version is available.")
                        if _prompt_yn(f"    Recreate venv with {best}?"):
                            import shutil as _shutil
                            _shutil.rmtree(venv_path)
                            if create_venv(project_root):
                                return _reexec_in_venv(venv_path)

            # Venv exists but not activated — re-exec inside it
            print("    Virtual environment found but not activated.")
            print(f"    Re-running setup inside {venv_path}...")
            return _reexec_in_venv(venv_path)
        else:
            # No venv — create it, then re-exec
            if _prompt_yn("    No virtual environment found. Create one?"):
                if create_venv(project_root):
                    return _reexec_in_venv(venv_path)
                else:
                    report.add(CheckResult(
                        name="Virtual env",
                        status=CheckStatus.ERROR,
                        message="Failed to create virtual environment",
                        detail="Try manually:\n"
                               f"  python3 -m venv {project_root / '.venv'}\n"
                               f"  source {project_root / '.venv'}/bin/activate\n"
                               '  pip install -e ".[dev]"',
                    ))
                    report.print_summary()
                    return 1

    # If we get here, we're inside the venv
    report.add(CheckResult(
        name="Virtual env",
        status=CheckStatus.OK,
        message=sys.prefix,
    ))

    # ── Core dependencies ──

    report.add(check_mitmdump())

    node_result = check_node()
    if node_result.status == CheckStatus.MISSING:
        if _prompt_yn("    Node.js not found. Install via Homebrew?"):
            if _brew_install("node"):
                node_result = check_node()  # re-check
            else:
                node_result = CheckResult(
                    name="Node.js",
                    status=CheckStatus.ERROR,
                    message="Homebrew install failed",
                    detail="Try manually: brew install node",
                )
    report.add(node_result)

    # ── iOS support (requires Xcode CLI Tools) ──

    # Auto-fix developer dir if simctl is missing due to renamed Xcode or
    # xcode-select pointing at CommandLineTools instead of a full Xcode.
    dev_dir_msg = _fix_developer_dir_for_setup()
    xcode_result = check_xcode_cli_tools()
    has_ios = xcode_result.status == CheckStatus.OK
    if has_ios and dev_dir_msg:
        xcode_result = CheckResult(
            name="Xcode CLI Tools",
            status=CheckStatus.OK,
            message="Installed (simctl available)",
            detail=dev_dir_msg,
        )
    report.add(xcode_result)

    if has_ios:
        # iOS tools — only check/install when Xcode is available

        libimobile_result = check_libimobiledevice()
        if libimobile_result.status == CheckStatus.MISSING:
            if _prompt_yn("    libimobiledevice not found. Install via Homebrew?"):
                if _brew_install("libimobiledevice"):
                    libimobile_result = check_libimobiledevice()  # re-check
                else:
                    libimobile_result = CheckResult(
                        name="libimobiledevice",
                        status=CheckStatus.ERROR,
                        message="Homebrew install failed",
                        detail="Try manually: brew install libimobiledevice",
                    )
        report.add(libimobile_result)

        ideviceinstaller_result = check_ideviceinstaller()
        if ideviceinstaller_result.status == CheckStatus.MISSING:
            if _prompt_yn("    ideviceinstaller not found. Install via Homebrew?"):
                if _brew_install("ideviceinstaller"):
                    ideviceinstaller_result = check_ideviceinstaller()  # re-check
                else:
                    ideviceinstaller_result = CheckResult(
                        name="ideviceinstaller",
                        status=CheckStatus.ERROR,
                        message="Homebrew install failed",
                        detail="Try manually: brew install ideviceinstaller",
                    )
        report.add(ideviceinstaller_result)

        # Simulator UI automation:
        #   - Xcode 26+ on Apple Silicon → sim-bridge runs natively, idb not needed
        #   - Older Xcode or Intel → fall back to the patched idb + fb-idb

        if _sim_bridge_supported():
            print(
                "    Xcode 26+ on Apple Silicon detected — "
                "sim-bridge handles simulator UI natively. Skipping idb."
            )
            report.add(CheckResult(
                name="idb_companion",
                status=CheckStatus.SKIPPED,
                message="Not required (sim-bridge active)",
                detail="Xcode 26+ on Apple Silicon: simulator UI runs through "
                       "sim-bridge. Existing idb installs still work as a fallback.",
            ))
            report.add(CheckResult(
                name="idb (fb-idb)",
                status=CheckStatus.SKIPPED,
                message="Not required (sim-bridge active)",
            ))
        else:
            idb_companion_result = check_idb_companion()
            if idb_companion_result.status == CheckStatus.MISSING:
                if _prompt_yn("    idb_companion not found. Download patched build?"):
                    if _install_patched_companion():
                        idb_companion_result = check_idb_companion()
                    else:
                        idb_companion_result = CheckResult(
                            name="idb_companion",
                            status=CheckStatus.WARNING,
                            message="Download failed (UI automation unavailable)",
                            detail="Try manually: https://github.com/quern-dev/idb/releases",
                        )
            elif idb_companion_result.message.startswith("installed (system"):
                if _prompt_yn(
                    "    Patched idb_companion available "
                    "(fixes Group element detection). Install?"
                ):
                    if _install_patched_companion():
                        idb_companion_result = check_idb_companion()
            report.add(idb_companion_result)

            idb_result = check_idb()
            if idb_result.status == CheckStatus.MISSING:
                print("    idb CLI not found. This is the Python client for idb_companion.")
                if _prompt_yn("    Install fb-idb via pip?"):
                    if sys.prefix != sys.base_prefix:
                        pip_cmd = str(Path(sys.prefix) / "bin" / "pip")
                    else:
                        pip_cmd = "pip" if _which("pip") else "pip3"
                    print("    Installing fb-idb...")
                    try:
                        result = subprocess.run(
                            [pip_cmd, "install", "fb-idb"],
                            stdin=subprocess.DEVNULL, timeout=120,
                        )
                        if result.returncode == 0:
                            _record_install("pip", "fb-idb")
                            if _which("pyenv"):
                                subprocess.run(
                                    ["pyenv", "rehash"],
                                    stdin=subprocess.DEVNULL, timeout=10,
                                )
                            idb_result = check_idb()  # re-check
                        else:
                            idb_result = CheckResult(
                                name="idb (fb-idb)",
                                status=CheckStatus.ERROR,
                                message="pip install failed",
                                detail="Try manually: pip install fb-idb",
                            )
                    except (FileNotFoundError, subprocess.TimeoutExpired):
                        idb_result = CheckResult(
                            name="idb (fb-idb)",
                            status=CheckStatus.ERROR,
                            message="pip install failed",
                            detail="Try manually: pip install fb-idb",
                        )
            report.add(idb_result)

        # Physical device support (pymobiledevice3 + tunneld)

        pmd3_result = check_pymobiledevice3()
        if pmd3_result.status == CheckStatus.WARNING:
            if not _which("pipx"):
                if _which("brew") and _prompt_yn(
                    "    pipx not found (needed for pymobiledevice3). "
                    "Install via Homebrew?",
                ):
                    if _brew_install("pipx"):
                        pipx_bin = _find_brew_binary("pipx")
                        if pipx_bin:
                            try:
                                subprocess.run(
                                    [pipx_bin, "ensurepath"],
                                    stdin=subprocess.DEVNULL,
                                    timeout=30, capture_output=True,
                                )
                            except (FileNotFoundError, subprocess.TimeoutExpired):
                                pass
            pipx_bin = _find_brew_binary("pipx")
            msg = pmd3_result.message or ""
            wants_global = _home_is_on_external()
            misplaced = "external home" in msg
            if pipx_bin and (misplaced or "Not installed" in msg
                             or "needed for physical" in msg):
                if misplaced:
                    prompt = (
                        "    pymobiledevice3 is installed under your external "
                        "home volume — won't be reachable at boot. Reinstall "
                        "system-wide via `sudo pipx install --global` "
                        "(requires sudo)?"
                    )
                elif wants_global:
                    prompt = (
                        "    pymobiledevice3 not found. Install system-wide "
                        "via `sudo pipx install --global` (your home is on an "
                        "external volume, requires sudo)?"
                    )
                else:
                    prompt = "    pymobiledevice3 not found. Install via pipx?"
                if _prompt_yn(prompt):
                    if wants_global:
                        # Inherit stdin so sudo can prompt for the password.
                        cmd = ["sudo", pipx_bin, "install", "--global",
                               "pymobiledevice3"]
                        run_kwargs = {"timeout": 300}
                        print("    Installing pymobiledevice3 via "
                              "`sudo pipx install --global`...")
                    else:
                        cmd = [pipx_bin, "install", "pymobiledevice3"]
                        run_kwargs = {
                            "stdin": subprocess.DEVNULL, "timeout": 300,
                        }
                        print("    Installing pymobiledevice3 via pipx...")
                    try:
                        result = subprocess.run(cmd, **run_kwargs)
                        if result.returncode == 0:
                            _record_install(
                                "pipx_global" if wants_global else "pipx",
                                "pymobiledevice3",
                            )
                            if wants_global:
                                _maybe_remove_user_pmd3(pipx_bin)
                            pmd3_result = check_pymobiledevice3()  # re-check
                        else:
                            detail = (
                                "Try manually: sudo pipx install --global "
                                "pymobiledevice3"
                                if wants_global
                                else "Try manually: pipx install pymobiledevice3"
                            )
                            pmd3_result = CheckResult(
                                name="pymobiledevice3",
                                status=CheckStatus.ERROR,
                                message="pipx install failed",
                                detail=detail,
                            )
                    except (FileNotFoundError, subprocess.TimeoutExpired):
                        pmd3_result = CheckResult(
                            name="pymobiledevice3",
                            status=CheckStatus.ERROR,
                            message="pipx install failed",
                            detail="Try manually: pipx install pymobiledevice3",
                        )
        report.add(pmd3_result)

        tunneld_result = check_tunneld()
        msg = tunneld_result.message or ""
        needs_install = (
            tunneld_result.status == CheckStatus.WARNING
            and "Not installed" in msg
        )
        needs_migration = (
            tunneld_result.status == CheckStatus.WARNING
            and "Plist outdated" in msg
        )
        if needs_install or needs_migration:
            if pmd3_result.status == CheckStatus.OK:
                if needs_migration:
                    prompt = (
                        "    tunneld plist references the old log path under "
                        "the user home (caused boot races on external-home "
                        "setups). Reinstall LaunchDaemon now (requires sudo)?"
                    )
                else:
                    prompt = (
                        "    tunneld not installed. Install LaunchDaemon "
                        "now (requires sudo)?"
                    )
                if _prompt_yn(prompt):
                    from server.device.tunneld import install_daemon
                    if install_daemon() == 0:
                        print("    Waiting for tunneld to start...", end="", flush=True)
                        import urllib.request

                        from server.device.tunneld import TUNNELD_URL
                        for _ in range(20):
                            time.sleep(0.5)
                            try:
                                req = urllib.request.Request(TUNNELD_URL, method="GET")
                                with urllib.request.urlopen(req, timeout=1):
                                    break
                            except Exception:
                                print(".", end="", flush=True)
                        print()
                        tunneld_result = check_tunneld()  # re-check
                    else:
                        tunneld_result = CheckResult(
                            name="tunneld",
                            status=CheckStatus.ERROR,
                            message="Installation failed",
                            detail="Try manually: ./quern tunneld install",
                        )
            elif needs_install:
                tunneld_result = CheckResult(
                    name="tunneld",
                    status=CheckStatus.WARNING,
                    message="Not installed (install pymobiledevice3 first)",
                    detail="Install with: pipx install pymobiledevice3 && ./quern tunneld install",
                )
        report.add(tunneld_result)

    else:
        print("\n    Xcode CLI Tools not found — skipping iOS dependencies.")
        print("    Install Xcode to enable iOS simulator and device support.\n")

    # ── Android support ──

    has_android = _which("adb") is not None
    if has_android:
        adb_version = _get_version(["adb", "--version"])
        report.add(CheckResult(
            name="Android (adb)",
            status=CheckStatus.OK,
            message=adb_version or "Available",
        ))

        # scrcpy — optional, for live preview
        scrcpy_result: CheckResult
        if _which("scrcpy") is not None:
            scrcpy_version = _get_version(["scrcpy", "--version"])
            scrcpy_result = CheckResult(
                name="Android (scrcpy)",
                status=CheckStatus.OK,
                message=scrcpy_version or "Available",
            )
        else:
            scrcpy_result = CheckResult(
                name="Android (scrcpy)",
                status=CheckStatus.SKIPPED,
                message="Not installed — live preview unavailable",
                detail="Install with: brew install scrcpy",
                fixable=True,
            )
            if _which("brew") and _prompt_yn(
                "    scrcpy not found (needed for Android live preview). "
                "Install via Homebrew?",
            ):
                if _brew_install("scrcpy"):
                    scrcpy_result = CheckResult(
                        name="Android (scrcpy)",
                        status=CheckStatus.OK,
                        message=_get_version(["scrcpy", "--version"]) or "Installed",
                    )
                else:
                    scrcpy_result = CheckResult(
                        name="Android (scrcpy)",
                        status=CheckStatus.WARNING,
                        message="Homebrew install failed",
                        detail="Try manually: brew install scrcpy",
                    )
        report.add(scrcpy_result)
    else:
        report.add(CheckResult(
            name="Android (adb)",
            status=CheckStatus.SKIPPED,
            message="Not installed — Android support unavailable",
            detail="Install Android Studio or: brew install android-platform-tools",
        ))

    if not has_ios and not has_android:
        report.add(CheckResult(
            name="Platform support",
            status=CheckStatus.WARNING,
            message="No iOS or Android tools found",
            detail="Install Xcode CLI Tools for iOS, or Android Studio/adb for Android.\n"
                   "At least one platform is needed for device management.",
        ))

    # ── Proxy / network checks ──

    report.add(check_vpn())
    report.add(check_mitmproxy_cert())

    # ── Crash reporter dialog ──

    report.add(configure_crash_reporter_dialog())

    # ── Simulator cert setup (only if iOS available) ──

    if has_ios:
        booted = check_booted_simulators()
        if booted:
            cert_path = Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.pem"
            if cert_path.exists():
                needs_cert = []
                for sim in booted:
                    installed = _is_cert_installed(sim["udid"])
                    if installed:
                        report.add(CheckResult(
                            name=f"Cert → {sim['name']}",
                            status=CheckStatus.OK,
                            message="CA certificate installed and verified",
                        ))
                    else:
                        needs_cert.append(sim)

                if needs_cert:
                    print(f"    Found {len(needs_cert)} booted simulator(s) needing CA cert:")
                    for sim in needs_cert:
                        print(f"      • {sim['name']} ({sim['udid'][:8]}…)")
                    if _prompt_yn("    Install mitmproxy CA cert into booted simulators?"):
                        for sim in needs_cert:
                            result = install_cert_simulator(sim["udid"], sim["name"])
                            report.add(result)
            else:
                print("    Booted simulators found but no CA cert yet — skipping cert install.")
                print("    Start the proxy once, then re-run setup to install certs.")

    # ── Wrapper script installation ──

    report.add(install_wrapper_script())

    # ── Claude Code skills ──

    if project_root:
        report.add(_install_skills(project_root))

    # ── Claude Code pre-commit checklist hook ──

    if project_root:
        report.add(_install_precommit_hook(project_root))

    # ── Build MCP server ──
    # Build the TypeScript MCP server so it's ready when Claude Code connects.
    # Without this, the MCP shows as broken until the first `quern start`.

    if node_result.status == CheckStatus.OK and project_root:
        report.add(_build_mcp(project_root))

    # ── Summary ──

    report.print_summary()
    return 1 if report.has_errors else 0


# ── Uninstall ────────────────────────────────────────────────────────────


def _brew_uninstall(formula: str) -> bool:
    """Uninstall a Homebrew formula. Returns True on success.

    Uses --ignore-dependencies to avoid cascading removal of shared
    dependencies (e.g. uninstalling pipx pulling python along with it).
    """
    print(f"    Uninstalling {formula} via Homebrew...")
    try:
        result = subprocess.run(
            ["brew", "uninstall", "--ignore-dependencies", formula],
            stdin=subprocess.DEVNULL, timeout=120,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def run_uninstall() -> int:
    """Remove Quern and its dependencies. Returns 0 on success, 1 on error."""
    print()
    print("  Quern — Uninstall")
    print()

    project_root = _find_project_root()
    manifest = _read_manifest()
    brew_packages = manifest.get("brew", [])
    pipx_packages = manifest.get("pipx", [])
    pipx_global_packages = manifest.get("pipx_global", [])

    # ── Confirmation ──

    print("  This will remove:")
    if brew_packages:
        print(f"    • Homebrew packages installed by setup: {', '.join(brew_packages)}")
    else:
        print("    • Homebrew packages: (none tracked — setup didn't install any)")
    if pipx_packages:
        print(f"    • pipx packages: {', '.join(pipx_packages)}")
    if pipx_global_packages:
        print(
            f"    • pipx --global packages (requires sudo): "
            f"{', '.join(pipx_global_packages)}",
        )
    print("    • The quern wrapper script (~/.local/bin/quern)")
    print("    • The Python virtual environment (.venv/)")
    print("    • MCP server registrations (claude-code, claude-desktop, cursor, opencode, codex)")
    print()
    if not _prompt_yn("  Proceed with uninstall?", default=False):
        print("  Aborted.")
        return 0

    errors = 0

    # ── Stop running server ──

    from server.lifecycle.state import is_server_healthy, read_state
    state = read_state()
    if state and is_server_healthy(state.get("server_port", 9100)):
        print()
        print("  Stopping running server...")
        pid = state.get("pid")
        if pid:
            import signal
            try:
                os.kill(pid, signal.SIGTERM)
                import time
                for _ in range(50):
                    time.sleep(0.1)
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                print("    Server stopped.")
            except ProcessLookupError:
                pass

    # ── Uninstall pipx packages first (before brew removes pipx itself) ──

    if pipx_packages and _which("pipx"):
        print()
        for pkg in pipx_packages:
            print(f"  Removing {pkg} (pipx)...")
            try:
                result = subprocess.run(
                    ["pipx", "uninstall", pkg],
                    stdin=subprocess.DEVNULL, timeout=60,
                )
                if result.returncode != 0:
                    print(f"    Warning: failed to uninstall {pkg}")
                    errors += 1
            except (FileNotFoundError, subprocess.TimeoutExpired):
                print(f"    Warning: failed to uninstall {pkg}")
                errors += 1

    if pipx_global_packages and _which("pipx"):
        print()
        for pkg in pipx_global_packages:
            print(f"  Removing {pkg} (sudo pipx --global)...")
            try:
                # Inherit stdin so sudo can prompt for the password.
                result = subprocess.run(
                    ["sudo", "pipx", "uninstall", "--global", pkg],
                    timeout=60,
                )
                if result.returncode != 0:
                    print(f"    Warning: failed to uninstall {pkg}")
                    errors += 1
            except (FileNotFoundError, subprocess.TimeoutExpired):
                print(f"    Warning: failed to uninstall {pkg}")
                errors += 1

    # ── Uninstall Homebrew formulas (only ones we installed) ──

    if brew_packages and _which("brew"):
        print()
        print(f"  Removing {len(brew_packages)} Homebrew package(s)...")
        for formula in brew_packages:
            if not _brew_uninstall(formula):
                print(f"    Warning: failed to uninstall {formula}")
                errors += 1
    elif not brew_packages:
        print()
        print("  No Homebrew packages to remove (none were installed by setup).")

    # ── Remove wrapper script ──

    wrapper = Path.home() / ".local" / "bin" / "quern"
    if wrapper.exists():
        print()
        print(f"  Removing wrapper script ({wrapper})...")
        try:
            wrapper.unlink()
            print("    Removed.")
        except OSError as e:
            print(f"    Warning: could not remove {wrapper}: {e}")
            errors += 1

    # ── Remove MCP registrations ──

    print()
    print("  Removing MCP server registrations...")
    _remove_mcp_registrations()

    # ── Remove tunneld LaunchDaemon ──

    try:
        from server.device.tunneld import PLIST_PATH
        if PLIST_PATH.exists():
            print()
            if _prompt_yn("  Remove tunneld LaunchDaemon (requires sudo)?", default=True):
                from server.device.tunneld import uninstall_daemon
                uninstall_daemon()
    except Exception:
        pass  # tunneld module may not import if deps are gone

    # ── Remove .venv ──

    if project_root:
        venv_path = project_root / ".venv"
        if venv_path.exists():
            print()
            print(f"  Removing virtual environment ({venv_path})...")
            shutil.rmtree(venv_path, ignore_errors=True)
            print("    Removed.")

    # ── Remove manifest and quern config dir ──

    if INSTALL_MANIFEST.exists():
        INSTALL_MANIFEST.unlink(missing_ok=True)

    # ── Summary ──

    print()
    print("─" * 50)
    if errors:
        print(f"  Uninstall completed with {errors} warning(s).")
    else:
        print("  Uninstall complete.")
    print()
    if project_root:
        print(f"  The source code is still at {project_root}")
        print("  To remove it entirely: rm -rf " + str(project_root))
    print()
    return 0


def _remove_mcp_registrations() -> None:
    """Remove quern-debug from all known MCP config files."""
    import json

    configs = [
        ("claude-code", Path.home() / ".claude.json", "mcpServers", "quern-debug"),
        (
            "claude-desktop",
            Path.home() / "Library" / "Application Support"
            / "Claude" / "claude_desktop_config.json",
            "mcpServers",
            "quern-debug",
        ),
        ("cursor", Path.home() / ".cursor" / "mcp.json", "mcpServers", "quern-debug"),
        ("opencode", Path.home() / ".config" / "opencode" / "opencode.json", "mcp", "quern"),
    ]

    for name, path, section_key, entry_key in configs:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
            section = data.get(section_key, {})
            if entry_key in section:
                del section[entry_key]
                data[section_key] = section
                path.write_text(json.dumps(data, indent=2) + "\n")
                print(f"    Removed from {name} ({path})")
        except (json.JSONDecodeError, OSError):
            pass

    # Codex uses TOML — simple text removal
    codex_path = Path.home() / ".codex" / "config.toml"
    if codex_path.exists():
        try:
            text = codex_path.read_text()
            if "[mcp_servers.quern]" in text:
                lines = text.splitlines(keepends=True)
                new_lines = []
                skip = False
                for line in lines:
                    if line.strip() == "[mcp_servers.quern]":
                        skip = True
                        continue
                    if skip and line.strip().startswith("["):
                        skip = False
                    stripped = line.strip()
                    if skip and (
                        stripped.startswith(("command", "args", "enabled"))
                        or not stripped
                    ):
                        continue
                    new_lines.append(line)
                codex_path.write_text("".join(new_lines))
                print(f"    Removed from codex ({codex_path})")
        except OSError:
            pass
