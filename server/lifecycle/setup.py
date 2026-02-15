"""quern-debug-server setup — interactive environment checker and installer.

Validates the Python virtual environment, system dependencies, installs
missing tools via Homebrew, and optionally configures simulators for proxy use.

Usage:
    quern-debug-server setup
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
            print("  Re-run 'quern-debug-server setup' after resolving them.")
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


def create_venv(project_root: Path) -> bool:
    """Create a .venv and install the project into it. Returns True on success."""
    venv_path = project_root / ".venv"
    print(f"    Creating virtual environment at {venv_path}...")

    rc, _, stderr = _run(
        [sys.executable, "-m", "venv", str(venv_path)], timeout=60,
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

    # Above max — might work but untested
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
               "Run 'quern-debug-server start -f --no-crash' to generate it,\n"
               "then Ctrl+C to stop.",
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


def install_cert_simulator(udid: str, name: str) -> CheckResult:
    """Install mitmproxy CA cert into a booted simulator."""
    cert_path = Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.pem"
    if not cert_path.exists():
        return CheckResult(
            name=f"Cert → {name}",
            status=CheckStatus.SKIPPED,
            message="No CA cert yet (start proxy first)",
        )

    rc, _, stderr = _run([
        "xcrun", "simctl", "keychain", udid, "add-root-cert", str(cert_path),
    ])
    if rc == 0:
        return CheckResult(
            name=f"Cert → {name}",
            status=CheckStatus.OK,
            message="CA certificate trusted",
        )
    return CheckResult(
        name=f"Cert → {name}",
        status=CheckStatus.ERROR,
        message=f"Failed to install cert: {stderr}",
    )


# ── Main setup flow ──────────────────────────────────────────────────────

def _reexec_in_venv(venv_path: Path) -> int:
    """Re-execute setup inside the venv so all checks run in the right environment."""
    venv_python = venv_path / "bin" / "python"
    if not venv_python.exists():
        return -1
    print(f"    Continuing setup inside {venv_path}...")
    print()
    result = subprocess.run(
        [str(venv_python), "-m", "server.main", "setup"],
        cwd=str(venv_path.parent),  # project root
    )
    return result.returncode


def run_setup() -> int:
    """Run the interactive setup. Returns 0 on success, 1 on errors."""
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
        print("  Install it first, then re-run: quern-debug-server setup")
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
                print("    quern-debug-server setup")
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
            if _brew_install("idb-companion"):
                idb_companion_result = check_idb_companion()  # re-check
            else:
                idb_companion_result = CheckResult(
                    name="idb_companion",
                    status=CheckStatus.ERROR,
                    message="Homebrew install failed",
                    detail="Try manually: brew install idb-companion",
                )
    report.add(idb_companion_result)

    idb_result = check_idb()
    if idb_result.status == CheckStatus.MISSING:
        print("    idb CLI not found. This is the Python client for idb_companion.")
        if _prompt_yn("    Install fb-idb via pip?"):
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

    # ── Proxy / network checks ──

    report.add(check_vpn())
    report.add(check_mitmproxy_cert())

    # ── Simulator cert setup ──

    if platform.system() == "Darwin" and _which("xcrun"):
        booted = check_booted_simulators()
        if booted:
            cert_path = Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.pem"
            if cert_path.exists():
                print(f"    Found {len(booted)} booted simulator(s):")
                for sim in booted:
                    print(f"      • {sim['name']} ({sim['udid'][:8]}…)")
                if _prompt_yn("    Install mitmproxy CA cert into booted simulators?"):
                    for sim in booted:
                        result = install_cert_simulator(sim["udid"], sim["name"])
                        report.add(result)
            else:
                print("    Booted simulators found but no CA cert yet — skipping cert install.")
                print("    Start the proxy once, then re-run setup to install certs.")

    # ── Summary ──

    report.print_summary()
    return 1 if report.has_errors else 0
