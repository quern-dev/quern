"""Every surface that shows a flow redacts its secrets (#742).

One test per surface, through the real route, so a surface added without
redaction is the one with no test here. The module itself is tested in
tests/test_redact.py. Each surface also keeps the real value stored, and
`reveal` shows it, said on the answer.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient

from server.config import ServerConfig
from server.main import create_app
from server.proxy import redact
from server.proxy.flow_store import FlowStore
from tests.test_flow_store import _make_flow

HEADERS = {"Authorization": "Bearer test-key-12345"}
SECRET = "Bearer s3cr3t-app-token"


@pytest.fixture(autouse=True)
def _fresh():
    redact._forget_for_tests()
    yield
    redact._forget_for_tests()


@pytest.fixture
def app():
    app = create_app(
        config=ServerConfig(api_key="test-key-12345", ring_buffer_size=50),
        enable_oslog=False, enable_crash=False, enable_proxy=False,
    )
    app.state.flow_store = FlowStore(max_size=50)
    return app


def _secret_flow(flow_id="f1"):
    flow = _make_flow(flow_id=flow_id, path="/v1/me?access_token=t0k3n&page=2",
                      timestamp=datetime.now(UTC))
    flow.request.headers = {"Authorization": SECRET, "Accept": "application/json"}
    flow.response.headers = {"Set-Cookie": "sid=c00kie; Path=/"}
    return flow


async def _call(app, method, path, **kw):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, headers=HEADERS, **kw)


def _assert_hidden(flow: dict):
    text = repr(flow)
    assert "s3cr3t-app-token" not in text and "t0k3n" not in text and "c00kie" not in text
    assert flow["request"]["headers"]["Authorization"].startswith("Bearer ‹redacted")
    assert flow["request"]["headers"]["Accept"] == "application/json"


async def test_query_flows_redacts_and_says_so(app):
    await app.state.flow_store.add(_secret_flow())
    resp = await _call(app, "GET", "/api/v1/proxy/flows")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    _assert_hidden(body["flows"][0])
    assert body["redacted"] == ["access_token", "authorization", "set-cookie"]
    assert "Bodies are not redacted" in body["redaction_note"]


async def test_query_flows_summaries_redact_their_urls(app):
    await app.state.flow_store.add(_secret_flow())
    body = (await _call(app, "GET", "/api/v1/proxy/flows",
                        params={"detail": "summary"})).json()
    item = body["flow_summaries"][0]
    assert "t0k3n" not in item["url"] and "t0k3n" not in item["path"]
    assert "&page=2" in item["url"]


async def test_get_flow_redacts_and_keeps_the_stored_value(app):
    await app.state.flow_store.add(_secret_flow())
    body = (await _call(app, "GET", "/api/v1/proxy/flows/f1")).json()
    _assert_hidden(body)
    stored = await app.state.flow_store.get("f1")
    assert stored.request.headers["Authorization"] == SECRET    # replay needs it


async def test_reveal_shows_what_it_names_and_says_so(app):
    await app.state.flow_store.add(_secret_flow())
    body = (await _call(app, "GET", "/api/v1/proxy/flows/f1",
                        params={"reveal": "authorization"})).json()
    assert body["request"]["headers"]["Authorization"] == SECRET
    assert "t0k3n" not in body["request"]["url"]                 # not named, still hidden
    assert body["revealed"] == ["authorization"]


async def test_reveal_true_shows_everything(app):
    await app.state.flow_store.add(_secret_flow())
    body = (await _call(app, "GET", "/api/v1/proxy/flows",
                        params={"reveal": "true"})).json()
    flow = body["flows"][0]
    assert flow["request"]["headers"]["Authorization"] == SECRET
    assert "access_token=t0k3n" in flow["request"]["url"]
    assert body["revealed"] == ["*"] and body["redacted"] is None


async def test_wait_for_flow_redacts(app):
    await app.state.flow_store.add(_secret_flow())
    body = (await _call(app, "POST", "/api/v1/proxy/flows/wait", json={
        "host": "api.example.com", "timeout": 1, "since": "2000-01-01T00:00:00Z",
    })).json()
    assert body["matched"] is True, body
    _assert_hidden(body["flow"])
    assert "authorization" in body["redacted"]


async def test_the_flow_summary_redacts_the_paths_it_prints(app):
    await app.state.flow_store.add(_secret_flow())
    body = (await _call(app, "GET", "/api/v1/proxy/flows/summary",
                        params={"window": "1h"})).json()
    assert "t0k3n" not in repr(body)


async def test_pending_rows_redact_their_urls(app):
    app.state.flow_store.note_started(_secret_flow("p1"))
    body = (await _call(app, "GET", "/api/v1/proxy/flows/pending")).json()
    assert len(body["pending"]) == 1, body                  # the row is there
    assert "t0k3n" not in body["pending"][0]["url"]


def test_the_stream_event_redacts_its_url():
    """The stream's route is an open-ended SSE generator; what it sends is
    the event this builds, through the same call."""
    from server.models import FlowEvent

    event = redact.redact_urls(FlowEvent.from_flow(_secret_flow()))
    assert "t0k3n" not in event.url and "t0k3n" not in event.path
