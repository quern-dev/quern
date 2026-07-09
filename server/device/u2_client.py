"""U2Backend — async wrapper around uiautomator2 for Android UI automation."""

from __future__ import annotations

import asyncio
import logging
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from server.models import DeviceError

logger = logging.getLogger("quern-debug-server.u2")

# Path to our forked Quern Driver APK (AdbKeyboard IME only, no launcher icon)
_QUERN_DRIVER_APK = Path.home() / ".quern" / "bin" / "quern-driver.apk"

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


class U2Backend:
    """Manages Android UI automation via uiautomator2.

    Provides the same interface as IdbBackend and WdaBackend so
    DeviceControllerUI can delegate to it for Android devices.
    """

    def __init__(self) -> None:
        self._devices: dict[str, object] = {}  # serial → u2.Device

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

    def _disconnect(self, serial: str) -> None:
        """Remove a cached connection."""
        self._devices.pop(serial, None)

    async def describe_all(
        self,
        udid: str,
        snapshot_depth: int | None = None,
        source_timeout: float | None = None,
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

    async def tap(self, udid: str, x: float, y: float) -> None:
        """Tap at coordinates."""

        def _do():
            device = self._connect(udid)
            device.click(int(x), int(y))

        try:
            await asyncio.to_thread(_do)
        except Exception as e:
            raise DeviceError(f"Tap failed: {e}", tool="u2") from e

    async def tap_by_selector(
        self,
        udid: str,
        identifier: str | None = None,
        label: str | None = None,
    ) -> dict | None:
        """Tap an element by native uiautomator2 selector — WITHOUT dumping the
        full hierarchy.

        describe_all()/dump_hierarchy() performs a full accessibility traversal
        that scrolls CoordinatorLayout/RecyclerView content out of view on some
        screens (e.g. cache-detail's header actions), so resolving an identifier
        via the tree can scroll the target away before the tap lands. A targeted
        selector click (UiObject.click) finds and taps just the one element with
        no such side effect — and it's faster (~0.3s vs ~1.2s).

        Returns a normalized {label, identifier, type, x, y} dict for the tapped
        element, or None if no match was found (caller should fall back to the
        dump-based path).
        """

        def _do():
            device = self._connect(udid)
            selectors: list[dict] = []
            if identifier:
                # quern identifiers are resource-ids with the package stripped
                # ("button_log"); match the suffix of the full resource-id.
                selectors.append({"resourceIdMatches": rf".*/{re.escape(identifier)}$"})
            if label:
                # A label may be visible text OR content-desc — try both.
                selectors.append({"text": label})
                selectors.append({"description": label})

            for sel in selectors:
                obj = device(**sel)
                if not obj.exists:
                    continue
                info = obj.info or {}
                obj.click()
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
            return None

        try:
            return await asyncio.to_thread(_do)
        except Exception as e:
            logger.debug("tap_by_selector fell back (%s)", e)
            return None

    async def swipe(
        self,
        udid: str,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
        duration: float = 0.5,
    ) -> None:
        """Swipe gesture."""

        def _do():
            device = self._connect(udid)
            device.swipe(int(start_x), int(start_y), int(end_x), int(end_y), duration=duration)

        try:
            await asyncio.to_thread(_do)
        except Exception as e:
            raise DeviceError(f"Swipe failed: {e}", tool="u2") from e

    async def type_text(self, udid: str, text: str) -> None:
        """Type text into the focused field. Supports Unicode via clipboard."""

        def _do():
            device = self._connect(udid)
            device.send_keys(text)

        try:
            await asyncio.to_thread(_do)
        except Exception as e:
            raise DeviceError(f"Type text failed: {e}", tool="u2") from e

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
    ) -> None:
        """Clear text in a field by selecting all and deleting."""

        def _do():
            device = self._connect(udid)
            # Tap the field to focus it
            device.click(int(x), int(y))
            # Select all via Ctrl+A keycode combo, then delete
            # On Android: use keyevent sequence
            import subprocess
            adb_serial = udid
            # Long press to trigger selection mode, then select all
            subprocess.run(
                ["adb", "-s", adb_serial, "shell", "input", "keyevent",
                 "KEYCODE_MOVE_HOME"],
                capture_output=True, timeout=5,
            )
            subprocess.run(
                ["adb", "-s", adb_serial, "shell", "input", "keyevent",
                 "--longpress", "KEYCODE_SHIFT_LEFT", "KEYCODE_MOVE_END"],
                capture_output=True, timeout=5,
            )
            subprocess.run(
                ["adb", "-s", adb_serial, "shell", "input", "keyevent",
                 "KEYCODE_DEL"],
                capture_output=True, timeout=5,
            )

        try:
            await asyncio.to_thread(_do)
        except Exception as e:
            raise DeviceError(f"Clear text failed: {e}", tool="u2") from e
