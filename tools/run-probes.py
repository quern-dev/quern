#!/usr/bin/env python3
"""Run every probe self-test that has a device available.

Skips a platform rather than failing it when nothing is booted: a machine with
no Android emulator running should report "iOS 11/11, Android skipped", not a
red run that hides a real regression on the other side.

Usage:
    python3 tools/run-probes.py                 # every available platform
    python3 tools/run-probes.py --ios-only
    python3 tools/run-probes.py --android-only
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def booted_simulator() -> str | None:
    # A host without Xcode has no xcrun at all. Raising here would take the
    # Android run down with it, which is the opposite of the skip-don't-fail
    # behaviour this runner exists to provide.
    try:
        out = subprocess.run(["xcrun", "simctl", "list", "devices", "booted"],
                             capture_output=True, text=True, timeout=30).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    for line in out.splitlines():
        if "(Booted)" in line and "(" in line:
            return line.split("(")[1].split(")")[0]
    return None


def attached_android() -> str | None:
    try:
        out = subprocess.run(["adb", "devices"], capture_output=True, text=True,
                             timeout=20).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    for line in out.splitlines()[1:]:
        if line.strip().endswith("\tdevice"):
            return line.split("\t")[0]
    return None


def run(name: str, script: Path, args: list[str]) -> tuple[str, int]:
    print(f"\n{'=' * 60}\n{name}\n{'=' * 60}")
    # The iOS runner needs `requests`, which lives in the project venv rather
    # than the system interpreter.
    venv = HERE.parent / ".venv" / "bin" / "python"
    python = str(venv) if venv.exists() else sys.executable
    result = subprocess.run([python, str(script), *args])
    return name, result.returncode


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ios-only", action="store_true")
    ap.add_argument("--android-only", action="store_true")
    args = ap.parse_args()

    results: list[tuple[str, int]] = []
    skipped: list[str] = []

    if not args.android_only:
        udid = booted_simulator()
        if udid:
            ios = HERE / "probe-app" / "selftest.py"
            results.append(run("iOS — app-delegate lifecycle", ios, ["--udid", udid]))
            # The same suite against the scene bundle. Which delegate callbacks
            # fire is decided at build time, so one binary cannot cover both,
            # and the guidance we publish about scene delivery is only verified
            # if something runs it.
            results.append(run("iOS — scene lifecycle", ios, ["--udid", udid, "--scene"]))
        else:
            skipped.append("iOS (no booted simulator)")

    if not args.ios_only:
        serial = attached_android()
        if serial:
            results.append(run("Android — QuernProbe",
                               HERE / "probe-app-android" / "selftest.py",
                               ["--serial", serial]))
        else:
            skipped.append("Android (no attached device)")

    print(f"\n{'=' * 60}\nSummary")
    for name, code in results:
        print(f"  {'PASS' if code == 0 else 'FAIL'}  {name}")
    for s in skipped:
        print(f"  SKIP  {s}")
    if not results:
        print("\nNothing ran. Boot a simulator or start an emulator first.")
        return 1
    return 1 if any(code for _, code in results) else 0


if __name__ == "__main__":
    sys.exit(main())
