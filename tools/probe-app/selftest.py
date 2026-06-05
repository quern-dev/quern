#!/usr/bin/env python3
"""QuernProbe self-test — drives the probe app through Quern's REST API.

Exercises identifier-based element interaction end to end: typing fidelity
(shift characters), hardware keyboard toggling, tab navigation, and control
state. Requires a running Quern server and a booted iOS simulator.

Usage:
    python3 selftest.py [--udid UDID]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import requests

BASE = "http://localhost:9100/api/v1"
BUNDLE_ID = "com.quern.probe"
SHIFT_TEXT = "ab_CD!2@x"


def api_key() -> str:
    state = json.loads((Path.home() / ".quern" / "state.json").read_text())
    return state["api_key"]


SESSION = requests.Session()


def call(method: str, path: str, **body):
    headers = {"X-API-Key": api_key()}
    url = f"{BASE}{path}"
    if method == "GET":
        response = SESSION.get(url, headers=headers, params=body, timeout=60)
    else:
        response = SESSION.request(method, url, headers=headers, json=body, timeout=60)
    response.raise_for_status()
    return response.json()


def tap(udid: str, identifier: str, **extra) -> None:
    call("POST", "/device/ui/tap-element", udid=udid, identifier=identifier, **extra)


def element_value(udid: str, identifier: str) -> str | None:
    data = call("GET", "/device/ui/element", udid=udid, identifier=identifier)
    return (data.get("element") or {}).get("value")


def soft_keyboard_visible(udid: str) -> bool:
    data = call("GET", "/device/ui", udid=udid)
    return any(el.get("identifier") == "shift" for el in data.get("elements", []))


def check(name: str, ok: bool, detail: str = "") -> bool:
    marker = "PASS" if ok else "FAIL"
    print(f"  [{marker}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--udid", default=None, help="Target simulator UDID")
    args = parser.parse_args()

    udid = args.udid
    if udid is None:
        udid = call("POST", "/devices/resolve")["udid"]
    print(f"target simulator: {udid}")

    here = Path(__file__).parent
    subprocess.run([str(here / "build.sh"), "--install", udid], check=True)

    try:
        call("POST", "/device/app/terminate", udid=udid, bundle_id=BUNDLE_ID)
    except requests.HTTPError:
        pass  # not running — fine
    call("POST", "/device/app/launch", udid=udid, bundle_id=BUNDLE_ID)
    time.sleep(1.5)

    results: list[bool] = []

    # -- Text input fidelity (shift characters) --
    print("text input:")
    tap(udid, "field_default")
    time.sleep(1.0)
    call("POST", "/device/ui/type", udid=udid, text=SHIFT_TEXT)
    time.sleep(0.5)
    typed = element_value(udid, "field_default")
    results.append(check("shift fidelity", typed == SHIFT_TEXT, f"got {typed!r}"))

    # -- Hardware keyboard toggle --
    print("hardware keyboard:")
    tap(udid, "field_url")
    time.sleep(1.0)
    call("POST", "/device/keyboard", udid=udid, enabled=False)
    time.sleep(1.5)
    results.append(check("soft keyboard visible", soft_keyboard_visible(udid)))
    call("POST", "/device/keyboard", udid=udid, enabled=True)
    time.sleep(1.5)
    results.append(check("soft keyboard hidden", not soft_keyboard_visible(udid)))
    # Keep the hardware keyboard attached: the soft keyboard would occlude
    # the tab bar for the navigation checks below.
    call("POST", "/device/keyboard", udid=udid, enabled=True)
    time.sleep(1.0)

    # -- Tab navigation + control state, all by identifier --
    print("controls:")
    tap(udid, "tab_controls")
    time.sleep(1.0)
    tap(udid, "control_switch", value="1")
    time.sleep(0.5)
    switch_value = element_value(udid, "control_switch")
    results.append(check("switch toggled on", switch_value == "1", f"got {switch_value!r}"))

    # -- Scroll tab reachable, first row present --
    print("scroll:")
    tap(udid, "tab_scroll")
    time.sleep(1.0)
    try:
        call("GET", "/device/ui/element", udid=udid, identifier="row_0")
        row0_present = True
    except requests.HTTPError:
        row0_present = False
    results.append(check("row_0 present", row0_present))

    # Restore the default keyboard state (software keyboard available).
    call("POST", "/device/keyboard", udid=udid, enabled=False)
    print()

    passed = sum(results)
    print(f"{passed}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
