"""U2Backend — async wrapper around uiautomator2 for Android UI automation."""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from server.config import CONFIG_DIR
from server.device.android.scrcpy_input import ScrcpyInput
from server.device.gestures import Plan, check_edge_start
from server.models import DeviceError, InvalidDeviceRequestError

logger = logging.getLogger(__name__)

# Path to our forked Quern Driver APK (AdbKeyboard IME only, no launcher icon)
_QUERN_DRIVER_APK = CONFIG_DIR / "bin" / "quern-driver.apk"

# Android class → iOS-style type mapping
_CLASS_MAP: dict[str, str] = {
    "android.widget.Button": "Button",
    "android.widget.ImageButton": "Button",
    "android.widget.TextView": "StaticText",
    "android.widget.EditText": "TextField",
    "android.widget.AutoCompleteTextView": "TextField",
    "android.widget.MultiAutoCompleteTextView": "TextField",
    "android.widget.ImageView": "Image",
    "android.widget.CheckBox": "CheckBox",
    "android.widget.RadioButton": "RadioButton",
    "android.widget.Switch": "Switch",
    "android.widget.ToggleButton": "Toggle",
    "android.widget.SeekBar": "Slider",
    "android.widget.ProgressBar": "ProgressIndicator",
    "android.widget.Spinner": "Picker",
    "android.widget.ScrollView": "ScrollView",
    "android.widget.HorizontalScrollView": "ScrollView",
    "android.widget.ListView": "ScrollView",
    "android.widget.GridView": "ScrollView",
    "android.widget.TabHost": "TabBar",
    "android.widget.TabWidget": "TabBar",
    "android.view.View": "Other",
    "android.view.ViewGroup": "Group",
    "android.widget.FrameLayout": "Group",
    "android.widget.LinearLayout": "Group",
    "android.widget.RelativeLayout": "Group",
    "android.widget.Toolbar": "Toolbar",
    "androidx.appcompat.widget.Toolbar": "Toolbar",
    "androidx.recyclerview.widget.RecyclerView": "ScrollView",
    "androidx.viewpager.widget.ViewPager": "ScrollView",
    "androidx.viewpager2.widget.ViewPager2": "ScrollView",
    "com.google.android.material.bottomnavigation.BottomNavigationView": "TabBar",
    "com.google.android.material.tabs.TabLayout": "TabBar",
    "com.google.android.material.floatingactionbutton.FloatingActionButton": "Button",
    "com.google.android.material.textfield.TextInputEditText": "TextField",
    "com.google.android.material.textfield.TextInputLayout": "Group",
}

# Android class names that hold editable text (value = text content)
_EDITABLE_CLASSES = frozenset({
    "android.widget.EditText",
    "android.widget.AutoCompleteTextView",
    "android.widget.MultiAutoCompleteTextView",
    "com.google.android.material.textfield.TextInputEditText",
})

# Button name mapping: press_button name → u2 press key
_BUTTON_MAP: dict[str, str] = {
    # iOS-compatible names
    "home": "home",
    "HOME": "home",
    # Android-specific
    "back": "back",
    "BACK": "back",
    "recents": "recent",
    "RECENTS": "recent",
    "recent": "recent",
    "RECENT": "recent",
    "enter": "enter",
    "ENTER": "enter",
    "delete": "delete",
    "DELETE": "delete",
    "volumeUp": "volume_up",
    "volumeDown": "volume_down",
    "power": "power",
    "POWER": "power",
    "menu": "menu",
    "MENU": "menu",
}

_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def _parse_bounds(bounds: str) -> dict[str, float] | None:
    """Parse Android bounds string '[x1,y1][x2,y2]' to {x, y, width, height}."""
    m = _BOUNDS_RE.match(bounds)
    if not m:
        return None
    x1, y1, x2, y2 = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
    return {"x": float(x1), "y": float(y1), "width": float(x2 - x1), "height": float(y2 - y1)}


def _map_class(class_name: str) -> str:
    """Map Android class name to iOS-style type name."""
    if class_name in _CLASS_MAP:
        return _CLASS_MAP[class_name]
    # Strip package prefix, keep final class name
    return class_name.rsplit(".", 1)[-1] if "." in class_name else class_name


def _normalize_node(node: ET.Element) -> dict:
    """Convert a uiautomator2 XML element to an idb-compatible dict."""
    class_name = node.get("class", "")
    mapped_type = _map_class(class_name)

    text = node.get("text", "")
    content_desc = node.get("content-desc", "")
    resource_id = node.get("resource-id", "")

    # Label: prefer text, fall back to content-desc
    label = text or content_desc

    # Identifier: strip package prefix from resource-id
    identifier = None
    if resource_id:
        identifier = resource_id.split("/", 1)[-1] if "/" in resource_id else resource_id

    # Value: for editable fields use text; otherwise normalize selection state.
    # Android exposes selection state via two distinct mechanisms depending on
    # widget type, both of which we collapse to AXValue "1"/"0" for parity
    # with iOS (where AXValue serves the same role for switches and radios).
    #   checkable=true → use the `checked` attribute (Switch, CheckBox,
    #     RadioButton, ToggleButton — Android's "is this compound button on?")
    #   selected=true → use the `selected` attribute (BottomNavigationView and
    #     TabLayout tabs — Android's "is this view the active one in its
    #     parent?"). Asymmetric on the false side: we don't emit "0" for every
    #     non-selected element because nearly everything on screen has
    #     selected="false" by default — that would flood AXValue with noise.
    value = None
    if class_name in _EDITABLE_CLASSES:
        value = text
    elif node.get("checkable") == "true":
        value = "1" if node.get("checked") == "true" else "0"
    elif node.get("selected") == "true":
        value = "1"

    # Frame from bounds
    bounds = node.get("bounds", "")
    frame = _parse_bounds(bounds)

    enabled = node.get("enabled", "true") == "true"

    return {
        "type": mapped_type,
        "AXLabel": label,
        "AXUniqueId": identifier,
        "AXValue": value,
        "frame": frame,
        "enabled": enabled,
        "role": "",
        "role_description": "",
        # Preserve raw uiautomator2 XML attributes (verbatim, before the
        # mappings above collapse them) so the agent can debug the
        # normalizer without dropping to `adb shell uiautomator dump`. The
        # API strips this from responses unless include_raw=true.
        "extra_attrs": dict(node.attrib),
    }


def _flatten_tree(node: ET.Element) -> list[dict]:
    """Recursively flatten XML tree into a list of normalized dicts."""
    result: list[dict] = []
    normalized = _normalize_node(node)
    # Skip invisible/zero-size nodes
    frame = normalized["frame"]
    if frame and (frame["width"] > 0 or frame["height"] > 0):
        result.append(normalized)
    for child in node:
        result.extend(_flatten_tree(child))
    return result


def _nested_tree(node: ET.Element) -> dict:
    """Recursively build nested dict from XML tree."""
    normalized = _normalize_node(node)
    children = [_nested_tree(child) for child in node]
    if children:
        normalized["children"] = children
    return normalized


def _patch_ime_setup(device, apk_path: Path) -> None:
    """Replace the device's _setup_ime to install our Quern Driver APK.

    The upstream uiautomator2 library bundles an APK with a Chinese UI and
    a launcher icon. Our fork (quern-android-driver) is stripped to just the
    AdbKeyboard IME with no launcher presence.
    """
    ime_id = "com.github.uiautomator/.AdbKeyboard"

    def _quern_setup_ime(self_device=device):
        logger.info("Installing Quern Driver APK from %s", apk_path)
        self_device.adb_device.install(str(apk_path), nolaunch=True, uninstall=True)
        # Wait for IME to be registered (use -a to include disabled IMEs)
        for _ in range(10):
            ime_list = self_device.shell(["ime", "list", "-s", "-a"]).output.strip()
            if ime_id in ime_list:
                return
            time.sleep(0.3)
        raise DeviceError("Failed to register Quern Keyboard IME", tool="u2")

    device._setup_ime = _quern_setup_ime
    logger.debug("Patched _setup_ime to use Quern Driver APK")


def _focused_text_and_hint(device: Any) -> tuple[str | None, str | None]:
    """The focused field's text and hint, or ``(None, None)`` if unreadable.

    Read from the hierarchy dump rather than the jsonrpc node info, because only
    the dump publishes `hint`; the info dict carries `text` alone, which is why
    an empty field and a filled one look identical through it.

    ``None`` is deliberately distinct from an empty string. "Could not look" is
    not "was empty", and the caller skips its check rather than reporting a
    clear as failed on the strength of a lookup that never ran. Measured at
    ~86ms on a Pixel_7 emulator, against a clear that already costs a tap and a
    broadcast.
    """
    try:
        root = ET.fromstring(device.dump_hierarchy())
    except Exception:
        logger.debug("clear_text: could not read the hierarchy", exc_info=True)
        return (None, None)

    for node in root.iter("node"):
        # A window and a view can both report focus; the editable one is the
        # field that was just cleared.
        if node.get("focused") == "true" and "EditText" in node.get("class", ""):
            return (node.get("text", ""), node.get("hint", ""))
    return (None, None)

#: The at-rest check before a fast-path tap right after a change (#514): the
#: target is at rest once every read across REST_SPAN_S agrees, with at least
#: REST_AGREE reads, read nearly back to back. Spacing matters more than
#: count: with ~100ms sleeps plus a two-selector read, reads fell ~240ms apart
#: -- one whole period of the Taps fixture's slider, which jumps every 120ms --
#: and three reads saw it in one place and called it still, 6 times in 10.
#: Back to back, reads come one round trip apart (~60ms measured), well inside
#: the half period, so a target cannot jump between two reads unseen.
REST_GAP_S = 0.02
REST_SPAN_S = 0.2
#: A read that comes back this long after the previous one starts the
#: agreement over: a target can jump and jump back inside a slow read, and a
#: read measured at 300ms let three agreeing reads miss two jumps.
REST_MAX_GAP_S = 0.15
REST_AGREE = 3
REST_BUDGET_S = 1.0


class _MatchesChanged(Exception):
    """The selector matched a different number of elements than was counted."""

    def __init__(self, count: int) -> None:
        super().__init__(count)
        self.count = count


def _press_bounds(info: dict) -> dict | None:
    """The frame a press aims at: the visible part, as u2's own `click()`
    takes it, else the whole -- or None when there is nothing to press.

    A frame with no area is nothing to press: it would aim at its corner,
    (0, 0) for an all-zero one (CodeRabbit). And a visible part with no area
    means none of the element is on screen, so the whole frame is not tried
    either -- pressing its centre would tap whatever covers it."""
    def edges(b: object) -> dict | None:
        if isinstance(b, dict) and all(k in b for k in ("left", "top", "right", "bottom")):
            return b
        return None

    def area(b: dict) -> bool:
        return b["right"] > b["left"] and b["bottom"] > b["top"]

    visible = edges(info.get("visibleBounds"))
    if visible is not None:
        return visible if area(visible) else None
    whole = edges(info.get("bounds"))
    return whole if whole is not None and area(whole) else None


def _wait_for_rest(read, first) -> dict:
    """Read a frame until every read across REST_SPAN_S agrees (at least
    REST_AGREE reads), or REST_BUDGET_S passes. Returns {waited_ms, settled,
    bounds, gone, changed}: `bounds` the last read; `gone` when a read failed
    or found no frame -- the element left, or the read did, and either way the
    selector's answer is no longer the one counted; `changed`, the new count,
    when the match count moved."""
    started = time.monotonic()
    current, since, run = first, started, 1
    last = started

    def done(settled, bounds, gone=False, changed=None):
        return {"waited_ms": round((time.monotonic() - started) * 1000),
                "settled": settled, "bounds": bounds, "gone": gone, "changed": changed}

    while True:
        now = time.monotonic()
        if run >= REST_AGREE and now - since >= REST_SPAN_S:
            return done(True, current)
        if now - started >= REST_BUDGET_S:
            return done(False, current)
        time.sleep(REST_GAP_S)
        try:
            bounds = read()
        except _MatchesChanged as e:
            return done(False, None, changed=e.count)
        except Exception:  # noqa: BLE001 -- see the docstring
            return done(False, None, gone=True)
        if not bounds:
            return done(False, None, gone=True)
        now = time.monotonic()
        if bounds == current and now - last <= REST_MAX_GAP_S:
            run += 1
        else:
            current, since, run = bounds, now, 1
        last = now


def _as_the_tree_labels(found: list, label: str) -> list:
    """The label matches the dump path would also call `label`.

    The tree labels a node by its text, and by its content-desc only when it
    has none, so a node reading "Other" with the content-desc "Twin" is not a
    "Twin" there. Counted here, it made the fast path defer to a tree that
    found one "Twin" and tapped it, `ok`, past the ambiguity this path had
    just seen (CodeRabbit). Content-desc matches on nodes with other text are
    kept only when nothing else matches, so a lone one is still pressed, as
    before.
    """
    def labelled(entry) -> bool:
        sel, _, info = entry
        return "description" not in sel or (info or {}).get("text") in (None, "", label)

    def node(entry) -> tuple:
        info = entry[2] or {}
        return (repr(info.get("bounds")), info.get("className"))

    # One node whose text and content-desc are both the label is found by both
    # selectors; it is one match, not two.
    texts = {node(e) for e in found if "description" not in e[0]}
    found = [e for e in found if "description" not in e[0] or node(e) not in texts]
    primary = [e for e in found if labelled(e)]
    return primary or found


def _one_control(found: list) -> bool:
    """Whether several matches are one control and its own parts.

    Every frame nests inside the largest, and at most one of them takes a tap
    by itself: a Material tab or bottom-nav item, clickable, around its title,
    which is not. A clickable control inside a clickable container -- a Switch
    in its row -- stays two, as on the dump path (#490; CodeRabbit).
    """
    infos = [info or {} for _, _, info in found]
    if sum(1 for i in infos if i.get("clickable")) > 1:
        return False
    return _nested([i.get("bounds") or {} for i in infos])


def _nested(frames: list[dict]) -> bool:
    """Whether every frame lies inside the largest: one element and its parts.

    A frame without its four edges is not evidence of nesting.
    """
    try:
        boxes = [(float(f["left"]), float(f["top"]), float(f["right"]), float(f["bottom"]))
                 for f in frames]
    except (KeyError, TypeError, ValueError):
        return False
    if not boxes:
        return False
    outer = max(boxes, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
    return all(outer[0] <= b[0] and outer[1] <= b[1] and b[2] <= outer[2] and b[3] <= outer[3]
               for b in boxes)


#: A scroll-sweep frame taking longer than this turns frames off for the
#: sweep (#515); a frame whose brightest and darkest pixels are closer than
#: BLANK_RANGE is blank -- a FLAG_SECURE window captures black.
FRAME_SLOW_S = 1.0
BLANK_RANGE = 8

#: The sweep's default budget of swipes down (up gets twice it). A swipe moves
#: about 7.5 rows of a typical list (measured on the probe's 200-row list), so
#: the old default of 10 reached about row 75 and a deeper row read as absent
#: (#232). The end detection from #515 stops a direction at its end, so a
#: generous default costs nothing on a short list -- only a long one, where
#: the time is the point.
DEFAULT_MAX_SWIPES = 30


class _PressMayHaveLanded(Exception):
    """A selector's click or long click failed after it was sent (#251)."""


#: A pinch's default narrow end on Android, past ScaleGestureDetector's 27mm.
PINCH_NARROW_MM = 30.0


class U2Backend:
    """Manages Android UI automation via uiautomator2.

    Provides the same interface as IdbBackend and WdaBackend so
    DeviceControllerUI can delegate to it for Android devices.
    """

    #: What this backend calls itself in an error. The dispatcher reads it
    #: off whichever backend it selected, so an error can no longer name a
    #: tool that was never involved (#186).
    TOOL_NAME = "u2"

    #: Through scrcpy's server, which can inject more than one pointer
    #: (#252); without scrcpy a gesture is refused with the install command.
    multitouch = True

    #: A swipe from the screen edge is the system's under gesture navigation.
    edge_swipes = True

    #: Seconds a keyboard restore may wait on the device (#513).
    RESTORE_TIMEOUT_S = 15.0

    #: Seconds without typing on a device before its keyboard is put back
    #: (#513). A device used without a reservation -- the single-user case --
    #: otherwise kept u2's keyboard until the server stopped, which can be
    #: days. Typing again switches back, at ~300ms.
    IDLE_RESTORE_S = 180.0

    def __init__(self) -> None:
        self._devices: dict[str, object] = {}  # serial → u2.Device
        from server.device.android.ime import ImeKeeper

        #: The keyboard each device had before typing replaced it (#513).
        self.ime = ImeKeeper()
        #: When quern last typed or cleared on each device (monotonic).
        self._last_typed: dict[str, float] = {}
        #: Guards `_last_typed`: typing stamps it and the idle sweep clears
        #: or backs it off, and neither may overwrite the other's newer value.
        self._typed_lock = threading.Lock()
        from server.device.android.adb import _find_sdk_tool

        self._touch = ScrcpyInput(_find_sdk_tool("adb"))

    def _connect(self, serial: str):
        """Lazy, cached connection to a device.

        Patches the device's _setup_ime to install our Quern Driver APK
        instead of the bundled openatx one (which has a Chinese UI and
        a visible launcher icon).
        """
        if serial in self._devices:
            return self._devices[serial]

        try:
            import uiautomator2 as u2
        except ImportError:
            raise DeviceError(
                "uiautomator2 not installed. Run: pip install uiautomator2",
                tool="u2",
            )

        logger.info("Connecting to Android device %s via uiautomator2", serial)
        device = u2.connect(serial)

        # Patch IME setup to use our Quern Driver APK
        if _QUERN_DRIVER_APK.exists():
            _patch_ime_setup(device, _QUERN_DRIVER_APK)

        self._devices[serial] = device
        return device

    def is_connected(self, serial: str) -> bool:
        """Whether this device already has a uiautomator2 connection."""
        return serial in self._devices

    def _disconnect(self, serial: str) -> None:
        """Remove a cached connection."""
        self._devices.pop(serial, None)

    async def close(self) -> None:
        """Stop the gesture servers this backend started, and their forwards."""
        await self._touch.close_all()

    async def frame(self, udid: str, scale: float = 0.25, quality: int = 50) -> bytes:
        """A small JPEG of the screen, for comparing frames (#502).

        Through uiautomator2's own server, scaled down on the device: measured
        on an emulator at a median of 44ms, against 135ms for `adb exec-out
        screencap -p`, which encodes and ships a full-resolution PNG. Raises
        DeviceError on failure.
        """
        import base64

        def _do() -> bytes:
            device = self._connect(udid)
            return base64.b64decode(device.jsonrpc.takeScreenshot(scale, quality))

        try:
            return await asyncio.to_thread(_do)
        except Exception as e:
            raise DeviceError(str(e), tool="u2") from e

    async def describe_all(
        self,
        udid: str,
        snapshot_depth: int | None = None,
        source_timeout: float | None = None,
        # Accepted for interface parity with SimBridgeBackend and IdbBackend,
        # and ignored: uiautomator's dump is complete, so there is
        # nothing to probe and nothing for the caller to switch off.
        probe: bool = True,
    ) -> list[dict]:
        """Get all UI elements as a flat list of idb-compatible dicts."""

        def _do():
            device = self._connect(udid)
            xml_str = device.dump_hierarchy(
                max_depth=snapshot_depth,
            )
            root = ET.fromstring(xml_str)
            return _flatten_tree(root)

        try:
            return await asyncio.to_thread(_do)
        except Exception as e:
            if "UiAutomation not connected" in str(e) or "connection" in str(e).lower():
                # Stale connection — reconnect and retry once
                self._disconnect(udid)
                try:
                    return await asyncio.to_thread(_do)
                except Exception as retry_err:
                    raise DeviceError(str(retry_err), tool="u2") from retry_err
            raise DeviceError(str(e), tool="u2") from e

    async def describe_all_nested(
        self,
        udid: str,
        snapshot_depth: int | None = None,
        source_timeout: float | None = None,
    ) -> list[dict]:
        """Get UI elements with hierarchy preserved."""

        def _do():
            device = self._connect(udid)
            xml_str = device.dump_hierarchy(max_depth=snapshot_depth)
            root = ET.fromstring(xml_str)
            return [_nested_tree(child) for child in root]

        try:
            return await asyncio.to_thread(_do)
        except Exception as e:
            raise DeviceError(str(e), tool="u2") from e

    async def describe_point(
        self, udid: str, x: float, y: float
    ) -> dict | None:
        """Find the deepest element at (x, y)."""
        elements = await self.describe_all(udid)
        best: dict | None = None
        best_area = float("inf")
        for el in elements:
            f = el.get("frame")
            if not f:
                continue
            if f["x"] <= x <= f["x"] + f["width"] and f["y"] <= y <= f["y"] + f["height"]:
                area = f["width"] * f["height"]
                if area < best_area:
                    best = el
                    best_area = area
        return best

    async def perform_gesture(self, udid: str, plan: Plan) -> None:
        """A gesture `server.device.gestures` laid out, through scrcpy (#252).

        Checked against the screen first, in its current orientation: a
        point off it is refused, as on iOS, rather than pinned to the edge.
        """
        def _size():
            return self._connect(udid).window_size()

        try:
            width, height = await asyncio.to_thread(_size)
        except Exception as e:
            raise DeviceError(f"could not read the screen size of {udid}: {e}", tool="u2") from e
        points = [p for path in plan.paths or [] for p in path] + list(plan.points or [])
        for x, y in points:
            if not (0 <= x <= width and 0 <= y <= height):
                raise InvalidDeviceRequestError(
                    f"point ({x:.0f}, {y:.0f}) is off the {width}x{height} screen", tool="u2")
        await self._touch.perform(udid, plan, int(width), int(height))

    async def gesture_defaults(self, udid: str) -> dict:
        """The default finger distances for this screen, in its pixels.

        `unit` is pixels per dp, which scales the defaults set in iOS points.
        A pinch's narrow end is 30mm, not 60dp: ScaleGestureDetector ignores
        fingers closer than `config_minScalingSpan` (27mm in AOSP), so a pinch
        starting 60dp (~10mm) apart was measured reading 1.00 for a 3x spread,
        and one starting 500px apart read 1.44 for 1.6x (emulator, 2.75x).
        """
        def _screen():
            device = self._connect(udid)
            info = device.info
            width, height = device.window_size()
            return info["displayWidth"] / info["displaySizeDpX"], width, height

        try:
            unit, width, height = await asyncio.to_thread(_screen)
            unit = float(unit)
        except Exception as e:
            # A distance off by a factor is no reason to refuse the gesture,
            # but the caller is told, not only the log: a pinch starting 60px
            # apart reads 1.00 here, and an ok that says nothing about it is
            # the failure CONTRIBUTING describes (review).
            return {"warning": f"could not read this screen's density ({e}), so the "
                               f"gesture's default distances are in raw pixels and a "
                               f"pinch may read too small; pass distance to set them"}
        # Along the long axis, whichever way the device is held: 30mm at 2x
        # spans ~1040px, the whole width of a portrait phone, putting a
        # finger in the side band where gesture navigation reads a touch as
        # Back -- and, on the probe app, outside the view being pinched
        # (measured: 0.5x read nothing).
        return {"unit": unit, "pinch_distance": PINCH_NARROW_MM / 25.4 * 160 * unit,
                "pinch_angle": 90.0 if height >= width else 0.0}

    async def tap(self, udid: str, x: float, y: float, hold: float | None = None) -> None:
        """Tap at coordinates; with `hold`, a long press of that many seconds (#251)."""

        def _do():
            device = self._connect(udid)
            if hold is None:
                device.click(int(x), int(y))
            else:
                device.long_click(int(x), int(y), float(hold))

        try:
            await asyncio.to_thread(_do)
        except Exception as e:
            raise DeviceError(f"Tap failed: {e}", tool="u2") from e

    async def tap_by_selector(
        self,
        udid: str,
        identifier: str | None = None,
        label: str | None = None,
        hold: float | None = None,
        confirm_rest: bool = False,
    ) -> dict | None:
        """Tap an element by native uiautomator2 selector — WITHOUT dumping the
        full hierarchy.

        `confirm_rest` (#514), for a tap moments after the screen changed:
        before pressing, read the target's frame back to back until every
        read across `REST_SPAN_S` agrees, for up to `REST_BUDGET_S` -- see
        REST_GAP_S for why spacing, not count, decides it. One that never settles is
        pressed anyway, as the iOS settle does, and `rest.settled` says so;
        one that vanishes while being read defers to the dump path. The
        answer gains `rest: {waited_ms, settled}`.

        describe_all()/dump_hierarchy() performs a full accessibility traversal
        that scrolls CoordinatorLayout/RecyclerView content out of view on some
        screens (e.g. cache-detail's header actions), so resolving an identifier
        via the tree can scroll the target away before the tap lands. A targeted
        selector read finds just the one element, and a press at its centre taps it, with
        no such side effect — and it's faster (~0.3s vs ~1.2s).

        Returns a normalized {label, identifier, type, x, y} dict for the tapped
        element, or None if no match was found (caller should fall back to the
        dump-based path).

        Or `{"defer": reason}`, pressing nothing, when this path cannot tell
        the press is not a guess (#512): `"ambiguous"` when the name matches
        more than one element, `"uncounted"` when the matches could not be
        counted. The caller hands those to the dump path, which answers
        `ambiguous` with the candidates. A label counts its text and
        content-desc matches together, since either is the label.

        Matches that nest -- every one inside the largest -- are one element
        and its own parts, and are pressed as before. That is the stock
        Material bottom-nav item and tab: the item's content-desc copies its
        title, and the TextView inside it shows the same text. Without the
        fold every such tab counted 2, and the dump path could not fold it
        either, since the item reads as a Group rather than a control (review).
        An element whose text and content-desc are both the label nests in
        itself.
        """

        def _do():
            device = self._connect(udid)
            # Each group is one way of naming the element, tried in order; the
            # selectors within a group are counted together.
            groups: list[list[dict]] = []
            if identifier:
                # quern identifiers are resource-ids with the package stripped
                # ("button_log"); match the suffix of the full resource-id.
                groups.append([{"resourceIdMatches": rf".*/{re.escape(identifier)}$"}])
            if label:
                # A label may be visible text OR content-desc — try both.
                groups.append([{"text": label}, {"description": label}])

            for group in groups:
                # `info_list`, not `exists`: one RPC per selector either way,
                # and it says how many matched and where, so whether the press
                # would be a guess costs nothing extra -- and gives the frame
                # the answer reports, so no separate `info` read either.
                try:
                    found = [(sel, i, info) for sel in group
                             for i, info in enumerate(device(**sel).info_list() or [])]
                except Exception:  # noqa: BLE001 -- a failed count is not a miss
                    # Read as None, this would sweep the screen for an element
                    # that may be in plain view. Logged with its cause, so a
                    # u2 too old for info_list (before 3.5.2) reads differently
                    # from a dropped RPC (CodeRabbit).
                    logger.debug("tap_by_selector could not count matches", exc_info=True)
                    return {"defer": "uncounted"}
                if label is not None and "description" in group[-1]:
                    found = _as_the_tree_labels(found, label)
                if not found:
                    continue
                if len(found) > 1 and not _one_control(found):
                    return {"defer": "ambiguous", "matches": len(found)}
                sel, index, info = found[0]
                obj = device(**sel, instance=index) if index else device(**sel)
                info = info or {}
                rest = None
                if confirm_rest:
                    counted = len(found)

                    def recount():
                        now = [e for s_ in group for e in
                               ((s_, i, inf) for i, inf in
                                enumerate(device(**s_).info_list() or []))]
                        if label is not None and "description" in group[-1]:
                            now = _as_the_tree_labels(now, label)
                        return len(now)

                    # The frame through one `info` per read (~50ms): with a
                    # two-selector recount on every read, reads fell ~120ms
                    # apart -- the fixture slider's half period -- and saw it
                    # still, 3 times in 10. The count is taken again once, at
                    # the end, before the press: a transition can attach a
                    # second match, or detach the first, while this waits
                    # (review).
                    rest = _wait_for_rest(lambda: _press_bounds(obj.info or {}),
                                          _press_bounds(info))
                    changed = rest.pop("changed")
                    if changed is None and not rest["gone"]:
                        try:
                            n = recount()
                        except Exception:  # noqa: BLE001 -- could not tell
                            return {"defer": "uncounted"}
                        if n != counted:
                            changed = n
                    if changed is not None:
                        return {"defer": "ambiguous" if changed > 1 else "vanished",
                                "matches": changed}
                    if rest.pop("gone"):
                        return {"defer": "vanished"}
                    if rest["bounds"]:
                        info = {**info, "visibleBounds": rest["bounds"]}
                    del rest["bounds"]
                # One round trip (#516): press the centre of the frame already
                # read, rather than `obj.click()`, which waits for the element,
                # reads its frame again and then presses -- three. The visible
                # part, as `click()` used, so a row half under a bar is pressed
                # where it shows.
                bounds = _press_bounds(info)
                if not bounds:
                    # Counted, then gone by its own read: uiautomator leaves no
                    # frame for an instance that left between the two (review).
                    return {"defer": "vanished"}
                px = (bounds["left"] + bounds["right"]) / 2
                py = (bounds["top"] + bounds["bottom"]) / 2
                try:
                    if hold is None:
                        device.click(px, py)
                    else:
                        device.long_click(px, py, float(hold))
                except Exception as e:
                    # Past this point the press may have reached the device: a
                    # read timeout after it landed looks the same as one before.
                    # Falling back to the tree path would press again (#407).
                    raise _PressMayHaveLanded(e) from e
                cx, cy = px, py
                cls = info.get("className", "")
                return {
                    "label": info.get("text")
                    or info.get("contentDescription")
                    or label
                    or "",
                    "identifier": identifier,
                    "type": _map_class(cls) if cls else None,
                    "x": cx,
                    "y": cy,
                    **({"rest": rest} if rest is not None else {}),
                }
            return None

        try:
            return await asyncio.to_thread(_do)
        except _PressMayHaveLanded as e:
            what = "long press" if hold is not None else "tap"
            raise DeviceError(
                f"the {what} on {udid} failed after it was sent ({e.__cause__!r}); it "
                f"may already have landed, so it was not sent again -- check the screen",
                tool="u2") from e
        except Exception as e:
            logger.debug("tap_by_selector fell back (%s)", e)
            return None

    async def scroll_into_view(
        self,
        udid: str,
        identifier: str | None = None,
        label: str | None = None,
        max_swipes: int = DEFAULT_MAX_SWIPES,
        report: dict | None = None,
    ) -> dict | None:
        """Scroll a scrollable container until the target element is on-screen —
        WITHOUT dumping the full hierarchy.

        Each swipe is followed by a small frame of the screen (#515), compared
        with the one before it as #502's frames are. A swipe that changed
        nothing means that direction is at its end, so the sweep turns round;
        when the first swipe each way changed nothing, the screen does not
        scroll and the sweep stops. Before this it swiped blind, 10 down and 20
        up, whatever happened. An unchanged frame ends one direction only,
        never the sweep, and a frame that could not be taken or compared reads
        as moved, so the worst case is the old blind sweep. `report`, when
        given, gains `swipes`, `moved`, `ends` (the directions found at their
        end) and `no_progress` -- which takes a third unchanged swipe, since it
        says the screen does not scroll. A blank frame (FLAG_SECURE captures
        black) and a capture slower than FRAME_SLOW_S read as no frame.

        A bounded, metered swipe loop that re-checks the target by selector
        (``obj.exists`` — not ``dump_hierarchy``, so no dump-induced scroll, see
        #49) after each swipe. Deliberately NOT native
        ``UiScrollable.scrollIntoView``, which no-ops on Compose ``LazyColumn``
        (not flagged ``scrollable``) and can't match lazily-composed rows (#50);
        plain swipes work regardless. Sweeps down first, then up (for
        top-anchored controls). Returns a normalized
        ``{label, identifier, type, x, y}`` dict once the element is on-screen,
        or ``None`` if it never appeared within the swipe budget.
        """

        def _result(info: dict) -> dict:
            bounds = info.get("bounds") or {}
            cx = (bounds.get("left", 0) + bounds.get("right", 0)) / 2
            cy = (bounds.get("top", 0) + bounds.get("bottom", 0)) / 2
            cls = info.get("className", "")
            return {
                "label": info.get("text")
                or info.get("contentDescription")
                or label
                or "",
                "identifier": identifier,
                "type": _map_class(cls) if cls else None,
                "x": cx,
                "y": cy,
            }

        def _do():
            device = self._connect(udid)
            selectors: list[dict] = []
            if identifier:
                selectors.append({"resourceIdMatches": rf".*/{re.escape(identifier)}$"})
            if label:
                selectors.append({"text": label})
                selectors.append({"description": label})
            if not selectors:
                return None

            def _match():
                # These containers (RecyclerView, Compose LazyColumn/ScrollView)
                # only keep on-screen rows in the accessibility tree, so
                # `.exists` is equivalent to "visible" — no separate visibility
                # check needed.
                for sel in selectors:
                    obj = device(**sel)
                    if obj.exists:
                        try:
                            return obj.info or {}
                        except Exception:
                            return None
                return None

            info = _match()
            if info is not None:  # already on-screen
                return _result(info)

            try:
                w, h = device.window_size()
            except Exception:
                w, h = 1080, 2160
            cx, y_lo, y_hi = w // 2, int(h * 0.30), int(h * 0.72)

            def _scroll(y1: int, y2: int) -> None:
                # Metered stepped swipe: no fling (target stays where it lands)
                # and no row press/long-press. A plain duration-swipe flings and
                # a drag highlights rows.
                device.swipe(cx, y1, cx, y2, steps=25)

            from server.device import frames
            from server.device.settle import changed_fraction

            out = report if report is not None else {}
            out.update(swipes=0, moved=False)

            slow = []

            def _frame():
                # A capture that took over FRAME_SLOW_S turns frames off for
                # the rest of the sweep: every swipe would pay it (review).
                if slow:
                    return None
                started = time.monotonic()
                try:
                    png = base64.b64decode(device.jsonrpc.takeScreenshot(0.25, 50))
                    frame = frames.prepare(png, "android")
                except Exception:  # noqa: BLE001 -- no frame reads as moved
                    logger.debug("scroll_into_view: no frame", exc_info=True)
                    frame = None
                if time.monotonic() - started > FRAME_SLOW_S:
                    slow.append(True)
                # A blank frame is no frame. A FLAG_SECURE window (banking,
                # password screens) captures black, which never changes, so a
                # list there read as not scrolling and the sweep stopped
                # (review).
                if frame is not None:
                    low, high = frame.getextrema()
                    if high - low < BLANK_RANGE:
                        return None
                return frame

            def _unchanged(before, after) -> bool:
                if before is None or after is None or before.size != after.size:
                    return False
                return changed_fraction(before, after) < frames.UNCHANGED_BELOW

            # Sweep down (reveal rows below) then up (rows above / top-anchored
            # controls), re-checking after each swipe.
            state = {"previous": _frame()}
            DOWN, UP = ("bottom", (y_hi, y_lo)), ("top", (y_lo, y_hi))

            def sweep(direction: tuple[str, tuple[int, int]],
                      budget: int) -> tuple[dict | None, bool | None]:
                """Swipe one way until the target shows, the frame stops
                changing (that direction's end), or the budget is spent.
                Returns (info, first swipe moved?)."""
                name, (y1, y2) = direction
                first_moved = None
                for n in range(budget):
                    _scroll(y1, y2)
                    out["swipes"] += 1
                    info = _match()
                    if info is not None:
                        # `moved` stays what was measured: a target that
                        # appeared because the screen finished loading did
                        # not prove anything moved (review).
                        return info, first_moved
                    current = _frame()
                    still = _unchanged(state["previous"], current)
                    if current is not None:
                        state["previous"] = current
                    if n == 0:
                        first_moved = not still
                    if still:
                        out.setdefault("ends", []).append(name)
                        break
                    out["moved"] = True
                return None, first_moved

            info, down_moved = sweep(DOWN, max_swipes)
            if info is not None:
                return _result(info)
            info, up_moved = sweep(UP, max_swipes * 2)
            if info is not None:
                return _result(info)
            if down_moved is False and up_moved is False:
                # Two unchanged frames, but the first may have been a false
                # read at the top of a list, and the answer is "this does not
                # scroll". One more swipe down settles it (review).
                _scroll(y_hi, y_lo)
                out["swipes"] += 1
                info = _match()
                if info is not None:
                    return _result(info)
                current = _frame()
                if _unchanged(state["previous"], current):
                    out["no_progress"] = True
                    return None
                # It moved: the bottom "end" was a false read. Carry on the
                # search -- down with what is left of that budget, then back
                # up -- rather than stopping short of rows further down
                # (CodeRabbit).
                if current is not None:
                    state["previous"] = current
                out["moved"] = True
                out["ends"] = [e for e in out.get("ends", []) if e != "bottom"]
                for direction, budget in ((DOWN, max(0, max_swipes - 2)),
                                          (UP, max_swipes * 2)):
                    out["ends"] = [e for e in out["ends"] if e != direction[0]]
                    info, _ = sweep(direction, budget)
                    if info is not None:
                        return _result(info)
            return None

        try:
            return await asyncio.to_thread(_do)
        except Exception as e:
            logger.debug("scroll_into_view failed (%s)", e)
            return None

    async def swipe(
        self,
        udid: str,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
        duration: float = 0.5,
        edge: str | None = None,
    ) -> None:
        """Swipe gesture. An `edge` swipe must start at that edge, where
        gesture navigation reads it as the system's: measured on the emulator
        and a Pixel 5, a swipe from x=1 was a back gesture (#251)."""

        def _do():
            device = self._connect(udid)
            if edge is not None:
                width, height = device.window_size()
                check_edge_start(edge, start_x, start_y, width, height, tool="u2")
            device.swipe(int(start_x), int(start_y), int(end_x), int(end_y), duration=duration)

        try:
            await asyncio.to_thread(_do)
        except InvalidDeviceRequestError:
            raise
        except Exception as e:
            raise DeviceError(f"Swipe failed: {e}", tool="u2") from e

    async def type_text(self, udid: str, text: str) -> None:
        """Type text into the focused field. Supports Unicode via clipboard."""

        def _do():
            device = self._connect(udid)
            # Held through send_keys, which selects u2's keyboard: a restore
            # landing between the two would lose the record (#513).
            with self.ime.lock_for(udid):
                self.ime.remember(udid, device)
                try:
                    device.send_keys(text)
                finally:
                    with self._typed_lock:
                        self._last_typed[udid] = time.monotonic()

        try:
            await asyncio.to_thread(_do)
        except Exception as e:
            raise DeviceError(f"Type text failed: {e}", tool="u2") from e

    async def restore_keyboard(
        self, udid: str, *, idle: tuple[float | None, float] | None = None,
    ) -> dict | None:
        """Put back the keyboard typing replaced on this device (#513).

        None when nothing was replaced. Never raises; a failure is the
        returned action's `ok: False`, and the record is kept to try again.
        """
        if udid not in await asyncio.to_thread(self.ime.pending):
            return None

        def _do():
            lock = self.ime.lock_for(udid)
            # Typing in progress finishes first; a device whose lock is held
            # past half the bound is reported, not waited on -- and the
            # thread ends, rather than outliving the await blocked for ever.
            if not lock.acquire(timeout=self.RESTORE_TIMEOUT_S / 2):
                return {"udid": udid, "action": "keyboard_restored", "ok": False,
                        "error": "the device stayed busy"}
            try:
                if idle is not None:
                    seen, now = idle
                    # Typed on since the sweep judged it idle: not idle any
                    # more (CodeRabbit). Checked, and the stamp updated, under
                    # the lock typing takes.
                    if self._last_typed.get(udid) != seen:
                        return None
                return self.ime.restore(udid, self._connect(udid))
            finally:
                lock.release()

        try:
            # Bounded: an unplugged phone can hold a u2 connect for a long
            # time, and this runs while a release or a shutdown waits on it.
            # The worker thread can outlive the bound, but it holds nothing the
            # event loop waits on (#513 review).
            result = await asyncio.wait_for(asyncio.to_thread(_do), self.RESTORE_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001 -- the connection itself failed
            result = {"udid": udid, "action": "keyboard_restored", "ok": False,
                      "error": str(e) or type(e).__name__}
        self._settle_idle(udid, idle, result)
        return result

    def _settle_idle(self, udid: str, idle: tuple[float | None, float] | None,
                     result: dict | None) -> None:
        """After an idle restore, whatever its outcome: a success clears the
        device's stamp, and any failure -- the restore's own, a busy device, a
        timeout, a failed connect -- backs it off for another IDLE_RESTORE_S,
        since some failures repeat every time and a retry every sweep was ~120
        warnings an hour. Only if the stamp is still the one the sweep judged
        by: a type in between wrote a newer one, which stands (CodeRabbit)."""
        if idle is None or result is None:
            return
        seen, now = idle
        with self._typed_lock:
            if self._last_typed.get(udid) != seen:
                return
            if result.get("ok"):
                self._last_typed.pop(udid, None)
            else:
                self._last_typed[udid] = now

    async def restore_keyboards(self) -> list[dict]:
        """Every keyboard still recorded, put back: at shutdown, and at startup
        for a record an earlier process left. Never raises.

        Devices adb does not list as connected are skipped, their records kept
        for the next start: waiting on an unplugged phone used the shutdown's
        budget before a connected one was reached. The rest run at once, for
        the same reason.
        """
        pending = list(await asyncio.to_thread(self.ime.pending))
        if not pending:
            return []
        online = await self._online_serials()
        actions: list[dict] = []
        reachable = []
        for udid in pending:
            if online is not None and udid not in online:
                actions.append({"udid": udid, "action": "keyboard_restored", "ok": False,
                                "error": "not connected; kept for the next start"})
            else:
                reachable.append(udid)
        for action in await asyncio.gather(*(self.restore_keyboard(u) for u in reachable)):
            if action is not None:
                actions.append(action)
        return actions

    async def restore_idle_keyboards(self, now: float | None = None) -> list[dict]:
        """Put back the keyboard on every device quern has not typed on for
        `IDLE_RESTORE_S` (#513). A record with no typing in this process --
        a startup repair that could not reach the device -- counts as idle.
        Devices adb does not list are left for a later pass. Never raises."""
        pending = list(await asyncio.to_thread(self.ime.pending))
        if not pending:
            return []
        now = time.monotonic() if now is None else now
        # Each stamp read once, when the device is judged idle: re-read after
        # the `adb devices` await below, a type landing during it became the
        # stamp the locked check compared against, and passed (CodeRabbit).
        seen = {u: self._last_typed.get(u) for u in pending}
        idle = [u for u in pending
                if now - (seen[u] if seen[u] is not None else float("-inf"))
                >= self.IDLE_RESTORE_S]
        if not idle:
            return []
        online = await self._online_serials()
        if online is not None:
            idle = [u for u in idle if u in online]
        return [a for a in await asyncio.gather(*(
                    self.restore_keyboard(u, idle=(seen[u], now))
                    for u in idle)) if a is not None]

    async def _online_serials(self) -> set[str] | None:
        """Serials `adb devices` lists as `device`, or None if it could not say
        -- in which case every record is tried."""
        from server.device.android.adb import _find_sdk_tool

        adb = _find_sdk_tool("adb")
        if not adb:
            return None
        try:
            proc = await asyncio.create_subprocess_exec(
                adb, "devices",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), 5.0)
        except (TimeoutError, OSError):
            return None
        if proc.returncode != 0:
            return None
        return {line.split("\t")[0] for line in out.decode(errors="replace").splitlines()[1:]
                if line.endswith("\tdevice")}

    async def press_button(self, udid: str, button: str) -> None:
        """Press a hardware button."""
        key = _BUTTON_MAP.get(button)
        if not key:
            raise DeviceError(
                f"Unknown button '{button}'. Supported: "
                f"{', '.join(sorted(set(_BUTTON_MAP.values())))}",
                tool="u2",
            )

        def _do():
            device = self._connect(udid)
            device.press(key)

        try:
            await asyncio.to_thread(_do)
        except Exception as e:
            raise DeviceError(f"Press button failed: {e}", tool="u2") from e

    async def select_all_and_delete(
        self,
        udid: str,
        x: float,
        y: float,
        element_type: str | None = None,
        identifier: str | None = None,  # unused: this backend clears at (x, y)
    ) -> None:
        """Empty the text field at (x, y), and confirm that it emptied.

        Clears through uiautomator2's IME broadcast rather than by synthesising
        keystrokes. The previous implementation opened a u2 connection, used it
        only for the focusing tap, and then shelled out to::

            input keyevent KEYCODE_MOVE_HOME
            input keyevent --longpress KEYCODE_SHIFT_LEFT KEYCODE_MOVE_END
            input keyevent KEYCODE_DEL

        `input keyevent` given several keycodes sends them *in sequence, not as
        a chord*, so Shift was never held and nothing was ever selected: the
        caret went to the start, then to the end, and the single `KEYCODE_DEL`
        backspaced one character. Measured on a Pixel 3 XL -- 36 characters in,
        35 characters out, `{"status": "ok"}` (#177).

        `clear_text()` broadcasts `ADB_KEYBOARD_CLEAR_TEXT` to the AdbKeyboard
        IME, which is the same path `type_text` already relies on, so it clears
        in one operation with nothing to hold across calls.

        The read-back is the other half, and the more important one. #98 fixed
        this same defect on the iOS web path and hardened it with verification;
        the native path was left with no check at all, which is why this went
        unnoticed. Clearing is *setup* -- it runs before the interesting part of
        a test -- so a silent partial clear surfaces later as a mismatched
        string in an unrelated assertion.
        """

        def _do():
            device = self._connect(udid)
            device.click(int(x), int(y))  # focus the field the caller named

            with self.ime.lock_for(udid):  # clear_text selects u2's keyboard
                self.ime.remember(udid, device)
                try:
                    device.clear_text()
                finally:
                    with self._typed_lock:
                        self._last_typed[udid] = time.monotonic()

            # An empty EditText reports its *hint* in `text`, so a cleared field
            # does not read empty -- `field_default` reads 'default'. The hint
            # is published as its own attribute in the hierarchy dump, though it
            # is absent from the cheaper jsonrpc node info, which is what made
            # this look undecidable at first. Measured on a Pixel_7 emulator:
            #
            #     empty      text='email'             hint='email'   equal
            #     populated  text='email@example.com' hint='email'   not equal
            #
            # So `text == hint` is an exact emptiness test rather than a
            # heuristic, and it separates all three shapes this has to tell
            # apart: leftover content from #177's one-character delete, a clear
            # that removed nothing at all, and a hint that happens to be a
            # substring of what was typed -- 'email' out of 'email@example.com',
            # which an earlier version of this check reported as a failure on a
            # field it had emptied correctly.
            text, hint = _focused_text_and_hint(device)
            if text is not None and text != hint:
                raise DeviceError(
                    f"clear_text left {len(text)} character(s) in the field: "
                    f"{text[:40]!r}",
                    tool="u2",
                )

        try:
            await asyncio.to_thread(_do)
        except DeviceError:
            raise
        except Exception as e:
            raise DeviceError(f"Clear text failed: {e}", tool="u2") from e
