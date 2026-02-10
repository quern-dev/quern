"""mitmproxy addon for iOS Debug Server.

This script runs INSIDE the mitmdump process, not inside our server.
It has zero imports from server.* — only stdlib + mitmproxy.

Communication:
  - stdout: JSON Lines (one JSON object per line) for flow data and status events
  - stdin:  JSON Lines for commands (set_filter, clear_filter, etc.)

Usage:
  mitmdump -s addon.py --listen-port 8080 --quiet
"""

from __future__ import annotations

import base64
import json
import sys
import threading
import time
import uuid
from typing import Any

from mitmproxy import http
from mitmproxy import ctx

# Maximum body size to include inline (100KB)
MAX_BODY_SIZE = 100 * 1024


def _write_json(obj: dict[str, Any]) -> None:
    """Write a JSON object as a single line to stdout."""
    data = json.dumps(obj, separators=(",", ":"), default=str)
    sys.stdout.buffer.write(data.encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def _encode_body(raw: bytes | None) -> tuple[str | None, int, bool, str]:
    """Encode a body for JSON output.

    Returns (body_str, body_size, truncated, encoding).
    """
    if raw is None or len(raw) == 0:
        return None, 0, False, "utf-8"

    body_size = len(raw)
    truncated = body_size > MAX_BODY_SIZE
    data = raw[:MAX_BODY_SIZE] if truncated else raw

    # Try UTF-8 first
    try:
        text = data.decode("utf-8")
        return text, body_size, truncated, "utf-8"
    except UnicodeDecodeError:
        pass

    # Fall back to base64 for binary
    encoded = base64.b64encode(data).decode("ascii")
    return encoded, body_size, truncated, "base64"


def _serialize_request(request: http.Request) -> dict[str, Any]:
    """Serialize an mitmproxy Request to a dict."""
    body_str, body_size, truncated, encoding = _encode_body(request.raw_content)

    # Flatten headers — last value wins for duplicate keys
    headers = {}
    for k, v in request.headers.items():
        headers[k.lower()] = v

    return {
        "method": request.method,
        "url": request.pretty_url,
        "host": request.pretty_host,
        "path": request.path,
        "headers": headers,
        "body": body_str,
        "body_size": body_size,
        "body_truncated": truncated,
        "body_encoding": encoding,
    }


def _serialize_response(response: http.Response) -> dict[str, Any]:
    """Serialize an mitmproxy Response to a dict."""
    body_str, body_size, truncated, encoding = _encode_body(response.raw_content)

    headers = {}
    for k, v in response.headers.items():
        headers[k.lower()] = v

    return {
        "status_code": response.status_code,
        "reason": response.reason or "",
        "headers": headers,
        "body": body_str,
        "body_size": body_size,
        "body_truncated": truncated,
        "body_encoding": encoding,
    }


def _compute_timing(flow: http.HTTPFlow) -> dict[str, float | None]:
    """Extract timing info from an mitmproxy flow."""
    ts = flow.timestamps if hasattr(flow, "timestamps") else None
    if ts is None:
        # Fallback: use the legacy timestamp_start/timestamp_end on request/response
        total_ms = None
        if flow.request.timestamp_start and flow.response and flow.response.timestamp_end:
            total_ms = (flow.response.timestamp_end - flow.request.timestamp_start) * 1000
        return {
            "dns_ms": None,
            "connect_ms": None,
            "tls_ms": None,
            "request_ms": None,
            "response_ms": None,
            "total_ms": round(total_ms, 1) if total_ms else None,
        }

    def _delta(a: str, b: str) -> float | None:
        t1 = getattr(ts, a, None)
        t2 = getattr(ts, b, None)
        if t1 is not None and t2 is not None:
            return round((t2 - t1) * 1000, 1)
        return None

    return {
        "dns_ms": _delta("dns_setup", "dns_complete") if hasattr(ts, "dns_setup") else None,
        "connect_ms": _delta("tcp_setup", "tcp_complete") if hasattr(ts, "tcp_setup") else None,
        "tls_ms": _delta("tls_setup", "tls_complete") if hasattr(ts, "tls_setup") else None,
        "request_ms": _delta("request_start", "request_complete") if hasattr(ts, "request_start") else None,
        "response_ms": _delta("response_start", "response_complete") if hasattr(ts, "response_start") else None,
        "total_ms": _delta("request_start", "response_complete") if hasattr(ts, "request_start") else None,
    }


def _get_tls_info(flow: http.HTTPFlow) -> dict[str, str] | None:
    """Extract TLS info if available."""
    if not flow.request.scheme == "https":
        return None

    info: dict[str, str] = {}
    client_conn = flow.client_conn
    if client_conn and hasattr(client_conn, "tls_version") and client_conn.tls_version:
        info["version"] = client_conn.tls_version

    sni = getattr(flow.client_conn, "sni", None) or flow.request.pretty_host
    if sni:
        info["sni"] = sni

    return info if info else None


class IOSDebugAddon:
    """mitmproxy addon that serializes flows to stdout as JSON Lines."""

    def __init__(self) -> None:
        self._host_filter: str | None = None
        self._intercept_pattern: str | None = None
        self._stdin_thread: threading.Thread | None = None
        self._running = False

    def load(self, loader: Any) -> None:
        """Called when the addon is loaded."""
        self._running = True
        self._stdin_thread = threading.Thread(target=self._read_stdin, daemon=True)
        self._stdin_thread.start()
        _write_json({"type": "status", "event": "started", "timestamp": time.time()})

    def done(self) -> None:
        """Called when mitmdump is shutting down."""
        self._running = False
        _write_json({"type": "status", "event": "stopped", "timestamp": time.time()})

    def response(self, flow: http.HTTPFlow) -> None:
        """Called when a complete response has been received."""
        if self._host_filter and flow.request.pretty_host != self._host_filter:
            return

        _write_json(self._serialize_flow(flow))

    def error(self, flow: http.HTTPFlow) -> None:
        """Called when a flow errors (connection refused, timeout, etc.)."""
        if self._host_filter and flow.request.pretty_host != self._host_filter:
            return

        _write_json(self._serialize_flow(flow))

    def _serialize_flow(self, flow: http.HTTPFlow) -> dict[str, Any]:
        """Convert an mitmproxy flow to our JSON format."""
        flow_id = f"f_{uuid.uuid4().hex[:12]}"

        result: dict[str, Any] = {
            "type": "flow",
            "id": flow_id,
            "timestamp": flow.request.timestamp_start or time.time(),
            "request": _serialize_request(flow.request),
        }

        if flow.response:
            result["response"] = _serialize_response(flow.response)
        else:
            result["response"] = None

        result["timing"] = _compute_timing(flow)
        result["tls"] = _get_tls_info(flow)
        result["error"] = str(flow.error) if flow.error else None

        return result

    def _read_stdin(self) -> None:
        """Background thread reading JSON commands from stdin."""
        try:
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    cmd = json.loads(line)
                except json.JSONDecodeError:
                    continue

                action = cmd.get("action")
                if action == "set_filter":
                    self._host_filter = cmd.get("host")
                    ctx.log.info(f"Host filter set: {self._host_filter}")
                elif action == "clear_filter":
                    self._host_filter = None
                    ctx.log.info("Host filter cleared")
                elif action == "set_intercept":
                    self._intercept_pattern = cmd.get("pattern")
                    ctx.log.info(f"Intercept pattern set: {self._intercept_pattern}")
                elif action == "clear_intercept":
                    self._intercept_pattern = None
                    ctx.log.info("Intercept pattern cleared")

                if not self._running:
                    break
        except Exception:
            pass  # stdin closed or broken pipe


addons = [IOSDebugAddon()]
