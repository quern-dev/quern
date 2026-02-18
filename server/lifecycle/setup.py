"""./quern setup — interactive environment checker and installer.

Validates the Python virtual environment, system dependencies, installs
missing tools via Homebrew, and optionally configures simulators for proxy use.

Usage:
    ./quern setup
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
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


def _brew_install(formula: str) -> bool:
    """Install a Homebrew formula. Returns True on success."""
    print(f"    Installing {formula} via Homebrew...")
    try:
        result = subprocess.run(
            ["brew", "install", formula],
            timeout=300,  # 5 min timeout for installs
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _prompt_yn(question: str, default: bool = True) -> bool:
    """Prompt the user for yes/no confirmation."""
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        answer = input(question + suffix).strip().lower()
    except (EOFError, KeyboardInterrupt):
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
    path_export = f'export PATH="$HOME/.local/bin:$PATH"'

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
                print(f"\n~/.local/bin is not in your PATH.")
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
                    f"⚠ Add ~/.local/bin to PATH manually:\n"
                    f"    echo 'export PATH=\"$HOME/.local/bin:$PATH\"' >> ~/.zshrc\n"
                    f"    source ~/.zshrc"
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
            timeout=300,
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
    """Check for idb_companion daemon (needed for idb CLI)."""
    tool = _which("idb_companion")
    if tool:
        version = _get_version(["idb_companion", "--help"])
        msg = "installed"
        return CheckResult(
            name="idb_companion",
            status=CheckStatus.OK,
            message=msg,
        )
    return CheckResult(
        name="idb_companion",
        status=CheckStatus.MISSING,
        message="Not installed (needed for idb CLI)",
        fixable=True,
    )


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
    """Check if pymobiledevice3 is installed (needed for physical device screenshots)."""
    from server.device.tunneld import find_pymobiledevice3_binary

    binary = find_pymobiledevice3_binary()
    if binary:
        rc, stdout, _ = _run([str(binary), "version"])
        version = stdout.strip() if rc == 0 else "installed"
        return CheckResult(
            name="pymobiledevice3",
            status=CheckStatus.OK,
            message=version,
        )
    return CheckResult(
        name="pymobiledevice3",
        status=CheckStatus.WARNING,
        message="Not installed (needed for physical device screenshots)",
        detail="Install with: pipx install pymobiledevice3",
    )


def check_tunneld() -> CheckResult:
    """Check if the tunneld LaunchDaemon is installed and running."""
    from server.device.tunneld import PLIST_PATH, TUNNELD_URL

    if not PLIST_PATH.exists():
        return CheckResult(
            name="tunneld",
            status=CheckStatus.WARNING,
            message="Not installed",
            detail="Install with: ./quern tunneld install\n"
                   "Required for physical device screenshots.",
        )

    # Check if running
    try:
        import urllib.request
        req = urllib.request.Request(TUNNELD_URL, method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            if resp.status == 200:
                return CheckResult(
                    name="tunneld",
                    status=CheckStatus.OK,
                    message=f"Running on {TUNNELD_URL}",
                )
    except Exception:
        pass

    return CheckResult(
        name="tunneld",
        status=CheckStatus.WARNING,
        message="Installed but not running",
        detail="Try: ./quern tunneld restart",
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
    result = subprocess.run(
        [str(venv_python), "-m", "server.main", "setup"],
        cwd=str(venv_path.parent),
        env=env,
    )
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
    print("  Quern Debug Server — Setup")
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

    # ── System dependencies (auto-install via Homebrew) ──

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

    report.add(check_xcode_cli_tools())
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

    # ── idb (for simulator UI automation) ──

    idb_companion_result = check_idb_companion()
    if idb_companion_result.status == CheckStatus.MISSING:
        if _prompt_yn("    idb_companion not found. Install via Homebrew?"):
            # Try the tap first (facebook/fb), then the plain formula
            success = _brew_install("facebook/fb/idb-companion")
            if not success:
                success = _brew_install("idb-companion")
            if success:
                idb_companion_result = check_idb_companion()  # re-check
            else:
                idb_companion_result = CheckResult(
                    name="idb_companion",
                    status=CheckStatus.WARNING,
                    message="Not installed (UI automation unavailable)",
                    detail="Try manually: brew tap facebook/fb && brew install idb-companion\n"
                           "Or see https://fbidb.io for alternative install methods",
                )
    report.add(idb_companion_result)

    idb_result = check_idb()
    if idb_result.status == CheckStatus.MISSING:
        print("    idb CLI not found. This is the Python client for idb_companion.")
        if _prompt_yn("    Install fb-idb via pip?"):
            # Use the venv's pip if we're inside one, otherwise fall back to system
            if sys.prefix != sys.base_prefix:
                pip_cmd = str(Path(sys.prefix) / "bin" / "pip")
            else:
                pip_cmd = "pip" if _which("pip") else "pip3"
            print(f"    Installing fb-idb...")
            try:
                result = subprocess.run([pip_cmd, "install", "fb-idb"], timeout=120)
                if result.returncode == 0:
                    # Rehash pyenv if it's being used
                    if _which("pyenv"):
                        subprocess.run(["pyenv", "rehash"], timeout=10)
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

    # ── Physical device support (pymobiledevice3 + tunneld) ──

    report.add(check_pymobiledevice3())
    report.add(check_tunneld())

    # ── Proxy / network checks ──

    report.add(check_vpn())
    report.add(check_mitmproxy_cert())

    # ── Simulator cert setup ──

    if platform.system() == "Darwin" and _which("xcrun"):
        booted = check_booted_simulators()
        if booted:
            cert_path = Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.pem"
            if cert_path.exists():
                # Check which sims already have the cert vs which need it
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

    # ── Summary ──

    report.print_summary()
    return 1 if report.has_errors else 0
