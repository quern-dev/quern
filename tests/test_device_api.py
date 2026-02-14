"""Integration tests for device API endpoints.

Uses httpx/ASGITransport against the real FastAPI app with mocked DeviceController.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from server.config import ServerConfig
from server.device.controller import DeviceController
from server.main import create_app
from server.models import AppInfo, DeviceError, DeviceInfo, DeviceState, DeviceType, UIElement


# ---------------------------------------------------------------------------
# Fixtures
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
    )


@pytest.fixture
def app():
    config = ServerConfig(api_key="test-key-12345")
    return create_app(config=config, enable_oslog=False, enable_crash=False, enable_proxy=False)


@pytest.fixture
def auth_headers():
    return {"Authorization": "Bearer test-key-12345"}


@pytest.fixture
def mock_controller(app):
    """Create a DeviceController with all methods mocked."""
    ctrl = DeviceController()
    ctrl._active_udid = "AAAA-1111"
    ctrl.simctl.is_available = AsyncMock(return_value=True)
    ctrl.list_devices = AsyncMock(return_value=[_device()])
    ctrl.check_tools = AsyncMock(return_value={"simctl": True, "idb": False})
    ctrl.boot = AsyncMock(return_value="AAAA-1111")
    ctrl.shutdown = AsyncMock()
    ctrl.install_app = AsyncMock(return_value="AAAA-1111")
    ctrl.launch_app = AsyncMock(return_value="AAAA-1111")
    ctrl.terminate_app = AsyncMock(return_value="AAAA-1111")
    ctrl.list_apps = AsyncMock(return_value=(
        [AppInfo(bundle_id="com.example.App", name="My App", app_type="User")],
        "AAAA-1111",
    ))
    ctrl.screenshot = AsyncMock(return_value=(b"\x89PNGfake", "image/png"))
    # Phase 3b: UI inspection mocks
    _sample_elements = [
        UIElement(type="Application", label="Springboard", frame={"x": 0, "y": 0, "width": 393, "height": 852}),
        UIElement(type="Button", label="Settings", identifier="Settings", frame={"x": 302, "y": 476, "width": 68, "height": 86}),
        UIElement(type="Button", label="Maps", identifier="Maps", frame={"x": 27, "y": 382, "width": 68, "height": 86}),
    ]
    ctrl.get_ui_elements = AsyncMock(return_value=(_sample_elements, "AAAA-1111"))
    ctrl.get_screen_summary = AsyncMock(return_value=(
        {
            "summary": "Springboard screen with 2 buttons.",
            "element_count": 3,
            "element_types": {"Application": 1, "Button": 2},
            "interactive_elements": [
                {"type": "Button", "label": "Settings", "identifier": "Settings"},
                {"type": "Button", "label": "Maps", "identifier": "Maps"},
            ],
        },
        "AAAA-1111",
    ))
    ctrl.tap = AsyncMock(return_value="AAAA-1111")
    ctrl.tap_element = AsyncMock(return_value={
        "status": "ok",
        "tapped": {"label": "Settings", "type": "Button", "identifier": "Settings", "x": 336.0, "y": 519.0},
    })
    ctrl.swipe = AsyncMock(return_value="AAAA-1111")
    ctrl.type_text = AsyncMock(return_value="AAAA-1111")
    ctrl.press_button = AsyncMock(return_value="AAAA-1111")
    ctrl.set_location = AsyncMock(return_value="AAAA-1111")
    ctrl.grant_permission = AsyncMock(return_value="AAAA-1111")
    ctrl.screenshot_annotated = AsyncMock(return_value=(b"\x89PNGannotated", "image/png"))
    app.state.device_controller = ctrl
    return ctrl


# ---------------------------------------------------------------------------
# GET /device/list
# ---------------------------------------------------------------------------


class TestListDevices:
    async def test_list_devices(self, app, auth_headers, mock_controller):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/device/list", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["devices"]) == 1
        assert data["devices"][0]["name"] == "iPhone 16 Pro"
        assert data["tools"]["simctl"] is True
        assert data["active_udid"] == "AAAA-1111"

    async def test_list_devices_error(self, app, auth_headers, mock_controller):
        mock_controller.list_devices = AsyncMock(
            side_effect=DeviceError("simctl failed", tool="simctl")
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/device/list", headers=auth_headers)
        assert resp.status_code == 500

    async def test_list_devices_no_auth(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/device/list")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /device/boot
# ---------------------------------------------------------------------------


class TestBootDevice:
    async def test_boot_by_udid(self, app, auth_headers, mock_controller):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/device/boot",
                json={"udid": "AAAA-1111"},
                headers=auth_headers,
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "booted"
        mock_controller.boot.assert_called_once_with(udid="AAAA-1111", name=None)

    async def test_boot_by_name(self, app, auth_headers, mock_controller):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/device/boot",
                json={"name": "iPhone 16 Pro"},
                headers=auth_headers,
            )
        assert resp.status_code == 200
        mock_controller.boot.assert_called_once_with(udid=None, name="iPhone 16 Pro")

    async def test_boot_no_booted_device_error(self, app, auth_headers, mock_controller):
        mock_controller.boot = AsyncMock(
            side_effect=DeviceError("No booted simulator found", tool="simctl")
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/device/boot",
                json={"udid": "bad"},
                headers=auth_headers,
            )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /device/shutdown
# ---------------------------------------------------------------------------


class TestShutdownDevice:
    async def test_shutdown(self, app, auth_headers, mock_controller):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/device/shutdown",
                json={"udid": "AAAA-1111"},
                headers=auth_headers,
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "shutdown"
        mock_controller.shutdown.assert_called_once_with(udid="AAAA-1111")


# ---------------------------------------------------------------------------
# App management endpoints
# ---------------------------------------------------------------------------


class TestAppEndpoints:
    async def test_install_app(self, app, auth_headers, mock_controller):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/device/app/install",
                json={"app_path": "/path/to/App.app"},
                headers=auth_headers,
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "installed"

    async def test_launch_app(self, app, auth_headers, mock_controller):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/device/app/launch",
                json={"bundle_id": "com.example.App"},
                headers=auth_headers,
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "launched"

    async def test_terminate_app(self, app, auth_headers, mock_controller):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/device/app/terminate",
                json={"bundle_id": "com.example.App"},
                headers=auth_headers,
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "terminated"

    async def test_list_apps(self, app, auth_headers, mock_controller):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/device/app/list",
                headers=auth_headers,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["apps"]) == 1
        assert data["apps"][0]["bundle_id"] == "com.example.App"
        assert data["udid"] == "AAAA-1111"

    async def test_list_apps_with_udid(self, app, auth_headers, mock_controller):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/device/app/list?udid=BBBB-2222",
                headers=auth_headers,
            )
        assert resp.status_code == 200
        mock_controller.list_apps.assert_called_once_with(udid="BBBB-2222")


# ---------------------------------------------------------------------------
# GET /device/screenshot
# ---------------------------------------------------------------------------


class TestScreenshot:
    async def test_screenshot_default(self, app, auth_headers, mock_controller):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/device/screenshot",
                headers=auth_headers,
            )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/png"
        assert resp.content == b"\x89PNGfake"
        mock_controller.screenshot.assert_called_once_with(
            udid=None, format="png", scale=0.5, quality=85,
        )

    async def test_screenshot_jpeg(self, app, auth_headers, mock_controller):
        mock_controller.screenshot = AsyncMock(
            return_value=(b"jpeg-data", "image/jpeg")
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/device/screenshot?format=jpeg&scale=1.0&quality=50",
                headers=auth_headers,
            )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/jpeg"
        mock_controller.screenshot.assert_called_once_with(
            udid=None, format="jpeg", scale=1.0, quality=50,
        )

    async def test_screenshot_error(self, app, auth_headers, mock_controller):
        mock_controller.screenshot = AsyncMock(
            side_effect=DeviceError("No booted simulator found", tool="simctl")
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/device/screenshot",
                headers=auth_headers,
            )
        assert resp.status_code == 400

    async def test_screenshot_invalid_format(self, app, auth_headers, mock_controller):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/device/screenshot?format=gif",
                headers=auth_headers,
            )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Controller not initialized
# ---------------------------------------------------------------------------


class TestControllerNotInitialized:
    async def test_503_when_controller_is_none(self, app, auth_headers):
        # Don't set mock_controller — leave it as None
        app.state.device_controller = None
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/device/list", headers=auth_headers)
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# GET /device/ui (Phase 3b)
# ---------------------------------------------------------------------------


class TestGetUIElements:
    async def test_get_ui_elements(self, app, auth_headers, mock_controller):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/device/ui", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["element_count"] == 3
        assert len(data["elements"]) == 3
        assert data["udid"] == "AAAA-1111"
        mock_controller.get_ui_elements.assert_called_once_with(udid=None)

    async def test_get_ui_elements_with_udid(self, app, auth_headers, mock_controller):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/device/ui?udid=BBBB-2222",
                headers=auth_headers,
            )
        assert resp.status_code == 200
        mock_controller.get_ui_elements.assert_called_once_with(udid="BBBB-2222")

    async def test_get_ui_elements_idb_not_found(self, app, auth_headers, mock_controller):
        mock_controller.get_ui_elements = AsyncMock(
            side_effect=DeviceError(
                "idb not found. Install with: pip install fb-idb",
                tool="idb",
            )
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/device/ui", headers=auth_headers)
        assert resp.status_code == 503

    async def test_get_ui_elements_no_booted(self, app, auth_headers, mock_controller):
        mock_controller.get_ui_elements = AsyncMock(
            side_effect=DeviceError("No booted simulator found", tool="simctl")
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/device/ui", headers=auth_headers)
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# GET /device/screen-summary (Phase 3b)
# ---------------------------------------------------------------------------


class TestScreenSummary:
    async def test_screen_summary(self, app, auth_headers, mock_controller):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/device/screen-summary", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "summary" in data
        assert data["element_count"] == 3
        assert data["udid"] == "AAAA-1111"
        assert "interactive_elements" in data

    async def test_screen_summary_with_udid(self, app, auth_headers, mock_controller):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/device/screen-summary?udid=BBBB-2222",
                headers=auth_headers,
            )
        assert resp.status_code == 200
        mock_controller.get_screen_summary.assert_called_once_with(max_elements=20, udid="BBBB-2222")


# ---------------------------------------------------------------------------
# POST /device/ui/tap-element (Phase 3b)
# ---------------------------------------------------------------------------


class TestTapElement:
    async def test_tap_element_ok(self, app, auth_headers, mock_controller):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/device/ui/tap-element",
                json={"label": "Settings"},
                headers=auth_headers,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["tapped"]["label"] == "Settings"
        mock_controller.tap_element.assert_called_once_with(
            label="Settings", identifier=None, element_type=None, udid=None,
        )

    async def test_tap_element_by_identifier(self, app, auth_headers, mock_controller):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/device/ui/tap-element",
                json={"identifier": "Settings"},
                headers=auth_headers,
            )
        assert resp.status_code == 200
        mock_controller.tap_element.assert_called_once_with(
            label=None, identifier="Settings", element_type=None, udid=None,
        )

    async def test_tap_element_with_type_filter(self, app, auth_headers, mock_controller):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/device/ui/tap-element",
                json={"label": "Calendar", "element_type": "Button"},
                headers=auth_headers,
            )
        assert resp.status_code == 200
        mock_controller.tap_element.assert_called_once_with(
            label="Calendar", identifier=None, element_type="Button", udid=None,
        )

    async def test_tap_element_ambiguous(self, app, auth_headers, mock_controller):
        mock_controller.tap_element = AsyncMock(return_value={
            "status": "ambiguous",
            "matches": [
                {"label": "Calendar", "type": "Button", "identifier": "Calendar-1"},
                {"label": "Calendar", "type": "Button", "identifier": "Calendar-2"},
            ],
            "message": "Found 2 matches, specify element_type or identifier to narrow",
        })
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/device/ui/tap-element",
                json={"label": "Calendar"},
                headers=auth_headers,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ambiguous"
        assert len(data["matches"]) == 2

    async def test_tap_element_not_found(self, app, auth_headers, mock_controller):
        mock_controller.tap_element = AsyncMock(
            side_effect=DeviceError("No element found matching label='Nonexistent'", tool="idb")
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/device/ui/tap-element",
                json={"label": "Nonexistent"},
                headers=auth_headers,
            )
        assert resp.status_code == 404

    async def test_tap_element_idb_not_found(self, app, auth_headers, mock_controller):
        mock_controller.tap_element = AsyncMock(
            side_effect=DeviceError(
                "idb not found. Install with: pip install fb-idb",
                tool="idb",
            )
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/device/ui/tap-element",
                json={"label": "Settings"},
                headers=auth_headers,
            )
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# POST /device/ui/tap (Phase 3c)
# ---------------------------------------------------------------------------


class TestTap:
    async def test_tap(self, app, auth_headers, mock_controller):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/device/ui/tap",
                json={"x": 100.0, "y": 200.0},
                headers=auth_headers,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["x"] == 100.0
        assert data["y"] == 200.0
        mock_controller.tap.assert_called_once_with(x=100.0, y=200.0, udid=None)

    async def test_tap_no_auth(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/device/ui/tap",
                json={"x": 100.0, "y": 200.0},
            )
        assert resp.status_code == 401

    async def test_tap_idb_not_found(self, app, auth_headers, mock_controller):
        mock_controller.tap = AsyncMock(
            side_effect=DeviceError("idb not found. Install with: pip install fb-idb", tool="idb")
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/device/ui/tap",
                json={"x": 100.0, "y": 200.0},
                headers=auth_headers,
            )
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# POST /device/ui/swipe (Phase 3c)
# ---------------------------------------------------------------------------


class TestSwipe:
    async def test_swipe(self, app, auth_headers, mock_controller):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/device/ui/swipe",
                json={"start_x": 100, "start_y": 400, "end_x": 100, "end_y": 100},
                headers=auth_headers,
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        mock_controller.swipe.assert_called_once_with(
            start_x=100, start_y=400, end_x=100, end_y=100, duration=0.5, udid=None,
        )

    async def test_swipe_with_duration(self, app, auth_headers, mock_controller):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/device/ui/swipe",
                json={"start_x": 0, "start_y": 0, "end_x": 0, "end_y": 500, "duration": 1.5},
                headers=auth_headers,
            )
        assert resp.status_code == 200
        mock_controller.swipe.assert_called_once_with(
            start_x=0, start_y=0, end_x=0, end_y=500, duration=1.5, udid=None,
        )

    async def test_swipe_no_auth(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/device/ui/swipe",
                json={"start_x": 0, "start_y": 0, "end_x": 0, "end_y": 100},
            )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /device/ui/type (Phase 3c)
# ---------------------------------------------------------------------------


class TestTypeText:
    async def test_type_text(self, app, auth_headers, mock_controller):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/device/ui/type",
                json={"text": "hello world"},
                headers=auth_headers,
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        mock_controller.type_text.assert_called_once_with(text="hello world", udid=None)

    async def test_type_text_no_auth(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/device/ui/type",
                json={"text": "test"},
            )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /device/ui/press (Phase 3c)
# ---------------------------------------------------------------------------


class TestPressButton:
    async def test_press_button(self, app, auth_headers, mock_controller):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/device/ui/press",
                json={"button": "HOME"},
                headers=auth_headers,
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        mock_controller.press_button.assert_called_once_with(button="HOME", udid=None)

    async def test_press_button_no_auth(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/device/ui/press",
                json={"button": "HOME"},
            )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /device/location (Phase 3c)
# ---------------------------------------------------------------------------


class TestSetLocation:
    async def test_set_location(self, app, auth_headers, mock_controller):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/device/location",
                json={"latitude": 37.7749, "longitude": -122.4194},
                headers=auth_headers,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["latitude"] == 37.7749
        assert data["longitude"] == -122.4194
        mock_controller.set_location.assert_called_once_with(
            latitude=37.7749, longitude=-122.4194, udid=None,
        )

    async def test_set_location_no_auth(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/device/location",
                json={"latitude": 0, "longitude": 0},
            )
        assert resp.status_code == 401

    async def test_set_location_error(self, app, auth_headers, mock_controller):
        mock_controller.set_location = AsyncMock(
            side_effect=DeviceError("simctl location failed: error", tool="simctl")
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/device/location",
                json={"latitude": 999, "longitude": 999},
                headers=auth_headers,
            )
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# POST /device/permission (Phase 3c)
# ---------------------------------------------------------------------------


class TestGrantPermission:
    async def test_grant_permission(self, app, auth_headers, mock_controller):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/device/permission",
                json={"bundle_id": "com.example.App", "permission": "photos"},
                headers=auth_headers,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["bundle_id"] == "com.example.App"
        assert data["permission"] == "photos"
        mock_controller.grant_permission.assert_called_once_with(
            bundle_id="com.example.App", permission="photos", udid=None,
        )

    async def test_grant_permission_no_auth(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/device/permission",
                json={"bundle_id": "com.example.App", "permission": "photos"},
            )
        assert resp.status_code == 401

    async def test_grant_permission_error(self, app, auth_headers, mock_controller):
        mock_controller.grant_permission = AsyncMock(
            side_effect=DeviceError("simctl privacy failed: unknown permission", tool="simctl")
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/device/permission",
                json={"bundle_id": "com.example.App", "permission": "badperm"},
                headers=auth_headers,
            )
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# GET /device/screenshot/annotated (Phase 3c)
# ---------------------------------------------------------------------------


class TestAnnotatedScreenshot:
    async def test_annotated_screenshot(self, app, auth_headers, mock_controller):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/device/screenshot/annotated",
                headers=auth_headers,
            )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/png"
        assert resp.content == b"\x89PNGannotated"
        mock_controller.screenshot_annotated.assert_called_once_with(
            udid=None, scale=0.5, quality=85,
        )

    async def test_annotated_screenshot_with_params(self, app, auth_headers, mock_controller):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/device/screenshot/annotated?scale=1.0&udid=BBBB-2222",
                headers=auth_headers,
            )
        assert resp.status_code == 200
        mock_controller.screenshot_annotated.assert_called_once_with(
            udid="BBBB-2222", scale=1.0, quality=85,
        )

    async def test_annotated_screenshot_no_auth(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/device/screenshot/annotated")
        assert resp.status_code == 401

    async def test_annotated_screenshot_error(self, app, auth_headers, mock_controller):
        mock_controller.screenshot_annotated = AsyncMock(
            side_effect=DeviceError("No booted simulator found", tool="simctl")
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/device/screenshot/annotated",
                headers=auth_headers,
            )
        assert resp.status_code == 400
