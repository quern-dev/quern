"""mitmproxy addon for Quern Debug Server.

This script runs INSIDE the mitmdump process, not inside our server.
It has zero imports from server.* — only stdlib + mitmproxy.

Communication:
  - stdout: JSON Lines (one JSON object per line) for flow data and status events
  - stdin:  JSON Lines for commands (set_filter, clear_filter, etc.)

Usage:
  mitmdump -s addon.py --listen-port 9101 --quiet
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.util
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from typing import Any

from mitmproxy import http
from mitmproxy import ctx
from mitmproxy import flowfilter


# ---------------------------------------------------------------------------
# Monkey-patch: capture PID/process_name from mitmproxy_rs local redirector
# ---------------------------------------------------------------------------

# client_conn_id -> {pid, process_name}
_client_process_info: dict[str, dict] = {}

try:
    from mitmproxy.proxy.server import LiveConnectionHandler

    _orig_init = LiveConnectionHandler.__init__

    def _patched_init(self, reader, writer, options, mode):
        _orig_init(self, reader, writer, options, mode)
        pid = writer.get_extra_info("pid")
        process_name = writer.get_extra_info("process_name")
        if pid is not None:
            _client_process_info[self.client.id] = {
                "pid": pid,
                "process_name": process_name,
            }

    LiveConnectionHandler.__init__ = _patched_init
except Exception:
    pass  # Not available — non-local mode or import failure


# ---------------------------------------------------------------------------
# PID → Simulator UDID resolution
# ---------------------------------------------------------------------------

# launchd_sim PID -> UDID
_launchd_sim_cache: dict[int, str] = {}
# source PID -> resolved UDID (stable while simulator is booted)
_pid_to_udid_cache: dict[int, str | None] = {}
_cache_lock = threading.Lock()

# UDID pattern in launchd_sim command line
_UDID_RE = re.compile(r"[0-9A-F]{8}-(?:[0-9A-F]{4}-){3}[0-9A-F]{12}", re.IGNORECASE)


def _refresh_launchd_sim_cache() -> None:
    """Rebuild the launchd_sim PID→UDID cache from ps output."""
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid,command"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return

        new_cache: dict[int, str] = {}
        for line in result.stdout.splitlines():
            if "launchd_sim" not in line:
                continue
            parts = line.strip().split(None, 1)
            if len(parts) < 2:
                continue
            try:
                pid = int(parts[0])
            except ValueError:
                continue
            m = _UDID_RE.search(parts[1])
            if m:
                new_cache[pid] = m.group(0)

        with _cache_lock:
            _launchd_sim_cache.clear()
            _launchd_sim_cache.update(new_cache)
    except Exception:
        pass


def _get_ppid(pid: int) -> int | None:
    """Get parent PID using libproc (no subprocess overhead)."""
    try:
        libproc_path = ctypes.util.find_library("libproc")
        if libproc_path is None:
            # Direct path fallback on macOS
            libproc_path = "/usr/lib/libproc.dylib"
        libproc = ctypes.CDLL(libproc_path, use_errno=True)

        # struct proc_bsdinfo
        class ProcBsdInfo(ctypes.Structure):
            _fields_ = [
                ("pbi_flags", ctypes.c_uint32),
                ("pbi_status", ctypes.c_uint32),
                ("pbi_xstatus", ctypes.c_uint32),
                ("pbi_pid", ctypes.c_uint32),
                ("pbi_ppid", ctypes.c_uint32),
                # ... more fields but we only need ppid
            ]

        buf = ProcBsdInfo()
        # PROC_PIDTBSDINFO = 3
        ret = libproc.proc_pidinfo(pid, 3, 0, ctypes.byref(buf), ctypes.sizeof(buf))
        if ret > 0:
            return buf.pbi_ppid
    except Exception:
        pass

    # Fallback to ps
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "ppid="],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0 and result.stdout.strip():
            return int(result.stdout.strip())
    except Exception:
        pass
    return None


def _resolve_simulator_udid(pid: int) -> str | None:
    """Walk parent chain to find a launchd_sim ancestor → simulator UDID."""
    with _cache_lock:
        if pid in _pid_to_udid_cache:
            return _pid_to_udid_cache[pid]

    # Walk up to 10 levels of parent chain
    current = pid
    visited: set[int] = set()
    for _ in range(10):
        if current is None or current <= 1 or current in visited:
            break
        visited.add(current)

        with _cache_lock:
            udid = _launchd_sim_cache.get(current)
        if udid:
            with _cache_lock:
                _pid_to_udid_cache[pid] = udid
            return udid

        current = _get_ppid(current)

    # Cache miss after full walk — try refreshing launchd_sim cache once
    _refresh_launchd_sim_cache()

    # Retry the walk after refresh
    current = pid
    visited.clear()
    for _ in range(10):
        if current is None or current <= 1 or current in visited:
            break
        visited.add(current)

        with _cache_lock:
            udid = _launchd_sim_cache.get(current)
        if udid:
            with _cache_lock:
                _pid_to_udid_cache[pid] = udid
            return udid

        current = _get_ppid(current)

    # Not from a simulator
    with _cache_lock:
        _pid_to_udid_cache[pid] = None
    return None

# Maximum body size to include inline (100KB)
MAX_BODY_SIZE = 100 * 1024

# Default timeout for held (intercepted) flows
DEFAULT_TIMEOUT_SECONDS = 30.0


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
    # Use .content (auto-decoded) instead of .raw_content (may be compressed)
    body_str, body_size, truncated, encoding = _encode_body(request.content)

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
    # Use .content (auto-decoded gzip/deflate/br) instead of .raw_content
    body_str, body_size, truncated, encoding = _encode_body(response.content)

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
    """mitmproxy addon that serializes flows to stdout as JSON Lines.

    Supports intercept (hold-and-release), mock responses, and host filtering.
    """

    def __init__(self) -> None:
        self._host_filter: str | None = None

        # Intercept state — protected by _held_lock
        self._intercept_pattern: str | None = None
        self._intercept_compiled: Any | None = None  # flowfilter result, callable
        self._held_flows: dict[str, tuple[http.HTTPFlow, float]] = {}  # id -> (flow, held_at)
        self._held_lock = threading.Lock()

        # Mock state — protected by _mock_lock
        self._mock_rules: list[dict] = []  # [{rule_id, pattern_str, compiled, response}]
        self._mock_lock = threading.Lock()

        self._timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
        self._stdin_thread: threading.Thread | None = None
        self._timeout_thread: threading.Thread | None = None
        self._running = False

    def load(self, loader: Any) -> None:
        """Called when the addon is loaded."""
        self._running = True
        self._stdin_thread = threading.Thread(target=self._read_stdin, daemon=True)
        self._stdin_thread.start()
        self._timeout_thread = threading.Thread(target=self._run_timeout_loop, daemon=True)
        self._timeout_thread.start()
        # Pre-populate launchd_sim → UDID cache for simulator flow tagging
        threading.Thread(target=_refresh_launchd_sim_cache, daemon=True).start()
        _write_json({"type": "status", "event": "started", "timestamp": time.time()})

    def done(self) -> None:
        """Called when mitmdump is shutting down. Resume all held flows."""
        self._running = False
        # Release all held flows to prevent hanging clients
        with self._held_lock:
            for flow_id, (flow, _) in list(self._held_flows.items()):
                try:
                    flow.resume()
                except Exception:
                    pass
                _write_json({
                    "type": "released",
                    "id": flow_id,
                    "reason": "shutdown",
                    "timestamp": time.time(),
                })
            self._held_flows.clear()
        _write_json({"type": "status", "event": "stopped", "timestamp": time.time()})

    def request(self, flow: http.HTTPFlow) -> None:
        """Called when a request is received. Check mocks first, then intercept."""
        # Apply host filter
        if self._host_filter and flow.request.pretty_host != self._host_filter:
            return

        # 1. Check mock rules first (mock takes priority over intercept)
        with self._mock_lock:
            for rule in self._mock_rules:
                compiled = rule["compiled"]
                if compiled and compiled(flow):
                    # Return synthetic response
                    resp = rule["response"]
                    flow.response = http.Response.make(
                        resp.get("status_code", 200),
                        resp.get("body", "").encode("utf-8"),
                        resp.get("headers", {"content-type": "application/json"}),
                    )
                    flow_id = f"f_{uuid.uuid4().hex[:12]}"
                    _write_json({
                        "type": "mock_hit",
                        "id": flow_id,
                        "rule_id": rule["rule_id"],
                        "timestamp": time.time(),
                        "request": _serialize_request(flow.request),
                        "response": {
                            "status_code": resp.get("status_code", 200),
                            "reason": "",
                            "headers": resp.get("headers", {}),
                            "body": resp.get("body", ""),
                            "body_size": len(resp.get("body", "").encode("utf-8")),
                            "body_truncated": False,
                            "body_encoding": "utf-8",
                        },
                    })
                    return

        # 2. Check intercept pattern
        with self._held_lock:
            if self._intercept_compiled and self._intercept_compiled(flow):
                flow_id = f"f_{uuid.uuid4().hex[:12]}"
                flow.intercept()
                self._held_flows[flow_id] = (flow, time.time())
                _write_json({
                    "type": "intercepted",
                    "id": flow_id,
                    "timestamp": time.time(),
                    "request": _serialize_request(flow.request),
                })

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

    def client_disconnected(self, client) -> None:
        """Clean up process info when a client disconnects."""
        _client_process_info.pop(client.id, None)

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

        # Source process tagging (from monkey-patched connection handler)
        client_id = flow.client_conn.id if flow.client_conn else None
        if client_id and client_id in _client_process_info:
            info = _client_process_info[client_id]
            pid = info.get("pid")
            result["source_process"] = info.get("process_name")
            result["source_pid"] = pid
            if pid is not None:
                result["simulator_udid"] = _resolve_simulator_udid(pid)

        # Client IP tagging (for physical device identification)
        if flow.client_conn:
            peername = getattr(flow.client_conn, "peername", None)
            if peername and isinstance(peername, (tuple, list)) and len(peername) >= 1:
                result["client_ip"] = peername[0]

        return result

    # -------------------------------------------------------------------
    # Timeout thread
    # -------------------------------------------------------------------

    def _run_timeout_loop(self) -> None:
        """Background thread that auto-releases held flows after timeout."""
        while self._running:
            time.sleep(1.0)
            now = time.time()
            expired: list[tuple[str, http.HTTPFlow]] = []

            with self._held_lock:
                for flow_id, (flow, held_at) in list(self._held_flows.items()):
                    if now - held_at >= self._timeout_seconds:
                        expired.append((flow_id, flow))
                        del self._held_flows[flow_id]

            # Resume outside the lock to avoid holding it during I/O
            for flow_id, flow in expired:
                try:
                    flow.resume()
                except Exception:
                    pass
                _write_json({
                    "type": "released",
                    "id": flow_id,
                    "reason": "timeout",
                    "timestamp": time.time(),
                })

    # -------------------------------------------------------------------
    # Stdin command processing
    # -------------------------------------------------------------------

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
                    self._handle_set_intercept(cmd)
                elif action == "clear_intercept":
                    self._handle_clear_intercept()
                elif action == "release_flow":
                    self._handle_release_flow(cmd)
                elif action == "modify_and_release":
                    self._handle_modify_and_release(cmd)
                elif action == "release_all":
                    self._handle_release_all()
                elif action == "set_mock":
                    self._handle_set_mock(cmd)
                elif action == "clear_mock":
                    self._handle_clear_mock(cmd)

                if not self._running:
                    break
        except Exception:
            pass  # stdin closed or broken pipe

    def _handle_set_intercept(self, cmd: dict) -> None:
        """Compile and set an intercept filter pattern."""
        pattern = cmd.get("pattern", "")
        try:
            compiled = flowfilter.parse(pattern)
        except ValueError:
            compiled = None
        if compiled is None:
            _write_json({
                "type": "error",
                "event": "invalid_intercept_pattern",
                "pattern": pattern,
                "timestamp": time.time(),
            })
            return

        with self._held_lock:
            self._intercept_pattern = pattern
            self._intercept_compiled = compiled

        _write_json({
            "type": "status",
            "event": "intercept_set",
            "pattern": pattern,
            "timestamp": time.time(),
        })

    def _handle_clear_intercept(self) -> None:
        """Clear intercept pattern and release all held flows."""
        released: list[tuple[str, http.HTTPFlow]] = []

        with self._held_lock:
            self._intercept_pattern = None
            self._intercept_compiled = None
            for flow_id, (flow, _) in list(self._held_flows.items()):
                released.append((flow_id, flow))
            self._held_flows.clear()

        for flow_id, flow in released:
            try:
                flow.resume()
            except Exception:
                pass
            _write_json({
                "type": "released",
                "id": flow_id,
                "reason": "intercept_cleared",
                "timestamp": time.time(),
            })

        _write_json({
            "type": "status",
            "event": "intercept_cleared",
            "timestamp": time.time(),
        })

    def _handle_release_flow(self, cmd: dict) -> None:
        """Release a single held flow."""
        flow_id = cmd.get("flow_id", "")
        with self._held_lock:
            entry = self._held_flows.pop(flow_id, None)

        if entry is None:
            return  # Already released or timed out

        flow, _ = entry
        try:
            flow.resume()
        except Exception:
            pass
        _write_json({
            "type": "released",
            "id": flow_id,
            "reason": "manual",
            "timestamp": time.time(),
        })

    def _handle_modify_and_release(self, cmd: dict) -> None:
        """Apply modifications to a held flow's request, then release it."""
        flow_id = cmd.get("flow_id", "")
        modifications = cmd.get("modifications", {})

        with self._held_lock:
            entry = self._held_flows.pop(flow_id, None)

        if entry is None:
            return

        flow, _ = entry

        # Apply modifications to the request
        if "method" in modifications:
            flow.request.method = modifications["method"]
        if "url" in modifications:
            flow.request.url = modifications["url"]
        if "headers" in modifications:
            for k, v in modifications["headers"].items():
                flow.request.headers[k] = v
        if "body" in modifications:
            flow.request.text = modifications["body"]

        try:
            flow.resume()
        except Exception:
            pass
        _write_json({
            "type": "released",
            "id": flow_id,
            "reason": "modified",
            "timestamp": time.time(),
        })

    def _handle_release_all(self) -> None:
        """Release all held flows."""
        released: list[tuple[str, http.HTTPFlow]] = []

        with self._held_lock:
            for flow_id, (flow, _) in list(self._held_flows.items()):
                released.append((flow_id, flow))
            self._held_flows.clear()

        for flow_id, flow in released:
            try:
                flow.resume()
            except Exception:
                pass
            _write_json({
                "type": "released",
                "id": flow_id,
                "reason": "release_all",
                "timestamp": time.time(),
            })

    def _handle_set_mock(self, cmd: dict) -> None:
        """Add a mock response rule."""
        rule_id = cmd.get("rule_id", f"mock_{uuid.uuid4().hex[:8]}")
        pattern = cmd.get("pattern", "")
        response = cmd.get("response", {})

        try:
            compiled = flowfilter.parse(pattern)
        except ValueError:
            compiled = None
        if compiled is None:
            _write_json({
                "type": "error",
                "event": "invalid_mock_pattern",
                "pattern": pattern,
                "rule_id": rule_id,
                "timestamp": time.time(),
            })
            return

        with self._mock_lock:
            self._mock_rules.append({
                "rule_id": rule_id,
                "pattern_str": pattern,
                "compiled": compiled,
                "response": response,
            })

        _write_json({
            "type": "status",
            "event": "mock_set",
            "rule_id": rule_id,
            "pattern": pattern,
            "timestamp": time.time(),
        })

    def _handle_clear_mock(self, cmd: dict) -> None:
        """Remove a specific mock rule or all mock rules."""
        rule_id = cmd.get("rule_id")

        with self._mock_lock:
            if rule_id:
                self._mock_rules = [r for r in self._mock_rules if r["rule_id"] != rule_id]
            else:
                self._mock_rules.clear()

        _write_json({
            "type": "status",
            "event": "mocks_cleared",
            "rule_id": rule_id,
            "timestamp": time.time(),
        })


addons = [IOSDebugAddon()]
