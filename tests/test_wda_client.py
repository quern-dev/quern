"""Tests for WdaBackend — format conversion, tree flattening, and backend dispatch."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import httpx

from server.device.wda_client import (
    ACTION_TIMEOUT,
    IDLE_TIMEOUT,
    SKELETON_QUERY_TIMEOUT,
    SNAPSHOT_MAX_DEPTH,
    SOURCE_TIMEOUT,
    WdaBackend,
    _ELEMENT_RESPONSE_ATTRIBUTES,
    _SKELETON_CONTAINER_TYPES,
    _map_wda_element,
    _map_wda_element_from_query,
    _parse_wda_error,
    convert_wda_tree_nested,
    find_element_at_point,
    flatten_wda_tree,
)
from server.models import (
    DeviceError,
    WdaAppCrashedError,
    WdaElementNotFoundError,
    WdaElementNotInteractableError,
    WdaError,
    WdaInvalidSessionError,
    WdaKeyboardNotPresentError,
    WdaStaleElementError,
)


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
    """Helper: create a WdaBackend with a pre-cached session for 'test-udid'.

    Uses a mock forward_proc (returncode=None → alive) so the cache check
    is a simple process-alive test, not a network /status ping.
    """
    backend = WdaBackend()
    mock_proc = MagicMock()
    mock_proc.returncode = None  # Process still alive
    backend._connections["test-udid"] = MagicMock(
        base_url="http://localhost:8100",
        forward_proc=mock_proc,
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
                timeout=ACTION_TIMEOUT,
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
                timeout=ACTION_TIMEOUT,
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
                timeout=ACTION_TIMEOUT,
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
                timeout=ACTION_TIMEOUT,
            )


class TestWdaBackendActivateApp:
    async def test_activate_app_success(self):
        backend = _make_session_backend()

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await backend.activate_app("test-udid", "com.example.App")

            mock_client.post.assert_called_once_with(
                "http://localhost:8100/session/test-session/wda/apps/activate",
                json={"bundleId": "com.example.App"},
                timeout=ACTION_TIMEOUT,
            )

    async def test_activate_app_failure_raises(self):
        backend = _make_session_backend()

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        mock_response.json = MagicMock(return_value={
            "value": {"error": "unknown error", "message": "activate failed"},
        })

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with pytest.raises(WdaError):
                await backend.activate_app("test-udid", "com.example.App")


class TestWdaBackendTerminateApp:
    async def test_terminate_app_success(self):
        backend = _make_session_backend()

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await backend.terminate_app("test-udid", "com.example.App")

            mock_client.post.assert_called_once_with(
                "http://localhost:8100/session/test-session/wda/apps/terminate",
                json={"bundleId": "com.example.App"},
                timeout=10.0,
            )

    async def test_terminate_app_failure_raises(self):
        backend = _make_session_backend()

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        mock_response.json = MagicMock(return_value={
            "value": {"error": "unknown error", "message": "terminate failed"},
        })

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with pytest.raises(WdaError):
                await backend.terminate_app("test-udid", "com.example.App")


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
    async def test_cached_tunneld_connection_verified(self):
        """Cached tunneld connection should ping /status before reuse."""
        backend = WdaBackend()
        backend._connections["test-udid"] = MagicMock(
            base_url="http://[fd35::1]:8100",
            forward_proc=None,
        )

        mock_resp = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("server.device.wda_client.httpx.AsyncClient", return_value=mock_client):
            url = await backend._get_base_url("test-udid")
            assert url == "http://[fd35::1]:8100"
            mock_client.get.assert_called_once_with(
                "http://[fd35::1]:8100/status", timeout=2.0,
            )

    async def test_stale_tunneld_connection_reconnects(self):
        """Stale tunneld connection should be dropped and reconnected."""
        backend = WdaBackend()
        backend._connections["test-udid"] = MagicMock(
            base_url="http://[fd35::1]:8100",
            forward_proc=None,
        )

        # Ping fails — stale tunnel
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("server.device.wda_client.httpx.AsyncClient", return_value=mock_client):
            with patch.object(
                backend, "_try_tunneld_connection",
                new_callable=AsyncMock,
                return_value="http://[fd99::2]:8100",
            ):
                url = await backend._get_base_url("test-udid")
                assert url == "http://[fd99::2]:8100"
                # Old connection should be replaced
                assert backend._connections["test-udid"].base_url == "http://[fd99::2]:8100"

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


# ---------------------------------------------------------------------------
# /source timeout + fallback tests
# ---------------------------------------------------------------------------


class TestDescribeAllTimeoutFallback:
    async def test_describe_all_fast_screen_uses_source(self):
        """Normal screens: /source returns quickly, no fallback needed."""
        backend = _make_session_backend()

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
            # /source was called with SOURCE_TIMEOUT
            mock_client.get.assert_called_once()
            call_kwargs = mock_client.get.call_args
            assert call_kwargs.kwargs.get("timeout") == SOURCE_TIMEOUT or call_kwargs[1].get("timeout") == SOURCE_TIMEOUT

    async def test_describe_all_timeout_falls_back_to_skeleton(self):
        """When /source times out but WDA is responsive, use skeleton queries."""
        backend = _make_session_backend()

        # Mock /status response (WDA is responsive)
        mock_status_response = MagicMock()
        mock_status_response.status_code = 200

        async def mock_get(url, **kwargs):
            if "/source" in url:
                raise httpx.ReadTimeout("timed out")
            return mock_status_response

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=mock_get)
            mock_client.post = AsyncMock(return_value=MagicMock(status_code=200, json=MagicMock(return_value={"value": []})))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with patch.object(backend, "build_screen_skeleton", new_callable=AsyncMock, return_value=[
                {"type": "TabBar", "AXLabel": "", "frame": {"x": 0, "y": 808, "width": 393, "height": 44}},
                {"type": "Button", "AXLabel": "Home", "frame": {"x": 2, "y": 808, "width": 96, "height": 44}},
            ]) as mock_skeleton:
                result = await backend.describe_all("test-udid")

                mock_skeleton.assert_called_once_with("test-udid")
                assert len(result) == 2
                assert result[0]["type"] == "TabBar"
                assert result[1]["AXLabel"] == "Home"

    async def test_describe_all_timeout_restarts_hung_wda(self):
        """When /source times out AND /status times out, restart WDA then skeleton fallback."""
        backend = _make_session_backend()
        backend._device_os_versions["test-udid"] = "iOS 17.4"

        restarted = False

        async def mock_get(url, **kwargs):
            nonlocal restarted
            if "/source" in url:
                raise httpx.ReadTimeout("timed out")
            if "/status" in url:
                if not restarted:
                    raise httpx.ReadTimeout("WDA hung")
                return MagicMock(status_code=200)
            return MagicMock(status_code=200)

        async def fake_stop(udid):
            pass

        async def fake_start(udid, os_version):
            nonlocal restarted
            restarted = True
            mock_proc = MagicMock()
            mock_proc.returncode = None
            backend._connections[udid] = MagicMock(
                base_url="http://localhost:8100",
                forward_proc=mock_proc,
                session_id="new-session",
            )
            return {"ready": True}

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=mock_get)
            mock_client.post = AsyncMock(return_value=MagicMock(status_code=200, json=MagicMock(return_value={"value": []})))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with patch("server.device.wda.stop_driver", new_callable=AsyncMock, side_effect=fake_stop) as mock_stop:
                with patch("server.device.wda.start_driver", new_callable=AsyncMock, side_effect=fake_start) as mock_start:
                    with patch.object(backend, "build_screen_skeleton", new_callable=AsyncMock, return_value=[]) as mock_skel:
                        result = await backend.describe_all("test-udid")

                        mock_stop.assert_called_once_with("test-udid")
                        mock_start.assert_called_once_with("test-udid", "iOS 17.4")
                        mock_skel.assert_called_once_with("test-udid")

            assert isinstance(result, list)
            assert restarted is True

    async def test_describe_all_nested_timeout_falls_back_to_skeleton(self):
        """describe_all_nested also falls back to skeleton on /source timeout."""
        backend = _make_session_backend()

        mock_status_response = MagicMock()
        mock_status_response.status_code = 200

        async def mock_get(url, **kwargs):
            if "/source" in url:
                raise httpx.ReadTimeout("timed out")
            return mock_status_response

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=mock_get)
            mock_client.post = AsyncMock(return_value=MagicMock(status_code=200, json=MagicMock(return_value={"value": []})))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with patch.object(backend, "build_screen_skeleton", new_callable=AsyncMock, return_value=[
                {"type": "NavigationBar", "AXLabel": "Settings", "frame": {"x": 0, "y": 0, "width": 393, "height": 44}},
            ]) as mock_skeleton:
                result = await backend.describe_all_nested("test-udid")

                mock_skeleton.assert_called_once_with("test-udid")
                assert isinstance(result, list)
                assert len(result) == 1
                assert result[0]["type"] == "NavigationBar"


class TestMapWdaElementFromQuery:
    def test_basic_mapping(self):
        el = {
            "name": "myButton",
            "label": "Submit",
            "value": None,
            "rect": {"x": 10, "y": 20, "width": 100, "height": 44},
            "isEnabled": True,
        }
        result = _map_wda_element_from_query(el, "XCUIElementTypeButton")
        assert result["type"] == "Button"
        assert result["AXLabel"] == "Submit"
        assert result["AXUniqueId"] == "myButton"
        assert result["frame"]["x"] == 10

    def test_missing_rect(self):
        el = {"name": "x", "label": "Y"}
        result = _map_wda_element_from_query(el, "XCUIElementTypeSwitch")
        assert result["type"] == "Switch"
        assert result["frame"] is None

    def test_empty_element(self):
        result = _map_wda_element_from_query({}, "XCUIElementTypeTextField")
        assert result["type"] == "TextField"
        assert result["AXLabel"] == ""
        assert result["AXUniqueId"] == ""

    def test_class_name_in_name_field_filtered(self):
        """WDA echoes class name as 'name' when no accessibility ID — should be empty."""
        el = {
            "name": "XCUIElementTypeButton",
            "label": "Submit",
            "rect": {"x": 10, "y": 20, "width": 100, "height": 44},
        }
        result = _map_wda_element_from_query(el, "XCUIElementTypeButton")
        assert result["AXUniqueId"] == ""
        assert result["AXLabel"] == "Submit"


class TestIsWdaResponsive:
    async def test_responsive_returns_true(self):
        backend = _make_session_backend()

        mock_resp = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("server.device.wda_client.httpx.AsyncClient", return_value=mock_client):
            assert await backend._is_wda_responsive("test-udid") is True

    async def test_timeout_returns_false(self):
        backend = _make_session_backend()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.ReadTimeout("hung"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("server.device.wda_client.httpx.AsyncClient", return_value=mock_client):
            assert await backend._is_wda_responsive("test-udid") is False

    async def test_no_connection_returns_false(self):
        backend = WdaBackend()
        # No connection, no os_version — _get_base_url will raise
        with patch.object(backend, "_get_base_url", new_callable=AsyncMock, side_effect=DeviceError("nope", tool="wda")):
            assert await backend._is_wda_responsive("test-udid") is False


# ---------------------------------------------------------------------------
# Snapshot depth tests
# ---------------------------------------------------------------------------


class TestSnapshotDepth:
    async def test_set_snapshot_depth_posts_when_different(self):
        """_set_snapshot_depth should POST to /appium/settings when depth changes."""
        backend = _make_session_backend()
        backend._current_depth["test-udid"] = 10

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await backend._set_snapshot_depth("test-udid", 25)

            mock_client.post.assert_called_once_with(
                "http://localhost:8100/session/test-session/appium/settings",
                json={"settings": {"snapshotMaxDepth": 25}},
                timeout=10.0,
            )
        assert backend._current_depth["test-udid"] == 25

    async def test_set_snapshot_depth_skips_when_same(self):
        """_set_snapshot_depth should NOT POST when depth is already current."""
        backend = _make_session_backend()
        backend._current_depth["test-udid"] = 10

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await backend._set_snapshot_depth("test-udid", 10)

            mock_client.post.assert_not_called()

    async def test_describe_all_with_snapshot_depth_updates_settings(self):
        """describe_all(snapshot_depth=20) should update settings before /source."""
        backend = _make_session_backend()
        backend._current_depth["test-udid"] = 10

        # Track call order
        call_order = []

        mock_settings_response = MagicMock()
        mock_settings_response.status_code = 200

        mock_source_response = MagicMock()
        mock_source_response.status_code = 200
        mock_source_response.json.return_value = {"value": SIMPLE_WDA_ELEMENT}

        async def mock_post(url, **kwargs):
            if "/appium/settings" in url:
                call_order.append("settings")
                return mock_settings_response
            return mock_settings_response

        async def mock_get(url, **kwargs):
            call_order.append("source")
            return mock_source_response

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=mock_post)
            mock_client.get = AsyncMock(side_effect=mock_get)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await backend.describe_all("test-udid", snapshot_depth=20)

            assert call_order == ["settings", "source"]
            assert len(result) == 1

    async def test_describe_all_without_snapshot_depth_no_settings_call(self):
        """describe_all() without snapshot_depth skips settings POST when depth is already correct."""
        backend = _make_session_backend()
        # Simulate depth already set (e.g. from session creation)
        backend._current_depth["test-udid"] = SNAPSHOT_MAX_DEPTH

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"value": SIMPLE_WDA_ELEMENT}

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.post = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await backend.describe_all("test-udid")

            # post should not have been called (depth already matches SNAPSHOT_MAX_DEPTH)
            mock_client.post.assert_not_called()

    async def test_delete_session_clears_depth(self):
        """delete_session should remove the depth entry."""
        backend = WdaBackend()
        backend._connections["test-udid"] = MagicMock(
            base_url="http://localhost:8100",
            forward_proc=None,
            session_id="sess-123",
        )
        backend._current_depth["test-udid"] = 15

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.delete = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await backend.delete_session("test-udid")

        assert "test-udid" not in backend._current_depth

    async def test_close_clears_depth(self):
        """close() should clear all depth entries."""
        backend = WdaBackend()
        backend._current_depth["dev1"] = 10
        backend._current_depth["dev2"] = 20

        await backend.close()

        assert len(backend._current_depth) == 0


# ---------------------------------------------------------------------------
# Sample WDA element query response data (for skeleton tests)
# ---------------------------------------------------------------------------

WDA_TABBAR_ELEMENT = {
    "ELEMENT": "tabbar-uuid-001",
    "element-6066-11e4-a52e-4f735466cecf": "tabbar-uuid-001",
    "type": "XCUIElementTypeTabBar",
    "label": "",
    "name": "",
    "rect": {"x": 0, "y": 808, "width": 393, "height": 44},
    "isEnabled": True,
    "value": None,
}

WDA_TABBAR_BUTTONS = [
    {
        "ELEMENT": "btn-uuid-001",
        "element-6066-11e4-a52e-4f735466cecf": "btn-uuid-001",
        "type": "XCUIElementTypeButton",
        "label": "Home",
        "name": "Home",
        "rect": {"x": 2, "y": 808, "width": 96, "height": 44},
        "isEnabled": True,
        "value": None,
    },
    {
        "ELEMENT": "btn-uuid-002",
        "element-6066-11e4-a52e-4f735466cecf": "btn-uuid-002",
        "type": "XCUIElementTypeButton",
        "label": "Search",
        "name": "Search",
        "rect": {"x": 100, "y": 808, "width": 96, "height": 44},
        "isEnabled": True,
        "value": None,
    },
]

WDA_NAVBAR_ELEMENT = {
    "ELEMENT": "navbar-uuid-001",
    "element-6066-11e4-a52e-4f735466cecf": "navbar-uuid-001",
    "type": "XCUIElementTypeNavigationBar",
    "label": "Map",
    "name": "Map",
    "rect": {"x": 0, "y": 0, "width": 393, "height": 44},
    "isEnabled": True,
    "value": None,
}

WDA_NAVBAR_BUTTONS = [
    {
        "ELEMENT": "btn-uuid-003",
        "element-6066-11e4-a52e-4f735466cecf": "btn-uuid-003",
        "type": "XCUIElementTypeButton",
        "label": "Back",
        "name": "Back",
        "rect": {"x": 0, "y": 0, "width": 44, "height": 44},
        "isEnabled": True,
        "value": None,
    },
]


# ---------------------------------------------------------------------------
# find_elements_by_query tests
# ---------------------------------------------------------------------------


class TestFindElementsByQuery:
    async def test_class_chain_query(self):
        backend = _make_session_backend()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"value": [WDA_TABBAR_ELEMENT]}

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await backend.find_elements_by_query(
                "test-udid", "class chain", "**/XCUIElementTypeTabBar",
            )

            assert len(result) == 1
            assert result[0]["type"] == "TabBar"
            assert result[0]["_wda_element_id"] == "tabbar-uuid-001"
            # Verify correct URL (session-scoped, not element-scoped)
            call_args = mock_client.post.call_args
            assert "/elements" in call_args[0][0]
            assert "/element/" not in call_args[0][0]

    async def test_scoped_child_query(self):
        backend = _make_session_backend()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"value": WDA_TABBAR_BUTTONS}

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await backend.find_elements_by_query(
                "test-udid", "class name", "XCUIElementTypeButton",
                scope_element_id="tabbar-uuid-001",
            )

            assert len(result) == 2
            assert result[0]["AXLabel"] == "Home"
            assert result[1]["AXLabel"] == "Search"
            # Verify scoped URL
            call_args = mock_client.post.call_args
            assert "/element/tabbar-uuid-001/elements" in call_args[0][0]

    async def test_accessibility_id_query(self):
        backend = _make_session_backend()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"value": [WDA_NAVBAR_BUTTONS[0]]}

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await backend.find_elements_by_query(
                "test-udid", "accessibility id", "Back",
            )

            assert len(result) == 1
            assert result[0]["AXLabel"] == "Back"

    async def test_timeout_returns_empty(self):
        backend = _make_session_backend()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=httpx.ReadTimeout("hung"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await backend.find_elements_by_query(
                "test-udid", "class chain", "**/XCUIElementTypeTabBar",
            )

            assert result == []

    async def test_non_200_returns_empty(self):
        backend = _make_session_backend()

        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.json.return_value = {"value": {"error": "no such element"}}

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await backend.find_elements_by_query(
                "test-udid", "class chain", "**/XCUIElementTypeAlert",
            )

            assert result == []

    async def test_custom_timeout(self):
        backend = _make_session_backend()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"value": []}

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await backend.find_elements_by_query(
                "test-udid", "class chain", "**/XCUIElementTypeTabBar",
                timeout=2.0,
            )

            call_args = mock_client.post.call_args
            assert call_args[1]["timeout"] == 2.0

    async def test_element_type_from_response(self):
        """When element has 'type' field, use it instead of query value."""
        backend = _make_session_backend()

        el_with_type = {
            "ELEMENT": "uuid-1",
            "type": "XCUIElementTypeButton",
            "label": "OK",
            "rect": {"x": 0, "y": 0, "width": 80, "height": 44},
        }

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"value": [el_with_type]}

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await backend.find_elements_by_query(
                "test-udid", "class chain", "**/XCUIElementTypeTabBar/XCUIElementTypeButton",
            )

            assert result[0]["type"] == "Button"

    async def test_class_chain_fallback_strips_prefix(self):
        """When element has no 'type' field, class chain value like **/XCUIElementTypeTabBar is stripped."""
        backend = _make_session_backend()

        el_without_type = {
            "ELEMENT": "uuid-1",
            "label": "Tab Bar",
            "rect": {"x": 0, "y": 808, "width": 393, "height": 44},
        }

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"value": [el_without_type]}

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await backend.find_elements_by_query(
                "test-udid", "class chain", "**/XCUIElementTypeTabBar",
            )

            # Should be "TabBar", not "**/XCUIElementTypeTabBar"
            assert result[0]["type"] == "TabBar"


# ---------------------------------------------------------------------------
# build_screen_skeleton tests
# ---------------------------------------------------------------------------


class TestBuildScreenSkeleton:
    async def test_containers_and_children(self):
        """Skeleton returns containers + descendant buttons via class chain queries."""
        backend = _make_session_backend()
        depth_calls = []

        async def mock_set_depth(udid, depth):
            depth_calls.append(depth)

        async def mock_find(udid, using, value, *, scope_element_id=None, timeout=None):
            # Phase 1: container queries
            if value == "**/XCUIElementTypeTabBar":
                return [{"type": "TabBar", "AXLabel": "", "frame": {"x": 0, "y": 808, "width": 393, "height": 44},
                         "AXUniqueId": "", "AXValue": None, "enabled": True, "role": "", "role_description": "",
                         "_wda_element_id": "tabbar-uuid"}]
            if value == "**/XCUIElementTypeNavigationBar":
                return [{"type": "NavigationBar", "AXLabel": "Map", "frame": {"x": 0, "y": 0, "width": 393, "height": 44},
                         "AXUniqueId": "Map", "AXValue": None, "enabled": True, "role": "", "role_description": "",
                         "_wda_element_id": "navbar-uuid"}]
            # Phase 2: unscoped class chain child queries
            if value == "**/XCUIElementTypeTabBar/**/XCUIElementTypeButton":
                return [
                    {"type": "Button", "AXLabel": "Home", "frame": {"x": 2, "y": 808, "width": 96, "height": 44},
                     "AXUniqueId": "Home", "AXValue": None, "enabled": True, "role": "", "role_description": "",
                     "_wda_element_id": "btn-1"},
                    {"type": "Button", "AXLabel": "Search", "frame": {"x": 100, "y": 808, "width": 96, "height": 44},
                     "AXUniqueId": "Search", "AXValue": None, "enabled": True, "role": "", "role_description": "",
                     "_wda_element_id": "btn-2"},
                ]
            if value == "**/XCUIElementTypeNavigationBar/**/XCUIElementTypeButton":
                return [{"type": "Button", "AXLabel": "Back", "frame": {"x": 0, "y": 0, "width": 44, "height": 44},
                         "AXUniqueId": "Back", "AXValue": None, "enabled": True, "role": "", "role_description": "",
                         "_wda_element_id": "btn-3"}]
            return []

        with patch.object(backend, "find_elements_by_query", side_effect=mock_find), \
             patch.object(backend, "_set_snapshot_depth", side_effect=mock_set_depth):
            result = await backend.build_screen_skeleton("test-udid")

        # 2 containers + 3 buttons = 5
        assert len(result) == 5
        types = [el["type"] for el in result]
        assert "TabBar" in types
        assert "NavigationBar" in types
        assert types.count("Button") == 3
        labels = [el["AXLabel"] for el in result]
        assert "Home" in labels
        assert "Search" in labels
        assert "Back" in labels
        # _wda_element_id should be stripped
        for el in result:
            assert "_wda_element_id" not in el
        # Depth bumped to 50 before Phase 2, restored to SNAPSHOT_MAX_DEPTH after
        assert depth_calls == [50, SNAPSHOT_MAX_DEPTH]

    async def test_dedup_by_wda_id(self):
        """Children with the same WDA element ID are deduped."""
        backend = _make_session_backend()

        async def mock_find(udid, using, value, *, scope_element_id=None, timeout=None):
            if value == "**/XCUIElementTypeTabBar":
                return [{"type": "TabBar", "AXLabel": "", "frame": {"x": 0, "y": 808, "width": 393, "height": 44},
                         "AXUniqueId": "", "AXValue": None, "enabled": True, "role": "", "role_description": "",
                         "_wda_element_id": "tabbar-uuid"}]
            if value == "**/XCUIElementTypeNavigationBar":
                return [{"type": "NavigationBar", "AXLabel": "", "frame": {"x": 0, "y": 0, "width": 393, "height": 44},
                         "AXUniqueId": "", "AXValue": None, "enabled": True, "role": "", "role_description": "",
                         "_wda_element_id": "navbar-uuid"}]
            # Both container child queries return same element (e.g. shared button)
            if "/**/XCUIElementTypeButton" in value:
                return [{"type": "Button", "AXLabel": "Home", "frame": {"x": 2, "y": 808, "width": 96, "height": 44},
                         "AXUniqueId": "Home", "AXValue": None, "enabled": True, "role": "", "role_description": "",
                         "_wda_element_id": "btn-1"}]
            return []

        with patch.object(backend, "find_elements_by_query", side_effect=mock_find), \
             patch.object(backend, "_set_snapshot_depth", new_callable=AsyncMock):
            result = await backend.build_screen_skeleton("test-udid")

        # 2 containers + 1 child (btn-1 deduped across TabBar and NavBar queries)
        assert len(result) == 3
        assert result[0]["type"] == "TabBar"
        assert result[1]["type"] == "NavigationBar"
        assert result[2]["type"] == "Button"

    async def test_partial_failures(self):
        """Missing containers (e.g. no Alert) are gracefully skipped."""
        backend = _make_session_backend()

        async def mock_find(udid, using, value, *, scope_element_id=None, timeout=None):
            if value == "**/XCUIElementTypeTabBar":
                return [{"type": "TabBar", "AXLabel": "", "frame": {"x": 0, "y": 808, "width": 393, "height": 44},
                         "AXUniqueId": "", "AXValue": None, "enabled": True, "role": "", "role_description": "",
                         "_wda_element_id": "tabbar-uuid"}]
            if "/**/XCUIElementTypeButton" in value:
                return [{"type": "Button", "AXLabel": "Tab1", "frame": {"x": 0, "y": 808, "width": 96, "height": 44},
                         "AXUniqueId": "", "AXValue": None, "enabled": True, "role": "", "role_description": "",
                         "_wda_element_id": "btn-1"}]
            return []

        with patch.object(backend, "find_elements_by_query", side_effect=mock_find), \
             patch.object(backend, "_set_snapshot_depth", new_callable=AsyncMock):
            result = await backend.build_screen_skeleton("test-udid")

        # 1 container + 1 button
        assert len(result) == 2
        assert result[0]["type"] == "TabBar"
        assert result[1]["type"] == "Button"

    async def test_empty_screen(self):
        """No containers found — returns empty list."""
        backend = _make_session_backend()

        async def mock_find(udid, using, value, *, scope_element_id=None, timeout=None):
            return []

        with patch.object(backend, "find_elements_by_query", side_effect=mock_find), \
             patch.object(backend, "_set_snapshot_depth", new_callable=AsyncMock):
            result = await backend.build_screen_skeleton("test-udid")

        assert result == []

    async def test_exception_in_container_query(self):
        """Exception in one container query doesn't break others."""
        backend = _make_session_backend()

        async def mock_find(udid, using, value, *, scope_element_id=None, timeout=None):
            if "TabBar" in value and "Button" not in value:
                raise httpx.ReadTimeout("hung")
            if value == "**/XCUIElementTypeNavigationBar":
                return [{"type": "NavigationBar", "AXLabel": "Map", "frame": {"x": 0, "y": 0, "width": 393, "height": 44},
                         "AXUniqueId": "", "AXValue": None, "enabled": True, "role": "", "role_description": "",
                         "_wda_element_id": "nav-uuid"}]
            if value == "**/XCUIElementTypeNavigationBar/**/XCUIElementTypeButton":
                return [{"type": "Button", "AXLabel": "Back", "frame": {"x": 0, "y": 0, "width": 44, "height": 44},
                         "AXUniqueId": "", "AXValue": None, "enabled": True, "role": "", "role_description": "",
                         "_wda_element_id": "btn-1"}]
            return []

        with patch.object(backend, "find_elements_by_query", side_effect=mock_find), \
             patch.object(backend, "_set_snapshot_depth", new_callable=AsyncMock):
            result = await backend.build_screen_skeleton("test-udid")

        # Should still have NavigationBar + its button despite TabBar failure
        assert len(result) == 2
        assert result[0]["type"] == "NavigationBar"
        assert result[1]["type"] == "Button"

    async def test_depth_restored_on_child_query_failure(self):
        """snapshotMaxDepth is restored even when all child queries fail."""
        backend = _make_session_backend()
        depth_calls = []

        async def mock_set_depth(udid, depth):
            depth_calls.append(depth)

        async def mock_find(udid, using, value, *, scope_element_id=None, timeout=None):
            if value == "**/XCUIElementTypeTabBar":
                return [{"type": "TabBar", "AXLabel": "", "frame": {"x": 0, "y": 808, "width": 393, "height": 44},
                         "AXUniqueId": "", "AXValue": None, "enabled": True, "role": "", "role_description": "",
                         "_wda_element_id": "tabbar-uuid"}]
            if "/**/XCUIElementTypeButton" in value:
                raise httpx.ReadTimeout("hung")
            return []

        with patch.object(backend, "find_elements_by_query", side_effect=mock_find), \
             patch.object(backend, "_set_snapshot_depth", side_effect=mock_set_depth):
            result = await backend.build_screen_skeleton("test-udid")

        # Container present, child query failed
        assert len(result) == 1
        assert result[0]["type"] == "TabBar"
        # Depth still restored
        assert depth_calls == [50, SNAPSHOT_MAX_DEPTH]


# ---------------------------------------------------------------------------
# Session setup settings tests
# ---------------------------------------------------------------------------


class TestSessionSetupSettings:
    async def test_session_setup_includes_compact_response_settings(self):
        """_ensure_session should POST settings with compact responses off."""
        backend = WdaBackend()
        mock_proc = MagicMock()
        mock_proc.returncode = None
        backend._connections["test-udid"] = MagicMock(
            base_url="http://localhost:8100",
            forward_proc=mock_proc,
            session_id=None,
        )

        # Track posted settings
        posted_settings = {}

        async def mock_post(url, **kwargs):
            if "/session" in url and "/appium/settings" not in url:
                return MagicMock(
                    status_code=200,
                    json=MagicMock(return_value={"sessionId": "new-sess", "value": {"sessionId": "new-sess"}}),
                )
            if "/appium/settings" in url:
                posted_settings.update(kwargs.get("json", {}).get("settings", {}))
                return MagicMock(status_code=200)
            return MagicMock(status_code=200)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=mock_post)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            session_id = await backend._ensure_session("test-udid")

            assert session_id == "new-sess"
            assert posted_settings.get("snapshotMaxDepth") == SNAPSHOT_MAX_DEPTH
            assert posted_settings.get("shouldUseCompactResponses") is False
            assert posted_settings.get("elementResponseAttributes") == _ELEMENT_RESPONSE_ATTRIBUTES


# ---------------------------------------------------------------------------
# Describe all skeleton fallback integration tests
# ---------------------------------------------------------------------------


class TestDescribeAllSkeletonFallback:
    async def test_source_timeout_calls_skeleton(self):
        """/source timeout → build_screen_skeleton called."""
        backend = _make_session_backend()

        mock_status = MagicMock(status_code=200)

        async def mock_get(url, **kwargs):
            if "/source" in url:
                raise httpx.ReadTimeout("timed out")
            return mock_status

        skeleton_result = [
            {"type": "TabBar", "AXLabel": "", "AXUniqueId": "", "AXValue": None,
             "frame": {"x": 0, "y": 808, "width": 393, "height": 44},
             "enabled": True, "role": "", "role_description": ""},
            {"type": "Button", "AXLabel": "Home", "AXUniqueId": "Home", "AXValue": None,
             "frame": {"x": 2, "y": 808, "width": 96, "height": 44},
             "enabled": True, "role": "", "role_description": ""},
        ]

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=mock_get)
            mock_client.post = AsyncMock(return_value=MagicMock(status_code=200, json=MagicMock(return_value={"value": []})))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with patch.object(backend, "build_screen_skeleton", new_callable=AsyncMock, return_value=skeleton_result):
                result = await backend.describe_all("test-udid")

                assert len(result) == 2
                assert result[0]["type"] == "TabBar"
                assert result[1]["AXLabel"] == "Home"


# ---------------------------------------------------------------------------
# WDA error parsing tests
# ---------------------------------------------------------------------------


def _mock_response(status_code: int, json_body: dict | None = None, text: str = "") -> MagicMock:
    """Helper: create a mock httpx.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text or str(json_body)
    if json_body is not None:
        resp.json = MagicMock(return_value=json_body)
    else:
        resp.json = MagicMock(side_effect=ValueError("No JSON"))
    return resp


class TestParseWdaError:
    def test_200_returns_none(self):
        resp = _mock_response(200, {"value": {}})
        assert _parse_wda_error(resp, "test-udid") is None

    def test_invalid_session(self):
        resp = _mock_response(404, {
            "value": {"error": "invalid session id", "message": "Session does not exist"},
        })
        error = _parse_wda_error(resp, "test-udid")
        assert isinstance(error, WdaInvalidSessionError)
        assert error.wda_error == "invalid session id"

    def test_no_such_element(self):
        resp = _mock_response(404, {
            "value": {"error": "no such element", "message": "unable to find element"},
        })
        error = _parse_wda_error(resp, "test-udid")
        assert isinstance(error, WdaElementNotFoundError)

    def test_stale_element(self):
        resp = _mock_response(404, {
            "value": {"error": "stale element reference", "message": "Element is stale"},
        })
        error = _parse_wda_error(resp, "test-udid")
        assert isinstance(error, WdaStaleElementError)

    def test_element_not_interactable(self):
        resp = _mock_response(400, {
            "value": {"error": "element not interactable", "message": "Element is not hittable"},
        })
        error = _parse_wda_error(resp, "test-udid")
        assert isinstance(error, WdaElementNotInteractableError)

    def test_keyboard_not_present(self):
        resp = _mock_response(400, {
            "value": {"error": "invalid element state", "message": "No keyboard is present"},
        })
        error = _parse_wda_error(resp, "test-udid")
        assert isinstance(error, WdaKeyboardNotPresentError)

    def test_invalid_element_state_non_keyboard(self):
        resp = _mock_response(400, {
            "value": {"error": "invalid element state", "message": "Element is not enabled"},
        })
        error = _parse_wda_error(resp, "test-udid")
        assert isinstance(error, WdaElementNotInteractableError)

    def test_app_crashed(self):
        resp = _mock_response(500, {
            "value": {"error": "unknown error", "message": "Application crash detected"},
        })
        error = _parse_wda_error(resp, "test-udid")
        assert isinstance(error, WdaAppCrashedError)

    def test_non_json_body(self):
        resp = _mock_response(502, json_body=None, text="Bad Gateway")
        error = _parse_wda_error(resp, "test-udid")
        assert isinstance(error, WdaError)
        assert not isinstance(error, WdaInvalidSessionError)
        assert "502" in str(error)

    def test_unknown_w3c_error(self):
        resp = _mock_response(500, {
            "value": {"error": "some new error", "message": "Something unexpected"},
        })
        error = _parse_wda_error(resp, "test-udid")
        assert isinstance(error, WdaError)
        assert error.wda_error == "some new error"


class TestRequestRaisesWdaError:
    async def test_request_raises_specific_error(self):
        backend = _make_session_backend()

        mock_response = _mock_response(404, {
            "value": {"error": "no such element", "message": "unable to find element"},
        })

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with pytest.raises(WdaElementNotFoundError):
                await backend._request("get", "test-udid", "/element")

    async def test_tap_raises_wda_error(self):
        backend = _make_session_backend()

        mock_response = _mock_response(400, {
            "value": {"error": "invalid element state", "message": "No keyboard is present"},
        })

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with pytest.raises(WdaKeyboardNotPresentError):
                await backend.tap("test-udid", 100, 200)

    async def test_backward_compat_except_device_error(self):
        """WdaError subclasses are caught by except DeviceError."""
        backend = _make_session_backend()

        mock_response = _mock_response(404, {
            "value": {"error": "no such element", "message": "unable to find element"},
        })

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with pytest.raises(DeviceError):
                await backend._request("get", "test-udid", "/element")


# ---------------------------------------------------------------------------
# Session auto-recovery tests
# ---------------------------------------------------------------------------


def _invalid_session_response() -> MagicMock:
    """Helper: mock httpx.Response for an invalid session error."""
    return _mock_response(404, {
        "value": {"error": "invalid session id", "message": "Session does not exist"},
    })


def _ok_response(json_body: dict | None = None) -> MagicMock:
    """Helper: mock httpx.Response for a 200 OK."""
    return _mock_response(200, json_body or {"value": {}})


class TestSessionAutoRecovery:
    async def test_request_retries_on_invalid_session(self):
        """Recovery succeeds: first call gets invalid session, retry with new session works."""
        backend = _make_session_backend()

        # First call: invalid session. Second call (after recovery): success.
        call_count = 0

        async def mock_get(url, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _invalid_session_response()
            return _ok_response()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=mock_get)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            # Mock _ensure_session to provide a new session on retry
            original_ensure = backend._ensure_session

            async def patched_ensure(udid):
                conn = backend._connections.get(udid)
                if conn and conn.session_id:
                    return conn.session_id
                # Simulate creating a new session
                conn.session_id = "new-session"
                return "new-session"

            with patch.object(backend, "_ensure_session", side_effect=patched_ensure):
                resp = await backend._request(
                    "get", "test-udid", "/element", use_session=True,
                )

            assert resp.status_code == 200
            assert call_count == 2
            assert backend._connections["test-udid"].session_id == "new-session"

    async def test_request_does_not_retry_twice(self):
        """No infinite loops: if retry also gets invalid session, raises."""
        backend = _make_session_backend()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=_invalid_session_response())
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            async def patched_ensure(udid):
                conn = backend._connections.get(udid)
                if conn and conn.session_id:
                    return conn.session_id
                conn.session_id = "new-session"
                return "new-session"

            with patch.object(backend, "_ensure_session", side_effect=patched_ensure):
                with pytest.raises(WdaInvalidSessionError):
                    await backend._request(
                        "get", "test-udid", "/element", use_session=True,
                    )

    async def test_request_no_retry_without_use_session(self):
        """Non-session requests don't attempt recovery."""
        backend = _make_session_backend()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=_invalid_session_response())
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with pytest.raises(WdaInvalidSessionError):
                await backend._request("get", "test-udid", "/status")

            # Session should NOT have been cleared (no recovery attempted)
            assert backend._connections["test-udid"].session_id == "test-session"

    async def test_session_recovery_clears_depth_cache(self):
        """Depth cache is cleared so the new session gets WDA settings reapplied."""
        backend = _make_session_backend()
        backend._current_depth["test-udid"] = 25

        call_count = 0

        async def mock_get(url, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _invalid_session_response()
            return _ok_response()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=mock_get)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            async def patched_ensure(udid):
                conn = backend._connections.get(udid)
                if conn and conn.session_id:
                    return conn.session_id
                conn.session_id = "new-session"
                return "new-session"

            with patch.object(backend, "_ensure_session", side_effect=patched_ensure):
                await backend._request(
                    "get", "test-udid", "/element", use_session=True,
                )

            assert "test-udid" not in backend._current_depth

    async def test_find_elements_recovers_from_invalid_session(self):
        """Element queries get session recovery via _request()."""
        backend = _make_session_backend()

        call_count = 0

        async def mock_post(url, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _invalid_session_response()
            return _mock_response(200, {"value": [WDA_TABBAR_ELEMENT]})

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=mock_post)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            async def patched_ensure(udid):
                conn = backend._connections.get(udid)
                if conn and conn.session_id:
                    return conn.session_id
                conn.session_id = "new-session"
                return "new-session"

            with patch.object(backend, "_ensure_session", side_effect=patched_ensure):
                result = await backend.find_elements_by_query(
                    "test-udid", "class chain", "**/XCUIElementTypeTabBar",
                )

            assert len(result) == 1
            assert call_count == 2

    async def test_find_elements_returns_empty_on_transport_error(self):
        """Graceful [] on DeviceError (backward compat)."""
        backend = _make_session_backend()

        with patch.object(
            backend, "_request",
            new_callable=AsyncMock,
            side_effect=DeviceError("connection lost", tool="wda"),
        ):
            result = await backend.find_elements_by_query(
                "test-udid", "class chain", "**/XCUIElementTypeTabBar",
            )

            assert result == []

    async def test_find_elements_scoped_query_path(self):
        """Scoped queries build /element/{id}/elements path."""
        backend = _make_session_backend()

        mock_resp = _mock_response(200, {"value": WDA_TABBAR_BUTTONS})

        with patch.object(backend, "_request", new_callable=AsyncMock, return_value=mock_resp) as mock_req:
            await backend.find_elements_by_query(
                "test-udid", "class name", "XCUIElementTypeButton",
                scope_element_id="tabbar-uuid-001",
            )

            call_args = mock_req.call_args
            assert call_args[0][2] == "/element/tabbar-uuid-001/elements"
            assert call_args[1]["use_session"] is True

    async def test_find_elements_unscoped_query_path(self):
        """Unscoped queries build /elements path."""
        backend = _make_session_backend()

        mock_resp = _mock_response(200, {"value": []})

        with patch.object(backend, "_request", new_callable=AsyncMock, return_value=mock_resp) as mock_req:
            await backend.find_elements_by_query(
                "test-udid", "class chain", "**/XCUIElementTypeTabBar",
            )

            call_args = mock_req.call_args
            assert call_args[0][2] == "/elements"
            assert call_args[1]["use_session"] is True

    async def test_concurrent_session_recovery(self):
        """Multiple parallel queries with invalid session → only 1 session creation.

        Uses the real _ensure_session locking to verify that concurrent recovery
        serializes session creation properly.
        """
        backend = _make_session_backend()
        creation_count = 0

        # Track which HTTP calls have been made per "session"
        # First batch (with test-session) all get invalid session.
        # Second batch (with recovered-session) succeed.
        def make_post_response(url, **kwargs):
            resp = MagicMock()
            if "test-session" in url:
                resp.status_code = 404
                resp.text = '{"value": {"error": "invalid session id", "message": "gone"}}'
                resp.json = MagicMock(return_value={
                    "value": {"error": "invalid session id", "message": "gone"},
                })
            elif "/appium/settings" in url:
                resp.status_code = 200
                resp.text = '{}'
                resp.json = MagicMock(return_value={})
            elif url.endswith("/session"):
                # Session creation POST /session
                nonlocal creation_count
                creation_count += 1
                resp.status_code = 200
                resp.text = '{"sessionId": "recovered-session"}'
                resp.json = MagicMock(return_value={
                    "sessionId": "recovered-session",
                    "value": {"sessionId": "recovered-session"},
                })
            else:
                # Element query with recovered session
                resp.status_code = 200
                resp.text = '{"value": []}'
                resp.json = MagicMock(return_value={"value": []})
            return resp

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=make_post_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            tasks = [
                backend.find_elements_by_query(
                    "test-udid", "class chain", "**/XCUIElementTypeButton",
                )
                for _ in range(5)
            ]
            results = await asyncio.gather(*tasks)

        # All tasks should complete
        assert len(results) == 5
        # Session should only be created once (lock serializes, others see cached)
        assert creation_count == 1


# ---------------------------------------------------------------------------
# ElementSelector DSL tests
# ---------------------------------------------------------------------------


class TestElementSelector:
    """Tests for the ElementSelector chainable query builder."""

    async def test_find_returns_all_matches(self):
        backend = _make_session_backend()
        mock_resp = _mock_response(200, {"value": WDA_TABBAR_BUTTONS})

        with patch.object(backend, "_request", new_callable=AsyncMock, return_value=mock_resp):
            results = await backend.element("test-udid", type="Button").find()

        assert len(results) == 2
        assert results[0]["AXLabel"] == "Home"
        assert results[1]["AXLabel"] == "Search"

    async def test_find_returns_empty_on_no_match(self):
        backend = _make_session_backend()
        mock_resp = _mock_response(200, {"value": []})

        with patch.object(backend, "_request", new_callable=AsyncMock, return_value=mock_resp):
            results = await backend.element("test-udid", name="NonExistent").find()

        assert results == []

    async def test_get_returns_first_match(self):
        backend = _make_session_backend()
        mock_resp = _mock_response(200, {"value": WDA_TABBAR_BUTTONS})

        with patch.object(backend, "_request", new_callable=AsyncMock, return_value=mock_resp):
            el = await backend.element("test-udid", type="Button").get()

        assert el["AXLabel"] == "Home"

    async def test_get_raises_on_no_match(self):
        backend = _make_session_backend()
        mock_resp = _mock_response(200, {"value": []})

        with patch.object(backend, "_request", new_callable=AsyncMock, return_value=mock_resp):
            with pytest.raises(WdaElementNotFoundError, match="No element found"):
                await backend.element("test-udid", name="Ghost").get()

    async def test_tap_hits_element_center(self):
        """tap() finds the element and taps its center coordinates."""
        backend = _make_session_backend()

        # Element at (100, 200) with size (120, 44) → center (160, 222)
        el_response = [{
            "ELEMENT": "btn-001",
            "element-6066-11e4-a52e-4f735466cecf": "btn-001",
            "type": "XCUIElementTypeButton",
            "label": "Login",
            "name": "loginButton",
            "rect": {"x": 100, "y": 200, "width": 120, "height": 44},
            "isEnabled": True,
            "value": None,
        }]

        find_resp = _mock_response(200, {"value": el_response})
        tap_resp = _mock_response(200, {"value": {}})

        call_log = []

        async def mock_request(method, udid, path, **kwargs):
            call_log.append((method, path, kwargs.get("json")))
            if path == "/elements":
                return find_resp
            return tap_resp

        with patch.object(backend, "_request", new_callable=AsyncMock, side_effect=mock_request):
            el = await backend.element("test-udid", name="loginButton").tap()

        assert el["AXLabel"] == "Login"
        # Second call should be the tap
        assert call_log[1][0] == "post"
        assert call_log[1][1] == "/wda/tap"
        assert call_log[1][2]["x"] == pytest.approx(160.0)
        assert call_log[1][2]["y"] == pytest.approx(222.0)

    async def test_clear_uses_wda_element_id(self):
        """clear() uses the native /element/{id}/clear endpoint when element has a WDA ID."""
        backend = _make_session_backend()

        el_response = [{
            "ELEMENT": "field-001",
            "element-6066-11e4-a52e-4f735466cecf": "field-001",
            "type": "XCUIElementTypeTextField",
            "label": "Email",
            "name": "emailField",
            "rect": {"x": 20, "y": 300, "width": 300, "height": 44},
            "isEnabled": True,
            "value": "old text",
        }]

        find_resp = _mock_response(200, {"value": el_response})
        clear_resp = _mock_response(200, {"value": {}})

        call_log = []

        async def mock_request(method, udid, path, **kwargs):
            call_log.append((method, path))
            if path == "/elements":
                return find_resp
            return clear_resp

        with patch.object(backend, "_request", new_callable=AsyncMock, side_effect=mock_request):
            await backend.element("test-udid", name="emailField").clear()

        assert call_log[1] == ("post", "/element/field-001/clear")

    async def test_wait_returns_when_element_appears(self):
        """wait() polls until the element appears."""
        backend = _make_session_backend()

        call_count = 0

        async def mock_find(udid, using, value, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return []
            return [{"AXLabel": "Done", "frame": {"x": 0, "y": 0, "width": 100, "height": 44}}]

        with patch.object(backend, "find_elements_by_query", new_callable=AsyncMock, side_effect=mock_find):
            el = await backend.element("test-udid", label="Done").wait(timeout=5, interval=0.05)

        assert el["AXLabel"] == "Done"
        assert call_count == 3

    async def test_wait_raises_on_timeout(self):
        """wait() raises WdaElementNotFoundError after timeout."""
        backend = _make_session_backend()

        with patch.object(
            backend, "find_elements_by_query",
            new_callable=AsyncMock, return_value=[],
        ):
            with pytest.raises(WdaElementNotFoundError, match="Timed out"):
                await backend.element("test-udid", name="Never").wait(timeout=0.1, interval=0.03)

    async def test_name_uses_accessibility_id_strategy(self):
        """name= alone uses 'accessibility id' (fastest strategy)."""
        backend = _make_session_backend()

        with patch.object(
            backend, "find_elements_by_query",
            new_callable=AsyncMock, return_value=[],
        ) as mock_query:
            await backend.element("test-udid", name="loginButton").find()

        mock_query.assert_called_once_with(
            "test-udid", "accessibility id", "loginButton",
            scope_element_id=None, timeout=None,
        )

    async def test_label_uses_predicate_string(self):
        """label= uses 'predicate string' with case-insensitive match."""
        backend = _make_session_backend()

        with patch.object(
            backend, "find_elements_by_query",
            new_callable=AsyncMock, return_value=[],
        ) as mock_query:
            await backend.element("test-udid", label="Submit").find()

        mock_query.assert_called_once_with(
            "test-udid", "predicate string", "label ==[c] 'Submit'",
            scope_element_id=None, timeout=None,
        )

    async def test_type_adds_xcui_prefix(self):
        """type= adds XCUIElementType prefix automatically."""
        backend = _make_session_backend()

        with patch.object(
            backend, "find_elements_by_query",
            new_callable=AsyncMock, return_value=[],
        ) as mock_query:
            await backend.element("test-udid", type="Button").find()

        mock_query.assert_called_once_with(
            "test-udid", "predicate string", "type == 'XCUIElementTypeButton'",
            scope_element_id=None, timeout=None,
        )

    async def test_combined_criteria_uses_and_predicate(self):
        """Multiple criteria combine with AND in predicate string."""
        backend = _make_session_backend()

        with patch.object(
            backend, "find_elements_by_query",
            new_callable=AsyncMock, return_value=[],
        ) as mock_query:
            await backend.element("test-udid", name="login", label="Log In", type="Button").find()

        args = mock_query.call_args[0]
        assert args[1] == "predicate string"
        assert "name == 'login'" in args[2]
        assert "label ==[c] 'Log In'" in args[2]
        assert "type == 'XCUIElementTypeButton'" in args[2]

    async def test_class_chain_passthrough(self):
        """class_chain= passes through directly."""
        backend = _make_session_backend()

        with patch.object(
            backend, "find_elements_by_query",
            new_callable=AsyncMock, return_value=[],
        ) as mock_query:
            await backend.element("test-udid", class_chain="**/XCUIElementTypeTabBar").find()

        mock_query.assert_called_once_with(
            "test-udid", "class chain", "**/XCUIElementTypeTabBar",
            scope_element_id=None, timeout=None,
        )

    async def test_predicate_passthrough(self):
        """predicate= passes through directly."""
        backend = _make_session_backend()

        with patch.object(
            backend, "find_elements_by_query",
            new_callable=AsyncMock, return_value=[],
        ) as mock_query:
            await backend.element("test-udid", predicate="value == '1' AND visible == 1").find()

        mock_query.assert_called_once_with(
            "test-udid", "predicate string", "value == '1' AND visible == 1",
            scope_element_id=None, timeout=None,
        )

    async def test_child_scoped_query(self):
        """child() resolves parent then queries within its scope."""
        backend = _make_session_backend()

        parent_response = _mock_response(200, {"value": [WDA_TABBAR_ELEMENT]})
        child_response = _mock_response(200, {"value": WDA_TABBAR_BUTTONS})

        call_log = []

        async def mock_request(method, udid, path, **kwargs):
            call_log.append((method, path, kwargs.get("json")))
            if "/element/" in path:
                return child_response
            return parent_response

        with patch.object(backend, "_request", new_callable=AsyncMock, side_effect=mock_request):
            children = await backend.element(
                "test-udid", type="TabBar",
            ).child(type="Button").find()

        assert len(children) == 2
        # First call: find parent (unscoped /elements)
        assert call_log[0][1] == "/elements"
        # Second call: find children scoped to parent
        assert call_log[1][1] == "/element/tabbar-uuid-001/elements"

    async def test_child_raises_when_parent_not_found(self):
        """child() raises when parent element doesn't exist."""
        backend = _make_session_backend()
        mock_resp = _mock_response(200, {"value": []})

        with patch.object(backend, "_request", new_callable=AsyncMock, return_value=mock_resp):
            with pytest.raises(WdaElementNotFoundError, match="No element found"):
                await backend.element(
                    "test-udid", name="NonExistent",
                ).child(type="Button").find()

    async def test_no_criteria_raises_valueerror(self):
        """ElementSelector with no criteria raises ValueError on execute."""
        backend = _make_session_backend()

        with pytest.raises(ValueError, match="at least one criterion"):
            await backend.element("test-udid").find()

    async def test_label_escapes_quotes(self):
        """Single quotes in label values are escaped for NSPredicate."""
        backend = _make_session_backend()

        with patch.object(
            backend, "find_elements_by_query",
            new_callable=AsyncMock, return_value=[],
        ) as mock_query:
            await backend.element("test-udid", label="It's here").find()

        args = mock_query.call_args[0]
        assert args[2] == "label ==[c] 'It\\'s here'"


# ---------------------------------------------------------------------------
# Connection auto-recovery tests
# ---------------------------------------------------------------------------


class TestConnectionRecovery:
    """Tests for transparent connection recovery on transport errors."""

    async def test_connection_retry_on_connect_error(self):
        """ConnectError → reconnect → success."""
        backend = _make_session_backend()
        call_count = 0

        async def mock_get(url, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.ConnectError("Connection refused")
            return _ok_response()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=mock_get)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with patch.object(backend, "_get_base_url",
                              return_value="http://localhost:8100"):
                resp = await backend._request("get", "test-udid", "/status")

        assert resp.status_code == 200
        assert call_count == 2

    async def test_connection_retry_on_read_error(self):
        """ReadError → reconnect → success."""
        backend = _make_session_backend()
        call_count = 0

        async def mock_get(url, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.ReadError("Connection reset")
            return _ok_response()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=mock_get)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with patch.object(backend, "_get_base_url",
                              return_value="http://localhost:8100"):
                resp = await backend._request("get", "test-udid", "/status")

        assert resp.status_code == 200
        assert call_count == 2

    async def test_no_double_connection_retry(self):
        """Both attempts fail → raises DeviceError (no infinite loop)."""
        backend = _make_session_backend()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with patch.object(backend, "_get_base_url",
                              return_value="http://localhost:8100"):
                with pytest.raises(DeviceError, match="after reconnect attempt"):
                    await backend._request("get", "test-udid", "/status")

    async def test_timeout_passthrough_no_connection_retry(self):
        """raise_on_timeout=True + TimeoutException → no retry, raw exception."""
        backend = _make_session_backend()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(
                side_effect=httpx.TimeoutException("timed out"),
            )
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with pytest.raises(httpx.TimeoutException):
                await backend._request(
                    "get", "test-udid", "/status", raise_on_timeout=True,
                )

            # Connection should NOT have been popped (timeout passthrough)
            assert "test-udid" in backend._connections

    async def test_connection_retry_clears_depth_cache(self):
        """_current_depth cleared so new session gets settings reapplied."""
        backend = _make_session_backend()
        backend._current_depth["test-udid"] = 25
        call_count = 0

        async def mock_get(url, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.ConnectError("refused")
            return _ok_response()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=mock_get)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with patch.object(backend, "_get_base_url",
                              return_value="http://localhost:8100"):
                await backend._request("get", "test-udid", "/status")

        assert "test-udid" not in backend._current_depth

    async def test_connection_then_session_recovery_chains(self):
        """ConnectError → reconnect → invalid session → session recovery → success.

        Verifies that connection and session recovery chain properly:
        3 HTTP attempts total.
        """
        backend = _make_session_backend()
        call_count = 0

        async def mock_get(url, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.ConnectError("refused")
            if call_count == 2:
                return _invalid_session_response()
            return _ok_response()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=mock_get)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            async def patched_ensure(udid):
                conn = backend._connections.get(udid)
                if conn and conn.session_id:
                    return conn.session_id
                conn = MagicMock(
                    base_url="http://localhost:8100",
                    session_id="new-session",
                )
                backend._connections[udid] = conn
                return "new-session"

            with patch.object(backend, "_ensure_session", side_effect=patched_ensure):
                with patch.object(backend, "_get_base_url",
                                  return_value="http://localhost:8100"):
                    resp = await backend._request(
                        "get", "test-udid", "/element",
                        use_session=True,
                    )

        assert resp.status_code == 200
        assert call_count == 3

    async def test_connection_retry_pops_connection_cache(self):
        """_connections[udid] is removed before the retry."""
        backend = _make_session_backend()
        assert "test-udid" in backend._connections

        popped = False

        async def mock_get(url, **kwargs):
            nonlocal popped
            if not popped:
                popped = True
                raise httpx.ReadError("reset")
            # On retry, verify connection was popped (will be re-added by _get_base_url mock)
            return _ok_response()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=mock_get)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            connections_during_retry = {}

            original_get_base_url = backend._get_base_url

            async def tracking_get_base_url(udid):
                connections_during_retry[udid] = udid in backend._connections
                return "http://localhost:8100"

            with patch.object(backend, "_get_base_url",
                              side_effect=tracking_get_base_url):
                await backend._request("get", "test-udid", "/status")

        # Connection should have been absent when _get_base_url was called on retry
        assert connections_during_retry["test-udid"] is False
