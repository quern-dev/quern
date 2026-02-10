"""Tests for the flow summary generator."""

from datetime import datetime, timedelta, timezone

import pytest

from server.models import (
    FlowRecord,
    FlowRequest,
    FlowResponse,
    FlowTiming,
)
from server.processing.summarizer import make_cursor, parse_cursor
from server.proxy.summary import generate_flow_summary


def _make_flow(
    flow_id: str = "f_test",
    method: str = "GET",
    host: str = "api.example.com",
    path: str = "/v1/test",
    status_code: int = 200,
    total_ms: float | None = 100.0,
    error: str | None = None,
    timestamp: datetime | None = None,
) -> FlowRecord:
    response = None if error and status_code == 0 else FlowResponse(
        status_code=status_code, reason="OK"
    )
    return FlowRecord(
        id=flow_id,
        timestamp=timestamp or datetime.now(timezone.utc),
        request=FlowRequest(
            method=method,
            url=f"https://{host}{path}",
            host=host,
            path=path,
        ),
        response=response,
        timing=FlowTiming(total_ms=total_ms),
        error=error,
    )


class TestGenerateFlowSummary:
    def test_empty_flows(self):
        result = generate_flow_summary([], window="5m")
        assert result.total_flows == 0
        assert result.by_host == []
        assert result.errors == []
        assert result.slow_requests == []
        assert "No HTTP traffic" in result.summary
        assert result.cursor.startswith("c_")

    def test_empty_flows_with_host_filter(self):
        result = generate_flow_summary([], window="5m", host="api.example.com")
        assert "No HTTP traffic to api.example.com" in result.summary

    def test_single_success(self):
        flows = [_make_flow()]
        result = generate_flow_summary(flows, window="5m")
        assert result.total_flows == 1
        assert len(result.by_host) == 1
        assert result.by_host[0].host == "api.example.com"
        assert result.by_host[0].success == 1
        assert result.by_host[0].client_error == 0
        assert result.by_host[0].server_error == 0

    def test_mixed_status_codes(self):
        now = datetime.now(timezone.utc)
        flows = [
            _make_flow(flow_id="f1", status_code=200, timestamp=now),
            _make_flow(flow_id="f2", status_code=201, timestamp=now + timedelta(seconds=1)),
            _make_flow(flow_id="f3", status_code=404, timestamp=now + timedelta(seconds=2)),
            _make_flow(flow_id="f4", status_code=500, timestamp=now + timedelta(seconds=3)),
            _make_flow(
                flow_id="f5", status_code=0, error="Connection refused",
                timestamp=now + timedelta(seconds=4),
            ),
        ]
        result = generate_flow_summary(flows, window="5m")
        assert result.total_flows == 5

        host = result.by_host[0]
        assert host.host == "api.example.com"
        assert host.success == 2
        assert host.client_error == 1
        assert host.server_error == 1
        assert host.connection_errors == 1

    def test_multiple_hosts(self):
        flows = [
            _make_flow(flow_id="f1", host="api.example.com"),
            _make_flow(flow_id="f2", host="api.example.com"),
            _make_flow(flow_id="f3", host="cdn.example.com"),
        ]
        result = generate_flow_summary(flows, window="5m")
        assert len(result.by_host) == 2
        # Sorted by count, api.example.com should be first
        assert result.by_host[0].host == "api.example.com"
        assert result.by_host[0].total == 2
        assert result.by_host[1].host == "cdn.example.com"
        assert result.by_host[1].total == 1

    def test_host_filter(self):
        flows = [
            _make_flow(flow_id="f1", host="api.example.com"),
            _make_flow(flow_id="f2", host="cdn.example.com"),
        ]
        result = generate_flow_summary(flows, window="5m", host="api.example.com")
        assert result.total_flows == 1
        assert len(result.by_host) == 1
        assert result.by_host[0].host == "api.example.com"

    def test_error_pattern_extraction(self):
        now = datetime.now(timezone.utc)
        flows = [
            _make_flow(flow_id="f1", method="POST", path="/v1/login", status_code=401,
                        timestamp=now),
            _make_flow(flow_id="f2", method="POST", path="/v1/login", status_code=401,
                        timestamp=now + timedelta(seconds=1)),
            _make_flow(flow_id="f3", method="GET", path="/v1/data", status_code=500,
                        timestamp=now + timedelta(seconds=2)),
        ]
        result = generate_flow_summary(flows, window="5m")
        assert len(result.errors) == 2

        # Sorted by count, the 401 pattern should be first
        assert result.errors[0].count == 2
        assert "POST /v1/login" in result.errors[0].pattern
        assert "401" in result.errors[0].pattern
        assert result.errors[1].count == 1
        assert "500" in result.errors[1].pattern

    def test_error_pattern_with_connection_error(self):
        flows = [
            _make_flow(flow_id="f1", status_code=0, error="Connection refused"),
        ]
        result = generate_flow_summary(flows, window="5m")
        assert len(result.errors) == 1
        assert "Connection refused" in result.errors[0].pattern

    def test_slow_request_identification(self):
        flows = [
            _make_flow(flow_id="f1", total_ms=50.0),
            _make_flow(flow_id="f2", total_ms=1500.0, path="/v1/upload"),
            _make_flow(flow_id="f3", total_ms=2300.0, path="/v1/export"),
        ]
        result = generate_flow_summary(flows, window="5m")
        assert len(result.slow_requests) == 2
        # Sorted by total_ms descending
        assert result.slow_requests[0].total_ms == 2300.0
        assert result.slow_requests[1].total_ms == 1500.0

    def test_slow_requests_not_flagged_under_threshold(self):
        flows = [
            _make_flow(flow_id="f1", total_ms=500.0),
            _make_flow(flow_id="f2", total_ms=999.0),
        ]
        result = generate_flow_summary(flows, window="5m")
        assert len(result.slow_requests) == 0

    def test_avg_latency(self):
        flows = [
            _make_flow(flow_id="f1", total_ms=100.0),
            _make_flow(flow_id="f2", total_ms=200.0),
            _make_flow(flow_id="f3", total_ms=300.0),
        ]
        result = generate_flow_summary(flows, window="5m")
        assert result.by_host[0].avg_latency_ms == 200.0

    def test_avg_latency_none_when_no_timing(self):
        flows = [
            _make_flow(flow_id="f1", total_ms=None),
        ]
        result = generate_flow_summary(flows, window="5m")
        assert result.by_host[0].avg_latency_ms is None

    def test_summary_text_with_errors(self):
        flows = [
            _make_flow(flow_id="f1", status_code=200),
            _make_flow(flow_id="f2", status_code=500, path="/v1/fail"),
        ]
        result = generate_flow_summary(flows, window="5m")
        assert "2 requests" in result.summary
        assert "1 host" in result.summary
        assert "error" in result.summary.lower()

    def test_summary_text_no_errors(self):
        flows = [
            _make_flow(flow_id="f1", status_code=200),
            _make_flow(flow_id="f2", status_code=201),
        ]
        result = generate_flow_summary(flows, window="5m")
        assert "No errors" in result.summary

    def test_summary_text_with_slow_requests(self):
        flows = [
            _make_flow(flow_id="f1", total_ms=2000.0, path="/v1/slow"),
        ]
        result = generate_flow_summary(flows, window="5m")
        assert "slow" in result.summary.lower()

    def test_cursor_round_trip(self):
        now = datetime.now(timezone.utc)
        flows = [_make_flow(timestamp=now)]
        result = generate_flow_summary(flows, window="5m")

        cursor = result.cursor
        assert cursor.startswith("c_")
        parsed = parse_cursor(cursor)
        assert parsed is not None
        # Should be close to the flow timestamp
        assert abs((parsed - now).total_seconds()) < 1

    def test_window_label_preserved(self):
        result = generate_flow_summary([], window="15m")
        assert result.window == "15m"

    def test_generated_at_is_recent(self):
        before = datetime.now(timezone.utc)
        result = generate_flow_summary([], window="5m")
        after = datetime.now(timezone.utc)
        assert before <= result.generated_at <= after
