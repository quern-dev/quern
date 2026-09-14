"""Tests for the mitmproxy addon's intercept, mock, and timeout logic.

Uses mock flow objects to avoid mitmproxy.test.tflow version issues.
The important thing is testing the addon's logic, not mitmproxy's internals.
"""

import json
import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from server.proxy.addon import IOSDebugAddon

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
    addon._mock_rules.append(
        {
            "rule_id": "mock_1",
            "pattern_str": "~d api.example.com",
            "compiled": lambda f: f.request.pretty_host == "api.example.com",
            "response": {
                "status_code": 200,
                "headers": {"content-type": "application/json"},
                "body": '{"mocked": true}',
            },
        }
    )

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
    addon._mock_rules.append(
        {
            "rule_id": "mock_404",
            "pattern_str": "~d api.example.com",
            "compiled": lambda f: f.request.pretty_host == "api.example.com",
            "response": {
                "status_code": 404,
                "headers": {"content-type": "text/plain"},
                "body": '{"error": "not found"}',
            },
        }
    )

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

    addon._mock_rules.append(
        {
            "rule_id": "mock_priority",
            "pattern_str": "~d api.example.com",
            "compiled": lambda f: f.request.pretty_host == "api.example.com",
            "response": {"status_code": 418, "headers": {}, "body": "teapot"},
        }
    )

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

    addon._handle_modify_and_release(
        {
            "flow_id": "f_mod",
            "modifications": {
                "method": "POST",
                "headers": {"x-custom": "value"},
            },
        }
    )

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
    addon._handle_set_mock(
        {
            "rule_id": "mock_test",
            "pattern": "~d api.example.com",
            "response": {"status_code": 200, "body": "ok"},
        }
    )

    with addon._mock_lock:
        assert len(addon._mock_rules) == 1
        assert addon._mock_rules[0]["rule_id"] == "mock_test"

    events = output.of_type("status")
    assert any(e.get("event") == "mock_set" for e in events)


def test_handle_set_mock_invalid(addon, output):
    """Invalid mock pattern should emit error and not add rule."""
    addon._handle_set_mock(
        {
            "rule_id": "mock_bad",
            "pattern": "~invalid_garbage !!!",
            "response": {"status_code": 200},
        }
    )

    with addon._mock_lock:
        assert len(addon._mock_rules) == 0

    errors = output.of_type("error")
    assert len(errors) == 1
    assert errors[0]["event"] == "invalid_mock_pattern"


def test_handle_set_mock_tilde_p_is_invalid(addon, output):
    """~p is not a valid mitmproxy filter operator and should be rejected."""
    addon._handle_set_mock(
        {
            "rule_id": "mock_path",
            "pattern": "~p /api/v2/filters",
            "response": {"status_code": 404, "body": "not found"},
        }
    )

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


# ---------------------------------------------------------------------------
# quern never intercepts its own update traffic
# ---------------------------------------------------------------------------


class TestAlwaysBypass:
    """quern's own hosts pass through, whatever the user's bypass list says.

    Configuring the system proxy made quern man-in-the-middle its own update
    check, so the certificate stopped verifying and the update check failed on
    exactly the machines running quern. quern caused that, so quern fixes it
    rather than printing advice about it.
    """

    def test_querns_own_host_is_bypassed_with_an_empty_list(self):
        addon = IOSDebugAddon()
        assert addon._bypass_patterns == [], "precondition: nothing configured"
        assert addon._is_bypassed("quern.dev") is True

    def test_subdomains_are_bypassed_too(self):
        addon = IOSDebugAddon()
        assert addon._is_bypassed("api.quern.dev") is True

    def test_clearing_the_bypass_list_does_not_expose_it(self):
        # The reason this is not just a seeded default. `clear_bypass` empties
        # the user's list, so a seed there would be silently removable -- the
        # same failure with one more step in front of it.
        addon = IOSDebugAddon()
        addon._handle_set_bypass({"patterns": ["example.com"]})
        addon._handle_clear_bypass()
        assert addon._bypass_patterns == []
        assert addon._is_bypassed("quern.dev") is True

    def test_removing_it_explicitly_does_not_expose_it_either(self):
        addon = IOSDebugAddon()
        addon._handle_remove_bypass({"patterns": ["quern.dev"]})
        assert addon._is_bypassed("quern.dev") is True

    def test_everything_else_is_still_intercepted(self):
        # The bypass is targeted. If it were not, the proxy would have quietly
        # stopped doing the one thing it exists for.
        addon = IOSDebugAddon()
        for host in ("example.com", "api.github.com", "quern.dev.evil.com"):
            assert addon._is_bypassed(host) is False, host

    def test_tls_is_never_terminated_for_querns_own_host(self):
        # The layer that matters. Skipping interception at the request hook
        # would be too late: mitmproxy would already have replaced the
        # certificate, which is the thing that failed verification.
        addon = IOSDebugAddon()
        data = MagicMock()
        data.context.client.sni = "quern.dev"
        data.ignore_connection = False
        addon.tls_clienthello(data)
        assert data.ignore_connection is True

    def test_tls_is_still_terminated_for_everything_else(self):
        addon = IOSDebugAddon()
        data = MagicMock()
        data.context.client.sni = "example.com"
        data.ignore_connection = False
        addon.tls_clienthello(data)
        assert data.ignore_connection is False


# ---------------------------------------------------------------------------
# A client refusing our certificate (#156)
# ---------------------------------------------------------------------------


class TestTlsRejectionIsRecorded:
    """`error` fires for an http.HTTPFlow, and a handshake the client aborts
    never becomes one -- so a rejection used to leave no trace at all.

    Verified against a real mitmdump before these were written: an untrusting
    client produced one event naming the SNI, the client IP and "tlsv1 alert
    unknown ca"; a trusting client produced none; a bypassed host produced none.
    These pin the logic that decides which of those three it is.
    """

    def _data(self, sni="example.com", peer=("10.0.0.5", 51234),
              error="The client does not trust the proxy's certificate for "
                    "example.com (tlsv1 alert unknown ca)",
              client_id="c1"):
        """`data.conn` *is* `data.context.client` for a client-side failure.

        mitmproxy only yields this hook when `self.conn == self.context.client`,
        so a fixture that sets them to two different mocks can let the code read
        one while the test configures the other -- which is how the first
        version of these tests passed while the hook was looking elsewhere.
        """
        client = MagicMock()
        client.sni = sni
        client.error = error
        client.peername = peer
        client.id = client_id
        data = MagicMock()
        data.conn = client
        data.context.client = client
        return data

    def test_a_rejection_names_the_device_and_the_host(self):
        addon = IOSDebugAddon()
        captured = CapturedOutput()
        captured.install()
        try:
            addon.tls_failed_client(self._data())
        finally:
            captured.restore()

        events = captured.of_type("tls_rejected")
        assert len(events) == 1, "a client refusing our cert recorded nothing"
        assert events[0]["sni"] == "example.com"
        assert events[0]["client_ip"] == "10.0.0.5", (
            "without the client IP the event cannot be attributed to a device"
        )
        assert events[0]["timestamp"]

    def test_a_bypassed_host_is_not_reported(self):
        """Its TLS is never terminated by us, so a failure there is between the
        client and the real server and says nothing about our CA."""
        addon = IOSDebugAddon()
        captured = CapturedOutput()
        captured.install()
        try:
            addon.tls_failed_client(self._data(sni="quern.dev"))
        finally:
            captured.restore()

        assert not captured.of_type("tls_rejected")

    def test_a_bytes_sni_is_decoded_not_stringified(self):
        """b"example.com" must reach the report as `example.com`.

        Asserted on the *recorded value*, not on the absence of an event. An
        earlier version of this test checked that a bytes bypassed host emitted
        nothing -- which passed without the decode too, because `fnmatch` raises
        on bytes-vs-str and the defensive handler swallowed it into an `error`
        event. Green, for the wrong reason, twice over.
        """
        addon = IOSDebugAddon()
        captured = CapturedOutput()
        captured.install()
        try:
            addon.tls_failed_client(self._data(sni=b"example.com"))
        finally:
            captured.restore()

        assert not captured.of_type("error"), "the hook fell into its handler"
        (event,) = captured.of_type("tls_rejected")
        assert event["sni"] == "example.com", (
            "a bytes SNI reached the report as its repr"
        )

    def test_a_bytes_sni_is_still_matched_against_the_bypass_list(self):
        addon = IOSDebugAddon()
        captured = CapturedOutput()
        captured.install()
        try:
            addon.tls_failed_client(self._data(sni=b"quern.dev"))
        finally:
            captured.restore()

        assert not captured.of_type("tls_rejected"), (
            "a bytes SNI slipped past the bypass check"
        )
        assert not captured.of_type("error"), (
            "it was 'bypassed' by raising, not by matching"
        )

    def test_the_hook_never_raises(self):
        """An exception out of a TLS hook would take down handling for every
        connection, to report a diagnostic."""
        addon = IOSDebugAddon()
        broken = MagicMock()
        type(broken).context = property(
            lambda _self: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        captured = CapturedOutput()
        captured.install()
        try:
            addon.tls_failed_client(broken)  # must not raise
        finally:
            captured.restore()

        assert captured.of_type("error"), (
            "the failure was swallowed without a word"
        )


class TestOnlyRealCertificateRejectionsAreReported:
    """`tls_failed_client` fires for every client-side handshake failure.

    Most say nothing about trust: a suspended app, a cancelled request or a
    flaky network all abort a handshake, and those are routine on iOS. Reported
    as refusals they would bury the real signal in noise from ordinary traffic
    -- and name no host, because a connection that dies before its ClientHello
    has no SNI. mitmproxy's own triage logs them at INFO or not at all.
    """

    def _fire(self, addon, **kw):
        base = {"sni": "example.com", "peername": ("10.0.0.5", 1),
                "error": "unknown ca", "id": "c1"}
        base.update(kw)
        client = MagicMock()
        for k, v in base.items():
            setattr(client, k, v)
        data = MagicMock()
        data.conn = client
        data.context.client = client
        captured = CapturedOutput()
        captured.install()
        try:
            addon.tls_failed_client(data)
        finally:
            captured.restore()
        return captured

    def test_an_abandoned_handshake_is_not_called_a_refusal(self):
        out = self._fire(
            IOSDebugAddon(), sni=None,
            error="The client disconnected during the handshake. This may "
                  "indicate that the client does not trust the certificate.",
        )
        assert not out.of_type("tls_rejected"), (
            "an app being suspended was reported as refusing our certificate"
        )

    def test_an_unparseable_hello_is_not_called_a_refusal(self):
        out = self._fire(IOSDebugAddon(), sni=None,
                         error="Cannot parse ClientHello: 160301...")
        assert not out.of_type("tls_rejected")

    def test_a_real_rejection_still_gets_through(self):
        out = self._fire(IOSDebugAddon(),
                         error="tlsv1 alert unknown ca")
        assert len(out.of_type("tls_rejected")) == 1

    def test_the_alert_is_capped(self):
        """mitmproxy puts the raw receive buffer in this string for an
        unparseable hello. One client streaming junk produced a 1.95 MB line,
        past the parent's 1 MB reader limit -- which raises, ends `_read_loop`
        for good, and stops all capture while mitmdump keeps running.
        """
        from server.proxy.addon import MAX_ALERT_LEN

        huge = "unknown ca " + ("ab" * 1_000_000)
        out = self._fire(IOSDebugAddon(), error=huge)
        (event,) = out.of_type("tls_rejected")
        assert len(event["error"]) <= MAX_ALERT_LEN
        assert len(json.dumps(event)) < 100_000, "the emitted line is unbounded"

    def test_a_filtered_host_is_not_reported(self):
        """Same filter every other emitter applies. Without it this names hosts
        the user excluded from capture, and publishes them via proxy_status."""
        addon = IOSDebugAddon()
        addon._host_filter = "api.example.com"
        out = self._fire(addon, sni="tracker.example.com")
        assert not out.of_type("tls_rejected")

    def test_the_filtered_host_itself_is_still_reported(self):
        addon = IOSDebugAddon()
        addon._host_filter = "api.example.com"
        out = self._fire(addon, sni="api.example.com")
        assert len(out.of_type("tls_rejected")) == 1

    def test_a_simulator_is_identified_by_process_not_by_ip(self):
        """A simulator shares the host's network stack, so its peer address is
        127.0.0.1 and the IP alone cannot say which device refused -- which is
        the case this hook exists for."""
        from server.proxy import addon as addon_mod

        addon_mod._client_process_info["c1"] = {
            "pid": 4242, "process_name": "MobileSafari",
        }
        try:
            with patch.object(addon_mod, "_resolve_simulator_udid",
                              return_value="F5AF3736"):
                out = self._fire(IOSDebugAddon(), peername=("127.0.0.1", 1))
        finally:
            addon_mod._client_process_info.pop("c1", None)

        (event,) = out.of_type("tls_rejected")
        assert event["source_process"] == "MobileSafari"
        assert event["simulator_udid"] == "F5AF3736"
