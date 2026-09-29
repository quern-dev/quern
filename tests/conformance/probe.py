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

#: The first iOS release that refuses to launch an app with no scene manifest.
#: Below it, the app-delegate build is what this suite runs -- it is the older
#: and more common shape, and dropping it would stop covering it anywhere.
SCENE_REQUIRED_IOS_MAJOR = 27


def scene_lifecycle_required(os_version: str) -> bool:
    """Does this runtime refuse the app-delegate build?

    iOS 27 makes a scene manifest mandatory: a bundle without one dies at
    startup with "UIScene life cycle is required for apps built with this SDK",
    and `launch_app` still reports success with a pid, because the process did
    start (#235). Nothing downstream can tell that apart from a slow launch, so
    the choice is made here, from the runtime version, rather than by trying the
    app-delegate build and reading the wreckage.

    Unparseable or empty versions answer False: the app-delegate build is the
    default, and guessing "scene" for a runtime we could not identify would swap
    what is covered on every machine whose device list shapes its versions
    differently.
    """
    digits = ""
    for char in os_version:
        if char.isdigit():
            digits += char
        elif digits:
            break
    if not digits:
        return False
    return int(digits) >= SCENE_REQUIRED_IOS_MAJOR


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
    SCROLL_TO_TOP = "scroll_to_top"
    SCROLL_TO_BOTTOM = "scroll_to_bottom"

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

    def row_locator(self, index: int) -> dict:
        """How to ask for a row on this platform.

        By identifier where the rows have one, by label otherwise. Android's
        RecyclerView gives every row the same resource id, so a test written
        against `row_identifier` alone cannot run there -- which is how the
        four scroll tests came to skip on Android, and how #232 (its sweep
        cannot reach past ~110 rows) went unnoticed.
        """
        identifier = self.row_identifier(index)
        if identifier is not None:
            return {"identifier": identifier}
        return {"label": self.row_label(index)}


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
        Ids.SCROLL_TO_TOP: "scroll_to_top",
        Ids.SCROLL_TO_BOTTOM: "scroll_to_bottom",
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
        Ids.SCROLL_TO_TOP: "scroll_to_top",
        Ids.SCROLL_TO_BOTTOM: "scroll_to_bottom",
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


def build_ios(*, scene: bool = False, timeout: float = 600.0) -> Path:
    """Build the iOS probe app and return the bundle path.

    `scene` builds the scene-lifecycle variant instead. The two are the same
    sources under two Info.plists, so which one a test runs against changes the
    lifecycle and nothing the contract names -- see `scene_lifecycle_required`
    for when the choice is forced.

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
            [str(script), *(["--scene"] if scene else [])],
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

    name = "QuernProbeScene.app" if scene else "QuernProbe.app"
    bundle = IOS.source_dir / "build" / name
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

    def __init__(
        self,
        client,
        udid: str,
        contract: ProbeContract,
        bundle_id: str = BUNDLE_ID,
    ):
        self.client = client
        self.udid = udid
        self.contract = contract
        #: Which of the two iOS bundles is installed. Carried rather than read
        #: from the module, because `relaunch` terminating the bundle that is
        #: *not* running succeeds and leaves the app up, so the reset silently
        #: does nothing and the next test inherits the last one's state.
        self.bundle_id = bundle_id

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

    def row(self, index: int) -> dict | None:
        """The row's element, however this platform identifies its rows."""
        locator = self.contract.row_locator(index)
        if "identifier" in locator:
            return self.element(locator["identifier"])
        wanted = locator["label"]
        for element in self.ui_tree().get("elements") or []:
            if element.get("label") == wanted:
                return element
        return None

    def scroll_to_row(self, index: int, **kw):
        return self.scroll_to(**self.contract.row_locator(index), **kw)

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
            json={"udid": self.udid, "bundle_id": self.bundle_id}, timeout=90.0,
        )
        self.client.json_ok(
            "POST", "/api/v1/device/app/launch",
            json={"udid": self.udid, "bundle_id": self.bundle_id}, timeout=180.0,
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
            f"{self.bundle_id} did not show {sentinel!r} within {timeout_s:.0f}s "
            f"after launch. A launch reported as successful whose UI never "
            f"appears is what an iOS 27 lifecycle mismatch looks like (#235). "
            f"On screen instead: {summary.text[:400]}"
        )

    # -- scroll fixture ----------------------------------------------------

    def viewport(self) -> Viewport | None:
        """Where the scroll list currently sits, read from the tree.

        Returns None when no rows are on screen -- a different answer from
        "position zero", and the distinction matters when a test is trying to
        work out whether it is even looking at the scroll tab.
        """
        rows: list[tuple[int, float, float]] = []
        readout = ""
        container = self.contract.id_for(Ids.SCROLL_CONTAINER)
        for element in self.ui_tree().get("elements") or []:
            if container and element.get("identifier") == container:
                readout = element.get("value") or ""
            if (element.get("label") or "").startswith("rows ") and not readout:
                readout = element["label"]          # Android carries it on a label
            index = self._row_index(element)
            frame = element.get("frame")
            if index is None or not frame:
                continue
            rows.append((index, frame.get("y", 0.0), frame.get("height", 0.0)))
        if not rows:
            return None
        rows.sort()
        first, first_y, height = rows[0]
        return Viewport(
            first=first, last=rows[-1][0], row_height=height or 1.0,
            offset_px=first * (height or 1.0) - first_y,
            app_readout=readout,
        )

    def _row_index(self, element: dict) -> int | None:
        """Pull the row number out of whichever field this platform uses.

        iOS puts it in the identifier (`row_12`); Android's RecyclerView
        recycles views, so every row shares the id `row_label` and the index
        lives in the label instead.
        """
        for key in ("identifier", "label"):
            value = element.get(key) or ""
            # iOS says `row_41` in the identifier and `Row 41` in the label;
            # Android has no per-row identifier and says `Row 41`. Both
            # spellings are read here rather than one per platform, because a
            # parser that knows only one of them fails silently -- it did,
            # returning no rows at all on Android, which reads as "the list is
            # empty" rather than as a mismatch.
            head, _, tail = value.replace("_", " ").partition(" ")
            if head.lower() == "row" and tail.isdigit():
                return int(tail)
        return None

    def swipe_down(self, *, settle: float = 0.9) -> None:
        """One sweep-sized swipe, with the geometry the server's loop uses.

        `_ios_scroll_to_element` swipes from 0.72 to 0.30 of screen height with
        a 0.3s duration. Matching it here means a manual sweep measures the same
        travel the real loop gets, rather than a number that only describes this
        helper. The settle delay is the deliberate difference: it is what the
        server does *not* do, and comparing the two is the whole diagnostic.
        """
        import time

        frame = None
        for element in self.ui_tree().get("elements") or []:
            if element.get("type") in ("Application", "Window") and element.get("frame"):
                frame = element["frame"]
                break
        height = (frame or {}).get("height") or 874.0
        width = (frame or {}).get("width") or 402.0
        self.client.json_ok(
            "POST", "/api/v1/device/ui/swipe",
            json={
                "udid": self.udid,
                "start_x": width / 2, "start_y": height * 0.72,
                "end_x": width / 2, "end_y": height * 0.30,
                "duration": 0.3,
            },
            timeout=90.0,
        )
        time.sleep(settle)

    def scroll_reset(self, *, to: str = "top") -> None:
        """Jump the list to a known end, so a scroll test starts where it says.

        Uses the fixture's own button rather than swiping back, because a swipe
        loop to return to the top is itself the thing under test -- resetting
        with it would make a scroll bug hide its own starting conditions. The
        jump is unanimated on both platforms for the same reason.

        This matters more than it looks: scroll position survives a tab switch,
        so without an explicit reset the second test to touch this tab starts
        wherever the first one left it.
        """
        logical = Ids.SCROLL_TO_TOP if to == "top" else Ids.SCROLL_TO_BOTTOM
        identifier = self.contract.id_for(logical)
        if identifier is None:  # pragma: no cover - both platforms define it
            raise AssertionError(f"no {logical!r} control in the {self.contract.platform} app")
        self.tap(identifier, skip_stability_check=True)
        import time
        time.sleep(0.4)


# -- scroll viewport tracing -------------------------------------------------


@dataclass(frozen=True)
class Viewport:
    """Which rows of the scroll fixture are on screen, and where it sits.

    Derived from the tree rather than from a screenshot. The rows are numbered
    and a recycling list keeps only the visible ones, so the tree already says
    exactly where the viewport is -- and `offset_px` turns that into a single
    scalar with sub-row precision, which is what makes travel per swipe
    measurable rather than merely describable.
    """

    first: int
    last: int
    row_height: float
    #: Content offset: `first * row_height - first_row_y`, increasing downward.
    #:
    #: **Origin-relative, so only differences are meaningful.** At the top of
    #: the list it equals minus the container's own y -- -116 on iOS, -507 on
    #: Android, where the units are pixels and the chrome above the list is
    #: taller. Subtracting two samples cancels the origin, which is the whole
    #: point: it measures travel with sub-row precision, where the row range
    #: alone cannot tell a half-scrolled row from a whole one.
    offset_px: float
    #: Seconds since the tracer started. A scroll that takes 183s and one that
    #: takes 17s are different failures, and the per-sample spacing shows where
    #: the time went -- a tree read costs ~1.8s, which is most of why sampling
    #: perturbs what it measures.
    at: float = 0.0
    #: The fixture's own answer, read from the scroll container's
    #: accessibilityValue: "rows 47-63 of 200". Collected as a cross-check
    #: rather than as the measurement. If this disagrees with the row range
    #: derived from the tree, the tree read is stale -- which is a different
    #: bug from the scroll skipping, and without both numbers they are
    #: indistinguishable.
    app_readout: str = ""

    @property
    def span(self) -> int:
        """Rows visible at once. The width of one sample's window."""
        return self.last - self.first + 1

    def __str__(self) -> str:
        text = f"rows {self.first}-{self.last} (span {self.span}, {self.offset_px:.0f}px)"
        if self.app_readout and self.app_readout != f"rows {self.first}-{self.last} of 200":
            text += f"  app says {self.app_readout!r}"
        return text


class ScrollTracer:
    """Records viewport samples so a scroll failure can be read afterwards.

    A scroll test that fails says "row 60 never appeared". That is true and
    useless: it does not say whether the list moved, how far each swipe carried
    it, or whether the target was skipped over between samples. The trace turns
    the same failure into a table someone can diagnose from.
    """

    def __init__(self, driver: ProbeDriver, *, screenshots: bool = False):
        import datetime
        import time

        self.driver = driver
        self.started = time.monotonic()
        self.started_wall = datetime.datetime.now(datetime.UTC)
        self.samples: list[tuple[str, Viewport | None]] = []
        self.timeline: dict | None = None
        self._timeline_active = False
        if screenshots:
            self.start_screenshots()

    def start_screenshots(self) -> None:
        """Turn on Quern's screenshot timeline for the life of this trace.

        The timeline middleware captures an image after every UI *action
        endpoint*, so the pictures line up with the calls this test makes.

        One limitation worth knowing before reading a trace: it sees HTTP
        requests, not the server's internal work. `scroll_to_element` performs
        up to 75 swipes inside a single request, and the timeline captures one
        screenshot when that request returns -- not one per swipe. So the
        pictures document the manual sweep, and the server's own loop stays a
        black box that only the before/after viewport samples describe.
        """
        response = self.driver.client.post(
            "/api/v1/device/screenshot/timeline/start",
            json={"udid": self.driver.udid}, timeout=60.0,
        )
        # 409 means someone else's timeline is already running; a trace is not
        # worth stealing it, and the viewport data stands on its own.
        self._timeline_active = response.is_success

    def stop_screenshots(self) -> None:
        if not self._timeline_active:
            return
        self._timeline_active = False
        response = self.driver.client.post(
            "/api/v1/device/screenshot/timeline/stop", timeout=90.0,
        )
        if response.is_success:
            self.timeline = response.json()

    def sample(self, note: str = "") -> Viewport | None:
        import dataclasses
        import time

        vp = self.driver.viewport()
        if vp is not None:
            vp = dataclasses.replace(vp, at=time.monotonic() - self.started)
        self.samples.append((note, vp))
        return vp

    def report(self, target: int | None = None) -> str:
        """Render the trace, naming the gaps a target could have fallen through."""
        self.stop_screenshots()
        if not self.samples:
            return "no viewport samples recorded"

        lines = ["viewport trace:"]
        seen: set[int] = set()
        previous: Viewport | None = None
        for note, vp in self.samples:
            if vp is None:
                lines.append(f"  {note or 'sample':<22} (no rows visible)")
                continue
            seen.update(range(vp.first, vp.last + 1))
            travel = ""
            gap = ""
            if previous is not None:
                travel = f"  travel {vp.offset_px - previous.offset_px:+8.0f}px"
                if vp.first > previous.last + 1:
                    gap = f"   *** never sampled: rows {previous.last + 1}-{vp.first - 1} ***"
            lines.append(f"  {vp.at:>6.1f}s  {note or 'sample':<20} {vp}{travel}{gap}")
            previous = vp

        if seen:
            missed = sorted(set(range(min(seen), max(seen) + 1)) - seen)
            if missed:
                lines.append(
                    f"  {len(missed)} row(s) never appeared in any sample: {missed}"
                )
            if target is not None:
                lines.append(
                    f"  target row {target}: "
                    + ("SEEN at least once" if target in seen
                       else "NEVER sampled — it was scrolled past between reads")
                )

        lines.extend(self._screenshot_lines())
        return "\n".join(lines)

    def _screenshot_lines(self) -> list[str]:
        """Correlate the captured images with the trace, by elapsed time.

        Screenshot timestamps are wall-clock ISO strings and the viewport
        samples are seconds-since-start, so they are converted to the same
        origin here. Without that the two lists sit side by side and the reader
        does the arithmetic -- which is exactly the work this is supposed to
        remove.
        """
        if not self.timeline:
            return []
        entries = self.timeline.get("entries") or []
        if not entries:
            return [
                f"screenshots: none captured (timeline "
                f"{self.timeline.get('session_id', '?')} recorded 0 actions)"
            ]

        import datetime

        lines = [
            f"screenshots ({len(entries)}) in {self.timeline.get('output_dir', '?')}:"
        ]
        for entry in entries:
            stamp = entry.get("timestamp", "")
            offset = ""
            try:
                when = datetime.datetime.fromisoformat(stamp)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=datetime.UTC)
                offset = f"{(when - self.started_wall).total_seconds():>6.1f}s"
            except (TypeError, ValueError):
                offset = "     ?"
            name = str(entry.get("screenshot", "")).rsplit("/", 1)[-1]
            lines.append(
                f"  {offset}  {entry.get('action', '?'):<28} "
                f"[{entry.get('status_code', '?')}]  {name}"
            )
        return lines
