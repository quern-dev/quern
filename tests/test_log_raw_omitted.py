"""Log answers leave out each entry's `raw` unless asked (#630).

For simulator and oslog entries `raw` is the whole JSON record the line came
from, about 2 KB: in the agent-guide trial 30 `tail_logs` entries came to
57 KB and overflowed the agent's tool-output limit.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient

from server.config import ServerConfig
from server.main import create_app
from server.models import LogEntry, LogLevel, LogSource

HEADERS = {"Authorization": "Bearer test-key-12345"}
RAW = '{"eventMessage": "boom", "padding": "' + "x" * 2000 + '"}'


@pytest.fixture
async def app():
    config = ServerConfig(api_key="test-key-12345")
    app = create_app(config=config, enable_oslog=False, enable_crash=False, enable_proxy=False)
    for level in (LogLevel.INFO, LogLevel.ERROR):
        await app.state.ring_buffer.append(LogEntry(
            id=uuid.uuid4().hex, timestamp=datetime.now(UTC), device_id="SIM-A",
            process="MyApp", level=level, message="boom", source=LogSource.SIMULATOR,
            raw=RAW,
        ))
    return app


async def _get(app, path, **params):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(path, headers=HEADERS, params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.parametrize("path,params", [
    ("/api/v1/logs/query", {}),
    ("/api/v1/logs/query", {"tail": "true", "limit": 50}),  # tail_logs
    ("/api/v1/logs/errors", {}),
])
class TestRaw:
    async def test_omitted_by_default(self, app, path, params):
        data = await _get(app, path, **params)
        assert data["entries"], "the fixture's entries must be in the answer"
        for entry in data["entries"]:
            assert "raw" not in entry
            assert entry["message"] == "boom"

    async def test_included_when_asked(self, app, path, params):
        data = await _get(app, path, include_raw="true", **params)
        assert data["entries"]
        assert all(entry["raw"] == RAW for entry in data["entries"])

    async def test_the_rest_of_the_answer_is_unchanged(self, app, path, params):
        """Same fields, same values, apart from raw: the omission must not
        drop the completeness fields or reshape anything else."""
        without = await _get(app, path, **params)
        with_raw = await _get(app, path, include_raw="true", **params)
        for entry in with_raw["entries"]:
            del entry["raw"]
        assert without == with_raw
