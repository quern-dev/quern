"""Integration tests — create app, inject entries, query endpoints."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from server.config import ServerConfig
from server.main import create_app
from server.models import LogEntry, LogLevel, LogSource


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
    return create_app(config=config, enable_oslog=False, enable_crash=False)


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
