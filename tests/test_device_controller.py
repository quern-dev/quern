"""Tests for DeviceController — mock SimctlBackend methods."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

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
        ctrl.simctl.list_devices = AsyncMock(return_value=[
            _device(udid="auto-udid", state=DeviceState.BOOTED),
            _device(udid="other-udid", state=DeviceState.SHUTDOWN),
        ])
        ctrl.devicectl.list_devices = AsyncMock(return_value=[])
        ctrl.usbmux.list_devices = AsyncMock(return_value=[])
        result = await ctrl.resolve_udid()
        assert result == "auto-udid"
        assert ctrl._active_udid == "auto-udid"

    async def test_no_booted_error(self):
        """Case 4: No booted devices raises error."""
        ctrl = DeviceController()
        ctrl.simctl.list_devices = AsyncMock(return_value=[
            _device(udid="off1", state=DeviceState.SHUTDOWN),
        ])
        ctrl.devicectl.list_devices = AsyncMock(return_value=[])
        ctrl.usbmux.list_devices = AsyncMock(return_value=[])
        with pytest.raises(DeviceError, match="No booted device"):
            await ctrl.resolve_udid()

    async def test_multiple_booted_error(self):
        """Case 5: Multiple booted devices raises error."""
        ctrl = DeviceController()
        ctrl.simctl.list_devices = AsyncMock(return_value=[
            _device(udid="dev1", name="iPhone A", state=DeviceState.BOOTED),
            _device(udid="dev2", name="iPhone B", state=DeviceState.BOOTED),
        ])
        ctrl.devicectl.list_devices = AsyncMock(return_value=[])
        ctrl.usbmux.list_devices = AsyncMock(return_value=[])
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
        ctrl.usbmux.is_available = AsyncMock(return_value=True)
        with patch("server.device.tunneld.is_tunneld_running", return_value=True):
            tools = await ctrl.check_tools()
        assert tools == {"simctl": True, "idb": True, "devicectl": True, "pymobiledevice3": True, "tunneld": True}

    async def test_simctl_only(self):
        ctrl = DeviceController()
        ctrl.simctl.is_available = AsyncMock(return_value=True)
        ctrl.idb.is_available = AsyncMock(return_value=False)
        ctrl.devicectl.is_available = AsyncMock(return_value=False)
        ctrl.usbmux.is_available = AsyncMock(return_value=False)
        with patch("server.device.tunneld.is_tunneld_running", return_value=False):
            tools = await ctrl.check_tools()
        assert tools == {"simctl": True, "idb": False, "devicectl": False, "pymobiledevice3": False, "tunneld": False}

    async def test_none_available(self):
        ctrl = DeviceController()
        ctrl.simctl.is_available = AsyncMock(return_value=False)
        ctrl.idb.is_available = AsyncMock(return_value=False)
        ctrl.devicectl.is_available = AsyncMock(return_value=False)
        ctrl.usbmux.is_available = AsyncMock(return_value=False)
        with patch("server.device.tunneld.is_tunneld_running", return_value=False):
            tools = await ctrl.check_tools()
        assert tools == {"simctl": False, "idb": False, "devicectl": False, "pymobiledevice3": False, "tunneld": False}


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
        ctrl.simctl.list_devices = AsyncMock(return_value=[
            _device(udid="found-udid", name="iPhone 16 Pro", state=DeviceState.SHUTDOWN),
        ])
        ctrl.simctl.boot = AsyncMock()
        udid = await ctrl.boot(name="iPhone 16 Pro")
        ctrl.simctl.boot.assert_called_once_with("found-udid")
        assert udid == "found-udid"

    async def test_boot_by_name_not_found(self):
        ctrl = DeviceController()
        ctrl.simctl.list_devices = AsyncMock(return_value=[])
        with pytest.raises(DeviceError, match="No simulator found with name"):
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
        ctrl.simctl.launch_app.assert_called_once_with("AAAA-1111", "com.example.App")
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
        ctrl.idb.describe_all.assert_called_once_with("AAAA-1111")

    async def test_with_explicit_udid(self):
        ctrl = DeviceController()
        ctrl.idb.describe_all = AsyncMock(return_value=[])

        elements, udid = await ctrl.get_ui_elements(udid="BBBB-2222")
        assert udid == "BBBB-2222"
        ctrl.idb.describe_all.assert_called_once_with("BBBB-2222")


class TestGetScreenSummary:
    async def test_returns_summary(self):
        ctrl = DeviceController()
        ctrl._active_udid = "AAAA-1111"
        ctrl.idb.describe_all = AsyncMock(return_value=_FAKE_IDB_OUTPUT)

        summary, udid = await ctrl.get_screen_summary()
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

    async def test_no_match_raises(self):
        ctrl = DeviceController()
        ctrl._active_udid = "AAAA-1111"
        ctrl.idb.describe_all = AsyncMock(return_value=_FAKE_IDB_OUTPUT)

        with pytest.raises(DeviceError, match="No element found"):
            await ctrl.tap_element(label="Nonexistent")

    async def test_no_label_or_identifier_raises(self):
        ctrl = DeviceController()
        with pytest.raises(DeviceError, match="Either label or identifier is required"):
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
        data = _FAKE_IDB_OUTPUT + [{
            "type": "StaticText",
            "AXLabel": "Settings",
            "AXUniqueId": "SettingsLabel",
            "frame": {"x": 0, "y": 0, "width": 100, "height": 20},
            "enabled": True,
            "role": "AXStaticText",
            "role_description": "text",
        }]
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


class TestGrantPermission:
    async def test_grant_permission_delegates_to_simctl(self):
        ctrl = DeviceController()
        ctrl._active_udid = "AAAA-1111"
        ctrl.simctl.grant_permission = AsyncMock()

        udid = await ctrl.grant_permission("com.example.App", "photos")
        assert udid == "AAAA-1111"
        ctrl.simctl.grant_permission.assert_called_once_with(
            "AAAA-1111", "com.example.App", "photos",
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
