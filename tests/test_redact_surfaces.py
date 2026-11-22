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


# -- intercepts, replay and mocks: shown redacted, sent real ---------------------


@pytest.fixture
def running(app):
    """A running proxy adapter whose commands to the addon are recorded."""
    from unittest.mock import AsyncMock

    from server.sources.proxy import ProxyAdapter

    store = FlowStore()
    adapter = ProxyAdapter(flow_store=store, listen_port=9101)
    adapter._running = True
    adapter.started_at = datetime.now(UTC)
    adapter.send_command = AsyncMock()
    app.state.proxy_adapter = adapter
    app.state.flow_store = store
    return adapter


def _sent(adapter, action):
    return [c.args[0] for c in adapter.send_command.call_args_list
            if c.args[0].get("action") == action]


async def _held(app, adapter):
    adapter._held_flows["h1"] = {
        "id": "h1", "held_at": datetime.now(UTC),
        "request": {"method": "GET", "url": "https://api.example.com/me?token=t0k3n",
                    "host": "api.example.com", "path": "/me?token=t0k3n",
                    "headers": {"Authorization": SECRET}},
    }
    return (await _call(app, "GET", "/api/v1/proxy/intercept/held")).json()


async def test_held_flows_are_shown_redacted(app, running):
    body = await _held(app, running)
    request = body["held_flows"][0]["request"]
    assert "s3cr3t-app-token" not in repr(request) and "t0k3n" not in repr(request)
    assert body["redacted"] == ["authorization", "token"]


async def test_a_release_copying_the_shown_headers_sends_the_real_token(app, running):
    """The agent changes one header and passes back the rest as it was shown:
    the request that goes out carries the real token, not the placeholder,
    which would fail with a 401 that looks like the app's own bug."""
    shown = (await _held(app, running))["held_flows"][0]["request"]
    resp = await _call(app, "POST", "/api/v1/proxy/intercept/release", json={
        "flow_id": "h1",
        "modifications": {"headers": {**shown["headers"], "X-Debug": "1"},
                          "url": shown["url"]},
    })
    assert resp.status_code == 200, resp.text
    (sent,) = _sent(running, "modify_and_release")
    assert sent["modifications"]["headers"]["Authorization"] == SECRET
    assert sent["modifications"]["headers"]["X-Debug"] == "1"
    assert sent["modifications"]["url"] == "https://api.example.com/me?token=t0k3n"
    assert resp.json()["restored"] == ["Authorization", "url"]


async def test_a_placeholder_this_server_never_showed_is_refused(app, running):
    await _held(app, running)
    resp = await _call(app, "POST", "/api/v1/proxy/intercept/release", json={
        "flow_id": "h1",
        "modifications": {"headers": {
            "Authorization": "Bearer ‹redacted, 9 chars, id 0123456789ab›"}},
    })
    assert resp.status_code == 400 and "not known to this server" in resp.text
    assert _sent(running, "modify_and_release") == []           # nothing went out


async def test_a_replay_with_a_shown_header_sends_the_real_one(app, running):
    from unittest.mock import AsyncMock, patch

    await running.flow_store.add(_secret_flow("r1"))
    shown = (await _call(app, "GET", "/api/v1/proxy/flows/r1")).json()
    with patch("server.api.proxy_intercept.httpx.AsyncClient") as cls:
        client = AsyncMock()
        client.request = AsyncMock(return_value=AsyncMock(status_code=200))
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        cls.return_value = client
        resp = await _call(app, "POST", "/api/v1/proxy/replay/r1", json={
            "modify_headers": {"Authorization": shown["request"]["headers"]["Authorization"],
                               "X-Debug": "1"}})
    assert resp.status_code == 200, resp.text
    sent = client.request.call_args.kwargs["headers"]
    assert sent["Authorization"] == SECRET and sent["X-Debug"] == "1"
    assert resp.json()["restored"] == ["Authorization"]


async def test_a_mock_set_from_a_shown_cookie_holds_the_real_one_and_lists_it_redacted(
        app, running):
    await running.flow_store.add(_secret_flow("m1"))
    shown = (await _call(app, "GET", "/api/v1/proxy/flows/m1")).json()
    cookie = shown["response"]["headers"]["Set-Cookie"]
    resp = await _call(app, "POST", "/api/v1/proxy/mocks", json={
        "pattern": "~d api.example.com",
        "response": {"status_code": 200, "headers": {"Set-Cookie": cookie}, "body": ""}})
    assert resp.status_code == 200, resp.text
    (sent,) = _sent(running, "set_mock")
    assert sent["response"]["headers"]["Set-Cookie"] == "sid=c00kie; Path=/"
    assert resp.json()["restored"] == ["Set-Cookie"]
    listed = (await _call(app, "GET", "/api/v1/proxy/mocks")).json()
    assert "c00kie" not in repr(listed)                 # the agent was never shown it


# -- the log buffer ---------------------------------------------------------------


def test_a_flows_log_line_redacts_its_query():
    from server.sources.proxy import _format_summary

    line = _format_summary(_secret_flow())
    assert line.startswith("GET /v1/me?access_token=‹redacted") and "t0k3n" not in line
    assert "&page=2" in line


async def test_an_intercepted_log_line_redacts_its_query(running):
    import asyncio

    entries = []

    async def emit(entry):
        entries.append(entry)
    running.emit = emit
    running._handle_intercepted({"id": "h9", "request": {
        "method": "GET", "path": "/me?token=t0k3n", "host": "api.example.com"}})
    await asyncio.sleep(0)
    assert entries and "t0k3n" not in entries[0].message
    assert entries[0].message.startswith("INTERCEPTED: GET /me?token=‹redacted")


# -- recordings ---------------------------------------------------------------------


def _record(flow, *, redact_on: bool):
    from types import SimpleNamespace

    from server.recording.recorder import Filters, RecordingManager

    rec = SimpleNamespace(filters=Filters(redact=redact_on))
    return RecordingManager._record_of(rec, "flow", flow), rec.filters


def test_a_recorded_flow_is_redacted_by_default():
    line, filters = _record(_secret_flow(), redact_on=True)
    assert "s3cr3t-app-token" not in repr(line) and "t0k3n" not in repr(line)
    assert filters.as_dict()["redact"] is True              # said in the manifest


def test_redact_false_records_the_real_values_and_says_so():
    line, filters = _record(_secret_flow(), redact_on=False)
    assert line["request"]["headers"]["Authorization"] == SECRET
    assert filters.as_dict()["redact"] is False


def test_a_recording_from_before_redaction_resumes_unredacted():
    """Half of one recording redacted would mislead whoever reads it."""
    from server.recording.recorder import Filters

    assert Filters.from_dict({"bodies": "all"}).redact is False
    assert Filters.from_dict({"redact": True}).redact is True


# -- the trace ---------------------------------------------------------------------


def test_a_traced_flow_redacts_its_url():
    from server.api.trace import _serialise
    from server.trace import Attribution

    class _Action:
        """Every field the serialiser reads: a real time and duration, the
        rest None."""

        timestamp = datetime.now(UTC)
        duration_ms = 0.0

        def __getattr__(self, name):
            return None

    entry = _serialise(Attribution(action=_Action(), flows=[_secret_flow()]), {})
    (flow,) = entry["flows"]
    assert "t0k3n" not in flow["url"] and "&page=2" in flow["url"]
