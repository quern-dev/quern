"""Tests for the mitmproxy addon's intercept, mock, and timeout logic.

Uses mock flow objects to avoid mitmproxy.test.tflow version issues.
The important thing is testing the addon's logic, not mitmproxy's internals.
"""

import json
import sys
import time
import threading
from unittest.mock import MagicMock, patch

import pytest

from server.proxy.addon import IOSDebugAddon, _serialize_request


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class CapturedOutput:
    """Captures JSON lines written to stdout by the addon."""

    def __init__(self):
        self.lines: list[dict] = []
        self._buffer_write = None

    def install(self):
        """Monkey-patch sys.stdout.buffer.write to capture output."""
        original_write = sys.stdout.buffer.write

        def capture_write(data: bytes) -> int:
            try:
                text = data.decode("utf-8").strip()
                if text:
                    self.lines.append(json.loads(text))
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
            return len(data)

        self._buffer_write = original_write
        sys.stdout.buffer.write = capture_write
        return self

    def restore(self):
        if self._buffer_write:
            sys.stdout.buffer.write = self._buffer_write

    def of_type(self, msg_type: str) -> list[dict]:
        return [m for m in self.lines if m.get("type") == msg_type]


def _make_mock_flow(
    method: str = "GET",
    host: str = "api.example.com",
    path: str = "/v1/test",
    url: str = "https://api.example.com/v1/test",
    scheme: str = "https",
) -> MagicMock:
    """Create a mock mitmproxy HTTPFlow with the minimum required attributes."""
    flow = MagicMock()
    flow.request = MagicMock()
    flow.request.method = method
    flow.request.pretty_url = url
    flow.request.pretty_host = host
    flow.request.path = path
    flow.request.scheme = scheme
    flow.request.raw_content = b""
    flow.request.headers = MagicMock()
    flow.request.headers.items.return_value = []
    flow.request.timestamp_start = time.time()
    flow.response = None
    flow.error = None
    flow.client_conn = MagicMock()
    flow.client_conn.tls_version = None
    flow.client_conn.sni = None

    # intercept() and resume() are the key methods
    flow.intercept = MagicMock()
    flow.resume = MagicMock()

    return flow


@pytest.fixture
def addon():
    """Create an addon instance (without calling load)."""
    a = IOSDebugAddon()
    a._running = True
    return a


@pytest.fixture
def output():
    """Capture stdout JSON lines."""
    cap = CapturedOutput().install()
    yield cap
    cap.restore()


# ---------------------------------------------------------------------------
# Intercept: request() hook
# ---------------------------------------------------------------------------


def test_request_no_intercept_passthrough(addon, output):
    """With no intercept pattern, request() should not hold the flow."""
    flow = _make_mock_flow()
    addon.request(flow)
    flow.intercept.assert_not_called()
    assert len(output.of_type("intercepted")) == 0


def test_request_matching_intercept_holds(addon, output):
    """Matching intercept pattern should hold the flow and emit event."""
    # Use a lambda to test addon logic without depending on flowfilter's
    # internal matching against MagicMock flows
    addon._intercept_compiled = lambda f: f.request.pretty_host == "api.example.com"
    addon._intercept_pattern = "~d api.example.com"

    flow = _make_mock_flow(host="api.example.com")
    addon.request(flow)

    flow.intercept.assert_called_once()
    events = output.of_type("intercepted")
    assert len(events) == 1
    assert events[0]["request"]["host"] == "api.example.com"

    # Should be in held_flows
    with addon._held_lock:
        assert len(addon._held_flows) == 1


def test_request_non_matching_intercept_passthrough(addon, output):
    """Non-matching intercept pattern should not hold the flow."""
    addon._intercept_compiled = lambda f: f.request.pretty_host == "other.example.com"
    addon._intercept_pattern = "~d other.example.com"

    flow = _make_mock_flow(host="api.example.com")
    addon.request(flow)

    flow.intercept.assert_not_called()
    assert len(output.of_type("intercepted")) == 0


# ---------------------------------------------------------------------------
# Mock: request() hook
# ---------------------------------------------------------------------------


def test_request_mock_match_returns_response(addon, output):
    """Matching mock rule should set flow.response and emit mock_hit."""
    addon._mock_rules.append({
        "rule_id": "mock_1",
        "pattern_str": "~d api.example.com",
        "compiled": lambda f: f.request.pretty_host == "api.example.com",
        "response": {
            "status_code": 200,
            "headers": {"content-type": "application/json"},
            "body": '{"mocked": true}',
        },
    })

    flow = _make_mock_flow(host="api.example.com")
    flow.response = None

    with patch("server.proxy.addon.http.Response.make") as mock_make:
        mock_make.return_value = MagicMock()
        addon.request(flow)
        mock_make.assert_called_once()

    events = output.of_type("mock_hit")
    assert len(events) == 1
    assert events[0]["rule_id"] == "mock_1"
    assert events[0]["response"]["status_code"] == 200

    # Flow should NOT be intercepted (mock takes priority)
    flow.intercept.assert_not_called()


def test_request_mock_custom_status_code(addon, output):
    """Mock with non-200 status_code should pass it through to Response.make."""
    addon._mock_rules.append({
        "rule_id": "mock_404",
        "pattern_str": "~d api.example.com",
        "compiled": lambda f: f.request.pretty_host == "api.example.com",
        "response": {
            "status_code": 404,
            "headers": {"content-type": "text/plain"},
            "body": '{"error": "not found"}',
        },
    })

    flow = _make_mock_flow(host="api.example.com")
    flow.response = None

    with patch("server.proxy.addon.http.Response.make") as mock_make:
        mock_make.return_value = MagicMock()
        addon.request(flow)
        mock_make.assert_called_once_with(
            404,
            b'{"error": "not found"}',
            {"content-type": "text/plain"},
        )

    events = output.of_type("mock_hit")
    assert len(events) == 1
    assert events[0]["response"]["status_code"] == 404
    assert events[0]["response"]["body"] == '{"error": "not found"}'


def test_mock_priority_over_intercept(addon, output):
    """When both mock and intercept match, mock should win."""
    addon._intercept_compiled = lambda f: f.request.pretty_host == "api.example.com"
    addon._intercept_pattern = "~d api.example.com"

    addon._mock_rules.append({
        "rule_id": "mock_priority",
        "pattern_str": "~d api.example.com",
        "compiled": lambda f: f.request.pretty_host == "api.example.com",
        "response": {"status_code": 418, "headers": {}, "body": "teapot"},
    })

    flow = _make_mock_flow(host="api.example.com")

    with patch("server.proxy.addon.http.Response.make") as mock_make:
        mock_make.return_value = MagicMock()
        addon.request(flow)

    # Mock hit, not intercepted
    assert len(output.of_type("mock_hit")) == 1
    assert len(output.of_type("intercepted")) == 0
    flow.intercept.assert_not_called()


# ---------------------------------------------------------------------------
# Timeout thread
# ---------------------------------------------------------------------------


def test_timeout_auto_releases(addon, output):
    """Held flows should auto-release after timeout."""
    addon._timeout_seconds = 0.5  # Short timeout for test

    flow = _make_mock_flow()
    with addon._held_lock:
        addon._held_flows["f_timeout"] = (flow, time.time() - 1.0)  # already expired

    # Start timeout thread
    timeout_thread = threading.Thread(target=addon._run_timeout_loop, daemon=True)
    timeout_thread.start()

    # Wait for the thread to process
    time.sleep(1.5)
    addon._running = False
    timeout_thread.join(timeout=2.0)

    flow.resume.assert_called_once()
    events = output.of_type("released")
    assert len(events) == 1
    assert events[0]["id"] == "f_timeout"
    assert events[0]["reason"] == "timeout"


# ---------------------------------------------------------------------------
# Stdin commands
# ---------------------------------------------------------------------------


def test_handle_set_intercept_valid(addon, output):
    """Valid pattern should be compiled and set."""
    addon._handle_set_intercept({"pattern": "~d api.example.com"})

    with addon._held_lock:
        assert addon._intercept_pattern == "~d api.example.com"
        assert addon._intercept_compiled is not None

    events = output.of_type("status")
    assert any(e.get("event") == "intercept_set" for e in events)


def test_handle_set_intercept_invalid(addon, output):
    """Invalid pattern should emit error and not set."""
    addon._handle_set_intercept({"pattern": "~invalid_garbage !!!"})

    with addon._held_lock:
        assert addon._intercept_pattern is None
        assert addon._intercept_compiled is None

    errors = output.of_type("error")
    assert len(errors) == 1
    assert errors[0]["event"] == "invalid_intercept_pattern"


def test_handle_clear_intercept_releases_all(addon, output):
    """Clearing intercept should release all held flows."""
    flow1 = _make_mock_flow()
    flow2 = _make_mock_flow()

    with addon._held_lock:
        addon._intercept_pattern = "~d api.example.com"
        addon._held_flows["f_1"] = (flow1, time.time())
        addon._held_flows["f_2"] = (flow2, time.time())

    addon._handle_clear_intercept()

    flow1.resume.assert_called_once()
    flow2.resume.assert_called_once()

    with addon._held_lock:
        assert addon._intercept_pattern is None
        assert len(addon._held_flows) == 0

    released = output.of_type("released")
    assert len(released) == 2
    assert all(e["reason"] == "intercept_cleared" for e in released)


def test_handle_release_flow(addon, output):
    """Releasing a single flow should resume it."""
    flow = _make_mock_flow()
    with addon._held_lock:
        addon._held_flows["f_rel"] = (flow, time.time())

    addon._handle_release_flow({"flow_id": "f_rel"})

    flow.resume.assert_called_once()
    with addon._held_lock:
        assert "f_rel" not in addon._held_flows

    released = output.of_type("released")
    assert len(released) == 1
    assert released[0]["reason"] == "manual"


def test_handle_release_unknown_flow_noop(addon, output):
    """Releasing an unknown flow should be a no-op."""
    addon._handle_release_flow({"flow_id": "f_nope"})
    assert len(output.of_type("released")) == 0


def test_handle_modify_and_release(addon, output):
    """Modify-and-release should apply changes then resume."""
    flow = _make_mock_flow()
    with addon._held_lock:
        addon._held_flows["f_mod"] = (flow, time.time())

    addon._handle_modify_and_release({
        "flow_id": "f_mod",
        "modifications": {
            "method": "POST",
            "headers": {"x-custom": "value"},
        },
    })

    # Verify modifications were applied
    assert flow.request.method == "POST"
    flow.request.headers.__setitem__.assert_called_with("x-custom", "value")
    flow.resume.assert_called_once()

    released = output.of_type("released")
    assert len(released) == 1
    assert released[0]["reason"] == "modified"


def test_handle_release_all(addon, output):
    """Release all should resume all held flows."""
    flows = [_make_mock_flow() for _ in range(3)]
    with addon._held_lock:
        for i, flow in enumerate(flows):
            addon._held_flows[f"f_{i}"] = (flow, time.time())

    addon._handle_release_all()

    for flow in flows:
        flow.resume.assert_called_once()

    with addon._held_lock:
        assert len(addon._held_flows) == 0

    released = output.of_type("released")
    assert len(released) == 3


# ---------------------------------------------------------------------------
# Mock commands
# ---------------------------------------------------------------------------


def test_handle_set_mock_valid(addon, output):
    """Valid mock rule should be compiled and added."""
    addon._handle_set_mock({
        "rule_id": "mock_test",
        "pattern": "~d api.example.com",
        "response": {"status_code": 200, "body": "ok"},
    })

    with addon._mock_lock:
        assert len(addon._mock_rules) == 1
        assert addon._mock_rules[0]["rule_id"] == "mock_test"

    events = output.of_type("status")
    assert any(e.get("event") == "mock_set" for e in events)


def test_handle_set_mock_invalid(addon, output):
    """Invalid mock pattern should emit error and not add rule."""
    addon._handle_set_mock({
        "rule_id": "mock_bad",
        "pattern": "~invalid_garbage !!!",
        "response": {"status_code": 200},
    })

    with addon._mock_lock:
        assert len(addon._mock_rules) == 0

    errors = output.of_type("error")
    assert len(errors) == 1
    assert errors[0]["event"] == "invalid_mock_pattern"


def test_handle_set_mock_tilde_p_is_invalid(addon, output):
    """~p is not a valid mitmproxy filter operator and should be rejected."""
    addon._handle_set_mock({
        "rule_id": "mock_path",
        "pattern": "~p /api/v2/filters",
        "response": {"status_code": 404, "body": "not found"},
    })

    with addon._mock_lock:
        assert len(addon._mock_rules) == 0

    errors = output.of_type("error")
    assert len(errors) == 1
    assert errors[0]["event"] == "invalid_mock_pattern"


def test_handle_clear_mock_specific(addon, output):
    """Clearing a specific mock rule should remove only that rule."""
    with addon._mock_lock:
        addon._mock_rules = [
            {"rule_id": "a", "pattern_str": "x", "compiled": None, "response": {}},
            {"rule_id": "b", "pattern_str": "y", "compiled": None, "response": {}},
        ]

    addon._handle_clear_mock({"rule_id": "a"})

    with addon._mock_lock:
        assert len(addon._mock_rules) == 1
        assert addon._mock_rules[0]["rule_id"] == "b"


def test_handle_clear_mock_all(addon, output):
    """Clearing all mock rules should empty the list."""
    with addon._mock_lock:
        addon._mock_rules = [
            {"rule_id": "a", "pattern_str": "x", "compiled": None, "response": {}},
            {"rule_id": "b", "pattern_str": "y", "compiled": None, "response": {}},
        ]

    addon._handle_clear_mock({})

    with addon._mock_lock:
        assert len(addon._mock_rules) == 0


# ---------------------------------------------------------------------------
# done() hook
# ---------------------------------------------------------------------------


def test_done_resumes_all_held(addon, output):
    """Shutting down should resume all held flows."""
    flows = [_make_mock_flow() for _ in range(2)]
    with addon._held_lock:
        for i, flow in enumerate(flows):
            addon._held_flows[f"f_{i}"] = (flow, time.time())

    addon.done()

    for flow in flows:
        flow.resume.assert_called_once()

    released = output.of_type("released")
    assert len(released) == 2
    assert all(e["reason"] == "shutdown" for e in released)

    # Should also emit stopped status
    status = output.of_type("status")
    assert any(e.get("event") == "stopped" for e in status)
