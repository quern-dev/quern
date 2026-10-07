#!/usr/bin/env python3
"""Find where a simulator stores a setting: snapshot, change it in Settings, diff.

    scripts/sim-settings-diff.py snapshot <udid> before.json
    (change the setting once in the Settings app)
    scripts/sim-settings-diff.py snapshot <udid> after.json
    scripts/sim-settings-diff.py diff before.json after.json [--noise idle1.json idle2.json]

This is how every entry in server/device/ios/sim_settings_catalog.json was
found, and how to find the next one. Take two snapshots of an idle simulator
first and pass them as --noise: some keys change on their own, and the diff
drops whatever changed between those two.

Look for the *input* file, not a recomputed copy. A configuration-profile
restriction shows up in UserSettings.plist and again in the effective copies
(EffectiveUserSettings.plist and friends); only UserSettings.plist survives a
boot, because the rest are recomputed from it.

Adapted from a snapshot-diff harness an app team wrote for the same purpose.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import plistlib
import sys
from pathlib import Path

DEVICES = Path.home() / "Library/Developer/CoreSimulator/Devices"
# Where system settings live inside a simulator's data directory.
ROOTS = [
    "data/Library/Preferences",
    "data/Library/UserConfigurationProfiles",
    "data/Containers/Shared/SystemGroup",
    "data/Containers/Shared/AppGroup",
    "data/Containers/Data/Application",
]
# Files that change for reasons unrelated to any setting -- quern's own
# accessibility session flips com.apple.Accessibility -- and key names that
# are timestamps or counters.
NOISE_FILES = ("com.apple.Accessibility.plist", "MCSettingsEvents.plist")
NOISE_KEY_PARTS = ("timestamp", "Timestamp", "LastUpdate", "lastUpdate", "Date", "date",
                   "Count", "count", "timesince", "AuditTokens")


def _short(text: str, limit: int = 300) -> str:
    """Readable but still comparable: cut long, with a digest of the whole,
    so two values that differ only past the cut do not read as the same."""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... sha256:{hashlib.sha256(text.encode()).hexdigest()[:16]}"


def _flatten(value, prefix: str, out: dict) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            _flatten(child, f"{prefix}/{key}", out)
    elif isinstance(value, list):
        out[prefix] = _short(json.dumps(value, default=str))
    elif isinstance(value, (bytes, bytearray)):
        # The digest, not only the length: a blob that changes in place is
        # as likely a home for a setting as one that grows.
        out[prefix] = f"<{len(value)} bytes {hashlib.sha256(value).hexdigest()[:16]}>"
    elif isinstance(value, (datetime.datetime, datetime.date)):
        return
    else:
        out[prefix] = value


def snapshot(udid: str) -> dict:
    device = DEVICES / udid
    if not device.is_dir():
        raise SystemExit(f"no simulator directory for {udid} under {DEVICES}")
    flat: dict = {}
    unreadable = 0
    for root in ROOTS:
        base = device / root
        if not base.exists():
            continue
        for path in base.rglob("*.plist"):
            if path.name in NOISE_FILES or "/Caches/" in str(path):
                continue
            if root.endswith("Application") and "/Library/Preferences/" not in str(path):
                continue
            try:
                data = plistlib.loads(path.read_bytes())
            except (OSError, plistlib.InvalidFileException, ValueError):
                unreadable += 1
                continue
            _flatten(data, str(path.relative_to(device)), flat)
    if unreadable:
        # Reported, so a setting stored in one of them is not silently missed.
        print(f"note: {unreadable} plist(s) could not be read and are not in the snapshot",
              file=sys.stderr)
    return flat


def changed_keys(before: dict, after: dict) -> set[str]:
    return {k for k in set(before) | set(after)
            if before.get(k, "<absent>") != after.get(k, "<absent>")}


def diff(before: dict, after: dict, noise: set[str] | None = None) -> list[str]:
    lines = []
    for key in sorted(changed_keys(before, after) - (noise or set())):
        if any(part in key.rsplit("/", 1)[-1] for part in NOISE_KEY_PARTS):
            continue
        old, new = before.get(key, "<absent>"), after.get(key, "<absent>")
        lines.append(f"{key}: {old!r} -> {new!r}")
    return lines


def main(argv: list[str]) -> int:
    if len(argv) >= 3 and argv[0] == "snapshot":
        Path(argv[2]).write_text(json.dumps(snapshot(argv[1]), default=str))
        print(f"snapshot of {argv[1]} -> {argv[2]}")
        return 0
    if len(argv) >= 3 and argv[0] == "diff":
        noise = None
        if len(argv) >= 6 and argv[3] == "--noise":
            idle = [json.loads(Path(p).read_text()) for p in argv[4:6]]
            noise = changed_keys(*idle)
        changes = diff(json.loads(Path(argv[1]).read_text()),
                       json.loads(Path(argv[2]).read_text()), noise)
        print("\n".join(changes) or "no changes")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
