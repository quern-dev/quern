"""Tests for WdaBackend — format conversion, tree flattening, and backend dispatch."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server.device.wda_client import (
    IDLE_TIMEOUT,
    WdaBackend,
    _map_wda_element,
    convert_wda_tree_nested,
    find_element_at_point,
    flatten_wda_tree,
)
from server.models import DeviceError


# ---------------------------------------------------------------------------
# Sample WDA source data
# ---------------------------------------------------------------------------

SIMPLE_WDA_ELEMENT = {
    "type": "XCUIElementTypeButton",
    "rawIdentifier": "loginButton",
    "name": "loginButton",
    "value": None,
    "label": "Log In",
    "rect": {"x": 100, "y": 200, "width": 120, "height": 44},
    "isEnabled": True,
    "children": [],
}

WDA_TREE = {
    "type": "XCUIElementTypeApplication",
    "rawIdentifier": "",
    "name": "MyApp",
    "value": None,
    "label": "MyApp",
    "rect": {"x": 0, "y": 0, "width": 393, "height": 852},
    "isEnabled": True,
    "children": [
        {
            "type": "XCUIElementTypeWindow",
            "rawIdentifier": "",
            "name": "",
            "value": None,
            "label": "",
            "rect": {"x": 0, "y": 0, "width": 393, "height": 852},
            "isEnabled": True,
            "children": [
                {
                    "type": "XCUIElementTypeButton",
                    "rawIdentifier": "loginButton",
                    "name": "loginButton",
                    "value": None,
                    "label": "Log In",
                    "rect": {"x": 100, "y": 200, "width": 120, "height": 44},
                    "isEnabled": True,
                    "children": [],
                },
                {
                    "type": "XCUIElementTypeStaticText",
                    "rawIdentifier": "",
                    "name": "",
                    "value": "Welcome",
                    "label": "Welcome to MyApp",
                    "rect": {"x": 50, "y": 100, "width": 293, "height": 30},
                    "isEnabled": True,
                    "children": [],
                },
                {
                    "type": "XCUIElementTypeTextField",
                    "rawIdentifier": "emailField",
                    "name": "emailField",
                    "value": "user@example.com",
                    "label": "Email",
                    "rect": {"x": 20, "y": 150, "width": 353, "height": 40},
                    "isEnabled": False,
                    "children": [],
                },
            ],
        },
    ],
}


# ---------------------------------------------------------------------------
# _map_wda_element tests
# ---------------------------------------------------------------------------


class TestMapWdaElement:
    def test_basic_mapping(self):
        result = _map_wda_element(SIMPLE_WDA_ELEMENT)
        assert result["type"] == "Button"  # XCUIElementType prefix stripped
        assert result["AXUniqueId"] == "loginButton"
        assert result["AXLabel"] == "Log In"
        assert result["AXValue"] is None
        assert result["enabled"] is True
        assert result["frame"] == {"x": 100, "y": 200, "width": 120, "height": 44}

    def test_type_prefix_stripping(self):
        el = {"type": "XCUIElementTypeStaticText", "children": []}
        result = _map_wda_element(el)
        assert result["type"] == "StaticText"

    def test_type_without_prefix(self):
        el = {"type": "Button", "children": []}
        result = _map_wda_element(el)
        assert result["type"] == "Button"

    def test_identifier_fallback_to_name(self):
        el = {"type": "XCUIElementTypeButton", "name": "myName", "children": []}
        result = _map_wda_element(el)
        assert result["AXUniqueId"] == "myName"

    def test_missing_rect(self):
        el = {"type": "XCUIElementTypeOther", "children": []}
        result = _map_wda_element(el)
        assert result["frame"] is None

    def test_disabled_element(self):
        el = {
            "type": "XCUIElementTypeButton",
            "isEnabled": False,
            "rect": {"x": 0, "y": 0, "width": 100, "height": 44},
            "children": [],
        }
        result = _map_wda_element(el)
        assert result["enabled"] is False

    def test_value_mapping(self):
        el = {
            "type": "XCUIElementTypeTextField",
            "value": "hello",
            "label": "Username",
            "children": [],
        }
        result = _map_wda_element(el)
        assert result["AXValue"] == "hello"
        assert result["AXLabel"] == "Username"

    def test_empty_label_and_identifier(self):
        el = {"type": "XCUIElementTypeOther", "children": []}
        result = _map_wda_element(el)
        assert result["AXLabel"] == ""
        assert result["AXUniqueId"] == ""


# ---------------------------------------------------------------------------
# flatten_wda_tree tests
# ---------------------------------------------------------------------------


class TestFlattenWdaTree:
    def test_single_element(self):
        flat = flatten_wda_tree(SIMPLE_WDA_ELEMENT)
        assert len(flat) == 1
        assert flat[0]["type"] == "Button"

    def test_nested_tree(self):
        flat = flatten_wda_tree(WDA_TREE)
        # Application > Window > Button, StaticText, TextField = 5 elements
        assert len(flat) == 5

        types = [el["type"] for el in flat]
        assert types == ["Application", "Window", "Button", "StaticText", "TextField"]

    def test_preserves_all_fields(self):
        flat = flatten_wda_tree(WDA_TREE)
        button = flat[2]  # Third element is the Button
        assert button["AXUniqueId"] == "loginButton"
        assert button["AXLabel"] == "Log In"
        assert button["frame"]["x"] == 100

    def test_disabled_element_preserved(self):
        flat = flatten_wda_tree(WDA_TREE)
        text_field = flat[4]  # TextField is last
        assert text_field["enabled"] is False
        assert text_field["AXValue"] == "user@example.com"


# ---------------------------------------------------------------------------
# convert_wda_tree_nested tests
# ---------------------------------------------------------------------------


class TestConvertWdaTreeNested:
    def test_preserves_hierarchy(self):
        result = convert_wda_tree_nested(WDA_TREE)
        assert len(result) == 1  # Root is single Application
        app = result[0]
        assert app["type"] == "Application"
        assert "children" in app
        assert len(app["children"]) == 1  # One Window

        window = app["children"][0]
        assert window["type"] == "Window"
        assert len(window["children"]) == 3  # Button, StaticText, TextField

    def test_leaf_has_no_children_key(self):
        result = convert_wda_tree_nested(SIMPLE_WDA_ELEMENT)
        assert len(result) == 1
        # Leaf with empty children list should not have 'children' key
        # (WDA gives children=[], which is falsy)
        assert "children" not in result[0]

    def test_field_conversion(self):
        result = convert_wda_tree_nested(WDA_TREE)
        button = result[0]["children"][0]["children"][0]
        assert button["AXUniqueId"] == "loginButton"
        assert button["AXLabel"] == "Log In"


# ---------------------------------------------------------------------------
# find_element_at_point tests
# ---------------------------------------------------------------------------


class TestFindElementAtPoint:
    def test_finds_deepest_element(self):
        flat = flatten_wda_tree(WDA_TREE)
        # Point (150, 220) is inside the Button (100,200,120,44)
        result = find_element_at_point(flat, 150, 220)
        assert result is not None
        assert result["type"] == "Button"

    def test_returns_none_for_empty_area(self):
        flat = flatten_wda_tree(WDA_TREE)
        # Point way off-screen
        result = find_element_at_point(flat, 5000, 5000)
        assert result is None

    def test_point_on_boundary(self):
        flat = flatten_wda_tree(WDA_TREE)
        # Exact top-left corner of button
        result = find_element_at_point(flat, 100, 200)
        assert result is not None
        assert result["type"] == "Button"

    def test_prefers_deeper_element(self):
        flat = flatten_wda_tree(WDA_TREE)
        # Point (200, 220) is inside both Window and Button
        result = find_element_at_point(flat, 200, 220)
        assert result is not None
        # Should prefer Button (deeper) over Window/Application
        assert result["type"] == "Button"

    def test_no_frame_elements_skipped(self):
        elements = [
            {"type": "Other", "frame": None},
            {"type": "Button", "frame": {"x": 0, "y": 0, "width": 100, "height": 100}},
        ]
        result = find_element_at_point(elements, 50, 50)
        assert result["type"] == "Button"


# ---------------------------------------------------------------------------
# WdaBackend HTTP method tests (mocked)
# ---------------------------------------------------------------------------


def _make_session_backend() -> WdaBackend:
    """Helper: create a WdaBackend with a pre-cached session for 'test-udid'."""
    backend = WdaBackend()
    backend._connections["test-udid"] = MagicMock(
        base_url="http://localhost:8100",
        forward_proc=None,
        session_id="test-session",
    )
    return backend


class TestWdaBackendTap:
    async def test_tap_sends_correct_request(self):
        backend = _make_session_backend()

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await backend.tap("test-udid", 150.5, 300.7)

            mock_client.post.assert_called_once_with(
                "http://localhost:8100/session/test-session/wda/tap",
                json={"x": 150.5, "y": 300.7},
                timeout=10.0,
            )


class TestWdaBackendSwipe:
    async def test_swipe_sends_correct_request(self):
        backend = _make_session_backend()

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await backend.swipe("test-udid", 100, 400, 100, 200, 0.5)

            mock_client.post.assert_called_once_with(
                "http://localhost:8100/session/test-session/wda/dragfromtoforduration",
                json={
                    "fromX": 100,
                    "fromY": 400,
                    "toX": 100,
                    "toY": 200,
                    "duration": 0.5,
                },
                timeout=10.0,
            )


class TestWdaBackendTypeText:
    async def test_type_text_sends_character_array(self):
        backend = _make_session_backend()

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await backend.type_text("test-udid", "hello")

            mock_client.post.assert_called_once_with(
                "http://localhost:8100/session/test-session/wda/keys",
                json={"value": ["h", "e", "l", "l", "o"]},
                timeout=10.0,
            )


class TestWdaBackendPressButton:
    async def test_press_button_sends_correct_name(self):
        backend = _make_session_backend()

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await backend.press_button("test-udid", "home")

            mock_client.post.assert_called_once_with(
                "http://localhost:8100/session/test-session/wda/pressButton",
                json={"name": "home"},
                timeout=10.0,
            )


class TestWdaBackendDescribeAll:
    async def test_describe_all_flattens_and_converts(self):
        backend = WdaBackend()
        backend._connections["test-udid"] = MagicMock(
            base_url="http://localhost:8100",
            forward_proc=None,
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"value": WDA_TREE, "sessionId": "abc"}

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await backend.describe_all("test-udid")

            assert len(result) == 5
            assert result[0]["type"] == "Application"
            assert result[2]["AXUniqueId"] == "loginButton"


# ---------------------------------------------------------------------------
# Backend dispatch tests
# ---------------------------------------------------------------------------


class TestBackendDispatch:
    def test_physical_device_uses_wda(self):
        """Physical devices should route to WdaBackend."""
        from server.device.controller import DeviceController
        from server.models import DeviceType

        ctrl = DeviceController()
        ctrl._device_type_cache["physical-udid"] = DeviceType.DEVICE
        backend = ctrl._ui_backend("physical-udid")
        assert isinstance(backend, WdaBackend)

    def test_simulator_uses_idb(self):
        """Simulators should route to IdbBackend."""
        from server.device.controller import DeviceController
        from server.device.idb import IdbBackend
        from server.models import DeviceType

        ctrl = DeviceController()
        ctrl._device_type_cache["sim-udid"] = DeviceType.SIMULATOR
        backend = ctrl._ui_backend("sim-udid")
        assert isinstance(backend, IdbBackend)

    def test_unknown_device_defaults_to_idb(self):
        """Unknown devices default to simulator (IdbBackend)."""
        from server.device.controller import DeviceController
        from server.device.idb import IdbBackend

        ctrl = DeviceController()
        # No entry in _device_type_cache
        backend = ctrl._ui_backend("unknown-udid")
        assert isinstance(backend, IdbBackend)


# ---------------------------------------------------------------------------
# Connection management tests
# ---------------------------------------------------------------------------


class TestWdaConnectionManagement:
    async def test_cached_connection_reused(self):
        backend = WdaBackend()
        backend._connections["test-udid"] = MagicMock(
            base_url="http://localhost:18100",
            forward_proc=None,
        )

        url = await backend._get_base_url("test-udid")
        assert url == "http://localhost:18100"

    async def test_dead_forward_proc_reconnects(self):
        """If the forward process died, should attempt reconnection."""
        backend = WdaBackend()
        dead_proc = MagicMock()
        dead_proc.returncode = 1  # Process exited
        backend._connections["test-udid"] = MagicMock(
            base_url="http://localhost:18100",
            forward_proc=dead_proc,
        )

        # Mock tunneld to return a valid tunnel
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch(
            "server.device.wda_client.WdaBackend._try_tunneld_connection",
            new_callable=AsyncMock,
            return_value="http://[fd35::1]:8100",
        ):
            url = await backend._get_base_url("test-udid")
            assert url == "http://[fd35::1]:8100"

    async def test_close_terminates_forward_procs(self):
        backend = WdaBackend()
        mock_proc = AsyncMock()
        mock_proc.returncode = None
        mock_proc.terminate = MagicMock()
        mock_proc.wait = AsyncMock()
        backend._connections["test-udid"] = MagicMock(
            base_url="http://localhost:18100",
            forward_proc=mock_proc,
            session_id=None,
        )

        await backend.close()
        mock_proc.terminate.assert_called_once()
        assert len(backend._connections) == 0


# ---------------------------------------------------------------------------
# delete_session tests
# ---------------------------------------------------------------------------


class TestDeleteSession:
    async def test_delete_session_sends_delete(self):
        backend = WdaBackend()
        backend._connections["test-udid"] = MagicMock(
            base_url="http://localhost:8100",
            forward_proc=None,
            session_id="sess-123",
        )

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.delete = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await backend.delete_session("test-udid")

            mock_client.delete.assert_called_once_with(
                "http://localhost:8100/session/sess-123",
                timeout=10.0,
            )

        assert backend._connections["test-udid"].session_id is None

    async def test_delete_session_noop_without_session(self):
        backend = WdaBackend()
        backend._connections["test-udid"] = MagicMock(
            base_url="http://localhost:8100",
            forward_proc=None,
            session_id=None,
        )

        # Should not raise
        await backend.delete_session("test-udid")

    async def test_delete_session_noop_no_connection(self):
        backend = WdaBackend()
        # No connection at all — should not raise
        await backend.delete_session("nonexistent")


# ---------------------------------------------------------------------------
# Auto-start tests
# ---------------------------------------------------------------------------


class TestAutoStart:
    async def test_auto_start_when_wda_unreachable(self):
        backend = WdaBackend()
        backend._device_os_versions["test-udid"] = "iOS 17.4"

        mock_result = {"status": "started", "pid": 42, "ready": True}

        with (
            patch.object(
                backend, "_try_tunneld_connection",
                new_callable=AsyncMock,
                side_effect=[None, "http://[fd35::1]:8100"],
            ),
            patch.object(
                backend, "_start_usbmux_forward",
                new_callable=AsyncMock,
                side_effect=DeviceError("not reachable", tool="wda"),
            ),
            patch("server.device.wda.start_driver", new_callable=AsyncMock, return_value=mock_result),
        ):
            url = await backend._get_base_url("test-udid")
            assert url == "http://[fd35::1]:8100"

    async def test_auto_start_skipped_without_os_version(self):
        backend = WdaBackend()
        # No os_version set

        with (
            patch.object(
                backend, "_try_tunneld_connection",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch.object(
                backend, "_start_usbmux_forward",
                new_callable=AsyncMock,
                side_effect=DeviceError("not reachable", tool="wda"),
            ),
        ):
            with pytest.raises(DeviceError, match="os_version unknown"):
                await backend._get_base_url("test-udid")

    async def test_auto_start_not_ready_raises(self):
        backend = WdaBackend()
        backend._device_os_versions["test-udid"] = "iOS 17.4"

        mock_result = {"status": "started", "pid": 42, "ready": False}

        with (
            patch.object(
                backend, "_try_tunneld_connection",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch.object(
                backend, "_start_usbmux_forward",
                new_callable=AsyncMock,
                side_effect=DeviceError("not reachable", tool="wda"),
            ),
            patch("server.device.wda.start_driver", new_callable=AsyncMock, return_value=mock_result),
        ):
            with pytest.raises(DeviceError, match="did not become responsive"):
                await backend._get_base_url("test-udid")


# ---------------------------------------------------------------------------
# Idle timeout tests
# ---------------------------------------------------------------------------


class TestIdleTimeout:
    async def test_idle_checker_cleans_idle_sessions(self):
        """Idle timeout deletes session and clears connection, but does NOT stop the driver."""
        backend = WdaBackend()
        backend._connections["test-udid"] = MagicMock(
            base_url="http://localhost:8100",
            forward_proc=None,
            session_id="sess-123",
        )
        # Set last interaction to way in the past
        backend._last_interaction["test-udid"] = time.monotonic() - (IDLE_TIMEOUT + 60)

        with (
            patch.object(backend, "delete_session", new_callable=AsyncMock) as mock_delete,
            patch("server.device.wda_client.IDLE_CHECK_INTERVAL", 0.01),
        ):
            # Run the checker briefly
            task = asyncio.create_task(backend._idle_checker())
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

            mock_delete.assert_called_once_with("test-udid")

        assert "test-udid" not in backend._connections
        assert "test-udid" not in backend._last_interaction

    async def test_idle_checker_skips_active_devices(self):
        backend = WdaBackend()
        backend._connections["test-udid"] = MagicMock(
            base_url="http://localhost:8100",
            forward_proc=None,
            session_id="sess-123",
        )
        # Set last interaction to recent
        backend._last_interaction["test-udid"] = time.monotonic()

        with (
            patch.object(backend, "delete_session", new_callable=AsyncMock) as mock_delete,
            patch("server.device.wda_client.IDLE_CHECK_INTERVAL", 0.01),
        ):
            task = asyncio.create_task(backend._idle_checker())
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

            mock_delete.assert_not_called()

        assert "test-udid" in backend._connections

    async def test_ensure_idle_task_creates_task(self):
        backend = WdaBackend()
        assert backend._idle_task is None
        backend._ensure_idle_task()
        assert backend._idle_task is not None
        # Clean up
        backend._idle_task.cancel()
        try:
            await backend._idle_task
        except asyncio.CancelledError:
            pass

    async def test_close_cancels_idle_task(self):
        backend = WdaBackend()
        backend._ensure_idle_task()
        assert backend._idle_task is not None
        await backend.close()
        assert backend._idle_task is None
