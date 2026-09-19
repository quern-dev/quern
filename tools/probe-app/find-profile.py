#!/usr/bin/env python3
"""Pick a development provisioning profile that can sign the probe app.

Run by `build.sh --device`. Prints, on three lines: the profile's path, its
team identifier, and the signing identity's common name.

Discovered rather than configured, because the alternative is a team id and a
profile UUID pasted into the script, and both rotate -- a profile expires once
a year, and the UUID changes every time Xcode reissues one. A stale constant
fails at `codesign` with "no identity found", which says nothing about which
of the two moved.

A wildcard profile (`TEAM.*`) is what makes this possible without an Xcode
project: it signs any bundle id in the team, so the fixture does not need one
registered. An explicit `TEAM.com.quern.probe` profile is preferred over it
when both are present, since that is the narrower grant.
"""

from __future__ import annotations

import plistlib
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

#: Xcode 16 moved profiles here from ~/Library/MobileDevice/Provisioning Profiles.
#: Both are searched: a machine that has upgraded keeps the old directory, and a
#: profile in it is still valid.
PROFILE_DIRS = (
    Path.home() / "Library/Developer/Xcode/UserData/Provisioning Profiles",
    Path.home() / "Library/MobileDevice/Provisioning Profiles",
)

BUNDLE_ID = "com.quern.probe"


def _decode(path: Path) -> dict | None:
    """A .mobileprovision is CMS-signed; `security cms -D` unwraps the plist."""
    result = subprocess.run(
        ["security", "cms", "-D", "-i", str(path)],
        capture_output=True, check=False,
    )
    if result.returncode != 0 or not result.stdout:
        return None
    try:
        return plistlib.loads(result.stdout)
    except Exception:  # noqa: BLE001 - a malformed profile is just not a candidate
        return None


def _candidates(udid: str | None) -> list[tuple[int, Path, dict]]:
    now = datetime.now(timezone.utc)
    found: list[tuple[int, Path, dict]] = []
    for directory in PROFILE_DIRS:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.mobileprovision")):
            profile = _decode(path)
            if profile is None:
                continue
            expires = profile.get("ExpirationDate")
            if isinstance(expires, datetime):
                # plistlib returns these naive, in UTC.
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                if expires <= now:
                    continue
            app_id = (profile.get("Entitlements") or {}).get(
                "application-identifier", "",
            )
            team = (profile.get("TeamIdentifier") or [""])[0]
            if not team or not app_id.startswith(f"{team}."):
                continue
            suffix = app_id[len(team) + 1:]
            if suffix == BUNDLE_ID:
                rank = 0
            elif suffix == "*":
                rank = 1
            else:
                continue
            # A development profile lists the devices it may run on. One with no
            # such list is a distribution profile: it signs, installs, and then
            # the app refuses to launch. Rejected here rather than at launch,
            # where the message is "the app could not be verified".
            devices = profile.get("ProvisionedDevices")
            if not devices:
                continue
            if udid and udid not in devices:
                continue
            found.append((rank, path, profile))
    return sorted(found, key=lambda item: item[0])


def _signing_identity(team: str) -> str | None:
    """The Apple Development certificate belonging to this team.

    Matched on the team rather than taking the first development identity:
    this machine has three identities across two teams, and signing with the
    one the profile does not name fails at install with a mismatch that reads
    like a corrupt bundle.
    """
    result = subprocess.run(
        ["security", "find-identity", "-v", "-p", "codesigning"],
        capture_output=True, text=True, check=False,
    )
    for line in result.stdout.splitlines():
        if "Apple Development" not in line:
            continue
        name = line.split('"')[1] if '"' in line else ""
        if not name:
            continue
        # The certificate's OU is the team; `security find-identity` does not
        # print it, so the certificate itself is read.
        cert = subprocess.run(
            ["security", "find-certificate", "-c", name, "-p"],
            capture_output=True, check=False,
        )
        subject = subprocess.run(
            ["openssl", "x509", "-noout", "-subject"],
            input=cert.stdout, capture_output=True, check=False,
        ).stdout.decode(errors="replace")
        if f"OU={team}" in subject:
            return name
    return None


def main() -> int:
    udid = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None
    candidates = _candidates(udid)
    if not candidates:
        where = "\n  ".join(str(d) for d in PROFILE_DIRS)
        for_device = f" listing device {udid}" if udid else ""
        print(
            f"No unexpired development provisioning profile{for_device} can "
            f"sign {BUNDLE_ID}.\nLooked in:\n  {where}\n"
            "Open any iOS project in Xcode once with your account signed in; "
            "Xcode issues a wildcard team profile, which is enough.",
            file=sys.stderr,
        )
        return 1

    _, path, profile = candidates[0]
    team = profile["TeamIdentifier"][0]
    identity = _signing_identity(team)
    if identity is None:
        print(
            f"Found {path.name} for team {team}, but no 'Apple Development' "
            f"certificate for that team is in the keychain.",
            file=sys.stderr,
        )
        return 1

    print(path)
    print(team)
    print(identity)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
