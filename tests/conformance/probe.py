"""The QuernProbe fixture apps, as a contract this suite can write tests against.

`tools/probe-app/` (UIKit) and `tools/probe-app-android/` (Kotlin) are mirrored
deliberately but not identically: where the platforms genuinely differ, so do the
apps. iOS has a segmented control and a stepper; Android has a checkbox. iOS
names its secure field `field_secure`, Android `field_password`. Android mirrors
its slider value into a separate `control_slider_value` label because `SeekBar`
reports its value inconsistently across API levels.

Those differences are real and worth keeping. What is not worth keeping is every
test branching on `if platform == "ios"`. So this module maps a **logical name**
onto the identifier each app actually uses, and a test asks for `SWITCH` or
`SECURE_FIELD`. A surface that exists on only one platform is declared as
`None` there, and the fixture skips rather than the test guessing.

The map is written out by hand rather than parsed from the sources. Deriving it
would make it agree with the apps by construction, including when an identifier
has been renamed out from under a test -- and the point of a contract is to fail
when one side moves.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: Both apps ship under the same id, deliberately.
BUNDLE_ID = "com.quern.probe"

#: A second bundle the iOS app builds for scene-lifecycle coverage.
SCENE_BUNDLE_ID = "com.quern.probe.scene"


class Ids:
    """Logical element names. Values are the keys into `ProbeContract.ids`."""

    # Text tab
    FIELD_DEFAULT = "field_default"
    FIELD_URL = "field_url"
    FIELD_EMAIL = "field_email"
    FIELD_SECURE = "field_secure"
    TEXT_EVENT_LOG = "text_event_log"

    # Controls tab
    SWITCH = "switch"
    SLIDER = "slider"
    SLIDER_VALUE = "slider_value"
    SEGMENT = "segment"
    STEPPER = "stepper"
    CHECKBOX = "checkbox"
    SHOW_ALERT = "show_alert"
    SHOW_SHEET = "show_sheet"
    CONTROL_READOUT = "control_readout"

    # Scroll tab
    SCROLL_CONTAINER = "scroll_container"

    # Links tab
    LINK_COUNT = "link_count"
    LINK_LAST_URI = "link_last_uri"

    # Logs tab
    LOG_START = "log_start"
    LOG_STOP = "log_stop"
    LOG_BURST = "log_burst"
    LOG_STATUS = "log_status"
    LOG_TICK_COUNT = "log_tick_count"

    # Web tab
    WEB_VIEW = "web_view"
    WEB_HEADING_NATIVE = "web_heading_native"

    # Diag tab
    CRASH_UNCAUGHT = "crash_uncaught"


#: Rows the scroll tab guarantees. `row_0` is on screen at rest; the last one is
#: reachable only by actually scrolling, which is what makes it a scroll test
#: rather than a tap test.
SCROLL_ROW_COUNT = 200


@dataclass(frozen=True)
class ProbeContract:
    """One platform's half of the fixture."""

    platform: str
    source_dir: Path
    #: Logical name -> the identifier that platform actually exposes.
    #: `None` means "this surface does not exist here".
    ids: dict[str, str | None]
    #: Tab name -> how to select it. iOS tabs carry `tab_<name>`; on Android the
    #: tabs are children of `probe_tabs` and are selected by label.
    tab_identifier: str | None
    #: Rows on the scroll tab are individually identified on iOS (`row_0` …) and
    #: share one id on Android (`row_label`), where the label distinguishes them.
    row_identifier_template: str | None
    row_label_template: str
    #: An identifier present as soon as the app has drawn its first screen.
    #: Polled after launch, because a blind sleep is either too short on a
    #: cold start or wasted on every warm one.
    ready_identifier: str = ""

    def id_for(self, logical: str) -> str | None:
        return self.ids.get(logical)

    def row_identifier(self, index: int) -> str | None:
        if self.row_identifier_template is None:
            return None
        return self.row_identifier_template.format(index=index)

    def row_label(self, index: int) -> str:
        return self.row_label_template.format(index=index)


_REPO_ROOT = Path(__file__).resolve().parents[2]

IOS = ProbeContract(
    platform="ios",
    source_dir=_REPO_ROOT / "tools" / "probe-app",
    ids={
        Ids.FIELD_DEFAULT: "field_default",
        Ids.FIELD_URL: "field_url",
        Ids.FIELD_EMAIL: "field_email",
        Ids.FIELD_SECURE: "field_secure",
        Ids.TEXT_EVENT_LOG: "text_event_log",
        Ids.SWITCH: "control_switch",
        Ids.SLIDER: "control_slider",
        # iOS reads the slider's value off the element itself; there is no
        # mirror label, and asking for one should skip rather than pass.
        Ids.SLIDER_VALUE: None,
        Ids.SEGMENT: "control_segment",
        Ids.STEPPER: "control_stepper",
        Ids.CHECKBOX: None,
        Ids.SHOW_ALERT: "control_show_alert",
        Ids.SHOW_SHEET: "control_show_sheet",
        Ids.CONTROL_READOUT: "control_value_log",
        Ids.SCROLL_CONTAINER: "scroll_table",
        Ids.LINK_COUNT: "link_count",
        Ids.LINK_LAST_URI: "link_last_uri",
        Ids.LOG_START: "log_start",
        Ids.LOG_STOP: "log_stop",
        Ids.LOG_BURST: "log_burst",
        Ids.LOG_STATUS: "log_status",
        Ids.LOG_TICK_COUNT: "log_tick_count",
        Ids.WEB_VIEW: "web_view",
        Ids.WEB_HEADING_NATIVE: "web_heading_native",
        Ids.CRASH_UNCAUGHT: "diag_crash_uncaught",
    },
    tab_identifier="tab_{name}",
    row_identifier_template="row_{index}",
    row_label_template="Row {index}",
    ready_identifier="tab_text",
)

ANDROID = ProbeContract(
    platform="android",
    source_dir=_REPO_ROOT / "tools" / "probe-app-android",
    ids={
        Ids.FIELD_DEFAULT: "field_default",
        Ids.FIELD_URL: "field_url",
        Ids.FIELD_EMAIL: "field_email",
        Ids.FIELD_SECURE: "field_password",
        Ids.TEXT_EVENT_LOG: "text_event_log",
        Ids.SWITCH: "control_switch",
        Ids.SLIDER: "control_slider",
        Ids.SLIDER_VALUE: "control_slider_value",
        Ids.SEGMENT: None,
        Ids.STEPPER: None,
        Ids.CHECKBOX: "control_checkbox",
        Ids.SHOW_ALERT: "control_show_alert",
        # No sheet surface on the Android side.
        Ids.SHOW_SHEET: None,
        Ids.CONTROL_READOUT: "control_readout",
        Ids.SCROLL_CONTAINER: "scroll_list",
        Ids.LINK_COUNT: "link_count",
        Ids.LINK_LAST_URI: "link_last_uri",
        Ids.LOG_START: "log_start",
        Ids.LOG_STOP: "log_stop",
        Ids.LOG_BURST: "log_burst",
        Ids.LOG_STATUS: "log_status",
        Ids.LOG_TICK_COUNT: "log_tick_count",
        Ids.WEB_VIEW: "web_view",
        Ids.WEB_HEADING_NATIVE: "web_heading_native",
        Ids.CRASH_UNCAUGHT: "diag_crash_uncaught",
    },
    tab_identifier=None,
    row_identifier_template=None,
    row_label_template="Row {index}",
    ready_identifier="probe_tabs",
)

CONTRACTS = {"ios": IOS, "android": ANDROID}


#: Tabs reachable directly on the iOS bar, in order.
#:
#: A UITabBar shows at most five *items*, and when there are more tabs than that
#: the fifth item is "More" rather than a tab -- so eight tabs means four are on
#: the bar and the other four are behind More, which keeps its own navigation
#: stack. Counting "five on the bar" is the easy mistake: it is five items, four
#: tabs. Measured against the live screen, which reports exactly
#: `Text, Controls, Scroll, Links, More`.
IOS_BAR_TABS = ("text", "controls", "scroll", "links")
IOS_MORE_TABS = ("logs", "location", "web", "diag")


class ProbeUnavailable(RuntimeError):
    """The fixture app could not be built or installed, with the reason."""


def build_ios(*, timeout: float = 600.0) -> Path:
    """Build the iOS probe app and return the bundle path.

    Raises `ProbeUnavailable` rather than failing a test directly, so the caller
    decides between skip and fail. A missing Xcode is a skip; a build that breaks
    is arguably a failure, but not one this suite is placed to judge.
    """
    script = IOS.source_dir / "build.sh"
    if not script.exists():
        raise ProbeUnavailable(f"no build script at {script}")
    if not shutil.which("xcrun"):
        raise ProbeUnavailable("xcrun is not on PATH; Xcode is required")

    try:
        result = subprocess.run(  # noqa: S603 - fixed path in this repo
            [str(script)],
            cwd=str(IOS.source_dir),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProbeUnavailable(
            f"probe-app build did not finish in {timeout:.0f}s"
        ) from exc

    bundle = IOS.source_dir / "build" / "QuernProbe.app"
    if result.returncode != 0 or not bundle.exists():
        raise ProbeUnavailable(
            f"probe-app build failed (exit {result.returncode}):\n"
            f"{result.stdout[-1500:]}\n{result.stderr[-1500:]}"
        )
    return bundle


def build_android(*, timeout: float = 900.0) -> Path:
    """Build the Android probe app and return the APK path."""
    script = ANDROID.source_dir / "build.sh"
    if not script.exists():
        raise ProbeUnavailable(f"no build script at {script}")
    if not shutil.which("adb"):
        raise ProbeUnavailable("adb is not on PATH")

    try:
        result = subprocess.run(  # noqa: S603 - fixed path in this repo
            [str(script)],
            cwd=str(ANDROID.source_dir),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProbeUnavailable(
            f"probe-app-android build did not finish in {timeout:.0f}s"
        ) from exc

    apks = sorted(
        (ANDROID.source_dir / "app" / "build" / "outputs" / "apk").rglob("*.apk")
    )
    if result.returncode != 0 or not apks:
        raise ProbeUnavailable(
            f"probe-app-android build failed (exit {result.returncode}):\n"
            f"{result.stdout[-1500:]}\n{result.stderr[-1500:]}"
        )
    return apks[0]


# -- driver ------------------------------------------------------------------


class ProbeDriver:
    """Identifier-based interaction with a running probe app.

    Deliberately thin. It exists to keep the platform's quirks -- the iOS More
    tab, the two names for "More", Android's lack of per-row identifiers -- in
    one place, not to become an abstraction layer that hides what the API
    returned. Every method returns the server's own response, so a test can
    still assert on it.
    """

    #: `tap_element` scrolls an off-screen target into view by default, which
    #: means asking for an identifier that is simply absent spends the full
    #: source timeout swiping before failing. Off unless a test is specifically
    #: testing scroll-to-find.
    NO_SCROLL = {"scroll_to_find": False}

    def __init__(self, client, udid: str, contract: ProbeContract):
        self.client = client
        self.udid = udid
        self.contract = contract

    # -- reading -----------------------------------------------------------

    def ui_tree(self, **params) -> dict:
        return self.client.json_ok(
            "GET", "/api/v1/device/ui",
            params={"udid": self.udid, **params}, timeout=90.0,
        )

    def element(self, identifier: str) -> dict | None:
        """One element's state, or None when it is not on screen."""
        resp = self.client.get(
            "/api/v1/device/ui/element",
            params={"udid": self.udid, "identifier": identifier},
            timeout=60.0,
        )
        if resp.status_code == 404:
            return None
        if not resp.is_success:
            raise AssertionError(
                f"GET /device/ui/element?identifier={identifier} -> "
                f"{resp.status_code}: {resp.text[:300]}"
            )
        return (resp.json() or {}).get("element") or None

    def text_of(self, identifier: str) -> str | None:
        """Visible text, however this element happens to carry it.

        Label, value and text are three different fields and which one holds the
        content varies by widget and platform -- a UILabel uses `label`, a text
        field uses `value`. Collapsing them here keeps that out of every
        assertion.
        """
        element = self.element(identifier)
        if element is None:
            return None
        return element.get("label") or element.get("value") or element.get("text")

    def labels(self) -> set[str]:
        """Every label anywhere in the tree, for presence checks."""

        def walk(node):
            if isinstance(node, dict):
                yield node
                for value in node.values():
                    yield from walk(value)
            elif isinstance(node, list):
                for value in node:
                    yield from walk(value)

        return {
            n["label"] for n in walk(self.ui_tree()) if isinstance(n, dict) and n.get("label")
        }

    # -- acting ------------------------------------------------------------

    def tap(self, identifier: str, **extra):
        return self.client.json_ok(
            "POST", "/api/v1/device/ui/tap-element",
            json={"udid": self.udid, "identifier": identifier, **self.NO_SCROLL, **extra},
            timeout=90.0,
        )

    def tap_label(self, label: str, **extra):
        return self.client.json_ok(
            "POST", "/api/v1/device/ui/tap-element",
            json={"udid": self.udid, "label": label, **self.NO_SCROLL, **extra},
            timeout=90.0,
        )

    def type_text(self, identifier: str, text: str, **extra):
        """Type into a named field.

        Always names the field. `TypeTextRequest` documents why: without a
        selector the text goes wherever the keyboard is pointed, and whether
        that was anywhere at all cannot be known afterwards.
        """
        return self.client.json_ok(
            "POST", "/api/v1/device/ui/type",
            json={"udid": self.udid, "identifier": identifier, "text": text, **extra},
            timeout=120.0,
        )

    def clear_text(self, identifier: str, **extra):
        return self.client.json_ok(
            "POST", "/api/v1/device/ui/clear",
            json={"udid": self.udid, "identifier": identifier, **extra},
            timeout=90.0,
        )

    def scroll_to(self, *, identifier: str | None = None, label: str | None = None,
                  max_swipes: int = 15):
        return self.client.json_ok(
            "POST", "/api/v1/device/ui/scroll-to-element",
            json={
                "udid": self.udid,
                **({"identifier": identifier} if identifier else {}),
                **({"label": label} if label else {}),
                "max_swipes": max_swipes,
            },
            timeout=180.0,
        )

    def wait_for(self, *, identifier: str | None = None, label: str | None = None,
                 condition: str = "exists", value: str | None = None,
                 timeout_s: float = 10.0):
        payload: dict = {"udid": self.udid, "condition": condition}
        if identifier:
            payload["identifier"] = identifier
        if label:
            payload["label"] = label
        if value is not None:
            payload["value"] = value
        payload["timeout"] = timeout_s
        return self.client.post(
            "/api/v1/device/ui/wait-for-element", json=payload,
            timeout=timeout_s + 60.0,
        )

    # -- navigation --------------------------------------------------------

    def goto(self, tab: str) -> None:
        """Select a tab by name, on either platform.

        The iOS path is the awkward one, and the awkwardness is iOS's, not
        Quern's -- lifted from `tools/probe-app/selftest.py`, which found it:

        * the bar shows five items and the rest live in a More list;
        * the More tab keeps its own navigation stack, so selecting it again
          returns to whatever was pushed rather than to the list;
        * "More" names two different elements -- the tab (a RadioButton) and the
          navigation back button (a Button) -- so a tap has to say which.
        """
        import time

        if self.contract.platform != "ios":
            self.tap_label(tab.capitalize())
            time.sleep(1.0)
            return

        name = tab.lower()
        if name in IOS_BAR_TABS:
            self.tap(self.contract.tab_identifier.format(name=name),
                     skip_stability_check=True)
            time.sleep(1.2)
            return

        self.tap_label("More", element_type="RadioButton", skip_stability_check=True)
        time.sleep(1.2)
        if "More" in self.labels():
            # Pop whatever the More stack was left on. Absent is fine: it means
            # we are already looking at the list.
            resp = self.client.post(
                "/api/v1/device/ui/tap-element",
                json={"udid": self.udid, "label": "More", "element_type": "Button",
                      **self.NO_SCROLL},
                timeout=90.0,
            )
            if resp.is_success:
                time.sleep(1.0)
        self.tap_label(tab.capitalize())
        time.sleep(1.2)

    def relaunch(self) -> None:
        """Terminate and relaunch, returning the app to its initial state.

        The reset mechanism for tests that need a known starting point. It is
        deliberately *not* `clear_text`: using the thing under test to set up
        the test means a bug in it fails every test that ran setup, and the one
        real defect arrives buried in a pile of consequences. That is not
        hypothetical here -- F8 in FINDINGS.md failed three tests, one of which
        was about clearing and two of which were about typing.
        """
        self.client.post(
            "/api/v1/device/app/terminate",
            json={"udid": self.udid, "bundle_id": BUNDLE_ID}, timeout=90.0,
        )
        self.client.json_ok(
            "POST", "/api/v1/device/app/launch",
            json={"udid": self.udid, "bundle_id": BUNDLE_ID}, timeout=180.0,
        )
        self.wait_until_ready()

    def wait_until_ready(self, timeout_s: float = 30.0) -> None:
        """Block until the app has drawn, or fail saying what was on screen.

        `launch_app` returns when the launch was *requested*, not when the first
        frame is up, so the query right after it can land on the launcher or on a
        half-built tree. That failure is intermittent and misleading -- it
        surfaces as "no element found matching label='Text'" with a screen
        summary full of images nothing in this app has.

        Polls for a sentinel rather than sleeping: a fixed delay is too short on
        a cold start and wasted on every warm one.
        """
        import time

        sentinel = self.contract.ready_identifier
        if not sentinel:
            time.sleep(2.0)
            return

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.element(sentinel) is not None:
                return
            time.sleep(0.5)

        summary = self.client.get(
            "/api/v1/device/screen-summary",
            params={"udid": self.udid}, timeout=60.0,
        )
        raise AssertionError(
            f"the probe app did not show {sentinel!r} within {timeout_s:.0f}s "
            f"after launch. On screen instead: {summary.text[:400]}"
        )
