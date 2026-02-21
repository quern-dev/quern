"""Tests for WdaBackend — format conversion, tree flattening, and backend dispatch."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import httpx

from server.device.wda_client import (
    IDLE_TIMEOUT,
    SKELETON_QUERY_TIMEOUT,
    SNAPSHOT_MAX_DEPTH,
    SOURCE_TIMEOUT,
    WdaBackend,
    _ELEMENT_RESPONSE_ATTRIBUTES,
    _SKELETON_CONTAINER_TYPES,
    _map_wda_element,
    _map_wda_element_from_query,
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
