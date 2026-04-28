"""Tests for U2Backend — Android UI automation via uiautomator2."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from unittest.mock import MagicMock, patch

import pytest

from server.device.u2_client import (
    U2Backend,
    _flatten_tree,
    _map_class,
    _normalize_node,
    _parse_bounds,
)

# ---------------------------------------------------------------------------
# Unit tests: normalization helpers
# ---------------------------------------------------------------------------


class TestParseBounds:
    def test_valid_bounds(self):
        result = _parse_bounds("[0,100][200,300]")
        assert result == {"x": 0.0, "y": 100.0, "width": 200.0, "height": 200.0}

    def test_zero_size(self):
        result = _parse_bounds("[50,50][50,50]")
        assert result == {"x": 50.0, "y": 50.0, "width": 0.0, "height": 0.0}

    def test_invalid_format(self):
        assert _parse_bounds("invalid") is None
        assert _parse_bounds("") is None

    def test_large_coordinates(self):
        result = _parse_bounds("[0,0][1440,2960]")
        assert result == {"x": 0.0, "y": 0.0, "width": 1440.0, "height": 2960.0}


class TestMapClass:
    def test_known_classes(self):
        assert _map_class("android.widget.Button") == "Button"
        assert _map_class("android.widget.ImageButton") == "Button"
        assert _map_class("android.widget.TextView") == "StaticText"
        assert _map_class("android.widget.EditText") == "TextField"
        assert _map_class("android.widget.ImageView") == "Image"
        assert _map_class("android.widget.CheckBox") == "CheckBox"
        assert _map_class("android.widget.Switch") == "Switch"
        assert _map_class("android.widget.SeekBar") == "Slider"
        assert _map_class("android.widget.ScrollView") == "ScrollView"
        assert _map_class("android.view.ViewGroup") == "Group"

    def test_material_classes(self):
        assert (
            _map_class("com.google.android.material.floatingactionbutton.FloatingActionButton")
            == "Button"
        )
        assert _map_class("com.google.android.material.tabs.TabLayout") == "TabBar"

    def test_recyclerview(self):
        assert _map_class("androidx.recyclerview.widget.RecyclerView") == "ScrollView"

    def test_unknown_class_strips_package(self):
        assert _map_class("com.example.app.CustomWidget") == "CustomWidget"

    def test_simple_class_name(self):
        assert _map_class("View") == "View"


class TestNormalizeNode:
    def _make_node(self, **attrs) -> ET.Element:
        return ET.Element("node", attrib=attrs)

    def test_button_with_text(self):
        node = self._make_node(
            **{
                "class": "android.widget.Button",
                "text": "Submit",
                "resource-id": "com.app:id/btn_submit",
                "content-desc": "",
                "bounds": "[100,200][300,250]",
                "enabled": "true",
                "checkable": "false",
                "checked": "false",
            }
        )
        result = _normalize_node(node)
        assert result["type"] == "Button"
        assert result["AXLabel"] == "Submit"
        assert result["AXUniqueId"] == "btn_submit"
        assert result["frame"] == {"x": 100.0, "y": 200.0, "width": 200.0, "height": 50.0}
        assert result["enabled"] is True
        assert result["AXValue"] is None  # Not editable

    def test_edittext_value_is_text(self):
        node = self._make_node(
            **{
                "class": "android.widget.EditText",
                "text": "hello world",
                "resource-id": "com.app:id/input",
                "content-desc": "Email input",
                "bounds": "[0,0][500,50]",
                "enabled": "true",
                "checkable": "false",
                "checked": "false",
            }
        )
        result = _normalize_node(node)
        assert result["type"] == "TextField"
        assert result["AXLabel"] == "hello world"
        assert result["AXValue"] == "hello world"  # Editable: value = text

    def test_checkbox_value(self):
        node = self._make_node(
            **{
                "class": "android.widget.CheckBox",
                "text": "Remember me",
                "resource-id": "",
                "content-desc": "",
                "bounds": "[0,0][100,100]",
                "enabled": "true",
                "checkable": "true",
                "checked": "true",
            }
        )
        result = _normalize_node(node)
        assert result["type"] == "CheckBox"
        assert result["AXValue"] == "1"

    def test_unchecked_checkbox(self):
        node = self._make_node(
            **{
                "class": "android.widget.CheckBox",
                "text": "Agree",
                "resource-id": "",
                "content-desc": "",
                "bounds": "[0,0][100,100]",
                "enabled": "true",
                "checkable": "true",
                "checked": "false",
            }
        )
        assert _normalize_node(node)["AXValue"] == "0"

    def test_selected_tab_value_is_one(self):
        """BottomNavigationView / TabLayout tabs use `selected` not `checked`.
        The selected tab should normalize to AXValue "1" so it can match a
        landmark with `selected: true` (parity with iOS RadioButton tabs)."""
        # The actual Maps Explore tab pattern observed on a Pixel_8_API35:
        # FrameLayout, not checkable, selected=true.
        node = self._make_node(
            **{
                "class": "android.widget.FrameLayout",
                "text": "",
                "resource-id": "",
                "content-desc": "Explore",
                "bounds": "[0,2000][270,2080]",
                "enabled": "true",
                "checkable": "false",
                "checked": "false",
                "selected": "true",
            }
        )
        assert _normalize_node(node)["AXValue"] == "1"

    def test_unselected_tab_value_is_none(self):
        """Non-selected tabs (and most other on-screen elements) should NOT
        emit AXValue "0" — almost every element on screen has selected=false
        by default, and emitting "0" would flood AXValue with meaningless
        noise. selected=false → AXValue None is the right asymmetry."""
        node = self._make_node(
            **{
                "class": "android.widget.FrameLayout",
                "text": "",
                "resource-id": "",
                "content-desc": "Go",
                "bounds": "[270,2000][540,2080]",
                "enabled": "true",
                "checkable": "false",
                "checked": "false",
                "selected": "false",
            }
        )
        assert _normalize_node(node)["AXValue"] is None

    def test_checked_takes_precedence_over_selected(self):
        """If a node is both checkable and selected (rare — e.g. a CheckBox
        in a selected list row), the checkable branch wins. The widget's
        compound-button state is the more specific signal."""
        node = self._make_node(
            **{
                "class": "android.widget.CheckBox",
                "text": "Item",
                "resource-id": "",
                "content-desc": "",
                "bounds": "[0,0][100,100]",
                "enabled": "true",
                "checkable": "true",
                "checked": "false",   # not checked
                "selected": "true",   # but parent row is selected
            }
        )
        # checked=false wins over selected=true → AXValue "0"
        assert _normalize_node(node)["AXValue"] == "0"

    def test_content_desc_fallback(self):
        node = self._make_node(
            **{
                "class": "android.widget.ImageButton",
                "text": "",
                "resource-id": "com.app:id/nav_back",
                "content-desc": "Navigate back",
                "bounds": "[0,0][48,48]",
                "enabled": "true",
                "checkable": "false",
                "checked": "false",
            }
        )
        result = _normalize_node(node)
        assert result["AXLabel"] == "Navigate back"
        assert result["type"] == "Button"

    def test_disabled_element(self):
        node = self._make_node(
            **{
                "class": "android.widget.Button",
                "text": "Next",
                "resource-id": "",
                "content-desc": "",
                "bounds": "[0,0][100,50]",
                "enabled": "false",
                "checkable": "false",
                "checked": "false",
            }
        )
        assert _normalize_node(node)["enabled"] is False

    def test_no_resource_id(self):
        node = self._make_node(
            **{
                "class": "android.widget.TextView",
                "text": "Hello",
                "resource-id": "",
                "content-desc": "",
                "bounds": "[0,0][100,50]",
                "enabled": "true",
                "checkable": "false",
                "checked": "false",
            }
        )
        assert _normalize_node(node)["AXUniqueId"] is None

    def test_resource_id_without_package(self):
        node = self._make_node(
            **{
                "class": "android.widget.Button",
                "text": "OK",
                "resource-id": "btn_ok",
                "content-desc": "",
                "bounds": "[0,0][100,50]",
                "enabled": "true",
                "checkable": "false",
                "checked": "false",
            }
        )
        assert _normalize_node(node)["AXUniqueId"] == "btn_ok"


class TestFlattenTree:
    def test_simple_hierarchy(self):
        xml = """<hierarchy rotation="0">
            <node class="android.widget.FrameLayout" bounds="[0,0][1080,1920]"
                  text="" resource-id="" content-desc="" enabled="true"
                  checkable="false" checked="false">
                <node class="android.widget.Button" bounds="[100,200][300,250]"
                      text="Click me" resource-id="com.app:id/btn"
                      content-desc="" enabled="true"
                      checkable="false" checked="false"/>
                <node class="android.widget.TextView" bounds="[100,300][500,350]"
                      text="Hello World" resource-id=""
                      content-desc="" enabled="true"
                      checkable="false" checked="false"/>
            </node>
        </hierarchy>"""
        root = ET.fromstring(xml)
        elements = _flatten_tree(root)
        # hierarchy root has no class/bounds, its children get flattened
        types = [e["type"] for e in elements]
        assert "Group" in types  # FrameLayout
        assert "Button" in types
        assert "StaticText" in types

    def test_skips_zero_size_nodes(self):
        xml = """<hierarchy rotation="0">
            <node class="android.view.View" bounds="[0,0][0,0]"
                  text="" resource-id="" content-desc="" enabled="true"
                  checkable="false" checked="false"/>
        </hierarchy>"""
        root = ET.fromstring(xml)
        elements = _flatten_tree(root)
        # The zero-size node should be skipped
        view_elements = [e for e in elements if e["type"] == "Other"]
        assert len(view_elements) == 0


# ---------------------------------------------------------------------------
# Integration: U2Backend methods with mocked u2 device
# ---------------------------------------------------------------------------

SAMPLE_HIERARCHY = """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy rotation="0">
    <node class="android.widget.FrameLayout" bounds="[0,0][1080,1920]"
          text="" resource-id="" content-desc="" enabled="true"
          checkable="false" checked="false">
        <node class="android.widget.Button" bounds="[100,200][300,280]"
              text="Login" resource-id="com.app:id/btn_login"
              content-desc="" enabled="true"
              checkable="false" checked="false"/>
        <node class="android.widget.EditText" bounds="[100,100][500,150]"
              text="user@example.com" resource-id="com.app:id/email_input"
              content-desc="Email" enabled="true"
              checkable="false" checked="false"/>
    </node>
</hierarchy>"""


@pytest.fixture
def mock_u2_device():
    """Create a mock uiautomator2 device."""
    device = MagicMock()
    device.dump_hierarchy.return_value = SAMPLE_HIERARCHY
    return device


@pytest.fixture
def u2_backend(mock_u2_device):
    """Create a U2Backend with a pre-cached mock device."""
    backend = U2Backend()
    backend._devices["emulator-5554"] = mock_u2_device
    return backend


class TestDescribeAll:
    @pytest.mark.asyncio
    async def test_returns_flat_elements(self, u2_backend, mock_u2_device):
        elements = await u2_backend.describe_all("emulator-5554")
        mock_u2_device.dump_hierarchy.assert_called_once()
        # Should have FrameLayout, Button, EditText
        types = [e["type"] for e in elements]
        assert "Group" in types
        assert "Button" in types
        assert "TextField" in types

    @pytest.mark.asyncio
    async def test_button_element_fields(self, u2_backend):
        elements = await u2_backend.describe_all("emulator-5554")
        button = next(e for e in elements if e["type"] == "Button")
        assert button["AXLabel"] == "Login"
        assert button["AXUniqueId"] == "btn_login"
        assert button["frame"] == {"x": 100.0, "y": 200.0, "width": 200.0, "height": 80.0}
        assert button["enabled"] is True

    @pytest.mark.asyncio
    async def test_edittext_has_value(self, u2_backend):
        elements = await u2_backend.describe_all("emulator-5554")
        text_field = next(e for e in elements if e["type"] == "TextField")
        assert text_field["AXValue"] == "user@example.com"
        assert text_field["AXUniqueId"] == "email_input"

    @pytest.mark.asyncio
    async def test_passes_snapshot_depth(self, u2_backend, mock_u2_device):
        await u2_backend.describe_all("emulator-5554", snapshot_depth=3)
        mock_u2_device.dump_hierarchy.assert_called_once_with(max_depth=3)


class TestDescribeAllNested:
    @pytest.mark.asyncio
    async def test_preserves_children(self, u2_backend):
        elements = await u2_backend.describe_all_nested("emulator-5554")
        # Root should be the FrameLayout with children
        assert len(elements) == 1
        root = elements[0]
        assert root["type"] == "Group"
        assert "children" in root
        assert len(root["children"]) == 2


class TestDescribePoint:
    @pytest.mark.asyncio
    async def test_finds_button_at_center(self, u2_backend):
        # Button is at [100,200][300,280], center is (200, 240)
        result = await u2_backend.describe_point("emulator-5554", 200, 240)
        assert result is not None
        assert result["type"] == "Button"

    @pytest.mark.asyncio
    async def test_finds_deepest_element(self, u2_backend):
        # Point (200, 240) is inside both FrameLayout and Button;
        # should return Button (smaller area = deeper)
        result = await u2_backend.describe_point("emulator-5554", 200, 240)
        assert result["type"] == "Button"

    @pytest.mark.asyncio
    async def test_returns_none_outside_all(self, u2_backend):
        result = await u2_backend.describe_point("emulator-5554", 9999, 9999)
        assert result is None


class TestTap:
    @pytest.mark.asyncio
    async def test_delegates_to_device_click(self, u2_backend, mock_u2_device):
        await u2_backend.tap("emulator-5554", 200.5, 240.7)
        mock_u2_device.click.assert_called_once_with(200, 240)


class TestSwipe:
    @pytest.mark.asyncio
    async def test_delegates_to_device_swipe(self, u2_backend, mock_u2_device):
        await u2_backend.swipe("emulator-5554", 100, 500, 100, 200, 0.3)
        mock_u2_device.swipe.assert_called_once_with(100, 500, 100, 200, duration=0.3)


class TestTypeText:
    @pytest.mark.asyncio
    async def test_delegates_to_send_keys(self, u2_backend, mock_u2_device):
        await u2_backend.type_text("emulator-5554", "hello world")
        mock_u2_device.send_keys.assert_called_once_with("hello world")


class TestPressButton:
    @pytest.mark.asyncio
    async def test_home_button(self, u2_backend, mock_u2_device):
        await u2_backend.press_button("emulator-5554", "home")
        mock_u2_device.press.assert_called_once_with("home")

    @pytest.mark.asyncio
    async def test_back_button(self, u2_backend, mock_u2_device):
        await u2_backend.press_button("emulator-5554", "back")
        mock_u2_device.press.assert_called_once_with("back")

    @pytest.mark.asyncio
    async def test_recents_button(self, u2_backend, mock_u2_device):
        await u2_backend.press_button("emulator-5554", "recents")
        mock_u2_device.press.assert_called_once_with("recent")

    @pytest.mark.asyncio
    async def test_unknown_button_raises(self, u2_backend):
        from server.models import DeviceError

        with pytest.raises(DeviceError, match="Unknown button"):
            await u2_backend.press_button("emulator-5554", "SIRI")


class TestConnectionLifecycle:
    @pytest.mark.asyncio
    async def test_lazy_connect(self):
        backend = U2Backend()
        with patch("server.device.u2_client.U2Backend._connect") as mock_connect:
            mock_device = MagicMock()
            mock_device.dump_hierarchy.return_value = SAMPLE_HIERARCHY
            mock_connect.return_value = mock_device
            await backend.describe_all("emulator-5554")
            mock_connect.assert_called_with("emulator-5554")

    def test_caches_connection(self):
        backend = U2Backend()
        mock_device = MagicMock()
        backend._devices["emulator-5554"] = mock_device
        result = backend._connect("emulator-5554")
        assert result is mock_device

    def test_disconnect_removes_cache(self):
        backend = U2Backend()
        backend._devices["emulator-5554"] = MagicMock()
        backend._disconnect("emulator-5554")
        assert "emulator-5554" not in backend._devices


class TestParseElementsIntegration:
    """Verify that U2Backend output works with parse_elements()."""

    @pytest.mark.asyncio
    async def test_u2_output_parses_to_uielements(self, u2_backend):
        from server.device.ui_elements import parse_elements

        raw = await u2_backend.describe_all("emulator-5554")
        elements = parse_elements(raw)
        assert len(elements) > 0
        # Check that UIElement fields are populated
        button = next(e for e in elements if e.type == "Button")
        assert button.label == "Login"
        assert button.identifier == "btn_login"
        assert button.frame is not None
        assert button.frame["width"] == 200.0
