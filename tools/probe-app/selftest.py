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
SCENE_BUNDLE_ID = "com.quern.probe.scene"
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


def visible_labels(udid: str) -> set[str]:
    def walk(node):
        if isinstance(node, dict):
            yield node
            for v in node.values():
                yield from walk(v)
        elif isinstance(node, list):
            for v in node:
                yield from walk(v)

    tree = call("GET", "/device/ui", udid=udid)
    return {n.get("label") for n in walk(tree) if n.get("label")}


def goto(udid: str, label: str, marker: str | None = None) -> None:
    """Select a tab by label, going through More when it is not on the bar.

    Three things make this less trivial than it looks, all of them real iOS
    behaviour an agent has to handle:

    * The bar shows five items; the rest live in a More list.
    * The More tab keeps its own navigation stack, so selecting it again
      returns to whatever was pushed rather than to the list. Popping needs the
      nav back button.
    * "More" names two different elements — the tab (RadioButton) and that back
      button (Button) — so taps have to say which.

    `scroll_to_find=False` matters too: it defaults on, so asking for a label
    that is not present spends the full 60s timeout trying to scroll it into
    view instead of failing.
    """
    if marker and element_text(udid, marker) is not None:
        return

    tab_id = f"tab_{label.lower()}"
    labels = visible_labels(udid)
    if label in labels:
        call("POST", "/device/ui/tap-element", udid=udid, identifier=tab_id,
             scroll_to_find=False)
        time.sleep(1.2)
        return

    call("POST", "/device/ui/tap-element", udid=udid, label="More",
         element_type="RadioButton", scroll_to_find=False)
    time.sleep(1.2)
    if "More" in visible_labels(udid):
        try:  # pop whatever the More stack was left on
            call("POST", "/device/ui/tap-element", udid=udid, label="More",
                 element_type="Button", scroll_to_find=False)
            time.sleep(1.0)
        except requests.HTTPError:
            pass  # already on the list
    call("POST", "/device/ui/tap-element", udid=udid, label=label, scroll_to_find=False)
    time.sleep(1.2)


def element_text(udid: str, identifier: str) -> str | None:
    """Read an element's visible text, tolerating a missing element."""
    try:
        found = call("GET", "/device/ui/element", udid=udid, identifier=identifier)
        el = found.get("element") or {}
    except requests.HTTPError:
        return None
    return el.get("label") or el.get("value") or el.get("text")


def open_url(udid: str, url: str) -> tuple[bool, str]:
    """Open a URL and clear iOS's confirmation prompt. Returns (reported_ok, detail).

    iOS puts up an "Open in "QuernProbe"?" alert for a custom-scheme link
    rather than dispatching it silently, and until it is answered the alert
    blocks every other query — a run that dies mid-test leaves the simulator
    wedged behind it, which is how this was found.

    Worth knowing generally: a deep link on iOS is not necessarily one action,
    and automation that opens one without handling the confirmation will look
    like it hung.
    """
    try:
        call("POST", "/device/open-url", udid=udid, url=url)
        reported, detail = True, "ok"
    except requests.HTTPError as e:
        reported, detail = False, str(e)[:120]

    time.sleep(1.5)
    if "Open" in visible_labels(udid):
        call("POST", "/device/ui/tap-element", udid=udid, label="Open")
        time.sleep(1.5)
    return reported, detail


def check(name: str, ok: bool, detail: str = "") -> bool:
    marker = "PASS" if ok else "FAIL"
    # Only on failure: a detail printed beside PASS reads as a reason the check
    # nearly failed, which is how "[PASS] Links tab reachable — link_count not
    # found" happened.
    print(f"  [{marker}] {name}" + (f" — {detail}" if detail and not ok else ""))
    return ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--udid", default=None, help="Target simulator UDID")
    parser.add_argument("--scene", action="store_true",
                        help="Exercise the scene-lifecycle bundle instead")
    args = parser.parse_args()

    udid = args.udid
    if udid is None:
        udid = call("POST", "/devices/resolve")["udid"]
    print(f"target simulator: {udid}")

    here = Path(__file__).parent
    bundle_id = SCENE_BUNDLE_ID if args.scene else BUNDLE_ID
    scheme = "quernprobescene" if args.scene else "quernprobe"
    build = [str(here / "build.sh")] + (["--scene"] if args.scene else []) + ["--install", udid]
    subprocess.run(build, check=True)
    print(f"lifecycle: {'scene' if args.scene else 'app delegate'} ({bundle_id})")

    try:
        call("POST", "/device/app/terminate", udid=udid, bundle_id=bundle_id)
    except requests.HTTPError:
        pass  # not running — fine
    call("POST", "/device/app/launch", udid=udid, bundle_id=bundle_id)
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

    # -- Deep links --
    # The iOS half of the story the Android probe tells. Here simctl reports an
    # unresolvable scheme as an error, so the tool is a usable witness; on
    # Android it answers "ok" regardless (quern #78). Asserting the app's own
    # counter on both platforms keeps the test honest either way.
    print("deep links:")
    goto(udid, "Links", marker="link_count")
    before = element_text(udid, "link_count")
    if check("Links tab reachable", before is not None, "link_count not found"):
        reported, _ = open_url(udid, f"{scheme}://open/product/abc123")
        time.sleep(2.0)
        goto(udid, "Links", marker="link_count")
        after = element_text(udid, "link_count")
        results.append(check("registered scheme reaches the app",
                             after is not None and int(after) == int(before) + 1,
                             f"count {before} -> {after}"))
        results.append(check("registered scheme URI recorded",
                             (element_text(udid, "link_last_uri") or "").startswith(f"{scheme}://"),
                             f"got {element_text(udid, 'link_last_uri')!r}"))

        route = element_text(udid, "link_route")
        expected = "scene:openURLContexts" if args.scene else "appdelegate:open"
        results.append(check(f"warm delivery routed via {expected}",
                             route == expected, f"got {route!r}"))

        held = element_text(udid, "link_count")
        reported, detail = open_url(udid, "nonexistentapp12345://foo/bar")
        time.sleep(2.0)
        goto(udid, "Links", marker="link_count")
        results.append(check("unresolvable scheme does not reach the app",
                             element_text(udid, "link_count") == held,
                             f"count {held} -> {element_text(udid, 'link_count')}"))
        # Contrast with Android, where this is the KNOWN BUG check: iOS surfaces
        # the failure, so open_url is trustworthy here and must stay that way.
        results.append(check("unresolvable scheme is reported as an error",
                             not reported, "open_url reported success"))
    else:
        results.append(False)

    # -- Cold launch: the app is terminated, and the URL starts it --
    # This is the path the guide describes and nothing exercised. It is also
    # where the lifecycles genuinely differ, so a test that only ever opens
    # links into a running app would not have caught either behaviour.
    print("cold launch:")
    try:
        call("POST", "/device/app/terminate", udid=udid, bundle_id=bundle_id)
    except requests.HTTPError:
        pass
    time.sleep(2.0)
    open_url(udid, f"{scheme}://open/cold/1")
    time.sleep(3.0)
    goto(udid, "Links", marker="link_count")
    cold_count = element_text(udid, "link_count")
    cold_route = element_text(udid, "link_route")

    if args.scene:
        # connectionOptions.urlContexts, delivered once.
        results.append(check("cold launch reaches the app", cold_count == "1",
                             f"link_count={cold_count}"))
        results.append(check("cold launch routed via scene:willConnectTo",
                             cold_route == "scene:willConnectTo", f"got {cold_route!r}"))
    else:
        # didFinishLaunching sees it in launchOptions, then open(_:) is called
        # with the same URL. An app that handles both without deduplicating
        # runs its routing twice per cold launch.
        results.append(check("cold launch delivers the URL twice", cold_count == "2",
                             f"link_count={cold_count} (expected 2)"))
        results.append(check("the later delivery is appdelegate:open",
                             cold_route == "appdelegate:open", f"got {cold_route!r}"))

    # -- Logs and web surfaces present --
    print("logs and web:")
    goto(udid, "Logs", marker="log_status")
    results.append(check("log controls present", element_text(udid, "log_status") is not None))
    goto(udid, "Web", marker="web_heading_native")
    time.sleep(1.5)
    # The previous version fell back to "any non-empty screen", so a failed
    # goto("Web") reported success. Assert a marker that exists only here.
    results.append(check("web screen reached",
                         element_text(udid, "web_heading_native") is not None))

    # Restore the default keyboard state (software keyboard available).
    call("POST", "/device/keyboard", udid=udid, enabled=False)
    print()

    passed = sum(results)
    print(f"{passed}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
