"""Integration tests for intercept, replay, and mock API endpoints.

Uses httpx/ASGITransport against the real FastAPI app. The proxy adapter's
send_command is mocked to no-op (no real mitmdump).
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from server.config import ServerConfig
from server.main import create_app
from server.models import FlowRecord, FlowRequest, FlowResponse, FlowTiming
from server.proxy.flow_store import FlowStore
from server.sources.proxy import ProxyAdapter


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_flow(
    flow_id: str = "f_test",
    method: str = "GET",
    host: str = "api.example.com",
    path: str = "/v1/test",
    url: str | None = None,
    status_code: int = 200,
    body: str | None = None,
) -> FlowRecord:
    if url is None:
        url = f"https://{host}{path}"
    return FlowRecord(
        id=flow_id,
        timestamp=datetime.now(timezone.utc),
        request=FlowRequest(
            method=method, url=url, host=host, path=path, body=body,
        ),
        response=FlowResponse(status_code=status_code, reason="OK"),
        timing=FlowTiming(total_ms=100.0),
    )


@pytest.fixture
def app():
    config = ServerConfig(api_key="test-key-12345")
    return create_app(config=config, enable_oslog=False, enable_crash=False, enable_proxy=False)


@pytest.fixture
def auth_headers():
    return {"Authorization": "Bearer test-key-12345"}


@pytest.fixture
def running_adapter(app):
    """Set up a proxy adapter in 'running' state with mocked send_command."""
    flow_store = FlowStore()
    adapter = ProxyAdapter(flow_store=flow_store, listen_port=9101)
    adapter._running = True
    adapter.started_at = datetime.now(timezone.utc)
    adapter.send_command = AsyncMock()
    app.state.proxy_adapter = adapter
    app.state.flow_store = flow_store
    return adapter


@pytest.fixture
def stopped_adapter(app):
    """Set up a proxy adapter in 'stopped' state."""
    flow_store = FlowStore()
    adapter = ProxyAdapter(flow_store=flow_store)
    adapter.send_command = AsyncMock()
    app.state.proxy_adapter = adapter
    app.state.flow_store = flow_store
    return adapter


# ---------------------------------------------------------------------------
# Intercept endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_intercept_set_when_running(app, auth_headers, running_adapter):
    """POST /intercept should accept a pattern when proxy is running."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/proxy/intercept",
            headers=auth_headers,
            json={"pattern": "~d api.example.com"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "accepted"
        assert data["pattern"] == "~d api.example.com"

    running_adapter.send_command.assert_called()


@pytest.mark.asyncio
async def test_intercept_set_when_stopped(app, auth_headers, stopped_adapter):
    """POST /intercept should return 503 when proxy is stopped."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/proxy/intercept",
            headers=auth_headers,
            json={"pattern": "~d api.example.com"},
        )
        assert resp.status_code == 503


@pytest.mark.asyncio
async def test_intercept_clear(app, auth_headers, running_adapter):
    """DELETE /intercept should clear pattern and release held flows."""
    # Inject a held flow
    running_adapter._held_flows["f_1"] = {
        "id": "f_1",
        "held_at": datetime.now(timezone.utc),
        "request": {"method": "GET", "path": "/test"},
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.delete(
            "/api/v1/proxy/intercept",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "accepted"
        assert "1" in data["note"]  # mentions count of released flows


@pytest.mark.asyncio
async def test_held_flows_empty(app, auth_headers, running_adapter):
    """GET /intercept/held should return empty when no flows held."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/proxy/intercept/held",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_held"] == 0
        assert data["held_flows"] == []


@pytest.mark.asyncio
async def test_held_flows_with_data(app, auth_headers, running_adapter):
    """GET /intercept/held should return held flows."""
    now = datetime.now(timezone.utc)
    running_adapter._held_flows["f_held"] = {
        "id": "f_held",
        "held_at": now,
        "request": {
            "method": "POST",
            "url": "https://api.example.com/v1/login",
            "host": "api.example.com",
            "path": "/v1/login",
            "headers": {},
            "body": None,
            "body_size": 0,
            "body_truncated": False,
            "body_encoding": "utf-8",
        },
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/proxy/intercept/held",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_held"] == 1
        assert data["held_flows"][0]["id"] == "f_held"


@pytest.mark.asyncio
async def test_held_flows_longpoll_timeout_zero(app, auth_headers, running_adapter):
    """Long-poll with timeout=0 should return immediately."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/proxy/intercept/held",
            headers=auth_headers,
            params={"timeout": 0},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_held"] == 0


@pytest.mark.asyncio
async def test_held_flows_longpoll_with_inject(app, auth_headers, running_adapter):
    """Long-poll should return when a flow is injected."""
    async def inject_flow():
        await asyncio.sleep(0.1)
        running_adapter._held_flows["f_inject"] = {
            "id": "f_inject",
            "held_at": datetime.now(timezone.utc),
            "request": {
                "method": "GET",
                "url": "https://api.example.com/test",
                "host": "api.example.com",
                "path": "/test",
                "headers": {},
                "body": None,
                "body_size": 0,
                "body_truncated": False,
                "body_encoding": "utf-8",
            },
        }
        running_adapter._intercept_event.set()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        inject_task = asyncio.create_task(inject_flow())
        resp = await client.get(
            "/api/v1/proxy/intercept/held",
            headers=auth_headers,
            params={"timeout": 5},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_held"] == 1
        await inject_task


@pytest.mark.asyncio
async def test_release_flow_success(app, auth_headers, running_adapter):
    """POST /intercept/release should release a held flow."""
    running_adapter._held_flows["f_rel"] = {
        "id": "f_rel",
        "held_at": datetime.now(timezone.utc),
        "request": {"method": "GET", "path": "/test"},
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/proxy/intercept/release",
            headers=auth_headers,
            json={"flow_id": "f_rel"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "released"
        assert data["flow_id"] == "f_rel"


@pytest.mark.asyncio
async def test_release_flow_not_found(app, auth_headers, running_adapter):
    """POST /intercept/release for unknown flow should return 404."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/proxy/intercept/release",
            headers=auth_headers,
            json={"flow_id": "f_nope"},
        )
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_release_flow_with_modifications(app, auth_headers, running_adapter):
    """Release with modifications should pass them through."""
    running_adapter._held_flows["f_mod"] = {
        "id": "f_mod",
        "held_at": datetime.now(timezone.utc),
        "request": {"method": "GET", "path": "/test"},
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/proxy/intercept/release",
            headers=auth_headers,
            json={
                "flow_id": "f_mod",
                "modifications": {"headers": {"x-debug": "true"}},
            },
        )
        assert resp.status_code == 200

    # Verify send_command was called with modify_and_release
    calls = running_adapter.send_command.call_args_list
    assert any(
        c.args[0].get("action") == "modify_and_release"
        for c in calls
    )


@pytest.mark.asyncio
async def test_release_all(app, auth_headers, running_adapter):
    """POST /intercept/release-all should release all held flows."""
    running_adapter._held_flows["f_1"] = {
        "id": "f_1",
        "held_at": datetime.now(timezone.utc),
        "request": {},
    }
    running_adapter._held_flows["f_2"] = {
        "id": "f_2",
        "held_at": datetime.now(timezone.utc),
        "request": {},
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/proxy/intercept/release-all",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "released"
        assert data["count"] == 2


# ---------------------------------------------------------------------------
# Replay endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_flow_not_found(app, auth_headers, running_adapter):
    """POST /replay/{id} for unknown flow should return 404."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/proxy/replay/f_nope",
            headers=auth_headers,
        )
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_replay_when_stopped(app, auth_headers, stopped_adapter):
    """POST /replay/{id} should return 503 when proxy is stopped."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/proxy/replay/f_test",
            headers=auth_headers,
        )
        assert resp.status_code == 503


@pytest.mark.asyncio
async def test_replay_success(app, auth_headers, running_adapter):
    """POST /replay/{id} should replay the flow and return result."""
    flow = _make_flow(flow_id="f_replay", url="https://api.example.com/v1/test")
    await running_adapter.flow_store.add(flow)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Mock the httpx client to avoid real HTTP call
        with patch("server.api.proxy.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_resp = AsyncMock()
            mock_resp.status_code = 200
            mock_client.request = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            resp = await client.post(
                "/api/v1/proxy/replay/f_replay",
                headers=auth_headers,
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "success"
            assert data["original_flow_id"] == "f_replay"
            assert data["status_code"] == 200


@pytest.mark.asyncio
async def test_replay_with_modifications(app, auth_headers, running_adapter):
    """POST /replay/{id} with body modifications should pass them through."""
    flow = _make_flow(flow_id="f_replay_mod")
    await running_adapter.flow_store.add(flow)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with patch("server.api.proxy.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_resp = AsyncMock()
            mock_resp.status_code = 201
            mock_client.request = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            resp = await client.post(
                "/api/v1/proxy/replay/f_replay_mod",
                headers=auth_headers,
                json={
                    "modify_headers": {"x-replay": "true"},
                    "modify_body": '{"replayed": true}',
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "success"
            assert data["status_code"] == 201


@pytest.mark.asyncio
async def test_replay_error_handling(app, auth_headers, running_adapter):
    """POST /replay/{id} should return error response on failure."""
    flow = _make_flow(flow_id="f_replay_err")
    await running_adapter.flow_store.add(flow)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with patch("server.api.proxy.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(side_effect=Exception("Connection refused"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            resp = await client.post(
                "/api/v1/proxy/replay/f_replay_err",
                headers=auth_headers,
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "error"
            assert "Connection refused" in data["error"]


# ---------------------------------------------------------------------------
# Mock endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mock_crud(app, auth_headers, running_adapter):
    """Full CRUD cycle for mock rules."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # POST — create a mock rule
        resp = await client.post(
            "/api/v1/proxy/mocks",
            headers=auth_headers,
            json={
                "pattern": "~d api.example.com",
                "response": {
                    "status_code": 200,
                    "headers": {"content-type": "application/json"},
                    "body": '{"mocked": true}',
                },
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "accepted"
        rule_id = data["rule_id"]

        # GET — list mock rules
        resp = await client.get("/api/v1/proxy/mocks", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["rules"][0]["rule_id"] == rule_id

        # DELETE specific rule
        resp = await client.delete(
            f"/api/v1/proxy/mocks/{rule_id}",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"

        # Verify empty
        resp = await client.get("/api/v1/proxy/mocks", headers=auth_headers)
        assert resp.json()["total"] == 0


@pytest.mark.asyncio
async def test_mock_delete_all(app, auth_headers, running_adapter):
    """DELETE /mocks should clear all mock rules."""
    # Add mock rules directly
    running_adapter._mock_rules = [
        {"rule_id": "a", "pattern": "~d a.com"},
        {"rule_id": "b", "pattern": "~d b.com"},
    ]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.delete("/api/v1/proxy/mocks", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "deleted"
        assert data["count"] == 2


@pytest.mark.asyncio
async def test_mock_requires_running_proxy(app, auth_headers, stopped_adapter):
    """POST /mocks should return 503 when proxy is stopped."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/proxy/mocks",
            headers=auth_headers,
            json={
                "pattern": "~d api.example.com",
                "response": {"status_code": 200, "body": "ok"},
            },
        )
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Status response includes new fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_includes_intercept_fields(app, auth_headers, running_adapter):
    """Proxy status should include intercept and mock fields."""
    running_adapter._intercept_pattern = "~d api.example.com"
    running_adapter._held_flows["f_1"] = {
        "id": "f_1",
        "held_at": datetime.now(timezone.utc),
        "request": {},
    }
    running_adapter._mock_rules = [{"rule_id": "m1", "pattern": "~d test.com"}]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/proxy/status", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["active_intercept"] == "~d api.example.com"
        assert data["held_flows_count"] == 1
        assert data["mock_rules_count"] == 1
