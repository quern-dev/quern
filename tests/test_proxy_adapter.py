"""Tests for ProxyAdapter — parsing and emission without spawning mitmdump."""

import json
from pathlib import Path

import pytest

from server.models import LogLevel, LogSource
from server.proxy.flow_store import FlowStore
from server.sources.proxy import ProxyAdapter, _classify_level, _format_summary

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
