"""Tests for DeviceController — mock SimctlBackend methods."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from server.device.controller import DeviceController
from server.models import DeviceError, DeviceInfo, DeviceState, DeviceType, UIElement

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _device(
    udid: str = "AAAA-1111",
    name: str = "iPhone 16 Pro",
    state: DeviceState = DeviceState.BOOTED,
) -> DeviceInfo:
    return DeviceInfo(
        udid=udid,
        name=name,
        state=state,
        device_type=DeviceType.SIMULATOR,
        os_version="iOS 18.6",
        runtime="com.apple.CoreSimulator.SimRuntime.iOS-18-6",
    )


# ---------------------------------------------------------------------------
# resolve_udid
# ---------------------------------------------------------------------------


class TestResolveUdid:
    async def test_explicit_udid(self):
        """Case 1: Explicit udid is used and becomes active."""
        ctrl = DeviceController()
        result = await ctrl.resolve_udid("explicit-udid")
        assert result == "explicit-udid"
        assert ctrl._active_udid == "explicit-udid"

    async def test_active_udid(self):
        """Case 2: Previously stored active udid is returned."""
        ctrl = DeviceController()
        ctrl._active_udid = "stored-udid"
        result = await ctrl.resolve_udid()
        assert result == "stored-udid"

    async def test_single_booted_auto_detect(self):
        """Case 3: Exactly 1 booted device is auto-detected."""
        ctrl = DeviceController()
        ctrl.simctl.list_devices = AsyncMock(
            return_value=[
                _device(udid="auto-udid", state=DeviceState.BOOTED),
                _device(udid="other-udid", state=DeviceState.SHUTDOWN),
            ]
        )
        ctrl.devicectl.list_devices = AsyncMock(return_value=[])
        ctrl.usbmux.list_devices = AsyncMock(return_value=[])
        ctrl.adb.list_devices = AsyncMock(return_value=[])
        result = await ctrl.resolve_udid()
        assert result == "auto-udid"
        assert ctrl._active_udid == "auto-udid"

    async def test_no_booted_error(self):
        """Case 4: No booted devices raises error."""
        ctrl = DeviceController()
        ctrl.simctl.list_devices = AsyncMock(
            return_value=[
                _device(udid="off1", state=DeviceState.SHUTDOWN),
            ]
        )
        ctrl.devicectl.list_devices = AsyncMock(return_value=[])
        ctrl.usbmux.list_devices = AsyncMock(return_value=[])
        ctrl.adb.list_devices = AsyncMock(return_value=[])
        with pytest.raises(DeviceError, match="No booted device"):
            await ctrl.resolve_udid()

    async def test_multiple_booted_error(self):
        """Case 5: Multiple booted devices raises error."""
        ctrl = DeviceController()
        ctrl.simctl.list_devices = AsyncMock(
            return_value=[
                _device(udid="dev1", name="iPhone A", state=DeviceState.BOOTED),
                _device(udid="dev2", name="iPhone B", state=DeviceState.BOOTED),
            ]
        )
        ctrl.devicectl.list_devices = AsyncMock(return_value=[])
        ctrl.usbmux.list_devices = AsyncMock(return_value=[])
        ctrl.adb.list_devices = AsyncMock(return_value=[])
        with pytest.raises(DeviceError, match="Multiple devices booted"):
            await ctrl.resolve_udid()


# ---------------------------------------------------------------------------
# check_tools
# ---------------------------------------------------------------------------


class TestCheckTools:
    async def test_all_available(self):
        ctrl = DeviceController()
        ctrl.simctl.is_available = AsyncMock(return_value=True)
        ctrl.idb.is_available = AsyncMock(return_value=True)
        ctrl.devicectl.is_available = AsyncMock(return_value=True)
        ctrl.pmd3.is_available = AsyncMock(return_value=True)
        ctrl.adb.is_available = AsyncMock(return_value=True)
        ctrl.sim_bridge_manager.is_available = AsyncMock(return_value=True)
        with patch("server.device.tunneld.is_tunneld_running", return_value=True):
            tools = await ctrl.check_tools()
        assert tools == {
            "simctl": True,
            "idb": True,
            "devicectl": True,
            "pymobiledevice3": True,
            "tunneld": True,
            "adb": True,
            "sim_bridge": True,
        }

    async def test_simctl_only(self):
        ctrl = DeviceController()
        ctrl.simctl.is_available = AsyncMock(return_value=True)
        ctrl.idb.is_available = AsyncMock(return_value=False)
        ctrl.devicectl.is_available = AsyncMock(return_value=False)
        ctrl.pmd3.is_available = AsyncMock(return_value=False)
        ctrl.adb.is_available = AsyncMock(return_value=False)
        ctrl.sim_bridge_manager.is_available = AsyncMock(return_value=False)
        with patch("server.device.tunneld.is_tunneld_running", return_value=False):
            tools = await ctrl.check_tools()
        assert tools == {
            "simctl": True,
            "idb": False,
            "devicectl": False,
            "pymobiledevice3": False,
            "tunneld": False,
            "adb": False,
            "sim_bridge": False,
        }

    async def test_none_available(self):
        ctrl = DeviceController()
        ctrl.simctl.is_available = AsyncMock(return_value=False)
        ctrl.idb.is_available = AsyncMock(return_value=False)
        ctrl.devicectl.is_available = AsyncMock(return_value=False)
        ctrl.pmd3.is_available = AsyncMock(return_value=False)
        ctrl.adb.is_available = AsyncMock(return_value=False)
        ctrl.sim_bridge_manager.is_available = AsyncMock(return_value=False)
        with patch("server.device.tunneld.is_tunneld_running", return_value=False):
            tools = await ctrl.check_tools()
        assert tools == {
            "simctl": False,
            "idb": False,
            "devicectl": False,
            "pymobiledevice3": False,
            "tunneld": False,
            "adb": False,
            "sim_bridge": False,
        }


# ---------------------------------------------------------------------------
# boot
# ---------------------------------------------------------------------------


class TestBoot:
    async def test_boot_by_udid(self):
        ctrl = DeviceController()
        ctrl.simctl.boot = AsyncMock()
        udid = await ctrl.boot(udid="AAAA-1111")
        ctrl.simctl.boot.assert_called_once_with("AAAA-1111")
        assert udid == "AAAA-1111"
        assert ctrl._active_udid == "AAAA-1111"

    async def test_boot_by_name(self):
        ctrl = DeviceController()
        ctrl.adb.is_available = AsyncMock(return_value=False)
        ctrl.simctl.list_devices = AsyncMock(
            return_value=[
                _device(udid="found-udid", name="iPhone 16 Pro", state=DeviceState.SHUTDOWN),
            ]
        )
        ctrl.simctl.boot = AsyncMock()
        udid = await ctrl.boot(name="iPhone 16 Pro")
        ctrl.simctl.boot.assert_called_once_with("found-udid")
        assert udid == "found-udid"

    async def test_boot_by_name_not_found(self):
        ctrl = DeviceController()
        ctrl.simctl.list_devices = AsyncMock(return_value=[])
        ctrl.adb.is_available = AsyncMock(return_value=False)
        with pytest.raises(DeviceError, match="No simulator or AVD found with name"):
            await ctrl.boot(name="Nonexistent")

    async def test_boot_no_args(self):
        ctrl = DeviceController()
        with pytest.raises(DeviceError, match="Either udid or name is required"):
            await ctrl.boot()


# ---------------------------------------------------------------------------
# shutdown
# ---------------------------------------------------------------------------


class TestShutdown:
    async def test_shutdown_clears_active(self):
        ctrl = DeviceController()
        ctrl._active_udid = "AAAA-1111"
        ctrl.simctl.shutdown = AsyncMock()
        await ctrl.shutdown("AAAA-1111")
        ctrl.simctl.shutdown.assert_called_once_with("AAAA-1111")
        assert ctrl._active_udid is None

    async def test_shutdown_different_device_keeps_active(self):
        ctrl = DeviceController()
        ctrl._active_udid = "AAAA-1111"
        ctrl.simctl.shutdown = AsyncMock()
        await ctrl.shutdown("BBBB-2222")
        assert ctrl._active_udid == "AAAA-1111"


# ---------------------------------------------------------------------------
# App management delegates
# ---------------------------------------------------------------------------


class TestAppDelegation:
    async def test_install_app(self):
        ctrl = DeviceController()
        ctrl._active_udid = "AAAA-1111"
        ctrl.simctl.install_app = AsyncMock()
        udid = await ctrl.install_app("/path/to/App.app")
        ctrl.simctl.install_app.assert_called_once_with("AAAA-1111", "/path/to/App.app")
        assert udid == "AAAA-1111"

    async def test_launch_app(self):
        ctrl = DeviceController()
        ctrl._active_udid = "AAAA-1111"
        ctrl.simctl.launch_app = AsyncMock()
        udid = await ctrl.launch_app("com.example.App")
        ctrl.simctl.launch_app.assert_called_once_with("AAAA-1111", "com.example.App", env=None)
        assert udid == "AAAA-1111"

    async def test_terminate_app(self):
        ctrl = DeviceController()
        ctrl._active_udid = "AAAA-1111"
        ctrl.simctl.terminate_app = AsyncMock()
        udid = await ctrl.terminate_app("com.example.App")
        ctrl.simctl.terminate_app.assert_called_once_with("AAAA-1111", "com.example.App")
        assert udid == "AAAA-1111"

    async def test_uninstall_app(self):
        ctrl = DeviceController()
        ctrl._active_udid = "AAAA-1111"
        ctrl.simctl.uninstall_app = AsyncMock()
        udid = await ctrl.uninstall_app("com.example.App")
        ctrl.simctl.uninstall_app.assert_called_once_with("AAAA-1111", "com.example.App")
        assert udid == "AAAA-1111"

    async def test_list_apps(self):
        ctrl = DeviceController()
        ctrl._active_udid = "AAAA-1111"
        ctrl.simctl.list_apps = AsyncMock(return_value=[])
        apps, udid = await ctrl.list_apps()
        ctrl.simctl.list_apps.assert_called_once_with("AAAA-1111")
        assert udid == "AAAA-1111"


# ---------------------------------------------------------------------------
# Physical device app lifecycle (always uses WDA)
# ---------------------------------------------------------------------------


class TestPhysicalAppLifecycle:
    async def test_launch_app_physical_uses_wda(self):
        ctrl = DeviceController()
        ctrl._active_udid = "PHYS-0001"
        ctrl._device_type_cache["PHYS-0001"] = DeviceType.DEVICE
        ctrl.wda_client.activate_app = AsyncMock()
        ctrl.devicectl.launch_app = AsyncMock()

        udid = await ctrl.launch_app("com.example.App")
        ctrl.wda_client.activate_app.assert_called_once_with("PHYS-0001", "com.example.App")
        ctrl.devicectl.launch_app.assert_not_called()
        assert udid == "PHYS-0001"

    async def test_terminate_app_physical_uses_wda(self):
        ctrl = DeviceController()
        ctrl._active_udid = "PHYS-0001"
        ctrl._device_type_cache["PHYS-0001"] = DeviceType.DEVICE
        ctrl.wda_client.terminate_app = AsyncMock()
        ctrl.devicectl.terminate_app = AsyncMock()

        udid = await ctrl.terminate_app("com.example.App")
        ctrl.wda_client.terminate_app.assert_called_once_with("PHYS-0001", "com.example.App")
        ctrl.devicectl.terminate_app.assert_not_called()
        assert udid == "PHYS-0001"


# ---------------------------------------------------------------------------
# screenshot
# ---------------------------------------------------------------------------


class TestScreenshot:
    async def test_screenshot_delegates(self):
        ctrl = DeviceController()
        ctrl._active_udid = "AAAA-1111"
        fake_png = b"\x89PNGfake"
        ctrl.simctl.screenshot = AsyncMock(return_value=fake_png)

        with patch("server.device.controller.process_screenshot") as mock_proc:
            mock_proc.return_value = (b"processed", "image/png")
            result_bytes, media_type = await ctrl.screenshot(format="png", scale=0.5)

        ctrl.simctl.screenshot.assert_called_once_with("AAAA-1111")
        mock_proc.assert_called_once_with(fake_png, format="png", scale=0.5, quality=85)
        assert result_bytes == b"processed"
        assert media_type == "image/png"


# ---------------------------------------------------------------------------
# UI inspection (Phase 3b)
# ---------------------------------------------------------------------------

_FAKE_IDB_OUTPUT = [
    {
        "type": "Application",
        "AXLabel": "TestApp",
        "AXUniqueId": None,
        "frame": {"x": 0, "y": 0, "width": 393, "height": 852},
        "enabled": True,
        "role": "AXApplication",
        "role_description": "application",
    },
    {
        "type": "Button",
        "AXLabel": "Settings",
        "AXUniqueId": "Settings",
        "frame": {"x": 302, "y": 476, "width": 68, "height": 86},
        "enabled": True,
        "role": "AXButton",
        "role_description": "button",
    },
    {
        "type": "Button",
        "AXLabel": "Calendar",
        "AXUniqueId": "Calendar-1",
        "frame": {"x": 119, "y": 382, "width": 68, "height": 86},
        "enabled": True,
        "role": "AXButton",
        "role_description": "button",
    },
    {
        "type": "Button",
        "AXLabel": "Calendar",
        "AXUniqueId": "Calendar-2",
        "frame": {"x": 210, "y": 500, "width": 68, "height": 86},
        "enabled": True,
        "role": "AXButton",
        "role_description": "button",
    },
]


class TestGetUIElements:
    async def test_returns_parsed_elements(self):
        ctrl = DeviceController()
        ctrl._active_udid = "AAAA-1111"
        ctrl.idb.describe_all = AsyncMock(return_value=_FAKE_IDB_OUTPUT)

        elements, udid = await ctrl.get_ui_elements()
        assert udid == "AAAA-1111"
        assert len(elements) == 4
        assert elements[0].type == "Application"
        assert elements[1].label == "Settings"
        ctrl.idb.describe_all.assert_called_once_with(
            "AAAA-1111", snapshot_depth=None, source_timeout=None
        )

    async def test_with_explicit_udid(self):
        ctrl = DeviceController()
        ctrl.idb.describe_all = AsyncMock(return_value=[])

        elements, udid = await ctrl.get_ui_elements(udid="BBBB-2222")
        assert udid == "BBBB-2222"
        ctrl.idb.describe_all.assert_called_once_with(
            "BBBB-2222", snapshot_depth=None, source_timeout=None
        )


class TestGetScreenSummary:
    async def test_returns_summary(self):
        ctrl = DeviceController()
        ctrl._active_udid = "AAAA-1111"
        ctrl.idb.describe_all = AsyncMock(return_value=_FAKE_IDB_OUTPUT)

        summary, _elements, udid = await ctrl.get_screen_summary()
        assert udid == "AAAA-1111"
        assert "summary" in summary
        assert summary["element_count"] == 4


class TestTap:
    async def test_tap_delegates_to_idb(self):
        ctrl = DeviceController()
        ctrl._active_udid = "AAAA-1111"
        ctrl.idb.tap = AsyncMock()

        udid = await ctrl.tap(100.5, 200.3)
        assert udid == "AAAA-1111"
        ctrl.idb.tap.assert_called_once_with("AAAA-1111", 100.5, 200.3)


class TestTapElement:
    async def test_single_match_taps(self):
        ctrl = DeviceController()
        ctrl._active_udid = "AAAA-1111"
        ctrl.idb.describe_all = AsyncMock(return_value=_FAKE_IDB_OUTPUT)
        ctrl.idb.tap = AsyncMock()

        result = await ctrl.tap_element(label="Settings")
        assert result["status"] == "ok"
        assert result["tapped"]["label"] == "Settings"
        assert result["tapped"]["x"] == 336.0  # 302 + 68/2
        assert result["tapped"]["y"] == 519.0  # 476 + 86/2
        ctrl.idb.tap.assert_called_once_with("AAAA-1111", 336.0, 519.0)

    async def test_multiple_matches_ambiguous(self):
        ctrl = DeviceController()
        ctrl._active_udid = "AAAA-1111"
        ctrl.idb.describe_all = AsyncMock(return_value=_FAKE_IDB_OUTPUT)

        result = await ctrl.tap_element(label="Calendar")
        assert result["status"] == "ambiguous"
        assert len(result["matches"]) == 2
        assert "Calendar-1" in [m["identifier"] for m in result["matches"]]
        assert "Calendar-2" in [m["identifier"] for m in result["matches"]]

    async def test_no_match_returns_not_found(self):
        ctrl = DeviceController()
        ctrl._active_udid = "AAAA-1111"
        ctrl.idb.describe_all = AsyncMock(return_value=_FAKE_IDB_OUTPUT)
        # iOS taps now scroll-to-find on a miss; simulate the scroll also
        # failing to surface the element so we exercise the not_found path.
        ctrl._ios_scroll_to_element = AsyncMock(return_value=None)

        result = await ctrl.tap_element(label="Nonexistent")
        assert result["status"] == "not_found"
        assert "No element found" in result["detail"]
        ctrl._ios_scroll_to_element.assert_awaited_once()

    async def test_no_label_or_identifier_raises(self):
        ctrl = DeviceController()
        with pytest.raises(
            DeviceError, match="Either label/label_contains/label_prefix or identifier is required"
        ):
            await ctrl.tap_element()

    async def test_by_identifier(self):
        ctrl = DeviceController()
        ctrl._active_udid = "AAAA-1111"
        ctrl.idb.describe_all = AsyncMock(return_value=_FAKE_IDB_OUTPUT)
        ctrl.idb.tap = AsyncMock()

        result = await ctrl.tap_element(identifier="Calendar-1")
        assert result["status"] == "ok"
        assert result["tapped"]["identifier"] == "Calendar-1"

    async def test_type_filter_narrows_results(self):
        ctrl = DeviceController()
        ctrl._active_udid = "AAAA-1111"
        # Add a non-button Calendar element
        data = _FAKE_IDB_OUTPUT + [
            {
                "type": "StaticText",
                "AXLabel": "Settings",
                "AXUniqueId": "SettingsLabel",
                "frame": {"x": 0, "y": 0, "width": 100, "height": 20},
                "enabled": True,
                "role": "AXStaticText",
                "role_description": "text",
            }
        ]
        ctrl.idb.describe_all = AsyncMock(return_value=data)
        ctrl.idb.tap = AsyncMock()

        # Without type filter, "Settings" matches both Button and StaticText → ambiguous
        result = await ctrl.tap_element(label="Settings")
        assert result["status"] == "ambiguous"

        # With type filter, narrows to just the Button
        result = await ctrl.tap_element(label="Settings", element_type="Button")
        assert result["status"] == "ok"
        assert result["tapped"]["type"] == "Button"


# ---------------------------------------------------------------------------
# swipe, type_text, press_button (Phase 3c — idb delegates)
# ---------------------------------------------------------------------------


class TestSwipe:
    async def test_swipe_delegates_to_idb(self):
        ctrl = DeviceController()
        ctrl._active_udid = "AAAA-1111"
        ctrl.idb.swipe = AsyncMock()

        udid = await ctrl.swipe(100, 200, 100, 600, duration=0.3)
        assert udid == "AAAA-1111"
        ctrl.idb.swipe.assert_called_once_with("AAAA-1111", 100, 200, 100, 600, 0.3)

    async def test_swipe_resolves_udid(self):
        ctrl = DeviceController()
        ctrl.idb.swipe = AsyncMock()
        udid = await ctrl.swipe(0, 0, 0, 100, udid="BBBB-2222")
        assert udid == "BBBB-2222"
        assert ctrl._active_udid == "BBBB-2222"


class TestTypeText:
    async def test_type_text_delegates_to_idb(self):
        ctrl = DeviceController()
        ctrl._active_udid = "AAAA-1111"
        ctrl.idb.type_text = AsyncMock()

        udid = await ctrl.type_text("hello world")
        assert udid == "AAAA-1111"
        ctrl.idb.type_text.assert_called_once_with("AAAA-1111", "hello world")


class TestPressButton:
    async def test_press_button_delegates_to_idb(self):
        ctrl = DeviceController()
        ctrl._active_udid = "AAAA-1111"
        ctrl.idb.press_button = AsyncMock()

        udid = await ctrl.press_button("HOME")
        assert udid == "AAAA-1111"
        ctrl.idb.press_button.assert_called_once_with("AAAA-1111", "HOME")


# ---------------------------------------------------------------------------
# set_location, grant_permission (Phase 3c — simctl delegates)
# ---------------------------------------------------------------------------


class TestSetLocation:
    async def test_set_location_delegates_to_simctl(self):
        ctrl = DeviceController()
        ctrl._active_udid = "AAAA-1111"
        ctrl.simctl.set_location = AsyncMock()

        udid = await ctrl.set_location(37.7749, -122.4194)
        assert udid == "AAAA-1111"
        ctrl.simctl.set_location.assert_called_once_with("AAAA-1111", 37.7749, -122.4194)


class TestClearAppData:
    async def test_clear_app_data_terminates_then_clears(self):
        ctrl = DeviceController()
        ctrl._active_udid = "AAAA-1111"
        ctrl.simctl.terminate_app = AsyncMock()
        ctrl.simctl.clear_app_data = AsyncMock()

        udid = await ctrl.clear_app_data("com.example.App")
        assert udid == "AAAA-1111"
        ctrl.simctl.terminate_app.assert_called_once_with("AAAA-1111", "com.example.App")
        ctrl.simctl.clear_app_data.assert_called_once_with("AAAA-1111", "com.example.App")

    async def test_clear_app_data_proceeds_if_not_running(self):
        ctrl = DeviceController()
        ctrl._active_udid = "AAAA-1111"
        ctrl.simctl.terminate_app = AsyncMock(
            side_effect=DeviceError("app not running", tool="simctl")
        )
        ctrl.simctl.clear_app_data = AsyncMock()

        udid = await ctrl.clear_app_data("com.example.App")
        assert udid == "AAAA-1111"
        ctrl.simctl.clear_app_data.assert_called_once_with("AAAA-1111", "com.example.App")


class TestGrantPermission:
    async def test_grant_permission_delegates_to_simctl(self):
        ctrl = DeviceController()
        ctrl._active_udid = "AAAA-1111"
        ctrl.simctl.grant_permission = AsyncMock()

        udid = await ctrl.grant_permission("com.example.App", "photos")
        assert udid == "AAAA-1111"
        ctrl.simctl.grant_permission.assert_called_once_with(
            "AAAA-1111",
            "com.example.App",
            "photos",
        )


# ---------------------------------------------------------------------------
# screenshot_annotated (Phase 3c)
# ---------------------------------------------------------------------------


class TestScreenshotAnnotated:
    async def test_screenshot_annotated_delegates(self):
        ctrl = DeviceController()
        ctrl._active_udid = "AAAA-1111"
        fake_png = b"\x89PNGfake"
        ctrl.simctl.screenshot = AsyncMock(return_value=fake_png)
        ctrl.idb.describe_all = AsyncMock(return_value=_FAKE_IDB_OUTPUT)

        with patch("server.device.controller_ui.annotate_screenshot") as mock_annotate:
            mock_annotate.return_value = (b"annotated-png", "image/png")
            result_bytes, media_type = await ctrl.screenshot_annotated(scale=0.5)

        ctrl.simctl.screenshot.assert_called_once_with("AAAA-1111")
        # annotate_screenshot should receive the raw png and parsed elements
        assert mock_annotate.call_count == 1
        call_args = mock_annotate.call_args
        assert call_args[0][0] == fake_png  # raw_png
        assert len(call_args[0][1]) == 4  # 4 elements from _FAKE_IDB_OUTPUT
        assert call_args[1]["scale"] == 0.5
        assert result_bytes == b"annotated-png"
        assert media_type == "image/png"


# ---------------------------------------------------------------------------
# WDA direct query (Phase 2 — direct WDA element queries)
# ---------------------------------------------------------------------------


class TestWdaDirectQuery:
    """Test _wda_direct_query() strategy mapping."""

    async def test_identifier_only_uses_accessibility_id(self):
        ctrl = DeviceController()
        ctrl._active_udid = "PHYS-0001"
        ctrl._device_type_cache["PHYS-0001"] = DeviceType.DEVICE
        ctrl.wda_client.find_elements_by_query = AsyncMock(return_value=[])

        await ctrl._wda_direct_query("PHYS-0001", identifier="myButton")
        # First tries accessibility id, then falls back to predicate string
        calls = ctrl.wda_client.find_elements_by_query.call_args_list
        assert calls[0] == call("PHYS-0001", "accessibility id", "myButton")
        assert calls[1] == call("PHYS-0001", "predicate string", "name == 'myButton'")

    async def test_label_only_uses_predicate(self):
        ctrl = DeviceController()
        ctrl._active_udid = "PHYS-0001"
        ctrl._device_type_cache["PHYS-0001"] = DeviceType.DEVICE
        ctrl.wda_client.find_elements_by_query = AsyncMock(return_value=[])

        await ctrl._wda_direct_query("PHYS-0001", label="Settings")
        call = ctrl.wda_client.find_elements_by_query.call_args
        assert call[0][1] == "predicate string"
        assert "label ==[c] 'Settings'" in call[0][2]

    async def test_identifier_with_type_uses_predicate(self):
        ctrl = DeviceController()
        ctrl._active_udid = "PHYS-0001"
        ctrl._device_type_cache["PHYS-0001"] = DeviceType.DEVICE
        ctrl.wda_client.find_elements_by_query = AsyncMock(return_value=[])

        await ctrl._wda_direct_query("PHYS-0001", identifier="myBtn", element_type="Button")
        call = ctrl.wda_client.find_elements_by_query.call_args
        assert call[0][1] == "predicate string"
        assert "name == 'myBtn'" in call[0][2]
        assert "type == 'XCUIElementTypeButton'" in call[0][2]

    async def test_label_with_single_quote_escaped(self):
        ctrl = DeviceController()
        ctrl._active_udid = "PHYS-0001"
        ctrl._device_type_cache["PHYS-0001"] = DeviceType.DEVICE
        ctrl.wda_client.find_elements_by_query = AsyncMock(return_value=[])

        await ctrl._wda_direct_query("PHYS-0001", label="O'Brien's")
        call = ctrl.wda_client.find_elements_by_query.call_args
        assert call[0][1] == "predicate string"
        assert "label ==[c] 'O\\'Brien\\'s'" in call[0][2]

    async def test_returns_parsed_elements(self):
        ctrl = DeviceController()
        ctrl._active_udid = "PHYS-0001"
        ctrl._device_type_cache["PHYS-0001"] = DeviceType.DEVICE
        ctrl.wda_client.find_elements_by_query = AsyncMock(
            return_value=[
                {
                    "type": "Button",
                    "AXLabel": "Done",
                    "AXUniqueId": "done_btn",
                    "frame": {"x": 10, "y": 20, "width": 80, "height": 40},
                    "enabled": True,
                },
            ]
        )

        elements, elapsed = await ctrl._wda_direct_query("PHYS-0001", identifier="done_btn")
        assert len(elements) == 1
        assert elements[0].type == "Button"
        assert elements[0].label == "Done"


class TestGetUIElementsWdaDispatch:
    """Test that get_ui_elements dispatches to WDA direct query for physical devices."""

    async def test_physical_with_filters_uses_direct_query(self):
        ctrl = DeviceController()
        ctrl._active_udid = "PHYS-0001"
        ctrl._device_type_cache["PHYS-0001"] = DeviceType.DEVICE
        ctrl.wda_client.find_elements_by_query = AsyncMock(
            return_value=[
                {
                    "type": "Button",
                    "AXLabel": "Done",
                    "AXUniqueId": "done_btn",
                    "frame": {"x": 10, "y": 20, "width": 80, "height": 40},
                    "enabled": True,
                },
            ]
        )
        ctrl.wda_client.describe_all = AsyncMock()

        elements, udid = await ctrl.get_ui_elements(
            "PHYS-0001",
            filter_identifier="done_btn",
        )
        assert len(elements) == 1
        assert elements[0].label == "Done"
        # Should NOT call describe_all
        ctrl.wda_client.describe_all.assert_not_called()
        # filter_identifier must map to the identifier locator (accessibility
        # id), not be misrouted into a label CONTAINS predicate.
        first_call = ctrl.wda_client.find_elements_by_query.call_args_list[0]
        assert first_call == call("PHYS-0001", "accessibility id", "done_btn")

    async def test_physical_filter_type_maps_to_type_predicate(self):
        ctrl = DeviceController()
        ctrl._active_udid = "PHYS-0001"
        ctrl._device_type_cache["PHYS-0001"] = DeviceType.DEVICE
        ctrl.wda_client.find_elements_by_query = AsyncMock(return_value=[])
        ctrl.wda_client.describe_all = AsyncMock()

        await ctrl.get_ui_elements("PHYS-0001", filter_type="Application")
        # filter_type must map to a type predicate, not a label BEGINSWITH.
        q = ctrl.wda_client.find_elements_by_query.call_args[0][2]
        assert "type == 'XCUIElementTypeApplication'" in q
        assert "BEGINSWITH" not in q

    async def test_physical_no_filters_uses_describe_all(self):
        ctrl = DeviceController()
        ctrl._active_udid = "PHYS-0001"
        ctrl._device_type_cache["PHYS-0001"] = DeviceType.DEVICE
        ctrl.wda_client.describe_all = AsyncMock(return_value=_FAKE_IDB_OUTPUT)
        ctrl.wda_client.find_elements_by_query = AsyncMock()

        elements, udid = await ctrl.get_ui_elements("PHYS-0001")
        assert len(elements) == 4
        # Should NOT call direct query
        ctrl.wda_client.find_elements_by_query.assert_not_called()

    async def test_simulator_with_filters_uses_describe_all(self):
        ctrl = DeviceController()
        ctrl._active_udid = "AAAA-1111"
        # Simulator (default) — no entry in _device_type_cache means SIMULATOR
        ctrl.idb.describe_all = AsyncMock(return_value=_FAKE_IDB_OUTPUT)
        ctrl.wda_client.find_elements_by_query = AsyncMock()

        elements, udid = await ctrl.get_ui_elements(
            "AAAA-1111",
            filter_label="Settings",
        )
        # Should use describe_all (idb), not WDA direct query
        ctrl.idb.describe_all.assert_called_once()
        ctrl.wda_client.find_elements_by_query.assert_not_called()

    async def test_physical_with_filters_and_valid_cache_uses_cache(self):
        ctrl = DeviceController()
        ctrl._active_udid = "PHYS-0001"
        ctrl._device_type_cache["PHYS-0001"] = DeviceType.DEVICE

        # Pre-populate cache
        import time

        from server.device.ui_elements import parse_elements

        cached_elements = parse_elements(_FAKE_IDB_OUTPUT)
        ctrl._ui_cache["PHYS-0001"] = (cached_elements, time.time())

        ctrl.wda_client.find_elements_by_query = AsyncMock()
        ctrl.wda_client.describe_all = AsyncMock()

        elements, udid = await ctrl.get_ui_elements(
            "PHYS-0001",
            filter_label="Settings",
        )
        assert len(elements) == 1
        assert elements[0].label == "Settings"
        # Should use cache, NOT direct query or describe_all
        ctrl.wda_client.find_elements_by_query.assert_not_called()
        ctrl.wda_client.describe_all.assert_not_called()


class TestGetScreenSummaryStrategy:
    """Test strategy parameter on get_screen_summary."""

    async def test_skeleton_strategy_physical_calls_build_skeleton(self):
        ctrl = DeviceController()
        ctrl._active_udid = "PHYS-0001"
        ctrl._device_type_cache["PHYS-0001"] = DeviceType.DEVICE
        ctrl.wda_client.build_screen_skeleton = AsyncMock(
            return_value=[
                {
                    "type": "TabBar",
                    "AXLabel": "Tab Bar",
                    "frame": {"x": 0, "y": 800, "width": 393, "height": 52},
                    "enabled": True,
                },
            ]
        )
        ctrl.wda_client.describe_all = AsyncMock()

        summary, _elements, udid = await ctrl.get_screen_summary(strategy="skeleton")
        assert udid == "PHYS-0001"
        ctrl.wda_client.build_screen_skeleton.assert_called_once_with("PHYS-0001")
        ctrl.wda_client.describe_all.assert_not_called()

    async def test_skeleton_strategy_simulator_falls_back(self):
        ctrl = DeviceController()
        ctrl._active_udid = "AAAA-1111"
        ctrl.idb.describe_all = AsyncMock(return_value=_FAKE_IDB_OUTPUT)
        ctrl.wda_client.build_screen_skeleton = AsyncMock()

        summary, _elements, udid = await ctrl.get_screen_summary(strategy="skeleton")
        assert udid == "AAAA-1111"
        # Simulator should NOT call build_screen_skeleton
        ctrl.wda_client.build_screen_skeleton.assert_not_called()
        ctrl.idb.describe_all.assert_called_once()

    async def test_no_strategy_default_behavior(self):
        ctrl = DeviceController()
        ctrl._active_udid = "AAAA-1111"
        ctrl.idb.describe_all = AsyncMock(return_value=_FAKE_IDB_OUTPUT)

        summary, _elements, udid = await ctrl.get_screen_summary()
        assert udid == "AAAA-1111"
        assert "summary" in summary
        ctrl.idb.describe_all.assert_called_once()


# ---------------------------------------------------------------------------
# UDID mapping (CoreDevice UUID -> libimobiledevice UDID)
# ---------------------------------------------------------------------------


class TestUdidMapping:
    async def test_list_devices_populates_mapping(self):
        """list_devices() should correlate devicectl and usbmux names."""
        ctrl = DeviceController()
        ctrl.simctl.list_devices = AsyncMock(return_value=[])
        ctrl.devicectl.list_devices = AsyncMock(
            return_value=[
                DeviceInfo(
                    udid="B34C4EE9-CORE-DEVICE-UUID",
                    name="iPhone 11",
                    state=DeviceState.BOOTED,
                    device_type=DeviceType.DEVICE,
                    os_version="iOS 18.4",
                ),
            ]
        )
        ctrl.usbmux.list_devices = AsyncMock(return_value=[])
        ctrl.usbmux.get_usb_udid_map = AsyncMock(
            return_value={
                "iPhone 11": "00008030-AABBCCDDEEFF",
            }
        )

        await ctrl.list_devices()

        assert ctrl._usbmux_udid_map["B34C4EE9-CORE-DEVICE-UUID"] == "00008030-AABBCCDDEEFF"

    async def test_get_libimobiledevice_udid_cached(self):
        """get_libimobiledevice_udid returns cached value without refreshing."""
        ctrl = DeviceController()
        ctrl._usbmux_udid_map["CORE-UUID"] = "00008030-CACHED"
        ctrl.simctl.list_devices = AsyncMock(return_value=[])
        ctrl.devicectl.list_devices = AsyncMock(return_value=[])
        ctrl.usbmux.list_devices = AsyncMock(return_value=[])
        ctrl.usbmux.get_usb_udid_map = AsyncMock(return_value={})

        result = await ctrl.get_libimobiledevice_udid("CORE-UUID")
        assert result == "00008030-CACHED"
        # Should not have called list_devices (no refresh needed)
        ctrl.simctl.list_devices.assert_not_called()

    async def test_get_libimobiledevice_udid_refreshes_on_miss(self):
        """get_libimobiledevice_udid refreshes device list on cache miss."""
        ctrl = DeviceController()
        ctrl.simctl.list_devices = AsyncMock(return_value=[])
        ctrl.devicectl.list_devices = AsyncMock(
            return_value=[
                DeviceInfo(
                    udid="NEW-CORE-UUID",
                    name="iPhone 15 Pro",
                    state=DeviceState.BOOTED,
                    device_type=DeviceType.DEVICE,
                    os_version="iOS 18.4",
                ),
            ]
        )
        ctrl.usbmux.list_devices = AsyncMock(return_value=[])
        ctrl.usbmux.get_usb_udid_map = AsyncMock(
            return_value={
                "iPhone 15 Pro": "00008030-NEWDEVICE",
            }
        )

        result = await ctrl.get_libimobiledevice_udid("NEW-CORE-UUID")
        assert result == "00008030-NEWDEVICE"

    async def test_get_libimobiledevice_udid_returns_none_for_network_only(self):
        """Network-only devices have no usbmux UDID."""
        ctrl = DeviceController()
        ctrl.simctl.list_devices = AsyncMock(return_value=[])
        ctrl.devicectl.list_devices = AsyncMock(
            return_value=[
                DeviceInfo(
                    udid="WIFI-ONLY-UUID",
                    name="iPhone via Wi-Fi",
                    state=DeviceState.BOOTED,
                    device_type=DeviceType.DEVICE,
                    os_version="iOS 18.4",
                ),
            ]
        )
        ctrl.usbmux.list_devices = AsyncMock(return_value=[])
        ctrl.usbmux.get_usb_udid_map = AsyncMock(return_value={})

        result = await ctrl.get_libimobiledevice_udid("WIFI-ONLY-UUID")
        assert result is None

    async def test_get_libimobiledevice_udid_pre_ios17_passthrough(self):
        """Pre-iOS 17 devices already use libimobiledevice UDIDs — return as-is."""
        ctrl = DeviceController()
        usbmux_udid = "4999b9b773908e7326d0405bedb5f57e277402f8"
        # Simulate usbmux-discovered device (already in device type cache)
        ctrl._device_type_cache[usbmux_udid] = DeviceType.DEVICE
        ctrl.simctl.list_devices = AsyncMock(return_value=[])
        ctrl.devicectl.list_devices = AsyncMock(return_value=[])
        ctrl.usbmux.list_devices = AsyncMock(return_value=[])
        ctrl.usbmux.get_usb_udid_map = AsyncMock(return_value={})

        result = await ctrl.get_libimobiledevice_udid(usbmux_udid)
        assert result == usbmux_udid
        # Should not need to refresh
        ctrl.simctl.list_devices.assert_not_called()


# ---------------------------------------------------------------------------
# Android device support
# ---------------------------------------------------------------------------


def _android_device(
    udid: str = "emulator-5554",
    name: str = "Pixel_7_API_34",
    state: DeviceState = DeviceState.BOOTED,
    device_type: DeviceType = DeviceType.ANDROID_EMULATOR,
) -> DeviceInfo:
    return DeviceInfo(
        udid=udid,
        name=name,
        state=state,
        device_type=device_type,
        os_version="14",
        runtime="API 34",
        device_family="Android",
    )


class TestIsAndroid:
    def test_android_emulator(self):
        ctrl = DeviceController()
        ctrl._device_type_cache["emulator-5554"] = DeviceType.ANDROID_EMULATOR
        assert ctrl._is_android("emulator-5554") is True

    def test_android_device(self):
        ctrl = DeviceController()
        ctrl._device_type_cache["R5CR10XXXXX"] = DeviceType.ANDROID_DEVICE
        assert ctrl._is_android("R5CR10XXXXX") is True

    def test_simulator_is_not_android(self):
        ctrl = DeviceController()
        ctrl._device_type_cache["AAAA-1111"] = DeviceType.SIMULATOR
        assert ctrl._is_android("AAAA-1111") is False

    def test_physical_ios_is_not_android(self):
        ctrl = DeviceController()
        ctrl._device_type_cache["PHYS-0001"] = DeviceType.DEVICE
        assert ctrl._is_android("PHYS-0001") is False


class TestAndroidAppLifecycle:
    async def test_launch_android_uses_adb(self):
        ctrl = DeviceController()
        ctrl._active_udid = "emulator-5554"
        ctrl._device_type_cache["emulator-5554"] = DeviceType.ANDROID_EMULATOR
        ctrl.adb.launch_app = AsyncMock()
        ctrl.simctl.launch_app = AsyncMock()

        udid = await ctrl.launch_app("com.example.app")
        ctrl.adb.launch_app.assert_called_once_with("emulator-5554", "com.example.app")
        ctrl.simctl.launch_app.assert_not_called()
        assert udid == "emulator-5554"

    async def test_terminate_android_uses_adb(self):
        ctrl = DeviceController()
        ctrl._active_udid = "emulator-5554"
        ctrl._device_type_cache["emulator-5554"] = DeviceType.ANDROID_EMULATOR
        ctrl.adb.terminate_app = AsyncMock()
        ctrl.simctl.terminate_app = AsyncMock()

        await ctrl.terminate_app("com.example.app")
        ctrl.adb.terminate_app.assert_called_once_with("emulator-5554", "com.example.app")
        ctrl.simctl.terminate_app.assert_not_called()

    async def test_install_android_uses_adb(self):
        ctrl = DeviceController()
        ctrl._active_udid = "emulator-5554"
        ctrl._device_type_cache["emulator-5554"] = DeviceType.ANDROID_EMULATOR
        ctrl.adb.install_app = AsyncMock()
        ctrl.simctl.install_app = AsyncMock()

        await ctrl.install_app("/path/to/app.apk")
        ctrl.adb.install_app.assert_called_once_with("emulator-5554", "/path/to/app.apk")
        ctrl.simctl.install_app.assert_not_called()

    async def test_uninstall_android_uses_adb(self):
        ctrl = DeviceController()
        ctrl._active_udid = "emulator-5554"
        ctrl._device_type_cache["emulator-5554"] = DeviceType.ANDROID_EMULATOR
        ctrl.adb.uninstall_app = AsyncMock()
        ctrl.simctl.uninstall_app = AsyncMock()

        await ctrl.uninstall_app("com.example.app")
        ctrl.adb.uninstall_app.assert_called_once_with("emulator-5554", "com.example.app")
        ctrl.simctl.uninstall_app.assert_not_called()

    async def test_list_apps_android_uses_adb(self):
        ctrl = DeviceController()
        ctrl._active_udid = "emulator-5554"
        ctrl._device_type_cache["emulator-5554"] = DeviceType.ANDROID_EMULATOR
        ctrl.adb.list_apps = AsyncMock(return_value=[])
        ctrl.simctl.list_apps = AsyncMock(return_value=[])

        apps, udid = await ctrl.list_apps()
        ctrl.adb.list_apps.assert_called_once_with("emulator-5554")
        ctrl.simctl.list_apps.assert_not_called()

    async def test_screenshot_android_uses_adb(self):
        ctrl = DeviceController()
        ctrl._active_udid = "emulator-5554"
        ctrl._device_type_cache["emulator-5554"] = DeviceType.ANDROID_EMULATOR
        fake_png = b"\x89PNG" + b"\x00" * 10000  # Must be >8KB to avoid blank detection
        ctrl.adb.screenshot = AsyncMock(return_value=fake_png)
        ctrl.simctl.screenshot = AsyncMock()

        with patch("server.device.controller.process_screenshot") as mock_proc:
            mock_proc.return_value = (b"processed", "image/png")
            result_bytes, media_type = await ctrl.screenshot(format="png", scale=0.5)

        ctrl.adb.screenshot.assert_called_once_with("emulator-5554")
        ctrl.simctl.screenshot.assert_not_called()


class TestAndroidListDevicesMerge:
    async def test_list_devices_includes_android(self):
        ctrl = DeviceController()
        ctrl.simctl.list_devices = AsyncMock(
            return_value=[
                _device(udid="SIM-1111", state=DeviceState.BOOTED),
            ]
        )
        ctrl.devicectl.list_devices = AsyncMock(return_value=[])
        ctrl.usbmux.list_devices = AsyncMock(return_value=[])
        ctrl.usbmux.get_usb_udid_map = AsyncMock(return_value={})
        ctrl.adb.list_devices = AsyncMock(
            return_value=[
                _android_device(udid="emulator-5554"),
            ]
        )

        devices = await ctrl.list_devices()
        assert len(devices) == 2
        udids = [d.udid for d in devices]
        assert "SIM-1111" in udids
        assert "emulator-5554" in udids

        # Verify device type cache was populated
        assert ctrl._device_type_cache["emulator-5554"] == DeviceType.ANDROID_EMULATOR


class TestAndroidCheckTools:
    async def test_check_tools_includes_adb(self):
        ctrl = DeviceController()
        ctrl.simctl.is_available = AsyncMock(return_value=True)
        ctrl.idb.is_available = AsyncMock(return_value=True)
        ctrl.devicectl.is_available = AsyncMock(return_value=True)
        ctrl.pmd3.is_available = AsyncMock(return_value=True)
        ctrl.adb.is_available = AsyncMock(return_value=True)
        with patch("server.device.tunneld.is_tunneld_running", return_value=True):
            tools = await ctrl.check_tools()
        assert "adb" in tools
        assert tools["adb"] is True

    async def test_check_tools_adb_missing(self):
        ctrl = DeviceController()
        ctrl.simctl.is_available = AsyncMock(return_value=True)
        ctrl.idb.is_available = AsyncMock(return_value=False)
        ctrl.devicectl.is_available = AsyncMock(return_value=False)
        ctrl.pmd3.is_available = AsyncMock(return_value=False)
        ctrl.adb.is_available = AsyncMock(return_value=False)
        with patch("server.device.tunneld.is_tunneld_running", return_value=False):
            tools = await ctrl.check_tools()
        assert tools["adb"] is False


class TestAndroidUIBackendSelection:
    def test_android_emulator_uses_u2_backend(self):
        ctrl = DeviceController()
        ctrl._device_type_cache["emulator-5554"] = DeviceType.ANDROID_EMULATOR
        from server.device.u2_client import U2Backend

        assert isinstance(ctrl._ui_backend("emulator-5554"), U2Backend)

    def test_android_physical_uses_u2_backend(self):
        ctrl = DeviceController()
        ctrl._device_type_cache["ZY224H6L"] = DeviceType.ANDROID_DEVICE
        from server.device.u2_client import U2Backend

        assert isinstance(ctrl._ui_backend("ZY224H6L"), U2Backend)


class TestScrollToElement:
    async def test_requires_label_or_identifier(self):
        ctrl = DeviceController()
        with pytest.raises(DeviceError, match="label or identifier"):
            await ctrl.scroll_to_element()

    def _ios_ctrl(self, backend):
        ctrl = DeviceController()
        ctrl._device_type_cache["AAAA-1111"] = DeviceType.SIMULATOR
        ctrl.resolve_udid = AsyncMock(return_value="AAAA-1111")
        ctrl._invalidate_ui_cache = MagicMock()
        ctrl._get_screen_dimensions = AsyncMock(
            return_value={"width": 402, "height": 852}
        )
        ctrl._ui_backend = MagicMock(return_value=backend)
        return ctrl

    @staticmethod
    def _el(y: float, *, height: float = 40.0):
        return UIElement(
            type="Button", label="Log", identifier="button_log",
            frame={"x": 100.0, "y": y, "width": 120.0, "height": height},
        )

    async def test_ios_already_visible_no_swipe(self):
        backend = MagicMock()
        backend.swipe = AsyncMock()
        ctrl = self._ios_ctrl(backend)
        # On-screen from the first fetch (center 420, within [0, 818]).
        ctrl.get_ui_elements = AsyncMock(
            return_value=([self._el(400)], "AAAA-1111")
        )

        result = await ctrl.scroll_to_element(identifier="button_log")
        assert result["status"] == "ok"
        assert result["element"]["identifier"] == "button_log"
        assert result["element"]["y"] == 420.0
        backend.swipe.assert_not_called()

    async def test_ios_scrolls_toward_offscreen_below(self):
        backend = MagicMock()
        backend.swipe = AsyncMock()
        ctrl = self._ios_ctrl(backend)
        # First below the viewport (center 1020), then in view after a swipe
        # (the third fetch is the settle re-confirm).
        ctrl.get_ui_elements = AsyncMock(side_effect=[
            ([self._el(1000)], "AAAA-1111"),
            ([self._el(400)], "AAAA-1111"),
            ([self._el(400)], "AAAA-1111"),
        ])

        result = await ctrl.scroll_to_element(identifier="button_log")
        assert result["status"] == "ok"
        backend.swipe.assert_called_once()
        # A target below the viewport should be revealed by a finger swipe UP
        # (start_y > end_y), which scrolls the content down.
        args = backend.swipe.call_args.args
        start_y, end_y = args[2], args[4]
        assert start_y > end_y

    async def test_ios_scrolls_toward_offscreen_above(self):
        backend = MagicMock()
        backend.swipe = AsyncMock()
        ctrl = self._ios_ctrl(backend)
        # First tucked under the top nav bar (top edge 4 < 50 inset), then in
        # view after scrolling up (the third fetch is the settle re-confirm).
        ctrl.get_ui_elements = AsyncMock(side_effect=[
            ([self._el(4)], "AAAA-1111"),
            ([self._el(120)], "AAAA-1111"),
            ([self._el(120)], "AAAA-1111"),
        ])

        result = await ctrl.scroll_to_element(identifier="button_log")
        assert result["status"] == "ok"
        backend.swipe.assert_called_once()
        # A target hidden above should be revealed by a finger swipe DOWN
        # (start_y < end_y), which scrolls the content up.
        args = backend.swipe.call_args.args
        start_y, end_y = args[2], args[4]
        assert start_y < end_y

    async def test_ios_not_found_after_budget(self):
        backend = MagicMock()
        backend.swipe = AsyncMock()
        ctrl = self._ios_ctrl(backend)
        # Never located (lazy/recycled row) → blind sweep, never visible.
        ctrl.get_ui_elements = AsyncMock(return_value=([], "AAAA-1111"))

        result = await ctrl.scroll_to_element(identifier="button_log", max_swipes=3)
        assert result["status"] == "not_found"
        assert backend.swipe.await_count > 0

    async def test_ios_aborts_when_container_stalls(self):
        backend = MagicMock()
        backend.swipe = AsyncMock()
        ctrl = self._ios_ctrl(backend)
        # Located below the viewport but the frame never moves → end of travel.
        ctrl.get_ui_elements = AsyncMock(return_value=([self._el(1000)], "AAAA-1111"))

        result = await ctrl.scroll_to_element(identifier="button_log", max_swipes=10)
        assert result["status"] == "not_found"
        # Stall detected after a couple of swipes — nowhere near the full budget.
        assert backend.swipe.await_count <= 3

    async def test_android_ok_delegates_to_backend(self):
        ctrl = DeviceController()
        ctrl._device_type_cache["emulator-5554"] = DeviceType.ANDROID_EMULATOR
        ctrl.resolve_udid = AsyncMock(return_value="emulator-5554")
        ctrl._invalidate_ui_cache = MagicMock()

        element = {"label": "Log", "identifier": "button_log",
                   "type": "Button", "x": 100, "y": 200}
        backend = MagicMock()
        backend.scroll_into_view = AsyncMock(return_value=element)
        ctrl._ui_backend = MagicMock(return_value=backend)

        result = await ctrl.scroll_to_element(identifier="button_log", max_swipes=5)
        assert result == {"status": "ok", "element": element}
        backend.scroll_into_view.assert_called_once_with(
            "emulator-5554", identifier="button_log", label=None, max_swipes=5,
        )

    async def test_android_not_found(self):
        ctrl = DeviceController()
        ctrl._device_type_cache["emulator-5554"] = DeviceType.ANDROID_EMULATOR
        ctrl.resolve_udid = AsyncMock(return_value="emulator-5554")
        ctrl._invalidate_ui_cache = MagicMock()

        backend = MagicMock()
        backend.scroll_into_view = AsyncMock(return_value=None)
        ctrl._ui_backend = MagicMock(return_value=backend)

        result = await ctrl.scroll_to_element(label="Nope")
        assert result["status"] == "not_found"


class TestTapElementIosScroll:
    """iOS tap_element auto-scrolls an off-screen target into view, then taps."""

    def _ios_ctrl(self, backend):
        ctrl = DeviceController()
        ctrl._device_type_cache["AAAA-1111"] = DeviceType.SIMULATOR
        ctrl.resolve_udid = AsyncMock(return_value="AAAA-1111")
        ctrl._invalidate_ui_cache = MagicMock()
        ctrl._get_screen_dimensions = AsyncMock(
            return_value={"width": 402, "height": 852}
        )
        ctrl._ui_backend = MagicMock(return_value=backend)
        return ctrl

    @staticmethod
    def _target():
        return UIElement(
            type="Button", label="Sign out", identifier="_SignOut button",
            frame={"x": 20.0, "y": 400.0, "width": 360.0, "height": 44.0},
        )

    async def test_miss_then_scroll_then_tap(self):
        backend = MagicMock()
        backend.tap = AsyncMock()
        ctrl = self._ios_ctrl(backend)
        # Traditional fetch misses (off-screen); scroll brings it into view.
        ctrl.get_ui_elements = AsyncMock(return_value=([], "AAAA-1111"))
        ctrl._ios_scroll_to_element = AsyncMock(return_value=self._target())

        result = await ctrl.tap_element(
            identifier="_SignOut button", skip_stability_check=True,
        )
        assert result["status"] == "ok"
        assert result["tapped"]["identifier"] == "_SignOut button"
        ctrl._ios_scroll_to_element.assert_awaited_once_with(
            "AAAA-1111", label=None, identifier="_SignOut button", max_swipes=10,
            # tap_element has already established the element is absent, so the
            # scroll loop skips its own opening lookup — a full tree read.
            target_known_absent=True,
        )
        backend.tap.assert_awaited_once()

    async def test_scroll_to_find_false_skips_scroll(self):
        backend = MagicMock()
        backend.tap = AsyncMock()
        ctrl = self._ios_ctrl(backend)
        ctrl.get_ui_elements = AsyncMock(return_value=([], "AAAA-1111"))
        ctrl._ios_scroll_to_element = AsyncMock(return_value=self._target())

        with patch(
            "server.device.controller_ui._capture_screenshot",
            AsyncMock(return_value=None),
        ):
            result = await ctrl.tap_element(
                identifier="_SignOut button", scroll_to_find=False,
            )
        assert result["status"] == "not_found"
        ctrl._ios_scroll_to_element.assert_not_called()

    async def test_scroll_miss_returns_not_found(self):
        backend = MagicMock()
        backend.tap = AsyncMock()
        ctrl = self._ios_ctrl(backend)
        ctrl.get_ui_elements = AsyncMock(return_value=([], "AAAA-1111"))
        ctrl._ios_scroll_to_element = AsyncMock(return_value=None)

        with patch(
            "server.device.controller_ui._capture_screenshot",
            AsyncMock(return_value=None),
        ):
            result = await ctrl.tap_element(label="Nope")
        assert result["status"] == "not_found"
        ctrl._ios_scroll_to_element.assert_awaited_once()
        backend.tap.assert_not_called()


class TestTapElementAutoScroll:
    def _android_ctrl(self):
        ctrl = DeviceController()
        ctrl._device_type_cache["emulator-5554"] = DeviceType.ANDROID_EMULATOR
        ctrl.resolve_udid = AsyncMock(return_value="emulator-5554")
        ctrl._invalidate_ui_cache = MagicMock()
        return ctrl

    async def test_scrolls_then_taps_when_offscreen(self):
        ctrl = self._android_ctrl()
        backend = MagicMock()
        # first selector tap misses (off-screen), succeeds after scroll
        backend.tap_by_selector = AsyncMock(
            side_effect=[None, {"identifier": "button_log", "type": "Button", "x": 1, "y": 2}]
        )
        backend.scroll_into_view = AsyncMock(return_value={"identifier": "button_log"})
        ctrl._ui_backend = MagicMock(return_value=backend)

        result = await ctrl.tap_element(identifier="button_log")
        assert result["status"] == "ok"
        backend.scroll_into_view.assert_called_once_with(
            "emulator-5554", identifier="button_log", label=None,
        )
        assert backend.tap_by_selector.call_count == 2

    async def test_no_scroll_when_already_tappable(self):
        ctrl = self._android_ctrl()
        backend = MagicMock()
        backend.tap_by_selector = AsyncMock(
            return_value={"identifier": "button_log", "type": "Button", "x": 1, "y": 2}
        )
        backend.scroll_into_view = AsyncMock()
        ctrl._ui_backend = MagicMock(return_value=backend)

        result = await ctrl.tap_element(identifier="button_log")
        assert result["status"] == "ok"
        backend.scroll_into_view.assert_not_called()

    async def test_scroll_to_find_false_skips_scroll(self):
        ctrl = self._android_ctrl()
        backend = MagicMock()
        backend.tap_by_selector = AsyncMock(return_value=None)  # miss
        backend.scroll_into_view = AsyncMock()
        ctrl._ui_backend = MagicMock(return_value=backend)
        # with scrolling disabled, it falls through to the dump path → not_found
        ctrl.get_ui_elements = AsyncMock(return_value=([], "emulator-5554"))

        with patch("server.device.controller_ui._capture_screenshot", AsyncMock(return_value=None)):
            result = await ctrl.tap_element(identifier="button_log", scroll_to_find=False)
        assert result["status"] == "not_found"
        backend.scroll_into_view.assert_not_called()
