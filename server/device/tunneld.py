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
import getpass
import json
import logging
import os
import plistlib
import shutil
import subprocess
import tempfile
import textwrap
import time
import urllib.request
from dataclasses import dataclass
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

# macOS 26 (Tahoe) launchd needs a moment after `bootout` before it will accept
# a fresh `bootstrap`; without the pause the load fails with
# `Bootstrap failed: 5: Input/output error`.
BOOTOUT_SETTLE_SECONDS = 1.5
BOOTSTRAP_RETRY_SETTLE_SECONDS = 3.0

# A reloaded daemon binds its port a moment after launchd returns.
RESTART_SERVING_POLLS = 10
RESTART_POLL_INTERVAL_SECONDS = 0.5

# Recovery from a wedge is a signal, not a reload: the daemon runs with
# `KeepAlive`, so getting the stuck process to exit is the whole job -- launchd
# respawns it against a clean state. `launchctl kill` names the job by label,
# which is what makes it grantable in sudoers as a single fully-specified
# command; `kill <pid>` would mean "kill anything as root".
LAUNCHCTL = "/bin/launchctl"
RECOVERY_ARGS = [LAUNCHCTL, "kill", "SIGKILL", f"system/{TUNNELD_LABEL}"]
SUDOERS_PATH = Path("/etc/sudoers.d/quern-tunneld")

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



@dataclass
class TunneldHealth:
    """What state the tunneld daemon is in, and what would resolve it."""

    status: str
    """healthy | wedged | stopped | not_installed | no_binary | stale_plist |
    binary_drift."""

    detail: str
    serving: bool = False
    launchd_state: str | None = None
    pid: int | None = None
    program: str | None = None
    remedy: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "healthy"


def launchd_job() -> dict[str, str]:
    """What launchd knows about the tunneld job, without needing sudo.

    `launchctl print` on a system-domain job is readable unprivileged -- verified
    on macOS 26 -- which is what lets this run inside read-only diagnostics
    without a password prompt. Only the handful of scalar fields are parsed;
    the output is a large nested dump and anything deeper would be brittle.
    """
    try:
        result = subprocess.run(
            ["launchctl", "print", f"system/{TUNNELD_LABEL}"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if result.returncode != 0:
        return {}

    wanted = ("state", "pid", "program", "last exit code")
    fields: dict[str, str] = {}
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key, value = key.strip(), value.strip()
        # Only top-level scalars: nested blocks repeat `state = active` for
        # every endpoint, which would otherwise overwrite the job's own state.
        if key in wanted and key not in fields:
            fields[key] = value
    return fields


async def tunneld_health() -> TunneldHealth:
    """Whether tunneld is actually serving, and if not, why.

    The distinction that matters is **wedged versus stopped**, because they look
    identical from the HTTP side and need opposite responses. A failed device
    pairing can leave the daemon alive and not serving: it holds no listener and
    never exits, so `KeepAlive` never fires and launchd reports it healthy
    indefinitely (#73). "HTTP down while launchd says running" is the signature
    that separates that from a daemon which is simply not started.

    This reports and does not remediate. Recovery is `bootout` + `bootstrap`,
    never `kickstart -k` -- that sends SIGKILL and hung launchctl on macOS 15,
    as `install_daemon` already notes.
    """
    binary = find_pymobiledevice3_binary()
    if binary is None:
        return TunneldHealth(
            status="no_binary",
            detail="pymobiledevice3 binary not found, so tunneld cannot run",
            remedy="pipx install pymobiledevice3",
        )

    if not PLIST_PATH.exists():
        return TunneldHealth(
            status="not_installed",
            detail="LaunchDaemon is not installed",
            remedy="./quern tunneld install",
        )

    serving = await is_tunneld_running()
    job = launchd_job()
    state = job.get("state")
    pid = int(job["pid"]) if job.get("pid", "").isdigit() else None
    program = job.get("program")

    if not serving:
        if state == "running":
            return TunneldHealth(
                status="wedged", serving=False, launchd_state=state, pid=pid,
                program=program,
                detail=(
                    f"alive (pid {pid}) but not serving on {TUNNELD_URL} — "
                    "launchd sees a healthy job and will never restart it"
                ),
                remedy=(
                    "sudo launchctl bootout system/" + TUNNELD_LABEL
                    + " && sudo launchctl bootstrap system " + str(PLIST_PATH)
                    + "  (not kickstart -k: it hangs launchctl)"
                ),
            )
        return TunneldHealth(
            status="stopped", serving=False, launchd_state=state, pid=pid,
            program=program,
            detail=f"installed but not running (launchd state: {state or 'unknown'})",
            remedy="./quern tunneld install",
        )

    if not installed_plist_is_current():
        return TunneldHealth(
            status="stale_plist", serving=True, launchd_state=state, pid=pid,
            program=program,
            detail=f"serving, but the installed plist is outdated ({installed_plist_log_path()})",
            remedy="./quern tunneld install",
        )

    # The daemon runs whatever the plist froze in, which is not necessarily the
    # binary quern would resolve today -- a second pipx install, or a per-user
    # one shadowing the shared path, drifts silently and is invisible from the
    # HTTP side because the old one keeps serving perfectly well.
    if program and Path(program) != binary:
        return TunneldHealth(
            status="binary_drift", serving=True, launchd_state=state, pid=pid,
            program=program,
            detail=f"serving from {program}, but quern resolves {binary}",
            remedy="./quern tunneld install  (re-freezes the plist onto the resolved binary)",
        )

    return TunneldHealth(
        status="healthy", serving=True, launchd_state=state, pid=pid, program=program,
        detail=f"serving on {TUNNELD_URL} (pid {pid})",
    )


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


def _read_installed_plist() -> dict | None:
    """Parse the installed plist, or None if missing/unparseable."""
    if not PLIST_PATH.exists():
        return None
    try:
        with open(PLIST_PATH, "rb") as f:
            return plistlib.load(f)
    except Exception:
        return None


def installed_plist_log_path() -> Path | None:
    """Return the StandardOutPath recorded in the installed plist, or None."""
    data = _read_installed_plist()
    if data is None:
        return None
    out = data.get("StandardOutPath")
    return Path(out) if out else None


def installed_plist_program() -> Path | None:
    """Return ProgramArguments[0] from the installed plist, or None."""
    data = _read_installed_plist()
    if data is None:
        return None
    args = data.get("ProgramArguments") or []
    return Path(args[0]) if args else None


def installed_plist_is_current() -> bool:
    """True iff the installed plist matches what we'd generate now.

    Checks two things:
      - StandardOutPath equals the current LOG_PATH (the migration from
        ~/.quern/tunneld.log → /Library/Logs/com.quern.tunneld.log).
      - ProgramArguments[0] equals the currently-discovered pymobiledevice3
        binary, or — when no binary is discoverable now — at least exists on
        disk. This catches drift from things like `sudo pipx install --global`
        creating a new binary at /usr/local/bin/ while the plist still bakes
        in the old per-user pipx path.
    """
    if installed_plist_log_path() != LOG_PATH:
        return False
    program = installed_plist_program()
    if program is None:
        return False
    current = find_pymobiledevice3_binary()
    if current is None:
        # Can't discover a binary now — only flag if what's in the plist is
        # broken on disk. Avoids false positives in odd states.
        return program.exists()
    return program == current


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

        if not _bootstrap_with_retry():
            print("Error: launchctl bootstrap failed.")
            print(f"  Diagnose with: sudo launchctl print system/{TUNNELD_LABEL}")
            print(f"  Validate plist: sudo plutil -lint {PLIST_PATH}")
            return 1

        print("tunneld LaunchDaemon installed successfully.")
        print(f"  Logs: {LOG_PATH}")
        print("  Check status: ./quern tunneld status")
        return 0
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def _wait_until_serving(
    polls: int = RESTART_SERVING_POLLS,
    interval: float = RESTART_POLL_INTERVAL_SECONDS,
) -> bool:
    """Poll until tunneld answers on its port, or give up."""
    for _ in range(polls):
        serving, _devices = _tunneld_devices()
        if serving:
            return True
        time.sleep(interval)
    return False


def can_recover_unattended() -> bool:
    """Whether sudo will run the recovery command *without a password*.

    Deliberately not `sudo -l <command>`: that answers "is this permitted by
    policy", which is a different and much weaker question. Any user with a
    blanket `(ALL) ALL` entry -- the default for an admin on macOS -- gets a
    zero exit for every command they could run *with* a password, so the probe
    reports unattended recovery is available when it is not.

    The listing is parsed instead, for the exact NOPASSWD rule. This is only
    used to phrase messages; the recovery itself passes `-n` so that a wrong
    answer here cannot turn into a hidden prompt.
    """
    try:
        result = subprocess.run(
            ["sudo", "-n", "-l"], capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False

    wanted = " ".join(RECOVERY_ARGS)
    return any(
        "NOPASSWD:" in line and wanted in line
        for line in result.stdout.splitlines()
    )


def recover_wedged_tunneld(non_interactive: bool = False) -> bool:
    """Kill a wedged tunneld and let launchd bring it back. Returns serving.

    Measured on a real wedge: pid 949 alive with no listener, killed, launchd
    respawned it as pid 3022 and it was serving 1.0s later. Same shape as the
    accessibility-bridge reset in #66 -- kill the process holding bad state and
    let its supervisor rebuild it -- which is available here only because the
    plist sets `KeepAlive`.

    Deliberately not `kickstart -k`: that tears the job down and rebuilds it,
    and hung launchctl on macOS 15. A signal leaves the job definition alone.

    Requires root. Callers that cannot obtain it should report
    `tunneld_health()`'s remedy rather than failing silently.
    """
    if not _run_sudo(RECOVERY_ARGS, timeout=30, non_interactive=non_interactive):
        return False
    return _wait_until_serving()


def _grant_user() -> str:
    """The human the grant is for, not the identity running the installer.

    `sudo ./quern tunneld grant-recovery` is the natural way to run something
    that writes to /etc/sudoers.d, and under sudo `getpass.getuser()` is root.
    Taking that at face value would write a rule granting root permission to do
    what root can already do, report success, and leave the actual user still
    being prompted -- a false all-clear that only shows up the next time a
    wedge is not healed.
    """
    return os.environ.get("SUDO_USER") or getpass.getuser()


def recovery_grant_line() -> str:
    """The sudoers rule that lets recovery run without a password.

    Fully specified, no wildcards: it authorises exactly one signal to exactly
    one job. It does not permit loading, unloading or reconfiguring daemons,
    and it cannot be widened by argument, because sudoers matches the whole
    command line rather than the binary.
    """
    return f"{_grant_user()} ALL=(root) NOPASSWD: {' '.join(RECOVERY_ARGS)}\n"


def install_recovery_grant() -> int:
    """Install the sudoers rule so a wedge can be healed unattended.

    Root is the one thing standing between "quern noticed the wedge" and
    "quern fixed it". Taking one narrow, explicit, reversible grant is better
    than the alternatives: prompting inside unrelated operations puts an auth
    dialog in front of a screenshot, and leaving it to the user means leaving
    it to someone who has no reason to suspect the daemon, since a wedged
    tunneld looks healthy to every obvious check (#73).

    Validated with `visudo -c` before installing. A malformed file in
    /etc/sudoers.d breaks sudo for everything on the machine, which would be a
    considerably worse failure than the one being fixed.
    """
    rule = recovery_grant_line()
    print("This authorises exactly one command to run without a password:")
    print(f"  {rule.strip()}")
    print()

    with tempfile.NamedTemporaryFile("w", suffix=".sudoers", delete=False) as tmp:
        tmp.write(rule)
        tmp_path = tmp.name

    try:
        check = subprocess.run(
            ["visudo", "-cf", tmp_path], capture_output=True, text=True, timeout=15,
        )
        if check.returncode != 0:
            print(f"Error: not valid sudoers syntax: {check.stderr.strip()}")
            return 1

        # `install` sets ownership and mode atomically; sudoers ignores a
        # drop-in that is group- or world-writable, and would do so silently.
        if not _run_sudo(
            ["install", "-o", "root", "-g", "wheel", "-m", "0440", tmp_path, str(SUDOERS_PATH)],
            timeout=30,
        ):
            print("Error: could not install the sudoers rule")
            return 1
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"Error: {exc}")
        return 1
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    if not can_recover_unattended():
        print(f"Warning: installed {SUDOERS_PATH}, but sudo still asks for a password.")
        return 1

    print(f"Installed {SUDOERS_PATH}. A wedged tunneld can now be healed automatically.")
    return 0


def revoke_recovery_grant() -> int:
    """Remove the sudoers rule."""
    if not SUDOERS_PATH.exists():
        print("No recovery grant is installed.")
        return 0
    if not _run_sudo(["rm", "-f", str(SUDOERS_PATH)], timeout=30):
        print("Error: could not remove the sudoers rule")
        return 1
    print(f"Removed {SUDOERS_PATH}. Recovery now needs an explicit sudo.")
    return 0


def _bootstrap_with_retry() -> bool:
    """Load the daemon, retrying once if launchd has not settled.

    macOS 26 (Tahoe) launchd needs a brief settle after `bootout` before
    `bootstrap` will accept a fresh load -- otherwise it fails with
    `Bootstrap failed: 5: Input/output error`. Manual bootstrap-after-sleep
    succeeds reliably, so that is what this replicates. The delays are short
    enough to be invisible in practice.

    Shared by install and restart deliberately: they had already drifted, and
    `kickstart -k` got into the restart path through the gap (#73).
    """
    time.sleep(BOOTOUT_SETTLE_SECONDS)
    bootstrap_cmd = ["launchctl", "bootstrap", "system", str(PLIST_PATH)]
    if _run_sudo(bootstrap_cmd, timeout=30):
        return True

    print("  bootstrap raced launchd state, retrying after settle...")
    time.sleep(BOOTSTRAP_RETRY_SETTLE_SECONDS)
    return _run_sudo(bootstrap_cmd, timeout=30)


def _tunneld_devices() -> tuple[bool, list[str]]:
    """Whether tunneld answers on its port, and the tunnels it reports.

    Synchronous twin of `is_tunneld_running()`, for the CLI paths that are not
    async. Serving is the signal that separates a healthy daemon from one that
    is alive and wedged, so it is what a restart has to prove (#73).
    """
    try:
        req = urllib.request.Request(TUNNELD_URL, method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            if resp.status != 200:
                return False, []
            return True, json.loads(resp.read())
    except Exception:
        return False, []


def _run_sudo(args: list[str], timeout: int, non_interactive: bool = False) -> bool:
    """Run a sudo command and return True on exit 0.

    Wraps subprocess timeout so a hung launchctl can't crash the install
    script with an unhandled TimeoutExpired. Returns False on any failure
    (non-zero exit, timeout, OS error) without propagating exceptions.

    `non_interactive` adds `-n`, which makes sudo fail rather than ask. Use it
    anywhere a prompt would have nobody to answer it: a blocked password prompt
    with no terminal is indistinguishable from a hang.
    """
    prefix = ["sudo", "-n"] if non_interactive else ["sudo"]
    try:
        result = subprocess.run([*prefix, *args], timeout=timeout)
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

    running, devices = _tunneld_devices()

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
    """Restart the tunneld daemon. Returns 0 on success.

    `bootout` then `bootstrap`, never `kickstart -k`. Kickstart sends SIGKILL
    and hung launchctl on macOS 15, and this is the command someone reaches for
    when tunneld already looks wrong -- the worst place to risk a hang.

    Success means *serving*, not exit 0. The wedge this recovers from is a
    daemon that is alive with no listener, which launchd reports as healthy, so
    trusting an exit code here would report the very state the restart was
    meant to clear (#73).
    """
    if not PLIST_PATH.exists():
        print("tunneld LaunchDaemon is not installed.")
        print("Install it first: ./quern tunneld install")
        return 1

    print("Restarting tunneld (requires sudo)...")

    # A loaded job only needs a signal: KeepAlive respawns it in about a
    # second, and that path needs one narrowly-scoped privilege rather than the
    # authority to load and unload system daemons.
    if launchd_job() and recover_wedged_tunneld():
        print(f"tunneld restarted, serving on {TUNNELD_URL}.")
        return 0

    # Not loaded, or the respawn did not come back -- reload the definition.
    _run_sudo([LAUNCHCTL, "bootout", f"system/{TUNNELD_LABEL}"], timeout=30)
    if not _bootstrap_with_retry():
        print("Error: launchctl bootstrap failed.")
        print(f"  Diagnose with: sudo launchctl print system/{TUNNELD_LABEL}")
        return 1

    if _wait_until_serving():
        print(f"tunneld restarted, serving on {TUNNELD_URL}.")
        return 0

    print(f"Error: tunneld reloaded but is not serving on {TUNNELD_URL}.")
    print(f"  Check the log: {LOG_PATH}")
    return 1


def cli_tunneld(args: list[str]) -> int:
    """Handle ./quern tunneld subcommands. Returns exit code."""
    if not args or args[0] in ("-h", "--help", "help"):
        print("Usage: ./quern tunneld <command>")
        print()
        print("Commands:")
        print("  install          Install tunneld as a LaunchDaemon (requires sudo)")
        print("  uninstall        Remove the tunneld LaunchDaemon (requires sudo)")
        print("  status           Show daemon status and connected devices")
        print("  restart          Restart the tunneld daemon (requires sudo)")
        print("  grant-recovery   Allow automatic recovery without a password")
        print("  revoke-recovery  Remove that authorisation")
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
    elif cmd == "grant-recovery":
        return install_recovery_grant()
    elif cmd == "revoke-recovery":
        return revoke_recovery_grant()
    else:
        print(f"Unknown command: {cmd}")
        print("Run './quern tunneld --help' for usage.")
        return 1
