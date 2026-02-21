"""Tests for WdaBackend — format conversion, tree flattening, and backend dispatch."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import httpx

from server.device.wda_client import (
    ELEMENT_QUERY_TIMEOUT,
    IDLE_TIMEOUT,
    SOURCE_TIMEOUT,
    WdaBackend,
    _FALLBACK_ELEMENT_TYPES,
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

    async def test_describe_all_timeout_falls_back_to_element_queries(self):
        """When /source times out but WDA is responsive, use element queries."""
        backend = _make_session_backend()

        # Mock element query responses
        mock_elements_response = MagicMock()
        mock_elements_response.status_code = 200
        mock_elements_response.json.return_value = {"value": [
            {
                "name": "loginButton",
                "label": "Log In",
                "value": None,
                "rect": {"x": 100, "y": 200, "width": 120, "height": 44},
                "isEnabled": True,
            },
        ]}

        # Mock /status response (WDA is responsive)
        mock_status_response = MagicMock()
        mock_status_response.status_code = 200

        call_count = 0

        async def mock_get(url, **kwargs):
            nonlocal call_count
            call_count += 1
            if "/source" in url:
                raise httpx.ReadTimeout("timed out")
            if "/status" in url:
                return mock_status_response
            return mock_status_response

        async def mock_post(url, **kwargs):
            if "/elements" in url:
                return mock_elements_response
            return mock_elements_response

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=mock_get)
            mock_client.post = AsyncMock(side_effect=mock_post)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await backend.describe_all("test-udid")

            # Should have results from element queries
            assert len(result) > 0
            # Each fallback type produces one element (our mock returns 1 per type)
            assert result[0]["AXLabel"] == "Log In"

    async def test_describe_all_timeout_restarts_hung_wda(self):
        """When /source times out AND /status times out, restart WDA then fallback."""
        backend = _make_session_backend()
        backend._device_os_versions["test-udid"] = "iOS 17.4"

        mock_elements_response = MagicMock()
        mock_elements_response.status_code = 200
        mock_elements_response.json.return_value = {"value": []}

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

        async def mock_post(url, **kwargs):
            if "/elements" in url:
                return mock_elements_response
            return MagicMock(status_code=200, json=MagicMock(return_value={"sessionId": "new-session", "value": {"sessionId": "new-session"}}))

        async def fake_stop(udid):
            pass

        async def fake_start(udid, os_version):
            nonlocal restarted
            restarted = True
            # Re-establish connection after restart (simulates WDA coming back)
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
            mock_client.post = AsyncMock(side_effect=mock_post)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with patch("server.device.wda.stop_driver", new_callable=AsyncMock, side_effect=fake_stop) as mock_stop:
                with patch("server.device.wda.start_driver", new_callable=AsyncMock, side_effect=fake_start) as mock_start:
                    result = await backend.describe_all("test-udid")

                    # Driver was restarted
                    mock_stop.assert_called_once_with("test-udid")
                    mock_start.assert_called_once_with("test-udid", "iOS 17.4")

            # Should return (possibly empty) results from fallback
            assert isinstance(result, list)
            assert restarted is True

    async def test_describe_all_nested_timeout_falls_back(self):
        """describe_all_nested also falls back on /source timeout."""
        backend = _make_session_backend()

        mock_elements_response = MagicMock()
        mock_elements_response.status_code = 200
        mock_elements_response.json.return_value = {"value": [
            {
                "name": "settingsSwitch",
                "label": "Dark Mode",
                "value": "1",
                "rect": {"x": 250, "y": 300, "width": 51, "height": 31},
                "isEnabled": True,
            },
        ]}

        mock_status_response = MagicMock()
        mock_status_response.status_code = 200

        async def mock_get(url, **kwargs):
            if "/source" in url:
                raise httpx.ReadTimeout("timed out")
            return mock_status_response

        async def mock_post(url, **kwargs):
            if "/elements" in url:
                return mock_elements_response
            return mock_elements_response

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=mock_get)
            mock_client.post = AsyncMock(side_effect=mock_post)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await backend.describe_all_nested("test-udid")

            # Falls back to flat list
            assert isinstance(result, list)
            assert len(result) > 0


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
        """describe_all() without snapshot_depth should not call /appium/settings."""
        backend = _make_session_backend()

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

            # post should not have been called (no settings update, no session create needed)
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
