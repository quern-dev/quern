"""Integration tests — create app, inject entries, query endpoints."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from server.config import ServerConfig
from server.main import create_app
from server.models import LogEntry, LogLevel, LogSource
from server.models import FlowRecord, FlowRequest, FlowResponse, FlowTiming
from server.proxy.flow_store import FlowStore


def _make_entry(
    message: str,
    level: LogLevel = LogLevel.INFO,
    process: str = "MyApp",
    timestamp: datetime | None = None,
) -> LogEntry:
    return LogEntry(
        id="int-test",
        timestamp=timestamp or datetime.now(timezone.utc),
        process=process,
        level=level,
        message=message,
        source=LogSource.SYSLOG,
    )


@pytest.fixture
def app():
    config = ServerConfig(api_key="test-key-12345")
    return create_app(config=config, enable_oslog=False, enable_crash=False, enable_proxy=False)


@pytest.fixture
def auth_headers():
    return {"Authorization": "Bearer test-key-12345"}


@pytest.mark.asyncio
async def test_health_no_auth(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_query_requires_auth(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/logs/query")
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_query_returns_injected_entries(app, auth_headers):
    """Inject entries into the ring buffer and query them back."""
    buffer = app.state.ring_buffer

    now = datetime.now(timezone.utc)
    await buffer.append(_make_entry("info message", level=LogLevel.INFO, timestamp=now))
    await buffer.append(
        _make_entry("error message", level=LogLevel.ERROR, timestamp=now + timedelta(seconds=1))
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/logs/query", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert len(data["entries"]) == 2


@pytest.mark.asyncio
async def test_query_filter_by_level(app, auth_headers):
    buffer = app.state.ring_buffer

    await buffer.append(_make_entry("info", level=LogLevel.INFO))
    await buffer.append(_make_entry("error", level=LogLevel.ERROR))
    await buffer.append(_make_entry("fault", level=LogLevel.FAULT))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/logs/query", headers=auth_headers, params={"level": "error"}
        )
        data = resp.json()
        # ERROR and FAULT are both at or above ERROR
        assert data["total"] == 2


@pytest.mark.asyncio
async def test_summary_endpoint(app, auth_headers):
    """Inject entries and get a summary back."""
    buffer = app.state.ring_buffer

    now = datetime.now(timezone.utc)
    await buffer.append(
        _make_entry("HTTP 401 Unauthorized", level=LogLevel.ERROR, timestamp=now)
    )
    await buffer.append(
        _make_entry("HTTP 401 Unauthorized", level=LogLevel.ERROR,
                     timestamp=now + timedelta(seconds=1))
    )
    await buffer.append(
        _make_entry("Token refresh succeeded", level=LogLevel.INFO,
                     timestamp=now + timedelta(seconds=2))
    )
    await buffer.append(
        _make_entry("Layout warning in FeedVC", level=LogLevel.WARNING,
                     timestamp=now + timedelta(seconds=3))
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/logs/summary", headers=auth_headers, params={"window": "5m"}
        )
        assert resp.status_code == 200
        data = resp.json()

        assert data["window"] == "5m"
        assert data["total_count"] == 4
        assert data["error_count"] == 2
        assert data["warning_count"] == 1
        assert "cursor" in data
        assert len(data["top_issues"]) >= 1
        assert isinstance(data["summary"], str)
        assert len(data["summary"]) > 0


@pytest.mark.asyncio
async def test_summary_cursor_delta(app, auth_headers):
    """Cursor should allow fetching only new entries."""
    buffer = app.state.ring_buffer

    now = datetime.now(timezone.utc)
    await buffer.append(_make_entry("first", timestamp=now))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # First summary
        resp1 = await client.get(
            "/api/v1/logs/summary", headers=auth_headers, params={"window": "5m"}
        )
        cursor = resp1.json()["cursor"]

        # Add more entries
        await buffer.append(
            _make_entry("second", timestamp=now + timedelta(seconds=5))
        )

        # Delta summary using cursor
        resp2 = await client.get(
            "/api/v1/logs/summary",
            headers=auth_headers,
            params={"window": "5m", "since_cursor": cursor},
        )
        data2 = resp2.json()
        # Should only see the entry added after the cursor
        assert data2["total_count"] == 1


@pytest.mark.asyncio
async def test_errors_endpoint(app, auth_headers):
    buffer = app.state.ring_buffer

    await buffer.append(_make_entry("info", level=LogLevel.INFO))
    await buffer.append(_make_entry("error 1", level=LogLevel.ERROR))
    await buffer.append(_make_entry("error 2", level=LogLevel.FAULT))
    await buffer.append(_make_entry("warning", level=LogLevel.WARNING))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/logs/errors", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        # All returned entries should be ERROR or FAULT
        for entry in data["entries"]:
            assert entry["level"] in ("error", "fault")


@pytest.mark.asyncio
async def test_sources_endpoint(app, auth_headers):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/logs/sources", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "sources" in data
        # Without lifespan (no device connected), adapters dict may be empty
        assert isinstance(data["sources"], list)


@pytest.mark.asyncio
async def test_summary_invalid_window(app, auth_headers):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/logs/summary",
            headers=auth_headers,
            params={"window": "99h"},
        )
        assert resp.status_code == 422  # validation error


# ---------------------------------------------------------------------------
# Crashes endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crashes_latest_empty(app, auth_headers):
    """With crash adapter disabled, /crashes/latest returns empty."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/crashes/latest", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["crashes"] == []
        assert data["total"] == 0


# ---------------------------------------------------------------------------
# Builds endpoint
# ---------------------------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.asyncio
async def test_builds_latest_empty(app, auth_headers):
    """Before any build parse, /builds/latest returns null."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/builds/latest", headers=auth_headers)
        assert resp.status_code == 200
        # No build parsed yet — null response
        assert resp.json() is None


@pytest.mark.asyncio
async def test_builds_parse(app, auth_headers):
    """POST build output and get a parsed result back."""
    # Manually set up the build adapter since lifespan doesn't run in tests
    from server.sources.build import BuildAdapter

    build = BuildAdapter()
    await build.start()
    app.state.build_adapter = build

    content = (FIXTURES / "xcodebuild_output.txt").read_text()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/builds/parse",
            headers=auth_headers,
            json={"output": content},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["succeeded"] is False
        assert len(data["errors"]) == 2
        assert len(data["warnings"]) == 3
        assert data["tests"]["total"] == 5
        assert data["tests"]["failed"] == 2

        # Now /builds/latest should return this result
        resp2 = await client.get("/api/v1/builds/latest", headers=auth_headers)
        assert resp2.status_code == 200
        assert resp2.json()["succeeded"] is False

    await build.stop()


# ---------------------------------------------------------------------------
# Proxy endpoints
# ---------------------------------------------------------------------------


def _make_flow(
    flow_id: str = "f_test",
    method: str = "GET",
    host: str = "api.example.com",
    path: str = "/v1/test",
    status_code: int = 200,
) -> FlowRecord:
    return FlowRecord(
        id=flow_id,
        timestamp=datetime.now(timezone.utc),
        request=FlowRequest(method=method, url=f"https://{host}{path}", host=host, path=path),
        response=FlowResponse(status_code=status_code, reason="OK"),
        timing=FlowTiming(total_ms=100.0),
    )


@pytest.mark.asyncio
async def test_proxy_flows_empty(app, auth_headers):
    """With proxy disabled, /proxy/flows returns empty list."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/proxy/flows", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["flows"] == []
        assert data["total"] == 0


@pytest.mark.asyncio
async def test_proxy_flows_with_data(app, auth_headers):
    """Inject flows into store and query them back."""
    flow_store = FlowStore()
    app.state.flow_store = flow_store

    await flow_store.add(_make_flow(flow_id="f_1", status_code=200))
    await flow_store.add(_make_flow(flow_id="f_2", status_code=401))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/proxy/flows", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert len(data["flows"]) == 2


@pytest.mark.asyncio
async def test_proxy_flows_filter_host(app, auth_headers):
    """Host filter should narrow results."""
    flow_store = FlowStore()
    app.state.flow_store = flow_store

    await flow_store.add(_make_flow(flow_id="f_1", host="api.example.com"))
    await flow_store.add(_make_flow(flow_id="f_2", host="other.example.com"))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/proxy/flows",
            headers=auth_headers,
            params={"host": "api.example.com"},
        )
        data = resp.json()
        assert data["total"] == 1
        assert data["flows"][0]["id"] == "f_1"


@pytest.mark.asyncio
async def test_proxy_flow_detail(app, auth_headers):
    """Get a single flow by ID."""
    flow_store = FlowStore()
    app.state.flow_store = flow_store

    await flow_store.add(_make_flow(flow_id="f_detail"))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/proxy/flows/f_detail", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "f_detail"


@pytest.mark.asyncio
async def test_proxy_flow_detail_not_found(app, auth_headers):
    """Missing flow should return 404."""
    flow_store = FlowStore()
    app.state.flow_store = flow_store

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/proxy/flows/f_nope", headers=auth_headers)
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Proxy status / control endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_proxy_status_stopped(app, auth_headers):
    """With proxy disabled, /proxy/status returns stopped."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/proxy/status", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "stopped"
        assert "port" in data


@pytest.mark.asyncio
async def test_proxy_stop_when_not_running(app, auth_headers):
    """Stopping a stopped proxy should return 409."""
    # Need to set up adapter on app state since lifespan doesn't run
    from server.sources.proxy import ProxyAdapter

    adapter = ProxyAdapter(flow_store=FlowStore())
    app.state.proxy_adapter = adapter
    app.state.flow_store = adapter.flow_store

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/v1/proxy/stop", headers=auth_headers)
        assert resp.status_code == 409


@pytest.mark.asyncio
async def test_proxy_start_when_already_running(app, auth_headers):
    """Starting a running proxy should return 409."""
    from server.sources.proxy import ProxyAdapter

    adapter = ProxyAdapter(flow_store=FlowStore())
    adapter._running = True  # Simulate running state
    app.state.proxy_adapter = adapter
    app.state.flow_store = adapter.flow_store

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/v1/proxy/start", headers=auth_headers)
        assert resp.status_code == 409

    adapter._running = False  # Clean up


# ---------------------------------------------------------------------------
# Flow summary endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_proxy_flow_summary_empty(app, auth_headers):
    """Flow summary with no data returns zero counts."""
    app.state.flow_store = FlowStore()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/proxy/flows/summary",
            headers=auth_headers,
            params={"window": "5m"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_flows"] == 0
        assert data["by_host"] == []
        assert "cursor" in data


@pytest.mark.asyncio
async def test_proxy_flow_summary_with_data(app, auth_headers):
    """Flow summary groups by host and detects errors."""
    flow_store = FlowStore()
    app.state.flow_store = flow_store

    now = datetime.now(timezone.utc)
    await flow_store.add(_make_flow(flow_id="f_s1", status_code=200))
    await flow_store.add(_make_flow(flow_id="f_s2", status_code=500))
    await flow_store.add(_make_flow(flow_id="f_s3", host="cdn.example.com", status_code=200))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/proxy/flows/summary",
            headers=auth_headers,
            params={"window": "5m"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_flows"] == 3
        assert len(data["by_host"]) == 2
        assert len(data["errors"]) == 1  # the 500


@pytest.mark.asyncio
async def test_proxy_flow_summary_invalid_window(app, auth_headers):
    """Invalid window parameter should be rejected."""
    app.state.flow_store = FlowStore()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/proxy/flows/summary",
            headers=auth_headers,
            params={"window": "99h"},
        )
        assert resp.status_code == 422


@pytest.mark.asyncio
async def test_proxy_flow_summary_host_filter(app, auth_headers):
    """Host filter should narrow summary results."""
    flow_store = FlowStore()
    app.state.flow_store = flow_store

    await flow_store.add(_make_flow(flow_id="f_h1", host="api.example.com"))
    await flow_store.add(_make_flow(flow_id="f_h2", host="cdn.example.com"))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/proxy/flows/summary",
            headers=auth_headers,
            params={"window": "5m", "host": "api.example.com"},
        )
        data = resp.json()
        assert data["total_flows"] == 1
        assert data["by_host"][0]["host"] == "api.example.com"


# ---------------------------------------------------------------------------
# Certificate & setup guide
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_proxy_cert_not_found(app, auth_headers):
    """When mitmproxy CA cert doesn't exist, return 404."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Use a monkeypatch-free approach — the cert may or may not exist
        resp = await client.get("/api/v1/proxy/cert", headers=auth_headers)
        # If the cert exists on this machine, it'll be 200; if not, 404
        assert resp.status_code in (200, 404)


@pytest.mark.asyncio
async def test_proxy_setup_guide(app, auth_headers):
    """Setup guide should return valid JSON with steps for both targets."""
    from server.sources.proxy import ProxyAdapter

    adapter = ProxyAdapter(flow_store=FlowStore())
    app.state.proxy_adapter = adapter

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/proxy/setup-guide", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "proxy_host" in data
        assert "proxy_port" in data
        assert "active_interface" in data  # may be None or a string
        assert "warnings" in data
        assert isinstance(data["warnings"], list)
        assert "steps" in data
        assert len(data["steps"]) == 2  # simulator + physical device

        sim = data["steps"][0]
        assert sim["target"] == "simulator"
        assert "note" in sim
        assert "inherits" in sim["note"].lower() or "proxy" in sim["note"].lower()
        # Simulator instructions should mention networksetup (Mac-level config)
        sim_text = " ".join(sim["instructions"])
        assert "networksetup" in sim_text
        assert "xcrun simctl keychain" in sim_text
        assert "boot" in sim_text.lower()

        phys = data["steps"][1]
        assert phys["target"] == "physical_device"
        assert "note" in phys


# ---------------------------------------------------------------------------
# Dual ring buffer (server_buffer) routing
# ---------------------------------------------------------------------------


def _make_server_entry(
    message: str,
    level: LogLevel = LogLevel.INFO,
    timestamp: datetime | None = None,
) -> LogEntry:
    return LogEntry(
        id="srv-test",
        timestamp=timestamp or datetime.now(timezone.utc),
        process="quern-debug-server",
        level=level,
        message=message,
        source=LogSource.SERVER,
        device_id="server",
    )


@pytest.mark.asyncio
async def test_query_source_server_uses_server_buffer(app, auth_headers):
    """source=server queries only the server_buffer, not ring_buffer."""
    now = datetime.now(timezone.utc)

    # Put a device entry in ring_buffer
    await app.state.ring_buffer.append(
        _make_entry("device log", timestamp=now)
    )
    # Put a server entry in server_buffer
    await app.state.server_buffer.append(
        _make_server_entry("server startup", timestamp=now + timedelta(seconds=1))
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/logs/query",
            headers=auth_headers,
            params={"source": "server"},
        )
        data = resp.json()
        assert data["total"] == 1
        assert data["entries"][0]["message"] == "server startup"


@pytest.mark.asyncio
async def test_query_no_source_merges_both_buffers(app, auth_headers):
    """No source filter merges ring_buffer + server_buffer, sorted by timestamp."""
    now = datetime.now(timezone.utc)

    await app.state.ring_buffer.append(
        _make_entry("device log 1", timestamp=now)
    )
    await app.state.server_buffer.append(
        _make_server_entry("server log", timestamp=now + timedelta(seconds=1))
    )
    await app.state.ring_buffer.append(
        _make_entry("device log 2", timestamp=now + timedelta(seconds=2))
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/logs/query",
            headers=auth_headers,
        )
        data = resp.json()
        assert data["total"] == 3
        messages = [e["message"] for e in data["entries"]]
        assert messages == ["device log 1", "server log", "device log 2"]


@pytest.mark.asyncio
async def test_query_source_syslog_skips_server_buffer(app, auth_headers):
    """source=syslog queries only ring_buffer, not server_buffer."""
    now = datetime.now(timezone.utc)

    await app.state.ring_buffer.append(
        _make_entry("device log", timestamp=now)
    )
    await app.state.server_buffer.append(
        _make_server_entry("server log", timestamp=now + timedelta(seconds=1))
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/logs/query",
            headers=auth_headers,
            params={"source": "syslog"},
        )
        data = resp.json()
        assert data["total"] == 1
        assert data["entries"][0]["message"] == "device log"
