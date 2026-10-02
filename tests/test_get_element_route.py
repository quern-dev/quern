"""GET /device/ui/element passes a read timeout through (F31).

It was the one read endpoint without `source_timeout`. Through WDA, a screen
whose tree takes longer than the default to serialise -- a 200-row table took
10.5s on a simulator -- could then only be answered from the partial fallback,
and an element that was on screen came back 404.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from server.config import ServerConfig
from server.device.controller import DeviceController
from server.main import create_app


@pytest.fixture
def app_and_controller():
    app = create_app(
        config=ServerConfig(api_key="test-key-12345"),
        enable_oslog=False, enable_crash=False, enable_proxy=False,
    )
    ctrl = MagicMock(spec=DeviceController)
    ctrl.get_element = AsyncMock(return_value=({"identifier": "row_40"}, "AAAA-1111"))
    app.state.device_controller = ctrl
    return app, ctrl


async def _get(app, params):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.get(
            "/api/v1/device/ui/element", params=params,
            headers={"Authorization": "Bearer test-key-12345"},
        )


async def test_the_timeout_reaches_the_controller(app_and_controller):
    app, ctrl = app_and_controller
    r = await _get(app, {"identifier": "row_40", "source_timeout": 30})
    assert r.status_code == 200, r.text
    assert ctrl.get_element.call_args.kwargs["source_timeout"] == 30


async def test_omitted_means_the_default(app_and_controller):
    app, ctrl = app_and_controller
    r = await _get(app, {"identifier": "row_40"})
    assert r.status_code == 200, r.text
    assert ctrl.get_element.call_args.kwargs["source_timeout"] is None


@pytest.mark.parametrize("bad", [0, 61, -5])
async def test_out_of_range_is_refused(app_and_controller, bad):
    app, ctrl = app_and_controller
    r = await _get(app, {"identifier": "row_40", "source_timeout": bad})
    assert r.status_code == 422, r.text
    ctrl.get_element.assert_not_called()


async def test_the_controller_hands_it_to_the_tree_read():
    ctrl = DeviceController()
    ctrl.get_ui_elements = AsyncMock(return_value=([], "AAAA-1111"))
    try:
        await ctrl.get_element(identifier="row_40", udid="AAAA-1111", source_timeout=30)
    except Exception:
        pass  # not found is fine; the read is what is under test
    assert ctrl.get_ui_elements.call_args.kwargs.get("source_timeout") == 30
