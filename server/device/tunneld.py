"""tunneld — pymobiledevice3 remote tunneld lifecycle and client operations.

Manages a macOS LaunchDaemon that runs `pymobiledevice3 remote tunneld`,
providing RemoteXPC tunnels for iOS 17+ developer services (screenshots, etc.).

Usage:
    ./quern tunneld install    # Install LaunchDaemon (prompts for sudo)
    ./quern tunneld uninstall  # Remove LaunchDaemon
    ./quern tunneld status     # Show daemon status and connected devices
    ./quern tunneld restart    # Restart the daemon
"""

from __future__ import annotations

import asyncio
import json
import logging
import plistlib
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

import httpx

logger = logging.getLogger("quern-debug-server.tunneld")

TUNNELD_LABEL = "com.quern.tunneld"
TUNNELD_URL = "http://127.0.0.1:49151"
PLIST_PATH = Path("/Library/LaunchDaemons/com.quern.tunneld.plist")
# System-owned location — never references the user's home directory, so the
# daemon can boot before login (or before an external home volume mounts) without
# launchd auto-creating ghost directories under /Volumes/<HomeVolume>/.
LOG_PATH = Path("/Library/Logs/com.quern.tunneld.log")

# Cache: CoreDevice UUID → pymobiledevice3 UDID
_tunnel_udid_cache: dict[str, str] = {}


def find_pymobiledevice3_binary() -> Path | None:
    """Find the pymobiledevice3 binary.

    Checks PATH first, then the common pipx install location.
    Resolves symlinks to get the real binary path (needed for the plist).
    """
    path = shutil.which("pymobiledevice3")
    if path:
        return Path(path).resolve()

    # Check common pipx location
    pipx_path = (
        Path.home() / ".local" / "pipx" / "venvs"
        / "pymobiledevice3" / "bin" / "pymobiledevice3"
    )
    if pipx_path.exists():
        return pipx_path.resolve()

    return None


async def is_tunneld_running() -> bool:
    """Check if tunneld is running by hitting its HTTP API."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(TUNNELD_URL, timeout=2.0)
            return resp.status_code == 200
    except Exception:
        return False


async def get_tunneld_devices() -> dict[str, list[dict]]:
    """Query tunneld for connected device tunnels.

    Returns the raw tunneld response: a dict mapping pymobiledevice3 UDIDs
    to lists of tunnel info dicts. Example:
        {"00008130-AAAA": [{"tunnel-address": "fd35::1", "tunnel-port": 61952, ...}]}

    Returns empty dict on error.
    """
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(TUNNELD_URL, timeout=5.0)
            if resp.status_code != 200:
                return {}
            return resp.json()
    except Exception:
        return {}


async def resolve_tunnel_udid(coredevice_uuid: str) -> str | None:
    """Map a CoreDevice UUID to the pymobiledevice3 tunnel UDID.

    devicectl uses CoreDevice UUIDs (53DA57AA-...), while pymobiledevice3
    uses ECID-based UDIDs (00008130-...). This queries the tunneld HTTP API
    and also asks devicectl for the mapping.

    Returns the pymobiledevice3 UDID, or None if not found.
    """
    # Check cache first
    if coredevice_uuid in _tunnel_udid_cache:
        return _tunnel_udid_cache[coredevice_uuid]

    devices = await get_tunneld_devices()
    if not devices:
        return None

    # If the input is already a tunnel UDID, return it directly
    if coredevice_uuid in devices:
        _tunnel_udid_cache[coredevice_uuid] = coredevice_uuid
        return coredevice_uuid

    # Map CoreDevice UUIDs to tunnel UDIDs via devicectl JSON which includes
    # both the CoreDevice UUID and the hardwareProperties.udid
    tmp_path = None
    try:
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        tmp_path = tmp.name
        tmp.close()

        proc = await asyncio.create_subprocess_exec(
            "xcrun", "devicectl", "list", "devices",
            "--json-output", tmp_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        if proc.returncode == 0:
            data = json.loads(Path(tmp_path).read_text())
            for dev in data.get("result", {}).get("devices", []):
                cd_uuid = dev.get("identifier", "")
                hw_udid = dev.get("hardwareProperties", {}).get("udid", "")
                if cd_uuid and hw_udid and hw_udid in devices:
                    _tunnel_udid_cache[cd_uuid] = hw_udid
    except Exception:
        logger.debug("Failed to map CoreDevice UUIDs via devicectl")
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)

    return _tunnel_udid_cache.get(coredevice_uuid)


def generate_plist(binary_path: Path) -> str:
    """Generate the LaunchDaemon plist XML for tunneld."""
    return textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
          "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
            <key>Label</key>
            <string>{TUNNELD_LABEL}</string>
            <key>ProgramArguments</key>
            <array>
                <string>{binary_path}</string>
                <string>remote</string>
                <string>tunneld</string>
            </array>
            <key>RunAtLoad</key>
            <true/>
            <key>KeepAlive</key>
            <true/>
            <key>StandardOutPath</key>
            <string>{LOG_PATH}</string>
            <key>StandardErrorPath</key>
            <string>{LOG_PATH}</string>
        </dict>
        </plist>
    """)


def installed_plist_log_path() -> Path | None:
    """Return the StandardOutPath recorded in the installed plist, or None.

    Returns None when the plist isn't installed or can't be parsed.
    """
    if not PLIST_PATH.exists():
        return None
    try:
        with open(PLIST_PATH, "rb") as f:
            data = plistlib.load(f)
        out = data.get("StandardOutPath")
        return Path(out) if out else None
    except Exception:
        return None


def installed_plist_is_current() -> bool:
    """True iff the installed plist's log path matches the current LOG_PATH.

    Returns False when the plist is missing, unparseable, or references the
    old user-home log path. Callers use this to detect installs that pre-date
    the move to /Library/Logs/ and prompt the user to reinstall.
    """
    return installed_plist_log_path() == LOG_PATH


def install_daemon() -> int:
    """Install the tunneld LaunchDaemon. Returns 0 on success.

    Safe to re-run as a repair tool: overwrites the existing plist and reloads
    the daemon, picking up any schema changes (e.g. the LOG_PATH migration).
    """
    binary = find_pymobiledevice3_binary()
    if not binary:
        print("Error: pymobiledevice3 not found.")
        print("Install it: pipx install pymobiledevice3")
        return 1

    plist_content = generate_plist(binary)

    # Write plist to a temp file, then sudo cp to /Library/LaunchDaemons/
    import tempfile
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".plist", delete=False,
    ) as tmp:
        tmp.write(plist_content)
        tmp_path = tmp.name

    try:
        print("Installing tunneld LaunchDaemon (requires sudo)...")
        print(f"  Binary: {binary}")
        print(f"  Plist:  {PLIST_PATH}")
        print()

        # Unload any already-loaded definition first. Without this, bootstrap
        # against a loaded service returns EIO 5, and the only escape used to
        # be kickstart -k — which sends SIGKILL to the running process and
        # hung launchctl on macOS 15 in practice. bootout is the documented
        # way to swap a plist; failure here (e.g. service not loaded) is
        # fine and expected on a fresh install.
        _run_sudo(["launchctl", "bootout", f"system/{TUNNELD_LABEL}"], timeout=30)

        if not _run_sudo(["cp", tmp_path, str(PLIST_PATH)], timeout=30):
            print("Error: Failed to copy plist (sudo cp failed)")
            return 1

        if not _run_sudo(["chown", "root:wheel", str(PLIST_PATH)], timeout=10):
            print("Warning: Failed to set plist ownership")

        # NamedTemporaryFile produces mode 600; LaunchDaemons should be 644
        # (root-writable, world-readable). 600 has historically worked but is
        # not what Apple recommends.
        if not _run_sudo(["chmod", "644", str(PLIST_PATH)], timeout=10):
            print("Warning: Failed to set plist permissions")

        if not _run_sudo(
            ["launchctl", "bootstrap", "system", str(PLIST_PATH)], timeout=30,
        ):
            print("Error: launchctl bootstrap failed.")
            print("  Diagnose with: sudo launchctl print system/com.quern.tunneld")
            print("  Validate plist: sudo plutil -lint " + str(PLIST_PATH))
            return 1

        print("tunneld LaunchDaemon installed successfully.")
        print(f"  Logs: {LOG_PATH}")
        print("  Check status: ./quern tunneld status")
        return 0
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def _run_sudo(args: list[str], timeout: int) -> bool:
    """Run a sudo command and return True on exit 0.

    Wraps subprocess timeout so a hung launchctl can't crash the install
    script with an unhandled TimeoutExpired. Returns False on any failure
    (non-zero exit, timeout, OS error) without propagating exceptions.
    """
    try:
        result = subprocess.run(["sudo", *args], timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"Warning: `sudo {' '.join(args)}` timed out after {timeout}s")
        return False
    except OSError as exc:
        print(f"Warning: `sudo {' '.join(args)}` failed: {exc}")
        return False
    return result.returncode == 0


def uninstall_daemon() -> int:
    """Uninstall the tunneld LaunchDaemon. Returns 0 on success."""
    if not PLIST_PATH.exists():
        print("tunneld LaunchDaemon is not installed.")
        return 0

    print("Removing tunneld LaunchDaemon (requires sudo)...")

    # Bootout (unload) the daemon
    result = subprocess.run(
        ["sudo", "launchctl", "bootout", f"system/{TUNNELD_LABEL}"],
        timeout=30,
    )
    if result.returncode != 0:
        print("Warning: launchctl bootout failed (daemon may not be loaded)")

    # Remove plist
    result = subprocess.run(
        ["sudo", "rm", "-f", str(PLIST_PATH)],
        timeout=10,
    )
    if result.returncode != 0:
        print("Error: Failed to remove plist")
        return 1

    print("tunneld LaunchDaemon removed.")
    return 0


def _print_status() -> int:
    """Print tunneld status. Returns 0."""
    binary = find_pymobiledevice3_binary()
    plist_installed = PLIST_PATH.exists()

    print()
    print("  tunneld Status")
    print("  " + "─" * 40)
    print(f"  Binary:    {binary or 'not found'}")
    print(f"  Plist:     {'installed' if plist_installed else 'not installed'}")
    if plist_installed and not installed_plist_is_current():
        old = installed_plist_log_path()
        print(f"  Plist log: {old} (outdated — expected {LOG_PATH})")
        print("             Reinstall to migrate: ./quern tunneld install")

    # Check if daemon is running (sync version)
    running = False
    try:
        import urllib.request
        req = urllib.request.Request(TUNNELD_URL, method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            running = resp.status == 200
            if running:
                import json
                devices = json.loads(resp.read())
    except Exception:
        devices = []

    print(f"  Running:   {'yes' if running else 'no'}")
    print(f"  URL:       {TUNNELD_URL}")

    if running and devices:
        print(f"  Devices:   {len(devices)} tunnel(s)")
        for udid in devices:
            print(f"    • {udid}")
    elif running:
        print("  Devices:   none connected")

    if not binary:
        print()
        print("  Install pymobiledevice3: pipx install pymobiledevice3")
    elif not plist_installed:
        print()
        print("  Install daemon: ./quern tunneld install")

    print()
    return 0


def _restart_daemon() -> int:
    """Restart the tunneld daemon. Returns 0 on success."""
    if not PLIST_PATH.exists():
        print("tunneld LaunchDaemon is not installed.")
        print("Install it first: ./quern tunneld install")
        return 1

    print("Restarting tunneld...")
    result = subprocess.run(
        ["sudo", "launchctl", "kickstart", "-k", f"system/{TUNNELD_LABEL}"],
        timeout=30,
    )
    if result.returncode != 0:
        print("Error: Failed to restart tunneld")
        return 1

    print("tunneld restarted.")
    return 0


def cli_tunneld(args: list[str]) -> int:
    """Handle ./quern tunneld subcommands. Returns exit code."""
    if not args or args[0] in ("-h", "--help", "help"):
        print("Usage: ./quern tunneld <command>")
        print()
        print("Commands:")
        print("  install     Install tunneld as a LaunchDaemon (requires sudo)")
        print("  uninstall   Remove the tunneld LaunchDaemon (requires sudo)")
        print("  status      Show daemon status and connected devices")
        print("  restart     Restart the tunneld daemon (requires sudo)")
        print()
        print("The tunneld daemon provides RemoteXPC tunnels for iOS 17+ devices,")
        print("enabling developer services like screenshots on physical devices.")
        return 0

    cmd = args[0]

    if cmd == "install":
        return install_daemon()
    elif cmd == "uninstall":
        return uninstall_daemon()
    elif cmd == "status":
        return _print_status()
    elif cmd == "restart":
        return _restart_daemon()
    else:
        print(f"Unknown command: {cmd}")
        print("Run './quern tunneld --help' for usage.")
        return 1
