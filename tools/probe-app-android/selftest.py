#!/usr/bin/env python3
"""Quern Probe (Android) self-test — drives the probe through Quern's REST API.

Identifier-based throughout: no coordinates, no uiautomator shelling out. The
point is to exercise the same path an agent uses, so a regression in the tools
fails here rather than in someone's session.

Usage:  python3 selftest.py [--serial emulator-5554]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PKG = "com.quern.probe"
BASE = "http://localhost:9100/api/v1"

_passed: list[str] = []
_failed: list[str] = []


def api(path: str, payload: dict | None = None, timeout: int = 90) -> dict:
    key = (Path.home() / ".quern" / "api-key").read_text().strip()
    url = f"{BASE}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "User-Agent": "quern-probe-selftest/1.0"},
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code, "detail": e.read().decode()[:400]}


def check(name: str, ok: bool, detail: str = "") -> bool:
    (_passed if ok else _failed).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  — ' + detail if detail and not ok else ''}")
    return ok


def walk(node):
    """Yield every dict in an arbitrarily nested tree."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


def find(serial: str, suffix: str) -> dict | None:
    """Find an element whose identifier ends with `suffix`.

    Fields come back as explicit nulls rather than missing keys, so `or ""` is
    load bearing: `.get("identifier", "")` returns None, not "".
    """
    tree = api(f"/device/ui?udid={serial}")
    for n in walk(tree):
        ident = n.get("identifier") or n.get("resource_id") or ""
        if ident.endswith(suffix):
            return n
    return None


def text_of(serial: str, suffix: str) -> str | None:
    el = find(serial, suffix)
    if el is None:
        return None
    return el.get("label") or el.get("text") or el.get("value")


def tap(serial: str, label: str) -> dict:
    return api("/device/ui/tap-element", {"udid": serial, "label": label})


def open_url(serial: str, url: str) -> dict:
    return api("/device/open-url", {"udid": serial, "url": url})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", default="emulator-5554")
    args = ap.parse_args()
    s = args.serial

    api("/device/app/launch", {"udid": s, "bundle_id": PKG})
    time.sleep(3)

    print("\nDeep links")
    tap(s, "Links")
    time.sleep(2)
    before = text_of(s, "link_count")
    if not check("Links tab reachable", before is not None, "link_count not in the UI tree"):
        return 1
    start = int(before)

    # Control case: a scheme the manifest registers. Must land.
    resp = open_url(s, "quernprobe://open/product/abc123")
    time.sleep(3)
    raw = text_of(s, "link_count")
    if not check("link_count still on screen after the deep link", raw is not None,
                 "element vanished — the activity was recreated rather than resumed"):
        return 1
    after = int(raw)
    check("registered scheme reaches the app", after == start + 1, f"count {start} -> {after}")
    check("registered scheme URI recorded",
          (text_of(s, "link_last_uri") or "").startswith("quernprobe://"),
          f"got {text_of(s, 'link_last_uri')!r}")

    # Regression guard for #78. `am start` exits 0 when it cannot resolve an
    # intent, so open_url answers {"status": "ok"} for a URL nothing handles.
    # The app is the only honest witness: the counter must not move.
    before2_raw = text_of(s, "link_count")
    resp = open_url(s, "nonexistentapp12345://foo/bar")
    time.sleep(3)
    after2_raw = text_of(s, "link_count")
    if not check("link_count readable either side of the unresolvable URL",
                 before2_raw is not None and after2_raw is not None,
                 f"before={before2_raw!r} after={after2_raw!r}"):
        return 1
    check("unresolvable scheme does not reach the app", int(after2_raw) == int(before2_raw),
          f"count {before2_raw} -> {after2_raw}")

    # This is the bug, asserted so the fix is visible when it lands: today the
    # tool claims success. When #78 is fixed this check flips and should be
    # inverted, not deleted.
    reported_ok = resp.get("status") == "ok"
    check("KNOWN BUG #78: unresolvable URL still reports ok", reported_ok,
          f"open_url returned {resp}")

    print("\nUI surfaces")
    tap(s, "Controls")
    time.sleep(2)
    check("controls tab renders", find(s, "control_switch") is not None)
    tap(s, "Scroll")
    time.sleep(2)
    check("scroll list renders", find(s, "scroll_list") is not None)
    tap(s, "Web")
    time.sleep(3)
    check("web view present", find(s, "web_view") is not None)

    print(f"\n{len(_passed)} passed, {len(_failed)} failed")
    for f in _failed:
        print(f"  failed: {f}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
