#!/usr/bin/env python3
"""The WebDriverAgent version quern pins (#479): check it against npm, or move it.

    scripts/wda-pin.py --check          exit 0 if the pin is npm's latest, 1 if it
                                        is behind (2 if npm could not be asked)
    scripts/wda-pin.py --check --json   the same, as {"pinned", "latest", "behind"}
    scripts/wda-pin.py --bump [VERSION] write VERSION (default: latest) and its
                                        sha512 into server/device/ios/wda-pin.json

A bump changes what every install builds, and an existing install rebuilds at
its next setup_wda (a simulator at its next start). So a bump gets a live test
on a simulator and a phone before it merges. Standard library only: the weekly
workflow runs this on a bare runner.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

PIN_FILE = Path(__file__).resolve().parent.parent / "server" / "device" / "ios" / "wda-pin.json"
REGISTRY = "https://registry.npmjs.org"


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.load(resp)


def _version(text: str) -> tuple:
    """1.2.10 > 1.2.9: compared as numbers. A part that is not one sorts first."""
    return tuple(int(p) if p.isdigit() else -1 for p in text.split("-")[0].split("."))


def _pin() -> dict:
    return json.loads(PIN_FILE.read_text())


def check(as_json: bool) -> int:
    pin = _pin()
    try:
        latest = _get(f"{REGISTRY}/{pin['package']}/latest")["version"]
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        print(f"could not ask npm for {pin['package']}'s latest version: {e}", file=sys.stderr)
        return 2
    # Behind only if npm's is newer: a pin moved ahead of `latest` (a release
    # npm has not tagged latest yet) is not something to catch up with.
    behind = _version(latest) > _version(pin["version"])
    if as_json:
        print(json.dumps({"pinned": pin["version"], "latest": latest, "behind": behind}))
    elif behind:
        print(f"WebDriverAgent pin {pin['version']} is behind npm's latest {latest}: "
              f"scripts/wda-pin.py --bump {latest}")
    else:
        print(f"WebDriverAgent pin {pin['version']} is npm's latest")
    return 1 if behind else 0


def bump(version: str | None) -> int:
    pin = _pin()
    ref = version or "latest"
    try:
        meta = _get(f"{REGISTRY}/{pin['package']}/{ref}")
        new = {"package": pin["package"], "version": meta["version"],
               "integrity": meta["dist"]["integrity"]}
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        print(f"could not read {pin['package']}@{ref} from npm: {e}", file=sys.stderr)
        return 2
    if not new["integrity"].startswith("sha512-"):
        print(f"{pin['package']}@{new['version']} has no sha512 integrity: {new['integrity']}",
              file=sys.stderr)
        return 2
    PIN_FILE.write_text(json.dumps(new, indent=2) + "\n")
    print(f"WebDriverAgent pinned at {new['version']} (was {pin['version']}). Live-test it "
          "on a simulator and a phone before merging.")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--bump", nargs="?", const="", metavar="VERSION")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.check:
        return check(args.json)
    return bump(args.bump or None)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
