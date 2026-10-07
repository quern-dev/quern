"""Integration tests for device API endpoints.

Uses httpx/ASGITransport against the real FastAPI app with mocked DeviceController.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

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
    ctrl.launch_app = AsyncMock(return_value=("AAAA-1111", {}))
    ctrl.terminate_app = AsyncMock(return_value="AAAA-1111")
    ctrl.uninstall_app = AsyncMock(return_value="AAAA-1111")
    ctrl.list_apps = AsyncMock(
        return_value=(
            [AppInfo(bundle_id="com.example.App", name="My App", app_type="User")],
            "AAAA-1111",
        )
    )
    ctrl.screenshot = AsyncMock(return_value=(b"\x89PNGfake", "image/png"))
    # Phase 3b: UI inspection mocks
    _sample_elements = [
        UIElement(
            type="Application",
            label="Springboard",
            frame={"x": 0, "y": 0, "width": 393, "height": 852},
        ),
        UIElement(
            type="Button",
            label="Settings",
            identifier="Settings",
            frame={"x": 302, "y": 476, "width": 68, "height": 86},
        ),
        UIElement(
            type="Button",
            label="Maps",
            identifier="Maps",
            frame={"x": 27, "y": 382, "width": 68, "height": 86},
        ),
    ]
    ctrl.get_ui_elements = AsyncMock(return_value=(_sample_elements, "AAAA-1111"))
    ctrl.get_screen_summary = AsyncMock(
        return_value=(
            {
                "summary": "Springboard screen with 2 buttons.",
                "element_count": 3,
                "element_types": {"Application": 1, "Button": 2},
                "interactive_elements": [
                    {"type": "Button", "label": "Settings", "identifier": "Settings"},
                    {"type": "Button", "label": "Maps", "identifier": "Maps"},
                ],
            },
            _sample_elements,
            "AAAA-1111",
        )
    )
    ctrl.tap = AsyncMock(return_value="AAAA-1111")
    ctrl.tap_element = AsyncMock(
        return_value={
            "status": "ok",
            "tapped": {
                "label": "Settings",
                "type": "Button",
                "identifier": "Settings",
                "x": 336.0,
                "y": 519.0,
            },
        }
    )
    ctrl.swipe = AsyncMock(return_value="AAAA-1111")
    ctrl.type_text = AsyncMock(
        return_value={"udid": "AAAA-1111", "verified": False})
    ctrl.clear_text = AsyncMock(return_value="AAAA-1111")
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
        mock_controller.boot.assert_called_once_with(udid="AAAA-1111", name=None, headless=False)

    async def test_boot_by_name(self, app, auth_headers, mock_controller):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/device/boot",
                json={"name": "iPhone 16 Pro"},
                headers=auth_headers,
            )
        assert resp.status_code == 200
        mock_controller.boot.assert_called_once_with(
            udid=None, name="iPhone 16 Pro", headless=False
        )

    async def test_boot_no_booted_device_error(self, app, auth_headers, mock_controller):
        mock_controller.boot = AsyncMock(
            side_effect=DeviceError("No booted device found", tool="simctl")
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

    async def test_uninstall_app(self, app, auth_headers, mock_controller):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/device/app/uninstall",
                json={"bundle_id": "com.example.App"},
                headers=auth_headers,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "uninstalled"
        assert data["bundle_id"] == "com.example.App"
        assert data["udid"] == "AAAA-1111"

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
            udid=None,
            format="png",
            scale=0.5,
            quality=85,
        )

    async def test_screenshot_jpeg(self, app, auth_headers, mock_controller):
        mock_controller.screenshot = AsyncMock(return_value=(b"jpeg-data", "image/jpeg"))
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/device/screenshot?format=jpeg&scale=1.0&quality=50",
                headers=auth_headers,
            )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/jpeg"
        mock_controller.screenshot.assert_called_once_with(
            udid=None,
            format="jpeg",
            scale=1.0,
            quality=50,
        )

    async def test_screenshot_error(self, app, auth_headers, mock_controller):
        mock_controller.screenshot = AsyncMock(
            side_effect=DeviceError("No booted device found", tool="simctl")
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
        mock_controller.get_ui_elements.assert_called_once_with(
            udid=None, snapshot_depth=None, source_timeout=None, mode=None
        )

    async def test_get_ui_elements_with_udid(self, app, auth_headers, mock_controller):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/device/ui?udid=BBBB-2222",
                headers=auth_headers,
            )
        assert resp.status_code == 200
        mock_controller.get_ui_elements.assert_called_once_with(
            udid="BBBB-2222", snapshot_depth=None, source_timeout=None, mode=None
        )

    async def test_get_ui_elements_strips_extra_attrs_by_default(
        self, app, auth_headers, mock_controller,
    ):
        """Without include_raw, the response should NOT include extra_attrs
        on each element — it's debug-only data and would inflate every UI
        tree response with redundant source attributes."""
        mock_controller.get_ui_elements = AsyncMock(
            return_value=(
                [UIElement(
                    type="Group",
                    label="Explore",
                    extra_attrs={"selected": "true", "checkable": "false"},
                )],
                "AAAA-1111",
            ),
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/device/ui", headers=auth_headers)
        assert resp.status_code == 200
        elem = resp.json()["elements"][0]
        assert "extra_attrs" not in elem

    async def test_get_ui_elements_include_raw_keeps_extra_attrs(
        self, app, auth_headers, mock_controller,
    ):
        """With include_raw=true, extra_attrs survive into the response so
        agents can debug the normalizer without dropping to adb."""
        mock_controller.get_ui_elements = AsyncMock(
            return_value=(
                [UIElement(
                    type="Group",
                    label="Explore",
                    extra_attrs={"selected": "true", "checkable": "false"},
                )],
                "AAAA-1111",
            ),
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/device/ui?include_raw=true",
                headers=auth_headers,
            )
        assert resp.status_code == 200
        elem = resp.json()["elements"][0]
        assert elem["extra_attrs"] == {"selected": "true", "checkable": "false"}

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
            side_effect=DeviceError("No booted device found", tool="simctl")
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

    async def test_identify_with_nothing_loaded_says_so_and_what_to_do(
        self, app, auth_headers, mock_controller,
    ):
        """"Could not identify" read exactly like "identified nothing"."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/device/screen-summary",
                                    params={"identify": "true"}, headers=auth_headers)
        data = resp.json()
        assert data["identified_as"] is None
        assert data["identify_error"] == "no_landmarks_loaded"
        assert "init_app_knowledge" in data["identify_hint"]

    async def test_a_successful_identify_carries_no_error_or_hint(
        self, app, auth_headers, mock_controller,
    ):
        """The two fields are the failure's; a match must not inherit them."""
        from server.knowledge.landmarks import Landmark, ScreenLandmarks
        app.state.landmark_registry.load("com.example.app", [ScreenLandmarks(
            screen="Home", landmarks=[Landmark(element="Button", label="Maps")])])
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/device/screen-summary",
                                    params={"identify": "true"}, headers=auth_headers)
        data = resp.json()
        assert data["identified_as"] == "Home"
        assert "identify_error" not in data and "identify_hint" not in data

    async def test_screen_summary_with_udid(self, app, auth_headers, mock_controller):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/device/screen-summary?udid=BBBB-2222",
                headers=auth_headers,
            )
        assert resp.status_code == 200
        mock_controller.get_screen_summary.assert_called_once_with(
            max_elements=20,
            udid="BBBB-2222",
            snapshot_depth=None,
            strategy=None,
            source_timeout=None,
            mode=None,
        )

    async def test_screen_summary_with_strategy(self, app, auth_headers, mock_controller):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/device/screen-summary?strategy=skeleton",
                headers=auth_headers,
            )
        assert resp.status_code == 200
        mock_controller.get_screen_summary.assert_called_once_with(
            max_elements=20,
            udid=None,
            snapshot_depth=None,
            strategy="skeleton",
            source_timeout=None,
            mode=None,
        )


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
            label="Settings",
            label_contains=None,
            label_prefix=None,
            identifier=None,
            element_type=None,
            udid="AAAA-1111",
            skip_stability_check=False,
            source_timeout=None,
            value=None,
            # None, not True: unset now means "ask the knowledge base"
            # rather than "always sweep" (#274). The handler passes the
            # request's value straight through, so this pins the default.
            scroll_to_find=None,
            snapshot_depth=None,
            duration=None,
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
            label=None,
            label_contains=None,
            label_prefix=None,
            identifier="Settings",
            element_type=None,
            udid="AAAA-1111",
            skip_stability_check=False,
            source_timeout=None,
            value=None,
            # None, not True: unset now means "ask the knowledge base"
            # rather than "always sweep" (#274). The handler passes the
            # request's value straight through, so this pins the default.
            scroll_to_find=None,
            snapshot_depth=None,
            duration=None,
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
            label="Calendar",
            label_contains=None,
            label_prefix=None,
            identifier=None,
            element_type="Button",
            udid="AAAA-1111",
            skip_stability_check=False,
            source_timeout=None,
            value=None,
            # None, not True: unset now means "ask the knowledge base"
            # rather than "always sweep" (#274). The handler passes the
            # request's value straight through, so this pins the default.
            scroll_to_find=None,
            snapshot_depth=None,
            duration=None,
        )

    async def test_tap_element_ambiguous(self, app, auth_headers, mock_controller):
        mock_controller.tap_element = AsyncMock(
            return_value={
                "status": "ambiguous",
                "matches": [
                    {"label": "Calendar", "type": "Button", "identifier": "Calendar-1"},
                    {"label": "Calendar", "type": "Button", "identifier": "Calendar-2"},
                ],
                "message": "Found 2 matches, specify element_type or identifier to narrow",
            }
        )
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
        mock_controller.tap.assert_called_once_with(x=100.0, y=200.0, udid=None, duration=None)

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
            start_x=100,
            start_y=400,
            end_x=100,
            end_y=100,
            duration=0.5,
            udid=None,
            edge=None,
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
            start_x=0,
            start_y=0,
            end_x=0,
            end_y=500,
            duration=1.5,
            udid=None,
            edge=None,
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
        # Untargeted, so the response says so rather than implying it landed.
        assert resp.json()["verified"] is False
        mock_controller.type_text.assert_called_once_with(
            text="hello world", udid="AAAA-1111", label=None, identifier=None,
        )

    async def test_a_verified_type_returns_the_field_s_value(
        self, app, auth_headers, mock_controller,
    ):
        """So a caller can see what the field holds -- iOS may have rewritten
        it -- without a second read."""
        mock_controller.type_text.return_value = {
            "udid": "AAAA-1111", "verified": True, "value": "Qft found it",
        }
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/device/ui/type",
                json={"text": "Qft found it", "identifier": "_Post log view"},
                headers=auth_headers,
            )
        assert resp.status_code == 200
        assert resp.json()["value"] == "Qft found it"

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


class TestClearText:
    async def test_clear_text(self, app, auth_headers, mock_controller):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/device/ui/clear",
                json={},
                headers=auth_headers,
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        assert resp.json()["udid"] == "AAAA-1111"
        mock_controller.clear_text.assert_called_once_with(
            udid=None, label=None, identifier=None,
        )

    async def test_clear_text_with_udid(self, app, auth_headers, mock_controller):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/device/ui/clear",
                json={"udid": "BBBB-2222"},
                headers=auth_headers,
            )
        assert resp.status_code == 200
        mock_controller.clear_text.assert_called_once_with(
            udid="BBBB-2222", label=None, identifier=None,
        )

    async def test_clear_text_no_auth(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/device/ui/clear",
                json={},
            )
        assert resp.status_code == 401


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
            latitude=37.7749,
            longitude=-122.4194,
            udid=None,
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
                json={"latitude": 10, "longitude": 10},
                headers=auth_headers,
            )
        assert resp.status_code == 500

    @pytest.mark.parametrize("lat, lon", [
        (90.5, 0), (-91, 0), (0, 180.5), (0, -181), (999, 999),
    ])
    async def test_a_coordinate_off_the_globe_is_refused_before_the_device(
        self, app, auth_headers, mock_controller, lat, lon,
    ):
        """simctl and the emulator console both answered ok for latitude 91 (F29)."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/device/location",
                json={"latitude": lat, "longitude": lon},
                headers=auth_headers,
            )
        assert resp.status_code == 422, resp.text
        mock_controller.set_location.assert_not_called()

    @pytest.mark.parametrize("lat, lon", [(90, 180), (-90, -180)])
    async def test_the_poles_and_the_antimeridian_are_on_the_globe(
        self, app, auth_headers, mock_controller, lat, lon,
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/device/location",
                json={"latitude": lat, "longitude": lon},
                headers=auth_headers,
            )
        assert resp.status_code == 200, resp.text

    async def test_a_physical_android_refusal_is_a_400(
        self, app, auth_headers, mock_controller,
    ):
        """It was a 500 prefixed `[adb]` on every physical phone (F28)."""
        from server.models import DeviceOperationUnsupportedError

        mock_controller.set_location = AsyncMock(side_effect=DeviceOperationUnsupportedError(
            "Location simulation needs the emulator console", tool="adb",
        ))
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/device/location",
                json={"latitude": 10, "longitude": 10},
                headers=auth_headers,
            )
        assert resp.status_code == 400, resp.text


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
            bundle_id="com.example.App",
            permission="photos",
            udid=None,
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
            udid=None,
            scale=0.5,
            quality=85,
            grid=None,
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
            udid="BBBB-2222",
            scale=1.0,
            quality=85,
            grid=None,
        )

    async def test_annotated_screenshot_no_auth(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/device/screenshot/annotated")
        assert resp.status_code == 401

    async def test_annotated_screenshot_error(self, app, auth_headers, mock_controller):
        mock_controller.screenshot_annotated = AsyncMock(
            side_effect=DeviceError("No booted device found", tool="simctl")
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/device/screenshot/annotated",
                headers=auth_headers,
            )
        assert resp.status_code == 400


class TestScrollToElement:
    async def test_scroll_to_element_ok(self, app, auth_headers, mock_controller):
        mock_controller.scroll_to_element = AsyncMock(
            return_value={
                "status": "ok",
                "element": {"label": "Log", "identifier": "button_log",
                            "type": "Button", "x": 100, "y": 200},
            }
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/device/ui/scroll-to-element",
                json={"identifier": "button_log"},
                headers=auth_headers,
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        mock_controller.scroll_to_element.assert_called_once_with(
            label=None, identifier="button_log", udid=None, max_swipes=10, snapshot_depth=None,
        )

    async def test_scroll_to_element_by_label_and_max_swipes(
        self, app, auth_headers, mock_controller
    ):
        mock_controller.scroll_to_element = AsyncMock(
            return_value={"status": "ok", "element": {}}
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/device/ui/scroll-to-element",
                json={"label": "Log cache", "max_swipes": 5},
                headers=auth_headers,
            )
        assert resp.status_code == 200
        mock_controller.scroll_to_element.assert_called_once_with(
            label="Log cache", identifier=None, udid=None, max_swipes=5, snapshot_depth=None,
        )

    async def test_scroll_to_element_not_found(self, app, auth_headers, mock_controller):
        mock_controller.scroll_to_element = AsyncMock(
            return_value={"status": "not_found", "detail": "gone"}
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/device/ui/scroll-to-element",
                json={"identifier": "missing"},
                headers=auth_headers,
            )
        assert resp.status_code == 404

    async def test_scroll_to_element_not_supported(self, app, auth_headers, mock_controller):
        mock_controller.scroll_to_element = AsyncMock(
            return_value={"status": "not_supported", "detail": "iOS"}
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/device/ui/scroll-to-element",
                json={"identifier": "button_log"},
                headers=auth_headers,
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "not_supported"

    async def test_scroll_to_element_requires_target(self, app, auth_headers, mock_controller):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/device/ui/scroll-to-element",
                json={},
                headers=auth_headers,
            )
        assert resp.status_code == 422


class TestGetUiTreeDoesNotCallARequestedSkeletonDegraded:
    """The sibling of the same guard on `get_screen_summary`.

    `strategy="skeleton"` never reads `/source`, so a note left by an earlier
    timeout is still recorded — and `get_ui_tree` reported it, telling a
    caller who deliberately chose a skeleton that their read had timed out.
    Found by review on #329; the summary path had the guard and this one did
    not.
    """

    @pytest.mark.asyncio
    async def test_a_requested_skeleton_carries_no_degraded_note(
        self, app, auth_headers, mock_controller
    ):
        from httpx import ASGITransport, AsyncClient

        mock_controller._is_physical = lambda _u: True
        mock_controller.resolve_udid = AsyncMock(return_value="PHYS-1")
        from unittest.mock import MagicMock

        mock_controller.wda_client = MagicMock()
        mock_controller.wda_client.build_screen_skeleton = AsyncMock(return_value=[])
        # A stale record from an earlier read that really did time out.
        mock_controller.wda_client.source_timed_out = lambda _u: 20.0

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/device/ui",
                params={"udid": "PHYS-1", "strategy": "skeleton"},
                headers=auth_headers,
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "degraded" not in body, body
        assert "source_timed_out" not in body, body


class TestEveryUiResponseNamesItsBackend:
    """Review of #236 proved two of the three advertised fields could be
    deleted with the whole suite still green — the claim was in the MCP
    descriptions and in nothing executable.

    These pin the routes. `get_screen_summary` is covered at the controller
    in test_device_controller.py; these are the ones that were only ever
    asserted in prose.
    """

    @staticmethod
    def _ctrl(mock_controller):
        mock_controller._last_read_backend = {"SIM-1": "sim-bridge"}
        mock_controller.resolve_udid = AsyncMock(return_value="SIM-1")
        mock_controller.backend_that_served = lambda udid: "sim-bridge"
        return mock_controller

    @pytest.mark.asyncio
    async def test_get_ui_tree_names_it(self, app, auth_headers, mock_controller):
        from httpx import ASGITransport, AsyncClient

        c = self._ctrl(mock_controller)
        c.get_ui_elements = AsyncMock(return_value=([], "SIM-1"))

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/device/ui", params={"udid": "SIM-1"}, headers=auth_headers,
            )
        assert resp.status_code == 200, resp.text
        assert resp.json().get("backend") == "sim-bridge", resp.json()

    @pytest.mark.asyncio
    async def test_wait_for_element_names_it_on_a_timeout(
        self, app, auth_headers, mock_controller
    ):
        """A timeout returns 200 with matched=false — a success response, and
        the moment someone asks which backend was driving."""
        from httpx import ASGITransport, AsyncClient

        c = self._ctrl(mock_controller)
        c.wait_for_element = AsyncMock(
            return_value=({"matched": False, "polls": 3}, "SIM-1"),
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/device/ui/wait-for-element",
                json={"identifier": "nope", "condition": "exists", "timeout": 1},
                headers=auth_headers,
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["matched"] is False
        assert body.get("backend") == "sim-bridge", body
