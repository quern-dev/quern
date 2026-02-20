"""WebDriverAgent setup for physical device UI automation.

Handles cloning, building, and installing WDA on physical iOS devices.
State is persisted in ~/.quern/wda-state.json following the cert_state.py pattern.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import plistlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from server.config import CONFIG_DIR

logger = logging.getLogger(__name__)

WDA_DIR = CONFIG_DIR / "wda"
WDA_REPO = WDA_DIR / "WebDriverAgent"
WDA_DERIVED = WDA_DIR / "build"
WDA_APP = WDA_DERIVED / "Build" / "Products" / "Debug-iphoneos" / "WebDriverAgentRunner-Runner.app"
WDA_STATE_FILE = CONFIG_DIR / "wda-state.json"

CLONE_TIMEOUT = 60
BUILD_TIMEOUT = 600


# ---------------------------------------------------------------------------
# State persistence (follows cert_state.py pattern)
# ---------------------------------------------------------------------------


def read_wda_state() -> dict[str, Any]:
    """Read wda-state.json with shared file lock."""
    if not WDA_STATE_FILE.exists():
        return {"cloned": False, "builds": {}}

    try:
        fd = WDA_STATE_FILE.open("r")
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)
            content = fd.read()
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            fd.close()

        if not content.strip():
            return {"cloned": False, "builds": {}}
        return json.loads(content)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read WDA state file: %s", e)
        return {"cloned": False, "builds": {}}


def save_wda_state(state: dict[str, Any]) -> None:
    """Write wda-state.json with exclusive file lock."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    if not WDA_STATE_FILE.exists():
        WDA_STATE_FILE.touch()

    fd = WDA_STATE_FILE.open("a+")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        fd.seek(0)
        fd.truncate()
        fd.write(json.dumps(state, indent=2))
        fd.flush()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


# ---------------------------------------------------------------------------
# Signing identity discovery
# ---------------------------------------------------------------------------


def discover_signing_identities() -> list[dict[str, str]]:
    """Read provisioning teams from Xcode's account preferences.

    Returns list of dicts with keys: team_id, team_name, team_type.

    Important: the team_id here is the *Xcode/App Store Connect* team ID,
    NOT the Organizational Unit from the keychain certificate (those can
    differ and xcodebuild only accepts the Xcode team ID).
    """
    import plistlib

    plist_path = Path.home() / "Library" / "Preferences" / "com.apple.dt.Xcode.plist"
    if not plist_path.exists():
        logger.warning("Xcode preferences not found at %s", plist_path)
        return []

    try:
        with open(plist_path, "rb") as f:
            prefs = plistlib.load(f)
    except Exception as e:
        logger.warning("Failed to read Xcode preferences: %s", e)
        return []

    teams_by_account = prefs.get("IDEProvisioningTeamByIdentifier", {})

    seen: set[str] = set()
    identities: list[dict[str, str]] = []
    for _account_id, teams in teams_by_account.items():
        for team in teams:
            team_id = team.get("teamID", "")
            if not team_id or team_id in seen:
                continue
            seen.add(team_id)
            identities.append({
                "team_id": team_id,
                "team_name": team.get("teamName", ""),
                "team_type": team.get("teamType", ""),
            })

    return identities


# ---------------------------------------------------------------------------
# Clone
# ---------------------------------------------------------------------------


async def clone_wda() -> bool:
    """Clone WebDriverAgent repo if not already present.

    Returns True if a fresh clone was performed, False if skipped.
    """
    if WDA_REPO.exists() and (WDA_REPO / ".git").exists():
        logger.info("WDA repo already cloned at %s", WDA_REPO)
        return False

    WDA_DIR.mkdir(parents=True, exist_ok=True)

    logger.info("Cloning WebDriverAgent into %s", WDA_REPO)
    proc = await asyncio.create_subprocess_exec(
        "git", "clone", "--depth", "1",
        "https://github.com/appium/WebDriverAgent.git",
        str(WDA_REPO),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=CLONE_TIMEOUT
        )
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(
            f"git clone timed out after {CLONE_TIMEOUT}s"
        )

    if proc.returncode != 0:
        raise RuntimeError(
            f"git clone failed (rc={proc.returncode}): {stderr.decode()}"
        )

    return True


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


async def build_wda(team_id: str) -> bool:
    """Build WDA for a given signing team.

    The build uses ``generic/platform=iOS`` so the artifact works on any
    arm64 device — no device-specific UDID is needed.  Builds are cached
    by *team_id* only; a rebuild is triggered when the team changes.

    Returns True if a fresh build was performed, False if skipped.
    """
    state = read_wda_state()
    if state.get("build_team_id") == team_id:
        logger.info("WDA already built for team %s", team_id)
        return False

    if not WDA_REPO.exists():
        raise RuntimeError("WDA repo not cloned — call clone_wda() first")

    logger.info("Building WDA for team %s", team_id)
    proc = await asyncio.create_subprocess_exec(
        "xcodebuild", "build-for-testing",
        "-project", str(WDA_REPO / "WebDriverAgent.xcodeproj"),
        "-scheme", "WebDriverAgentRunner",
        "-destination", "generic/platform=iOS",
        f"DEVELOPMENT_TEAM={team_id}",
        "CODE_SIGNING_ALLOWED=YES",
        "-allowProvisioningUpdates",
        "-derivedDataPath", str(WDA_DERIVED),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=BUILD_TIMEOUT
        )
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(
            f"xcodebuild timed out after {BUILD_TIMEOUT}s"
        )

    if proc.returncode != 0:
        stderr_text = stderr.decode()
        stdout_text = stdout.decode()
        combined = stderr_text + stdout_text

        # Detect specific failure modes and provide actionable guidance
        if "No Account for Team" in combined:
            raise RuntimeError(
                f"Xcode has no account logged in for team '{team_id}'. "
                "Open Xcode → Settings → Accounts and sign in with the "
                "Apple ID associated with this team, then retry."
            )
        if "No signing certificate" in combined:
            raise RuntimeError(
                f"No signing certificate found for team '{team_id}'. "
                "Open Xcode → Settings → Accounts → select the team → "
                "Manage Certificates → add an 'Apple Development' certificate."
            )

        stdout_tail = "\n".join(stdout_text.splitlines()[-20:])
        raise RuntimeError(
            f"xcodebuild failed (rc={proc.returncode}):\n"
            f"stderr: {stderr_text}\n"
            f"stdout (last 20 lines): {stdout_tail}"
        )

    # Update state
    now = datetime.now(timezone.utc).isoformat()
    state = read_wda_state()
    state["cloned"] = True
    state["build_team_id"] = team_id
    state["built_at"] = now
    save_wda_state(state)

    return True


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------


def _parse_ios_major_version(os_version: str) -> int:
    """Extract major version from strings like 'iOS 17.2' or 'iOS 15.8.6'."""
    m = re.search(r"(\d+)", os_version)
    if not m:
        raise ValueError(f"Cannot parse iOS version from: {os_version!r}")
    return int(m.group(1))


async def install_wda(udid: str, os_version: str) -> None:
    """Install WDA app on a physical device.

    Routes by iOS version:
    - iOS 17+: xcrun devicectl device install app
    - iOS 15-16: ideviceinstaller -u <udid> -i <app>
    """
    if not WDA_APP.exists():
        raise RuntimeError(
            f"WDA app not found at {WDA_APP} — build first"
        )

    major = _parse_ios_major_version(os_version)

    if major >= 17:
        logger.info("Installing WDA via devicectl on device %s", udid)
        proc = await asyncio.create_subprocess_exec(
            "xcrun", "devicectl", "device", "install", "app",
            "--device", udid, str(WDA_APP),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    else:
        logger.info("Installing WDA via ideviceinstaller on device %s", udid)
        proc = await asyncio.create_subprocess_exec(
            "ideviceinstaller", "-u", udid, "-i", str(WDA_APP),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        tool = "devicectl" if major >= 17 else "ideviceinstaller"
        raise RuntimeError(
            f"{tool} install failed (rc={proc.returncode}): {stderr.decode()}"
        )

    # Record install in state
    now = datetime.now(timezone.utc).isoformat()
    state = read_wda_state()
    state.setdefault("installs", {})[udid] = {"installed_at": now}
    save_wda_state(state)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


async def setup_wda(
    udid: str,
    os_version: str,
    team_id: str | None = None,
) -> dict[str, Any]:
    """Full WDA setup orchestrator.

    Steps:
    1. Discover signing identities
    2. If no team_id provided, auto-select (1 identity) or return list (multiple)
    3. Clone WDA repo (idempotent)
    4. Build WDA (idempotent per device+team)
    5. Install WDA on device

    Returns a result dict with status and details.
    """
    # Step 1: Discover signing teams from Xcode preferences
    identities = discover_signing_identities()

    if not identities:
        return {
            "status": "error",
            "error": "No provisioning teams found in Xcode preferences. "
                     "Open Xcode → Settings → Accounts and sign in with "
                     "an Apple Developer account.",
        }

    # Step 2: Resolve team_id
    if team_id is None:
        if len(identities) == 1:
            team_id = identities[0]["team_id"]
        else:
            return {
                "status": "needs_identity_selection",
                "identities": identities,
                "message": "Multiple signing identities found. "
                           "Call again with team_id set to one of the listed team IDs.",
            }

    # Validate that the chosen team_id exists in identities
    valid_teams = {i["team_id"] for i in identities}
    if team_id not in valid_teams:
        return {
            "status": "error",
            "error": f"team_id '{team_id}' not found in available identities. "
                     f"Available: {sorted(valid_teams)}",
        }

    # Step 3: Clone
    cloned = await clone_wda()

    # Update clone state
    state = read_wda_state()
    state["cloned"] = True
    save_wda_state(state)

    # Step 4: Build (device-independent, keyed by team_id only)
    built = await build_wda(team_id)

    # Step 5: Install
    await install_wda(udid, os_version)

    return {
        "status": "ok",
        "udid": udid,
        "team_id": team_id,
        "cloned": cloned,
        "built": built,
        "installed": True,
    }
