"""quern setup — interactive environment checker and installer.

Validates the Python virtual environment, system dependencies, installs
missing tools via Homebrew, and optionally configures simulators for proxy use.

Usage:
    quern setup
    quern uninstall
"""

from __future__ import annotations

import contextlib
import os
import platform
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

# Before the server imports below, not after: an updater from 0.18.3 or older
# imports this file into a process still holding the previous release's
# `server.config`, and the next line asks it for a name that release lacks
# (#212). `stale_modules` has no server imports, so it is always current.
from server.lifecycle.stale_modules import refresh_if_stale

refresh_if_stale()

from server.config import CONFIG_DIR, quern_cmd  # noqa: E402
from server.device._xcode import xcode_available  # noqa: E402
from server.lifecycle.invocation import MENUBAR, invoked_by, run_it_yourself  # noqa: E402

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
            print(f"  Re-run '{quern_cmd()} setup' after resolving them.")
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

def _run(
    cmd: list[str], timeout: int = 30, env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """Run a command and return (returncode, stdout, stderr).

    ``env`` replaces the child's environment wholesale, for the tools that
    cannot start without a runtime fix-up.
    """
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=env,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", f"Command timed out: {' '.join(cmd)}"
    except (OSError, subprocess.SubprocessError) as e:
        # Base classes, not the subclasses we happened to see first. A file
        # that exists but cannot be exec'd raises neither FileNotFoundError nor
        # TimeoutExpired: a truncated binary raises OSError (Exec format
        # error), one that lost its exec bit in extraction raises
        # PermissionError. Those are precisely the corruptions the callers
        # probe *for*, so letting them escape turns a health check into a
        # traceback out of `quern setup` and `quern update`.
        return -1, "", f"Could not run {cmd[0]}: {e}"


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

    Skips the initial ``xcrun simctl help`` probe when no developer dir is
    configured — that probe triggers the macOS install dialog on a clean
    machine. We can still find an Xcode in ``/Applications`` and set
    DEVELOPER_DIR ourselves; the dialog is only avoided in the no-dev-dir
    case.
    """
    if os.environ.get("DEVELOPER_DIR"):
        return None

    # Only probe simctl if a developer dir is configured. Without one,
    # xcrun would trigger the macOS install dialog before returning.
    if xcode_available():
        rc, _, _ = _run(["xcrun", "simctl", "help"])
        if rc == 0:
            return None

    # No working simctl — find a usable Xcode in /Applications and point
    # DEVELOPER_DIR at it. Once DEVELOPER_DIR is set, xcrun uses it
    # directly and won't trigger the dialog even on a clean machine.
    rc, current_dir, _ = _run(["xcode-select", "-p"])
    current_dir = current_dir.strip() if rc == 0 else "(unknown)"

    for xcode_app in sorted(Path("/Applications").glob("Xcode*.app")):
        candidate = xcode_app / "Contents" / "Developer"
        if candidate.exists():
            os.environ["DEVELOPER_DIR"] = str(candidate)
            xcode_available.cache_clear()
            rc, _, _ = _run(["xcrun", "simctl", "help"])
            if rc == 0:
                return (
                    f"Xcode developer tools not found at default location ({current_dir}).\n"
                    f"Using {xcode_app} instead.\n"
                    f"To make this permanent: sudo xcode-select -s '{candidate}'"
                )
            del os.environ["DEVELOPER_DIR"]
            xcode_available.cache_clear()

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


INSTALL_MANIFEST = CONFIG_DIR / "installed-by-setup.json"


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


#: Questions setup could not ask, in the order it would have asked them.
#: Reset by `run_setup`; reported by it at the end.
_UNASKED: list[str] = []


def _can_prompt() -> bool:
    """Whether there is anyone to answer a question.

    A GUI-launched process has no controlling terminal, so `/dev/tty` cannot be
    opened. That is reachable in practice, not in theory: the menu-bar app's
    "Restart to Update" runs `quern update`, which runs setup.
    """
    if sys.stdin.isatty():
        return True
    try:
        open("/dev/tty").close()
    except OSError:
        return False
    return True


#: Set once by `run_setup`, read by `_prompt_yn`. A parameter would have to be
#: threaded through some twenty call sites and every function between them,
#: which is how one gets missed and a single prompt goes on blocking an
#: unattended run.
_ASSUME_YES = False


def _prompt_yn(question: str, default: bool = True, *, deliberate: bool = False) -> bool:
    """Prompt the user for yes/no confirmation.

    When stdin is not a TTY (e.g. ``curl | bash``), reopens /dev/tty so
    interactive prompts still work.

    With no terminal at all this declines *and records that it did*. Declining
    is the safe answer -- several of these install things, and answering the
    default would have setup say yes on the user's behalf -- but doing it
    silently meant a menu-bar update ran a visibly different setup from a
    terminal one, and said so nowhere. The question is printed and kept, so the
    output shows what was asked and `run_setup` can say how many went
    unanswered.

    `-y` answers with the default instead, the way `apt-get -y` does -- except
    for a prompt marked `deliberate`, which it must not answer at all. Consent
    that the user has to be told about afterwards is not consent: installing a
    MITM certificate authority outlives the session that wanted it, and the
    user has to know it happened in order to undo it. Those keep declining, and
    are still recorded as unasked.
    """
    suffix = " [Y/n] " if default else " [y/N] "
    if _ASSUME_YES and not deliberate:
        # Printed, not silent: the transcript has to show what was asked and
        # what was answered on the user's behalf.
        print(f"{question}{suffix}— {'yes' if default else 'no'} (-y)")
        return default
    if _ASSUME_YES and deliberate:
        _UNASKED.append(question)
        print(f"{question}{suffix}— not answered by -y; this one is yours to make")
        return False
    if sys.stdin.isatty():
        try:
            answer = input(question + suffix).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
    else:
        # stdin is a pipe (curl | bash) — read from the real terminal. One
        # open attempt, and its failure is the test for whether anyone is
        # there: asking separately meant opening /dev/tty twice per question.
        try:
            tty = open("/dev/tty")
        except OSError:
            _UNASKED.append(question)
            print(f"{question}{suffix}— no terminal to ask on, assuming no")
            return False
        try:
            print(question + suffix, end="", flush=True)
            answer = tty.readline().strip().lower()
        except (EOFError, KeyboardInterrupt, OSError):
            print()
            return False
        finally:
            tty.close()
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
    """Build the MCP TypeScript server, reporting it as a setup check.

    Delegates rather than reimplementing. This was a near-copy of
    `_ensure_mcp_built` and carried every defect that one had: it ran
    `npm install` before deciding whether a build was needed, judged freshness
    from `dist/index.js` alone, and let a missing npm raise. That last one is
    not theoretical here -- `run_setup` calls this unguarded, and `quern update`
    calls `run_setup`, so the crash that took down `quern start` (#193) had a
    second route into the updater.

    Two copies of a decision drift, and the one a reader happens to open wins.
    """
    mcp_dir = project_root / "mcp"
    if not (mcp_dir / "src").exists():
        return CheckResult(
            name="MCP server",
            status=CheckStatus.WARNING,
            message="mcp/src/ not found — skipped",
        )

    from server.__main__ import _ensure_mcp_built

    if _ensure_mcp_built(quiet=False):
        return CheckResult(
            name="MCP server",
            status=CheckStatus.OK,
            message="Up to date",
        )
    return CheckResult(
        name="MCP server",
        status=CheckStatus.ERROR,
        message="build failed",
        detail=(
            "Try manually: cd mcp && npm install && npm run build. If npm is "
            "not found, note that a GUI launch does not see a node installed "
            "by fnm or nvm — run this from a terminal."
        ),
    )


def _other_quern_on_path(ours: Path) -> list[Path]:
    """Other executables named `quern` on PATH, in PATH order.

    A second copy is not itself a problem -- ours is normally found first. The
    problem is that zsh caches the path it resolved for a command and does not
    notice a new file appearing in a directory already on PATH. So the shell
    that just ran setup keeps invoking the old copy, while `type -a` re-scans
    and reports ours, which reads as though the right wrapper is running. The
    cure is `rehash`, and it is only worth mentioning when a stale hash is
    actually possible -- with no second copy, zsh re-scans on its own.
    """
    found: list[Path] = []
    for entry in os.environ.get("PATH", "").split(":"):
        if not entry:
            continue
        candidate = Path(entry) / "quern"
        try:
            if candidate == ours or not os.access(candidate, os.X_OK):
                continue
            if not candidate.is_file():
                continue
        except OSError:
            continue
        if candidate not in found:
            found.append(candidate)
    return found


#: Where the `quern` wrapper lives.
#:
#: A module constant rather than `Path.home() / ...` computed at each use, so
#: tests can redirect it the way they already redirect INSTALL_MANIFEST. They
#: could not: `run_uninstall` built the path inline, the uninstall tests patched
#: six other things and not `Path.home`, and so every full test run deleted the
#: developer's own wrapper. On a machine where `~/.local/bin/quern` is the only
#: way `quern` resolves, that breaks the CLI until setup is run again -- which
#: presented as the command working intermittently for months.
WRAPPER_PATH = Path.home() / ".local" / "bin" / "quern"


def install_wrapper_script() -> CheckResult:
    """Install quern wrapper script to ~/.local/bin.

    Messages built after this point start saying `quern` rather than a path,
    because `quern_cmd` resolves per call rather than caching -- setup is
    precisely the process where the right answer changes partway through.
    """
    local_bin = WRAPPER_PATH.parent
    wrapper_path = WRAPPER_PATH

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

        shadowed = _other_quern_on_path(wrapper_path)
        if shadowed:
            listed = "\n".join(f"    {p}" for p in shadowed)
            return CheckResult(
                name="Wrapper script",
                status=CheckStatus.WARNING,
                message=f"Installed to {wrapper_path}",
                detail=(
                    "Another quern is on your PATH:\n"
                    f"{listed}\n"
                    "  Your shell may have cached it and will keep running it.\n"
                    "  Run: rehash   (or open a new terminal)"
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


def build_preview_app() -> CheckResult:
    """Compile the screen-mirror app during setup rather than on first use.

    It used to be built lazily, the first time something asked for a preview.
    That left a hole: the menu-bar app only offers "Screen Mirror…" when the
    bundle exists on disk, so on a fresh install the item was missing until
    the user had already driven a preview from the API or an MCP tool. The
    menu bar is the route for people who would rather not do that, so the
    feature was hidden from exactly the audience it is for.

    Building it here costs about 1.5 seconds and makes the menu item appear.

    Treated as optional, the way scrcpy is: a machine without Xcode Command
    Line Tools cannot compile it, and that is a missing convenience rather
    than a broken install.
    """
    name = "Screen mirror (Quern Preview)"

    if _which("swiftc") is None:
        result = CheckResult(
            name=name,
            status=CheckStatus.SKIPPED,
            message="Xcode Command Line Tools not found — screen mirror unavailable",
            detail="Install with: xcode-select --install",
            fixable=True,
        )
        if _prompt_yn(
            "    Xcode Command Line Tools not found (needed for the screen-mirror "
            "app). Open the installer?",
        ):
            # `xcode-select --install` hands off to a macOS dialog and returns
            # immediately, so there is nothing to wait on and no exit code
            # worth trusting -- it also reports failure when the tools are
            # already present. Say what happens next instead.
            _run(["xcode-select", "--install"])
            print("    A macOS installer dialog should have opened.")
            print(f"    Re-run `{quern_cmd()} setup` once it finishes to build the app.")
        return result

    try:
        from server.device.preview import build_preview_bundle

        build_preview_bundle()
    except (RuntimeError, OSError) as e:
        # OSError as well as RuntimeError, per the error-path convention in
        # CONTRIBUTING: catch the base class, not the subclasses seen so far.
        # The build stats files, creates directories, writes a plist, copies an
        # icon and launches a process -- an unwritable ~/.quern or a transient
        # filesystem fault raises OSError, and catching only RuntimeError would
        # end setup entirely over an optional convenience.
        return CheckResult(
            name=name,
            status=CheckStatus.WARNING,
            message="Could not build the screen-mirror app",
            detail=str(e),
        )

    return CheckResult(
        name=name,
        status=CheckStatus.OK,
        message="Built (available from the menu bar)",
    )


# The Developer ID team that signs Quern releases. A fetched app must carry
# this, not merely a valid signature from anyone.
RELEASE_TEAM_ID = "3QUH73KW5Q"


MENUBAR_APP_DIR = Path.home() / "Applications"
"""Where the menu-bar app is installed for the user to find.

Not the install root. The release asset delivers the app to
``~/.local/share/quern``, but that is a dot-directory: Spotlight excludes it,
Launchpad does not look there, and neither does a person. An app nobody can
find is one nobody can restart -- which matters because quitting it is a menu
item.

~/Applications rather than /Applications so no admin prompt appears inside an
otherwise unprivileged setup.
"""


def menubar_app_path() -> Path:
    """The installed location of the menu-bar app."""
    return MENUBAR_APP_DIR / "Quern.app"


class _UntrustedBundle(RuntimeError):
    """The downloaded app is not the one we would have published.

    Separate from every other failure in the fetch so it can be reported
    differently: a transfer that failed is a network problem and the manual
    download is a fine answer, while a bundle that failed verification must
    not be installed by hand either.
    """


def _verify_menubar_app(app: Path, expected_version: str) -> None:
    """Raise unless the bundle is signed by us, notarized, accepted and current.

    Three checks, because each catches something the others do not: the
    signature can be valid while belonging to someone else, the team can match
    on a bundle that was never notarized, and a bundle can carry a stapled
    ticket while its resources have been altered -- that last one is real, and
    an earlier version of this function produced exactly it by extracting with
    Python's tarfile.
    """
    # The designated requirement is the check that actually anchors to Apple.
    # `codesign --verify` alone validates the internal seal, not the
    # certificate chain, and TeamIdentifier is read out of the leaf's OU field
    # -- a self-signed certificate can simply claim ours. `spctl` does anchor,
    # but a user can turn assessments off. `-R` cannot be disabled or spoofed.
    requirement = (
        f'anchor apple generic and certificate leaf[subject.OU] = "{RELEASE_TEAM_ID}"'
    )
    checks = [
        (
            ["codesign", "--verify", "--deep", "--strict", f"-R={requirement}", str(app)],
            "signature is not valid, or is not ours",
        ),
        (["spctl", "-a", "-vvv", str(app)], "Gatekeeper rejects it"),
    ]
    for cmd, failure in checks:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)  # noqa: S603
        if proc.returncode != 0:
            raise _UntrustedBundle(
                f"{failure} "
                f"({proc.stderr.strip() or proc.stdout.strip()})"
            )

    proc = subprocess.run(  # noqa: S603
        ["codesign", "-dv", str(app)], capture_output=True, text=True, timeout=60,
    )
    team = None
    for line in (proc.stderr + proc.stdout).splitlines():
        if line.startswith("TeamIdentifier="):
            team = line.split("=", 1)[1].strip()
            break
    if team != RELEASE_TEAM_ID:
        raise _UntrustedBundle(
            f"signed by team {team or '<none>'}, expected {RELEASE_TEAM_ID}"
        )

    # Bind the bundle to the release that was asked for. Everything above is
    # satisfied by *any* genuine Quern app we ever signed, so a replaced asset
    # containing an older real build would pass all of it -- a downgrade, not
    # a forgery. The asset name identifies the release; this checks that its
    # contents agree.
    proc = subprocess.run(  # noqa: S603
        [
            "/usr/libexec/PlistBuddy", "-c", "Print :CFBundleShortVersionString",
            str(app / "Contents" / "Info.plist"),
        ],
        capture_output=True, text=True, timeout=60,
    )
    stamped = proc.stdout.strip()
    if proc.returncode != 0 or stamped != expected_version:
        raise _UntrustedBundle(
            f"it is v{stamped or '<unknown>'}, but v{expected_version} was requested"
        )


#: A size cap as well as a clock. The deadline bounds how long a hostile or
#: broken server can stream, not how much it can write: at line rate, 180s is
#: tens of gigabytes into the install volume. The real asset is single-digit
#: megabytes. A module constant so a test can shrink it rather than writing
#: 200MB to prove the cap exists.
MAX_ASSET_BYTES = 200 * 1024 * 1024


def download_release_app(url: str, version: str, work: Path) -> Path:
    """Download the release asset at `url` into `work` and return its verified
    Quern.app. Raises `_UntrustedBundle` if it does not verify, and RuntimeError
    or OSError for anything else.

    `work` should be on the same filesystem as wherever the app ends up. On
    this project's own machines the install and $TMPDIR sit on different
    volumes, and a move between them is copytree + rmtree: a failure mid-copy
    leaves a partial Quern.app, and what gets verified is not byte-for-byte
    what gets installed. Staying on one filesystem makes the final step a
    rename.
    """
    import urllib.request

    asset_name = f"quern-{version}.tar.gz"
    tarball = work / asset_name
    # urlretrieve takes no timeout and defaults to none, so a stalled
    # transfer hangs setup with no deadline at all. Stream it instead,
    # with a socket timeout and a whole-operation deadline -- a partial
    # download that never finishes is the failure mode here, not a slow
    # one.
    deadline = time.monotonic() + 180
    max_bytes = MAX_ASSET_BYTES
    written = 0
    with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310
        with open(tarball, "wb") as out:
            while True:
                if time.monotonic() > deadline:
                    raise RuntimeError(
                        "download exceeded 180s; giving up rather than "
                        "holding setup open"
                    )
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise RuntimeError(
                        f"download exceeded {max_bytes // (1024 * 1024)}MB; "
                        "refusing to keep writing"
                    )
                out.write(chunk)

    # macOS tar, not Python's tarfile. The archive carries AppleDouble
    # metadata (`._Contents` and friends); macOS tar applies those as
    # extended attributes and removes them, while tarfile extracts them
    # as literal files *inside* the bundle. That breaks the code
    # signature seal -- CodeResources sealed a directory that did not
    # contain them -- and Gatekeeper then rejects the app with "a
    # sealed resource is missing or invalid". Measured: 21 entries
    # extracted where a correct bundle has 10.
    #
    # Only the app is extracted. The source tree beside it is already
    # installed, and unpacking it over a running install is not this
    # step's job.
    member = f"quern-{version}/Quern.app"
    proc = subprocess.run(  # noqa: S603
        ["/usr/bin/tar", "-xzf", str(tarball), "-C", str(work), member],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"could not extract {member}: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    extracted = work / member
    if not extracted.is_dir():
        raise RuntimeError(f"{asset_name} contains no Quern.app")

    # Verify before installing. This is an executable fetched over the
    # network and then launched, so "the release asset said so" is not
    # sufficient provenance: a replaced asset would otherwise be
    # installed and run. Checked against the identity that signs
    # releases, not merely "validly signed by someone".
    _verify_menubar_app(extracted, version)

    return extracted


def fetch_menubar_app(project_root: Path) -> CheckResult | None:
    """Fetch the menu-bar app when a release install is missing it.

    v0.15.0 reached existing users without the app. Their updater ran from an
    older version that fetched GitHub's generated source tarball, because the
    code that prefers the release asset shipped *inside* that asset and so
    could not help itself. The update reported success, setup reported all
    checks passed, and the app was simply absent with nothing saying so.

    Those machines will not repair themselves: `quern update` sees the latest
    version already installed and downloads nothing. Setup is the first code
    of ours that runs on them, so the guarantee belongs here rather than in
    the download path -- any future capability delivered only via the asset
    would hit the same bootstrap problem.

    Returns None when there is nothing to do: not macOS, a git checkout (where
    a developer builds the app themselves), or the app is already present.
    """
    if platform.system() != "Darwin":
        return None
    if (project_root / ".git").exists():
        return None
    # Both locations. The asset delivers to the install root and
    # launch_menubar_app moves it to ~/Applications, so after the first setup
    # the app is only in the second place -- checking the delivery location
    # alone would re-download it on every run.
    app = project_root / "Quern.app"
    if app.exists() or menubar_app_path().exists():
        return None

    import http.client
    import json as _json
    import tempfile
    import urllib.request

    from server import get_version

    name = "Quern app"
    version = get_version()
    asset_name = f"quern-{version}.tar.gz"
    manual = (
        f"Download {asset_name} from\n"
        f"      https://github.com/quern-dev/quern/releases/tag/v{version}\n"
        f"      and copy Quern.app to {project_root}"
    )

    try:
        api = f"https://api.github.com/repos/quern-dev/quern/releases/tags/v{version}"
        with urllib.request.urlopen(api, timeout=15) as resp:  # noqa: S310
            release = _json.loads(resp.read())

        url = next(
            (
                a.get("browser_download_url")
                for a in release.get("assets", [])
                if a.get("name") == asset_name
            ),
            None,
        )
        if url and not url.startswith("https://github.com/"):
            # The URL comes out of the API response. Releases are served from
            # github.com; anything else means the response is not what we
            # think it is, and following it would fetch code from elsewhere.
            raise _UntrustedBundle(f"asset URL is not on github.com: {url}")
        if not url:
            # Releases cut before the asset existed have nothing to offer, and
            # saying "not available for this release" is more useful than a
            # download error.
            return CheckResult(
                name=name,
                status=CheckStatus.SKIPPED,
                message=f"Not published with v{version}",
                detail="This release has no Quern app asset.",
            )

        print(f"    Quern app missing — fetching it from the v{version} release...")
        # Beside the destination, so the final step is a rename -- see
        # download_release_app.
        with tempfile.TemporaryDirectory(dir=project_root) as tmp:
            extracted = download_release_app(url, version, Path(tmp))
            # os.replace, so the destination either has the whole verified
            # bundle or nothing at all. A half-written Quern.app would be
            # launched by the next step and would make every future setup
            # return early, wedging the install with no route back.
            os.replace(str(extracted), str(app))
    except _UntrustedBundle as e:
        # Distinct from a transfer failure on purpose. This is the one case
        # where the manual route must NOT be offered: it would tell the user
        # to download by hand the very asset that just failed verification,
        # bypassing the check entirely.
        return CheckResult(
            name=name,
            status=CheckStatus.ERROR,
            message="The downloaded Quern app failed verification",
            detail=(
                f"{e}\n"
                "      Not installed. This is not a network problem -- the "
                "asset did not verify.\n"
                "      Do not install it by hand; report it instead."
            ),
        )
    except (OSError, RuntimeError, ValueError, http.client.HTTPException,
            subprocess.SubprocessError) as e:
        # Never fatal. A missing Quern app is a missing convenience, and
        # failing setup over it would be worse than the gap it fills.
        # HTTPException is not an OSError, so a truncated response used to come
        # out of here as a traceback despite that intent.
        return CheckResult(
            name=name,
            status=CheckStatus.WARNING,
            message="Could not fetch the Quern app",
            detail=f"{e}\n      {manual}",
        )

    return CheckResult(
        name=name,
        status=CheckStatus.OK,
        message=f"Fetched from the v{version} release",
    )


def launch_menubar_app(project_root: Path) -> CheckResult | None:
    """Install the menu-bar app where it can be found, and (re)launch it.

    Two things this has to get right, both learned the hard way.

    **Location.** The release asset delivers ``Quern.app`` to the install root,
    which lives under ``~/.local`` -- a dot-directory Spotlight excludes. An
    app installed there cannot be found by search, does not appear in
    Launchpad, and is in neither place a person looks. Since quitting it is a
    menu item, that makes it unrestartable in practice. It is moved to
    ``~/Applications``.

    **Relaunch.** ``open`` on a bundle activates a running instance rather than
    starting the new binary, so after an update the old build simply stayed
    running while setup reported "Launched" -- true, and describing something
    that had not happened. A running instance is asked to quit first.

    Returns None for source-only installs (no app in the payload).
    """
    delivered = project_root / "Quern.app"
    installed = menubar_app_path()

    if not delivered.exists() and not installed.exists():
        return None

    if not delivered.exists() and _menubar_app_running():
        # Nothing new to run, so nothing to restart. This used to quit and
        # reopen the app on every setup -- which is every setup on a git
        # install, where nothing is ever delivered -- and when the reopen
        # failed it left the machine with no menu bar at all (#215).
        return CheckResult(
            name="Quern app",
            status=CheckStatus.OK,
            message=f"Running from {installed}",
        )

    # Move the delivered copy into place, replacing any older install.
    if delivered.exists():
        try:
            MENUBAR_APP_DIR.mkdir(parents=True, exist_ok=True)
            staging = installed.with_name("Quern.app.incoming")
            shutil.rmtree(staging, ignore_errors=True)
            shutil.move(str(delivered), str(staging))
            # Quit before replacing: a running app whose bundle is swapped
            # underneath it keeps executing the old image from an inode that
            # no longer has a name, which is a confusing state to debug.
            # Whether it was running is recorded first: a first install has
            # nothing to stop, and must not later claim it stopped something.
            #
            # The bundle being replaced, not any Quern: the quit asks by
            # application name, so asking for it when someone else's copy is
            # running stops an app this has no business stopping.
            stopped = _menubar_app_running(installed)
            if stopped and not _quit_menubar_app(installed):
                # Not replaced: a bundle swapped under a live app leaves it
                # executing an image that no longer has a name.
                return CheckResult(
                    name="Quern app",
                    status=CheckStatus.WARNING,
                    message="Not updated — the running app would not quit",
                    detail=f"Quit Quern from its menu, then run: {quern_cmd()} setup",
                )
            shutil.rmtree(installed, ignore_errors=True)
            os.replace(str(staging), str(installed))
        except OSError as e:
            # Put it back if we can. The move empties the payload directory
            # before the final rename, so a failure after that point left the
            # app at the staging name while this message pointed at a path
            # that no longer existed.
            location = next(
                (c for c in (installed, staging, delivered) if c.exists()), None
            )
            if location == staging:
                with contextlib.suppress(OSError):
                    shutil.move(str(staging), str(delivered))
                    location = delivered
            where = (
                f"The app is at {location}."
                if location
                else (
                    "The app could not be located; re-run "
                    f"`{quern_cmd()} setup` to fetch it again."
                )
            )
            return CheckResult(
                name="Quern app",
                status=CheckStatus.WARNING,
                message=f"Could not install to {MENUBAR_APP_DIR}",
                detail=f"{e}\n      {where}",
            )
    else:
        stopped = False

    rc, err = _open_menubar_app(installed)
    if rc == 0:
        return CheckResult(
            name="Quern app",
            status=CheckStatus.OK,
            message=f"Running from {installed}",
        )
    start = f"open {shlex.quote(str(installed))}"
    if stopped:
        # We stopped the running app to install this one, so the machine now
        # has no menu bar because of us. Say that, not merely that a launch
        # failed.
        return CheckResult(
            name="Quern app",
            status=CheckStatus.WARNING,
            message="Stopped to install the new version, and did not restart",
            detail=f"{err.strip() or 'open failed'}\n      Start it with: {start}",
        )
    return CheckResult(
        name="Quern app",
        status=CheckStatus.WARNING,
        message="Could not launch Quern.app",
        detail=f"{err.strip() or 'open failed'}\n      Try: {start}",
    )


_MENUBAR_PROCESS = "Quern.app/Contents/MacOS/QuernMenuBar"

#: `open` answering -600 (procNotFound) right after a quit, measured once on
#: a live update: a manual `open` a minute later worked. The quit wait below
#: ends when pgrep stops listing the process, which is not necessarily when
#: LaunchServices has finished with it, so a short retry covers the gap.
_OPEN_ATTEMPTS = 5
_OPEN_RETRY_DELAY = 1.0
_LS_PROC_NOT_FOUND = "-600"


def _menubar_app_running(app: Path | None = None) -> bool:
    """Whether a menu-bar app is running -- any copy, or the one at `app`.

    Any copy by default, which is the cautious answer for setup: opening a
    second app beside one already running is worse than leaving it. `quern
    menubar` names its bundle, because "running" there is a claim about that
    app, and with a second copy anywhere on disk the general match reported a
    freshly installed, never-launched app as running.
    """
    import re

    pattern = (f"{re.escape(str(app))}/Contents/MacOS/QuernMenuBar"
               if app is not None else _MENUBAR_PROCESS)
    rc, out, _err = _run(["pgrep", "-f", pattern])
    return rc == 0 and bool(out.strip())


def _menubar_app_pids(app: Path | None = None) -> list[str]:
    """The running menu-bar processes -- any copy, or the one at `app`."""
    import re

    pattern = (f"{re.escape(str(app))}/Contents/MacOS/QuernMenuBar"
               if app is not None else _MENUBAR_PROCESS)
    rc, out, _err = _run(["pgrep", "-f", pattern])
    return out.split() if rc == 0 else []


def _open_menubar_app(app: Path) -> tuple[int, str]:
    """`open` the app, retrying only the error a just-quit app produces."""
    rc, err = 1, ""
    for attempt in range(_OPEN_ATTEMPTS):
        rc, _out, err = _run(["open", str(app)])
        if rc == 0 or _LS_PROC_NOT_FOUND not in err:
            return rc, err
        if attempt + 1 < _OPEN_ATTEMPTS:
            time.sleep(_OPEN_RETRY_DELAY)
    return rc, err


def _quit_menubar_app(app: Path | None = None) -> bool:
    """Stop the menu-bar app at `app`, and say whether it is gone.

    Returns True when nothing was running there, or when it exited. **False
    means it is still alive**, and a caller about to replace its bundle must
    stop: swapping the bundle under a live app leaves it executing an image
    with no name on disk, which is a confusing state to debug and the reason
    this quit exists at all.

    Two ways, in order of politeness:

    * `osascript` asks the application to quit, so it can tear down its status
      item. But AppleScript addresses an application by *name*, so it reaches
      whichever copy macOS has registered -- possibly someone else's checkout.
      It is therefore used only when the copy we mean is the only one running.
    * Otherwise, and as a fallback, SIGTERM to the pids of *that bundle*, which
      cannot touch another copy.

    With no `app`, this keeps the old behaviour for callers that mean "any
    copy": ask by name, wait, report.
    """
    ours = _menubar_app_pids(app)
    if not ours:
        return True

    others = [pid for pid in _menubar_app_pids() if pid not in ours]
    if not others:
        _run(["osascript", "-e", 'tell application "Quern" to quit'], timeout=10)
        if _wait_for_exit(app):
            return True

    # Either another copy is running -- and asking by name could stop it -- or
    # the polite request did not take.
    if ours:
        _run(["kill", "-TERM", *ours], timeout=10)
    return _wait_for_exit(app)


def _wait_for_exit(app: Path | None, attempts: int = 20) -> bool:
    for _ in range(attempts):
        if not _menubar_app_running(app):
            return True
        time.sleep(0.25)
    return not _menubar_app_running(app)


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

    # Retiring a skill leaves a symlink behind on every machine that ever ran
    # setup, pointing at a directory that no longer exists. Only links we own
    # are removed: a symlink into our own skills directory whose target is gone.
    removed = []
    for link_path in sorted(skills_dest.iterdir()):
        if not link_path.is_symlink() or link_path.exists():
            continue
        try:
            target = Path(os.readlink(link_path))
        except OSError:
            continue
        # A relative target is relative to the link's own directory, not to
        # wherever this process happens to be running.
        if not target.is_absolute():
            target = link_path.parent / target
        if target.parent.resolve() != skills_src.resolve():
            continue
        try:
            link_path.unlink()
        except OSError:
            # One link we cannot remove is not a reason to leave the rest.
            continue
        removed.append(link_path.name)

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

    if not installed and not removed:
        return CheckResult(
            name="Claude Code skills",
            status=CheckStatus.OK,
            message="No skills to install",
        )

    summary = f"Linked {len(installed)} skill(s) to ~/.claude/skills/"
    if removed:
        summary += f", removed {len(removed)} stale link(s)"
    return CheckResult(
        name="Claude Code skills",
        status=CheckStatus.OK,
        message=summary,
        detail=", ".join(installed + [f"{name} (stale link removed)" for name in removed]),
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
    quern_dir = CONFIG_DIR
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
    # /usr/bin/xcrun ships on every modern macOS as an install-prompt stub
    # even when no CLT is present, so `which xcrun` returning a path isn't
    # proof anything works. Gate on xcode-select reporting an actual
    # developer dir before invoking xcrun — otherwise we'd trigger the
    # macOS install dialog here on Android-only machines.
    if not xcode_available():
        return CheckResult(
            name="Xcode CLI Tools",
            status=CheckStatus.MISSING,
            message="Not installed (no developer directory configured)",
            detail=(
                "Install Xcode or the Command Line Tools to enable iOS support. "
                "If you only need Android, this is safe to ignore — Quern will "
                "skip the iOS toolchain. Install with: xcode-select --install"
            ),
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


def check_node(sites: list | None = None) -> CheckResult:
    """Check the `node` every part of the system will run, not just ours (#214).

    Four places pick a `node` and they disagree routinely -- see
    `server.lifecycle.node_env`. Only a *missing* node here is MISSING, since
    that is the one setup can offer to fix and the one the MCP build needs.
    Anything else wrong -- too old, or absent where GUI apps look -- is a
    WARNING, never a failure: an install that has been working must not have
    setup or an update refuse over the user's Node arrangement.
    """
    from server.lifecycle import node_env

    if sites is None:
        try:
            sites = node_env.probe()
        except Exception as exc:  # noqa: BLE001
            # Never fatal. This runs inside `quern update`, *after* the pull:
            # a probe that raised there left the install pulled but not
            # rebuilt, with a traceback, on a machine whose node was fine.
            return CheckResult(
                name="Node.js", status=CheckStatus.WARNING,
                message="could not be checked",
                detail=f"{exc}\nThe MCP wrapper needs Node {node_env.MIN_NODE_MAJOR}+; "
                       f"{quern_cmd()} doctor shows each place a node is picked.",
            )
    here = sites[0]
    if here.status == node_env.MISSING:
        return CheckResult(
            name="Node.js",
            status=CheckStatus.MISSING,
            message="Not installed (needed for MCP server)",
            fixable=True,
        )

    problems = [site for site in sites if site.status not in (node_env.OK, node_env.SKIPPED)]
    if not problems:
        return CheckResult(name="Node.js", status=CheckStatus.OK,
                           message=here.version or "installed")

    lines = []
    for site in problems:
        found = f"{site.version or 'no version'} at {site.path}" if site.path else site.status
        lines.append(f"{site.place} ({site.used_by}): {found}")
        lines.append(f"  {node_env.fix_for(site, sites)}")
    lines.append(f"Details: {quern_cmd()} doctor")
    names = ", ".join(site.place for site in problems)
    return CheckResult(
        name="Node.js",
        status=CheckStatus.WARNING,
        message=f"{here.version or 'installed'} here; needs attention for: {names} "
                f"(the MCP wrapper needs Node {node_env.MIN_NODE_MAJOR}+)",
        detail="\n".join(lines),
    )


def check_menubar_current(project_root: Path) -> CheckResult | None:
    """Say when a git install's menu-bar app is older than quern (#200).

    A release install gets the matching app with every update; a git install
    never does, and nothing said so. A warning with the command, not an
    install: a developer may be running their own build on purpose.
    """
    if platform.system() != "Darwin" or not (project_root / ".git").exists():
        return None
    from server.lifecycle import menubar

    state = menubar.state()
    if not state.behind:
        return None
    return CheckResult(
        name="Quern app version",
        status=CheckStatus.WARNING,
        message=f"v{state.version} is older than quern v{state.quern_version}",
        detail=f"A git install's updates don't include the app. Run: {quern_cmd()} menubar install",
    )

def _node_can_build(node_result: CheckResult) -> bool:
    """Whether to build the MCP wrapper after the Node check.

    Present is enough: Node 20 builds it fine, and a warning about some *other*
    place's node -- or about this one being too old to *run* it -- is no reason
    to leave the wrapper stale. Before #214 this read `status == OK`, which was
    only equivalent while the check could say nothing but OK or MISSING.
    """
    return node_result.status in (CheckStatus.OK, CheckStatus.WARNING)


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


def _companion_probe_env(companion: Path) -> dict[str, str]:
    """The environment the patched companion needs in order to start.

    It resolves its frameworks through ``DYLD_FRAMEWORK_PATH``, which
    ``IDBController._companion_env`` supplies at runtime. A probe that omits it
    reports a perfectly good install as broken, so this mirrors that function
    rather than running the binary bare. See #190.
    """
    import os

    fw = companion.parent / "Frameworks"
    env = os.environ.copy()
    env["DYLD_FRAMEWORK_PATH"] = f"{fw}:{fw / 'PackageFrameworks'}"
    return env


def check_idb_companion() -> CheckResult:
    """Check for idb_companion, preferring the patched build in ~/.quern/bin/."""
    quern_companion = CONFIG_DIR / "bin" / "idb_companion"
    if quern_companion.is_file():
        # Existence is not health. This reported OK for anything occupying the
        # path, so a truncated download or a half-extracted tarball read as a
        # working install -- and because the patched copy is *preferred*, it
        # would shadow a working system one while claiming to be fine (#190).
        rc, _, _ = _run(
            [str(quern_companion), "--version"],
            timeout=10,
            env=_companion_probe_env(quern_companion),
        )
        if rc != 0:
            return CheckResult(
                name="idb_companion",
                status=CheckStatus.ERROR,
                message=f"installed but not running ({quern_companion})",
                detail=(
                    "The binary is present but exited "
                    f"{rc} when asked for its version. Re-run '{quern_cmd()} setup' "
                    "to reinstall it; until then simulator UI automation will "
                    "fall back to whatever else is available."
                ),
                fixable=True,
            )
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
            detail=(
                "Patched build available with improved Group element "
                f"detection: {quern_cmd()} setup"
            ),
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

    dest = CONFIG_DIR / "bin"
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
               f"Run '{quern_cmd()} start -f --no-crash' to generate it,\n"
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
        PLIST_PATH,
        TUNNELD_URL,
        installed_plist_drift,
    )

    if not PLIST_PATH.exists():
        return CheckResult(
            name="tunneld",
            status=CheckStatus.WARNING,
            message="Not installed",
            detail=f"Install with: {quern_cmd()} tunneld install\n"
                   "Required for physical device screenshots.",
        )

    drift = installed_plist_drift()

    # Check if running
    running = False
    try:
        import urllib.request
        req = urllib.request.Request(TUNNELD_URL, method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            running = resp.status == 200
    except Exception:
        pass

    if drift:
        # The reason, not a guess at it. This reported the log path whichever
        # condition had failed, so a drifted *binary* was described as a stale
        # log path -- with both paths printed identical, because they were.
        # `installed_plist_drift` was written to fix exactly that and this
        # caller was never switched over to it.
        return CheckResult(
            name="tunneld",
            status=CheckStatus.WARNING,
            message=f"Plist outdated — {drift}",
            detail=f"Reinstall with: {quern_cmd()} tunneld install",
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
        detail=f"Try: {quern_cmd()} tunneld restart",
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
    # Skip the simctl probe when there's no developer dir — would trigger
    # the macOS install dialog otherwise.
    if not xcode_available():
        return []
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
                cert_manager.is_cert_installed(controller, udid)
            )
        finally:
            loop.close()
    except Exception:
        # Still fail safe -- setup must not crash because a cert check did --
        # but not silently. This swallowed a TypeError from a stale call
        # signature, so a wrong argument reported "no cert installed" and setup
        # cheerfully offered to reinstall one that was already there.
        import logging

        logging.getLogger("quern-debug-server.setup").warning(
            "Could not check the CA on %s", udid, exc_info=True,
        )
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
                cert_manager.is_cert_installed(controller, udid)
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


def _print_unasked() -> None:
    """Name the questions nobody was asked, if there were any.

    A function rather than inline at the end of `run_setup`, because the early
    exits need it too. Without a terminal every prompt declines rather than
    hanging, so a menu-bar or `curl | bash` setup on a machine with no venv
    lands on the declined-venv exit *every time* -- and was told it "declined"
    something it was never asked, with no pointer to run setup where it can be.
    """
    if not _UNASKED:
        return
    # Named, not counted. "3 questions were skipped" tells the reader they
    # missed something without telling them what, which is the same dead
    # end as saying nothing.
    print("  Setup had no terminal, so these were declined without asking:")
    for question in _UNASKED:
        print(f"    • {question}")
    print()
    for line in run_it_yourself([quern_cmd(), "setup"]):
        print(f"  {line}")
    print()


def run_setup(assume_yes: bool = False) -> int:
    """Run the interactive setup. Returns 0 on success, 1 on errors.

    `assume_yes` answers every prompt with its default, the way `apt-get -y`
    does -- except the ones marked `deliberate`, which no flag answers. See
    `_prompt_yn`.
    """
    global _ASSUME_YES
    _ASSUME_YES = assume_yes
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

    _UNASKED.clear()
    if not _can_prompt():
        print("  No terminal attached, so nothing can be asked. Setup will do")
        print("  what it can and decline the rest rather than answer for you.")
        if invoked_by() == MENUBAR:
            print("  To answer them, open a terminal and run: quern setup")
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
        print(f"  Install it first, then re-run: {quern_cmd()} setup")
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
                print(f"    {quern_cmd()} setup")
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
                            # The venv has been deleted and not replaced. Falling
                            # through from here reached the branch below, which
                            # prints "Virtual environment found but not
                            # activated" -- of a directory that no longer exists
                            # -- and then re-execs into it, returning -1 and
                            # exiting 255 with no summary and no guidance. Same
                            # shape as the declined-venv fall-through, one branch
                            # up: a prompt accepted, and execution continuing
                            # into code that assumes it worked.
                            report.add(CheckResult(
                                name="Virtual env",
                                status=CheckStatus.ERROR,
                                message=f"Could not recreate the venv with {best}",
                                detail=(
                                    f"The previous virtualenv at {venv_path} has "
                                    "been removed and the replacement could not "
                                    "be built, so there is no environment to run "
                                    "in.\nTo build one by hand:\n"
                                    f"  {best} -m venv {venv_path}\n"
                                    f"  source {venv_path}/bin/activate\n"
                                    '  pip install -e ".[dev]"'
                                ),
                            ))
                            report.print_summary()
                            _print_unasked()
                            return 1

            # Venv exists but not activated — re-exec inside it
            print("    Virtual environment found but not activated.")
            print(f"    Re-running setup inside {venv_path}...")
            return _reexec_in_venv(venv_path)
        else:
            # No venv — create it, then re-exec. Not asked about: a venv inside
            # the install directory *is* the install, the way node_modules is
            # `npm install`. Asking made an unattended run decline it and stop
            # with a tree it could not run, and there was never a second
            # answer: every check below this point needs it.
            #
            # What used to live here was the *declined* branch, which existed
            # because declining fell through to the block below -- commented
            # "we're inside the venv" and reporting the check OK -- and setup
            # then died several hundred lines later on `ModuleNotFoundError:
            # No module named 'httpx'`, naming a dependency the user never
            # mentioned. There is nothing left to decline.
            print("    No virtual environment found. Creating one...")
            if create_venv(project_root):
                return _reexec_in_venv(venv_path)
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
            _print_unasked()
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
                            detail=f"Try manually: {quern_cmd()} tunneld install",
                        )
            elif needs_install:
                tunneld_result = CheckResult(
                    name="tunneld",
                    status=CheckStatus.WARNING,
                    message="Not installed (install pymobiledevice3 first)",
                    detail=(
                        "Install with: pipx install pymobiledevice3 && "
                        f"{quern_cmd()} tunneld install"
                    ),
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
                    if _prompt_yn(
                        "    Install mitmproxy CA cert into booted simulators?",
                        deliberate=True,
                    ):
                        for sim in needs_cert:
                            result = install_cert_simulator(sim["udid"], sim["name"])
                            report.add(result)
            else:
                print("    Booted simulators found but no CA cert yet — skipping cert install.")
                print("    Start the proxy once, then re-run setup to install certs.")

    # ── Wrapper script installation ──

    report.add(install_wrapper_script())

    # ── Screen-mirror app ──
    #
    # Before the menu-bar launch below, deliberately: the menu only shows
    # "Screen Mirror…" when this bundle exists, and it reads that at launch.

    report.add(build_preview_app())

    # ── Menu-bar app (only present in bundled release tarballs) ──

    if project_root:
        # Fetch before launching: a release install that arrived without the
        # app has nothing to launch, and reported nothing at all rather than
        # saying so.
        fetch_result = fetch_menubar_app(project_root)
        if fetch_result is not None:
            report.add(fetch_result)
        menubar_result = launch_menubar_app(project_root)
        if menubar_result is not None:
            report.add(menubar_result)
        stale = check_menubar_current(project_root)
        if stale is not None:
            report.add(stale)

    # ── Claude Code skills ──

    if project_root:
        report.add(_install_skills(project_root))

    # ── Claude Code pre-commit checklist hook ──

    if project_root:
        report.add(_install_precommit_hook(project_root))

    # ── Build MCP server ──
    # Build the TypeScript MCP server so it's ready when Claude Code connects.
    # Without this, the MCP shows as broken until the first `quern start`.

    if _node_can_build(node_result) and project_root:
        report.add(_build_mcp(project_root))

    # ── Tool inventory ──
    #
    # Last, and never fatal: it answers "what is this machine actually
    # running", which is the question a "works on mine" turns into. Recording
    # it here means a later doctor can say what moved.

    for result in report_tool_sites(record=True):
        report.add(result)

    # ── Summary ──

    report.print_summary()

    _print_unasked()

    return 1 if report.has_errors else 0


# ── Tool inventory ───────────────────────────────────────────────────────


TOOL_SNAPSHOT = CONFIG_DIR / "tool-sites.json"


def _collect_sites_sync() -> list[dict]:
    """Every install site quern uses. Sync, because setup is."""
    import asyncio

    from server.device.tool_versions import collect_sites, upgrade_note

    try:
        sites = asyncio.run(collect_sites())
    except Exception:
        return []
    return [
        {
            "name": s.name, "role": s.role, "version": s.version,
            "path": s.path, "source": s.source, "available": s.available,
            "volatile_path": s.volatile_path, "upgrade_note": upgrade_note(s),
            "diagnostic": s.diagnostic,
        }
        for s in sites
    ]


def record_tool_sites() -> None:
    """Write down what was in use, so a later run can see what moved.

    A snapshot of observed reality with a timestamp, not a claim about what
    was tested -- it cannot go stale in the way a hand-maintained manifest can,
    because nothing has to remember to update it.

    Volatile paths are stored with the path dropped. fnm hands node out from a
    directory named for the pid that asked, so recording the path would
    guarantee a spurious "moved" on the next shell; the source is the durable
    part.
    """
    import json

    sites = _collect_sites_sync()
    if not sites:
        return
    for site in sites:
        if site.get("volatile_path"):
            site["path"] = None
    try:
        TOOL_SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        TOOL_SNAPSHOT.write_text(json.dumps(
            {"recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "sites": sites},
            indent=2,
        ))
    except OSError:
        pass


def _load_recorded_sites() -> dict:
    """The recorded snapshot, or nothing if it is not one.

    Shape-checked rather than trusted. It is a file on disk that a person can
    edit, and a doctor run that raises on a malformed snapshot fails at the
    point it was meant to be reporting.
    """
    import json

    try:
        data = json.loads(TOOL_SNAPSHOT.read_text())
    except (OSError, ValueError):
        # ValueError covers both JSONDecodeError and UnicodeDecodeError: bytes
        # that are not valid text fail in read_text, before json sees them.
        return {}
    if not isinstance(data, dict):
        return {}
    sites = data.get("sites")
    if not isinstance(sites, list):
        return {}
    usable = [
        s for s in sites
        if isinstance(s, dict) and isinstance(s.get("name"), str)
        and isinstance(s.get("role"), str)
    ]
    return {"recorded_at": data.get("recorded_at"), "sites": usable}


def report_tool_sites(record: bool = False) -> list[CheckResult]:
    """What quern is using, and anything that has moved since it was recorded."""
    sites = _collect_sites_sync()
    if not sites:
        return []

    results: list[CheckResult] = []
    for site in sites:
        label = f"{site['name']} ({site['role']})"
        if not site["available"]:
            results.append(CheckResult(
                name=label, status=CheckStatus.SKIPPED, message="not found",
            ))
            continue
        where = site["source"]
        if site["volatile_path"]:
            where += " (per-shell path)"
        # A tool that answered correctly but complained on the way. Reported as
        # a warning rather than OK: nothing failed, which is exactly why it
        # would otherwise go unnoticed until it surfaced as something else.
        # The remedy is in the tool's own environment, so quern names it and
        # changes nothing -- that venv is not ours to rewrite.
        diagnostic = site.get("diagnostic")
        notes = [n for n in (site.get("upgrade_note"), diagnostic) if n]
        results.append(CheckResult(
            name=label,
            status=CheckStatus.WARNING if diagnostic else CheckStatus.OK,
            message=f"{site['version'] or 'version unknown'} — {where}",
            detail="; ".join(notes),
        ))

    results.extend(_report_drift(sites))
    if record:
        record_tool_sites()
    return results


def _report_drift(sites: list[dict]) -> list[CheckResult]:
    """Anything that changed version or moved since it was last recorded."""
    recorded = _load_recorded_sites()
    previous = {(s["name"], s["role"]): s for s in recorded.get("sites", [])}
    if not previous:
        return []

    drifted: list[str] = []
    for site in sites:
        was = previous.get((site["name"], site["role"]))
        if was is None:
            continue
        if was.get("version") and site["version"] and was["version"] != site["version"]:
            drifted.append(
                f"{site['name']} ({site['role']}) {was['version']} → {site['version']}"
            )
        # A volatile path was recorded as None, so it cannot report a move it
        # would make on every shell.
        elif was.get("path") and site["path"] and was["path"] != site["path"]:
            drifted.append(
                f"{site['name']} ({site['role']}) moved: {was['path']} → {site['path']}"
            )
    if not drifted:
        return []
    return [CheckResult(
        name="tool drift",
        status=CheckStatus.WARNING,
        message=f"{len(drifted)} change(s) since {recorded.get('recorded_at', 'the last record')}",
        detail="\n".join("  " + d for d in drifted)
               + f"\n  Re-record with: {quern_cmd()} setup",
    )]


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

    wrapper = WRAPPER_PATH
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
