"""Tests for ProxyAdapter — parsing and emission without spawning mitmdump."""

import json
import os
import signal
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from server.models import LogLevel, LogSource
from server.proxy.flow_store import FlowStore
from server.sources.proxy import ADDON_PATH, ProxyAdapter, _classify_level, _format_summary

FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture_flows() -> list[dict]:
    """Load flow events from the fixture file."""
    flows = []
    for line in (FIXTURES / "proxy_flow_sample.jsonl").read_text().strip().split("\n"):
        data = json.loads(line)
        if data.get("type") == "flow":
            flows.append(data)
    return flows


@pytest.fixture
def flow_store():
    return FlowStore(max_size=100)


@pytest.fixture
def emitted_entries():
    return []


@pytest.fixture
def adapter(flow_store, emitted_entries):
    async def capture(entry):
        emitted_entries.append(entry)

    return ProxyAdapter(
        device_id="test",
        on_entry=capture,
        flow_store=flow_store,
    )


@pytest.mark.asyncio
async def test_handle_flow_200(adapter, flow_store, emitted_entries):
    """A 200 GET should create a FlowRecord and an INFO LogEntry."""
    flows = _load_fixture_flows()
    await adapter._handle_flow(flows[0])  # 200 GET

    # FlowRecord stored
    assert flow_store.size == 1
    record = await flow_store.get("f_aaa111")
    assert record is not None
    assert record.request.method == "GET"
    assert record.request.host == "api.example.com"
    assert record.response.status_code == 200

    # LogEntry emitted
    assert len(emitted_entries) == 1
    entry = emitted_entries[0]
    assert entry.source == LogSource.PROXY
    assert entry.level == LogLevel.INFO
    assert entry.process == "network"
    assert entry.subsystem == "api.example.com"
    assert "GET" in entry.message
    assert "200" in entry.message


@pytest.mark.asyncio
async def test_handle_flow_401(adapter, flow_store, emitted_entries):
    """A 401 POST should produce a WARNING LogEntry."""
    flows = _load_fixture_flows()
    await adapter._handle_flow(flows[1])  # 401 POST

    assert flow_store.size == 1
    record = await flow_store.get("f_bbb222")
    assert record.response.status_code == 401

    entry = emitted_entries[0]
    assert entry.level == LogLevel.WARNING
    assert "POST" in entry.message
    assert "401" in entry.message


@pytest.mark.asyncio
async def test_handle_flow_500(adapter, flow_store, emitted_entries):
    """A 500 response should produce an ERROR LogEntry."""
    flows = _load_fixture_flows()
    await adapter._handle_flow(flows[2])  # 500 GET

    entry = emitted_entries[0]
    assert entry.level == LogLevel.ERROR
    assert "500" in entry.message


@pytest.mark.asyncio
async def test_handle_flow_connection_error(adapter, flow_store, emitted_entries):
    """A connection error (no response) should produce an ERROR LogEntry."""
    flows = _load_fixture_flows()
    await adapter._handle_flow(flows[3])  # Connection refused

    record = await flow_store.get("f_ddd444")
    assert record.error == "Connection refused"
    assert record.response is None

    entry = emitted_entries[0]
    assert entry.level == LogLevel.ERROR
    assert "ERROR" in entry.message


@pytest.mark.asyncio
async def test_all_fixture_flows_parsed(adapter, flow_store, emitted_entries):
    """All fixture flows should parse without errors."""
    flows = _load_fixture_flows()
    for flow_data in flows:
        await adapter._handle_flow(flow_data)

    assert flow_store.size == len(flows)
    assert len(emitted_entries) == len(flows)


@pytest.mark.asyncio
async def test_parse_flow_with_timing(adapter, flow_store):
    """Timing data should be preserved in the FlowRecord."""
    flows = _load_fixture_flows()
    await adapter._handle_flow(flows[0])

    record = await flow_store.get("f_aaa111")
    assert record.timing.connect_ms == 12.4
    assert record.timing.tls_ms == 45.2
    assert record.timing.total_ms == 120.5


@pytest.mark.asyncio
async def test_parse_flow_with_tls(adapter, flow_store):
    """TLS info should be preserved in the FlowRecord."""
    flows = _load_fixture_flows()
    await adapter._handle_flow(flows[0])

    record = await flow_store.get("f_aaa111")
    assert record.tls is not None
    assert record.tls["version"] == "TLSv1.3"
    assert record.tls["sni"] == "api.example.com"


def test_classify_level_info():
    from server.models import FlowRecord, FlowRequest, FlowResponse, FlowTiming
    from datetime import datetime, timezone

    flow = FlowRecord(
        id="t", timestamp=datetime.now(timezone.utc),
        request=FlowRequest(method="GET", url="", host="", path=""),
        response=FlowResponse(status_code=200),
    )
    assert _classify_level(flow) == LogLevel.INFO


def test_classify_level_warning():
    from server.models import FlowRecord, FlowRequest, FlowResponse
    from datetime import datetime, timezone

    flow = FlowRecord(
        id="t", timestamp=datetime.now(timezone.utc),
        request=FlowRequest(method="GET", url="", host="", path=""),
        response=FlowResponse(status_code=404),
    )
    assert _classify_level(flow) == LogLevel.WARNING


def test_classify_level_error_5xx():
    from server.models import FlowRecord, FlowRequest, FlowResponse
    from datetime import datetime, timezone

    flow = FlowRecord(
        id="t", timestamp=datetime.now(timezone.utc),
        request=FlowRequest(method="GET", url="", host="", path=""),
        response=FlowResponse(status_code=503),
    )
    assert _classify_level(flow) == LogLevel.ERROR


def test_classify_level_error_connection():
    from server.models import FlowRecord, FlowRequest
    from datetime import datetime, timezone

    flow = FlowRecord(
        id="t", timestamp=datetime.now(timezone.utc),
        request=FlowRequest(method="GET", url="", host="", path=""),
        error="Connection refused",
    )
    assert _classify_level(flow) == LogLevel.ERROR


def test_format_summary_200():
    from server.models import FlowRecord, FlowRequest, FlowResponse, FlowTiming
    from datetime import datetime, timezone

    flow = FlowRecord(
        id="t", timestamp=datetime.now(timezone.utc),
        request=FlowRequest(method="GET", url="", host="", path="/v1/users"),
        response=FlowResponse(status_code=200, reason="OK", body_size=512),
        timing=FlowTiming(total_ms=120.5),
    )
    msg = _format_summary(flow)
    assert "GET /v1/users" in msg
    assert "200 OK" in msg
    assert "121ms" in msg or "120ms" in msg  # rounding


def test_format_summary_error():
    from server.models import FlowRecord, FlowRequest
    from datetime import datetime, timezone

    flow = FlowRecord(
        id="t", timestamp=datetime.now(timezone.utc),
        request=FlowRequest(method="GET", url="", host="", path="/health"),
        error="Connection refused",
    )
    msg = _format_summary(flow)
    assert "GET /health" in msg
    assert "Connection refused" in msg


def test_reconfigure_port():
    """Reconfigure should update listen_port when stopped."""
    adapter = ProxyAdapter(flow_store=FlowStore())
    assert adapter.listen_port == 9101
    adapter.reconfigure(listen_port=9090)
    assert adapter.listen_port == 9090


def test_reconfigure_host():
    """Reconfigure should update listen_host when stopped."""
    adapter = ProxyAdapter(flow_store=FlowStore())
    assert adapter.listen_host == "0.0.0.0"
    adapter.reconfigure(listen_host="127.0.0.1")
    assert adapter.listen_host == "127.0.0.1"


def test_reconfigure_while_running_raises():
    """Reconfigure should raise when adapter is running."""
    adapter = ProxyAdapter(flow_store=FlowStore())
    adapter._running = True
    with pytest.raises(RuntimeError, match="Cannot reconfigure while running"):
        adapter.reconfigure(listen_port=9090)
    adapter._running = False  # clean up


# ---------------------------------------------------------------------------
# Intercept/mock event handler tests (Phase 2c)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_intercepted(adapter, emitted_entries):
    """_handle_intercepted should store held flow and emit NOTICE entry."""
    import time
    import asyncio

    data = {
        "id": "f_int1",
        "timestamp": time.time(),
        "request": {
            "method": "POST",
            "url": "https://api.example.com/v1/login",
            "host": "api.example.com",
            "path": "/v1/login",
            "headers": {},
        },
    }
    adapter._handle_intercepted(data)

    assert "f_int1" in adapter._held_flows
    assert adapter._held_flows["f_int1"]["request"]["method"] == "POST"
    assert adapter._intercept_event.is_set()

    # Allow the fire-and-forget emit to complete
    await asyncio.sleep(0.05)
    assert len(emitted_entries) == 1
    entry = emitted_entries[0]
    assert entry.level == LogLevel.NOTICE
    assert "INTERCEPTED" in entry.message
    assert "POST" in entry.message


@pytest.mark.asyncio
async def test_handle_released(adapter):
    """_handle_released should remove flow from held_flows."""
    adapter._held_flows["f_rel"] = {"id": "f_rel", "held_at": None, "request": {}}
    adapter._handle_released({"id": "f_rel"})
    assert "f_rel" not in adapter._held_flows


@pytest.mark.asyncio
async def test_handle_released_unknown_noop(adapter):
    """_handle_released for unknown flow should be a no-op."""
    adapter._handle_released({"id": "f_nope"})
    assert "f_nope" not in adapter._held_flows


@pytest.mark.asyncio
async def test_handle_mock_hit(adapter, flow_store, emitted_entries):
    """_handle_mock_hit should create FlowRecord and emit MOCK log entry."""
    import time

    data = {
        "type": "mock_hit",
        "id": "f_mock1",
        "rule_id": "mock_test",
        "timestamp": time.time(),
        "request": {
            "method": "GET",
            "url": "https://api.example.com/v1/data",
            "host": "api.example.com",
            "path": "/v1/data",
            "headers": {},
            "body": None,
            "body_size": 0,
            "body_truncated": False,
            "body_encoding": "utf-8",
        },
        "response": {
            "status_code": 200,
            "reason": "",
            "headers": {"content-type": "application/json"},
            "body": '{"mocked": true}',
            "body_size": 16,
            "body_truncated": False,
            "body_encoding": "utf-8",
        },
    }

    await adapter._handle_mock_hit(data)

    # FlowRecord stored
    assert flow_store.size == 1
    record = await flow_store.get("f_mock1")
    assert record is not None
    assert record.response.status_code == 200

    # LogEntry emitted
    assert len(emitted_entries) == 1
    entry = emitted_entries[0]
    assert "MOCK" in entry.message
    assert "GET" in entry.message
    assert "200" in entry.message


@pytest.mark.asyncio
async def test_get_held_flows(adapter):
    """get_held_flows should return flows with computed age."""
    from datetime import datetime, timezone, timedelta

    now = datetime.now(timezone.utc)
    adapter._held_flows["f_1"] = {
        "id": "f_1",
        "held_at": now - timedelta(seconds=5),
        "request": {"method": "GET", "path": "/test"},
    }

    held = adapter.get_held_flows()
    assert len(held) == 1
    assert held[0]["id"] == "f_1"
    assert held[0]["age_seconds"] >= 4.0  # at least 4 seconds old


@pytest.mark.asyncio
async def test_wait_for_held_timeout(adapter):
    """wait_for_held should return False on timeout."""
    result = await adapter.wait_for_held(timeout=0.1)
    assert result is False


@pytest.mark.asyncio
async def test_wait_for_held_signal(adapter):
    """wait_for_held should return True when event is set."""
    import asyncio

    async def signal_after():
        await asyncio.sleep(0.05)
        adapter._intercept_event.set()

    task = asyncio.create_task(signal_after())
    result = await adapter.wait_for_held(timeout=2.0)
    assert result is True
    await task


@pytest.mark.asyncio
async def test_handle_status_event_intercept_set(adapter):
    """intercept_set status should update pattern."""
    adapter._handle_status_event({"event": "intercept_set", "pattern": "~d test.com"})
    assert adapter._intercept_pattern == "~d test.com"


@pytest.mark.asyncio
async def test_handle_status_event_intercept_cleared(adapter):
    """intercept_cleared status should clear pattern and held flows."""
    adapter._intercept_pattern = "~d test.com"
    adapter._held_flows["f_1"] = {"id": "f_1"}
    adapter._handle_status_event({"event": "intercept_cleared"})
    assert adapter._intercept_pattern is None
    assert len(adapter._held_flows) == 0


@pytest.mark.asyncio
async def test_handle_status_event_mocks_cleared(adapter):
    """mocks_cleared status should clear mock rules."""
    adapter._mock_rules = [{"rule_id": "a"}, {"rule_id": "b"}]
    adapter._handle_status_event({"event": "mocks_cleared", "rule_id": None})
    assert len(adapter._mock_rules) == 0


@pytest.mark.asyncio
async def test_handle_status_event_mock_cleared_specific_is_noop(adapter):
    """mocks_cleared with rule_id should be a no-op (caller already handled it)."""
    adapter._mock_rules = [{"rule_id": "a"}, {"rule_id": "b"}]
    adapter._handle_status_event({"event": "mocks_cleared", "rule_id": "a"})
    # Per-rule echo is ignored — both rules still present
    assert len(adapter._mock_rules) == 2


@pytest.mark.asyncio
async def test_update_mock_preserves_rule(adapter):
    """update_mock should correctly update the rule in _mock_rules."""
    from unittest.mock import AsyncMock
    adapter.send_command = AsyncMock()

    # Seed a rule
    adapter._mock_rules = [
        {"rule_id": "r1", "pattern": "~d api.example.com", "response": {"status_code": 200, "body": "ok"}},
    ]

    updated = await adapter.update_mock("r1", response={"status_code": 404, "body": "not found"})

    assert updated["rule_id"] == "r1"
    assert updated["pattern"] == "~d api.example.com"  # unchanged
    assert updated["response"]["status_code"] == 404

    # _mock_rules should have exactly one rule with the updated response
    assert len(adapter._mock_rules) == 1
    assert adapter._mock_rules[0]["response"]["status_code"] == 404

    # Simulate the echo arriving — should NOT wipe the rule
    adapter._handle_status_event({"event": "mocks_cleared", "rule_id": "r1"})
    assert len(adapter._mock_rules) == 1


# ---------------------------------------------------------------------------
# Server-side pattern validation (set_mock / set_intercept)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_mock_validates_pattern(adapter):
    """set_mock should reject invalid patterns before sending to addon."""
    adapter.send_command = MagicMock()  # should never be called

    with pytest.raises(ValueError, match="Invalid filter expression"):
        await adapter.set_mock(
            pattern="~p /api/v2/filters",
            response={"status_code": 404, "body": "not found"},
        )

    assert len(adapter._mock_rules) == 0
    adapter.send_command.assert_not_called()


@pytest.mark.asyncio
async def test_set_intercept_validates_pattern(adapter):
    """set_intercept should reject invalid patterns before sending to addon."""
    adapter.send_command = MagicMock()

    with pytest.raises(ValueError, match="Invalid filter expression"):
        await adapter.set_intercept("~p /api/v2/filters")

    assert adapter._intercept_pattern is None
    adapter.send_command.assert_not_called()


@pytest.mark.asyncio
async def test_set_mock_stores_response(adapter):
    """set_mock should store the response dict in _mock_rules for list_mocks."""
    from unittest.mock import AsyncMock
    adapter.send_command = AsyncMock()

    response = {"status_code": 404, "body": '{"error": "not found"}', "headers": {"content-type": "text/plain"}}
    rule_id = await adapter.set_mock(
        pattern="~d api.example.com",
        response=response,
    )

    assert len(adapter._mock_rules) == 1
    stored = adapter._mock_rules[0]
    assert stored["rule_id"] == rule_id
    assert stored["response"] == response
    assert stored["response"]["status_code"] == 404


# ---------------------------------------------------------------------------
# Stale mitmdump cleanup tests
# ---------------------------------------------------------------------------


def _mock_run(responses):
    """Create a side_effect function for subprocess.run that returns responses in order."""
    calls = iter(responses)

    def side_effect(*args, **kwargs):
        resp = next(calls)
        mock = MagicMock()
        mock.returncode = resp.get("returncode", 0)
        mock.stdout = resp.get("stdout", "")
        return mock

    return side_effect


def test_kill_stale_mitmdump_kills_our_process():
    """Should kill a stale mitmdump running our addon."""
    addon_cmd = f"/usr/bin/mitmdump -s {ADDON_PATH} --listen-port 9101"
    responses = [
        {"returncode": 0, "stdout": "12345\n"},      # lsof
        {"returncode": 0, "stdout": addon_cmd},       # ps
    ]

    with patch("server.sources.proxy.subprocess.run", side_effect=_mock_run(responses)):
        with patch("server.sources.proxy.os.kill") as mock_kill:
            # Make the process disappear after SIGTERM
            mock_kill.side_effect = lambda pid, sig: (
                None if sig == signal.SIGTERM
                else (_ for _ in ()).throw(ProcessLookupError)
            )
            # os.kill(pid, 0) check — process gone
            def kill_effect(pid, sig):
                if sig == 0:
                    raise ProcessLookupError
            mock_kill.side_effect = [None, ProcessLookupError]

            ProxyAdapter._kill_stale_mitmdump(9101)

            # First call: SIGTERM
            assert mock_kill.call_count >= 1
            mock_kill.assert_any_call(12345, signal.SIGTERM)


def test_kill_stale_mitmdump_skips_foreign_process():
    """Should NOT kill a process that isn't our mitmdump."""
    responses = [
        {"returncode": 0, "stdout": "99999\n"},                      # lsof
        {"returncode": 0, "stdout": "/usr/bin/nginx -g daemon off;"}, # ps
    ]

    with patch("server.sources.proxy.subprocess.run", side_effect=_mock_run(responses)):
        with patch("server.sources.proxy.os.kill") as mock_kill:
            ProxyAdapter._kill_stale_mitmdump(9101)
            mock_kill.assert_not_called()


def test_kill_stale_mitmdump_port_free():
    """Should do nothing when the port is free."""
    responses = [
        {"returncode": 1, "stdout": ""},  # lsof — nothing found
    ]

    with patch("server.sources.proxy.subprocess.run", side_effect=_mock_run(responses)):
        with patch("server.sources.proxy.os.kill") as mock_kill:
            ProxyAdapter._kill_stale_mitmdump(9101)
            mock_kill.assert_not_called()


def test_kill_stale_mitmdump_lsof_fails():
    """Should not crash if lsof raises an exception."""
    with patch("server.sources.proxy.subprocess.run", side_effect=OSError("lsof not found")):
        with patch("server.sources.proxy.os.kill") as mock_kill:
            # Should not raise
            ProxyAdapter._kill_stale_mitmdump(9101)
            mock_kill.assert_not_called()
