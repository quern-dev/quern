"""mitmproxy addon for Quern.

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
import fnmatch
import json
import re
import subprocess
import sys
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from mitmproxy import ctx, flowfilter, http

# ---------------------------------------------------------------------------
# Monkey-patch: capture PID/process_name from mitmproxy_rs local redirector
# ---------------------------------------------------------------------------

# client_conn_id -> {pid, process_name}
_client_process_info: dict[str, dict] = {}

#: Entries for clients that have disconnected, kept briefly.
#:
#: A flow is serialised when its response completes, and `client_disconnected`
#: used to drop the entry the instant the socket closed -- so a client that
#: disconnects promptly after its last response loses its attribution in the
#: gap between the two. Measured on an emulator: every
#: `connectivitycheck.gstatic.com` flow was attributed and every
#: `www.google.com` one was not, on the same device in the same run, purely on
#: which connections lingered.
#:
#: This is not new and is not specific to Android -- an iOS simulator's flows
#: go through the same two hooks -- it is simply visible now that something
#: depends on the lookup succeeding. Bounded so a long-running proxy cannot
#: accumulate entries for connections that will never be asked about again.
_recent_process_info: OrderedDict[str, dict] = OrderedDict()
_RECENT_PROCESS_INFO_MAX = 512

try:
    from mitmproxy.proxy.server import LiveConnectionHandler

    _orig_init = LiveConnectionHandler.__init__

    def _patched_init(self, reader, writer, options, mode):
        _orig_init(self, reader, writer, options, mode)
        pid = writer.get_extra_info("pid")
        process_name = writer.get_extra_info("process_name")
        if pid is None:
            # mitmproxy_rs fills that in only for the local redirector. A
            # device configured to *use* the proxy arrives as an ordinary
            # network connection with no such info -- and for an Android
            # emulator that is the only route there is, because the redirector
            # cannot see QEMU's traffic at all (measured: zero flows with the
            # QEMU binary in `local_capture`, against a positive control in
            # the same run where `curl` was captured with its pid).
            #
            # The connection is still a socket this host owns, though, so the
            # source port identifies the process that opened it. That is done
            # here rather than later because these sockets are short-lived --
            # observed CLOSED seconds afterwards -- and this hook runs at
            # connection setup, while it is certainly open.
            # Started on a worker, not awaited here. This hook runs
            # synchronously on mitmproxy's event loop, and `lsof` took ~40ms
            # measured -- with a 5s worst case -- so doing it inline stalled
            # every other connection, physical devices included, behind one
            # local one. The socket still has to be read while it is open,
            # which is why the work starts now rather than at serialise time;
            # only the *waiting* is deferred.
            future = _SOCKET_LOOKUP_POOL.submit(_pid_from_local_socket, writer)
            _client_process_info[self.client.id] = {"future": future}
            return
        if pid is not None:
            _client_process_info[self.client.id] = {
                "pid": pid,
                "process_name": process_name,
            }

    LiveConnectionHandler.__init__ = _patched_init
except Exception:
    pass  # Not available — non-local mode or import failure


#: Workers for the socket lookup. Bounded: one thread per in-flight local
#: connection would be unbounded by anything the proxy controls, and the work
#: is a short subprocess rather than something worth parallelising widely.
_SOCKET_LOOKUP_POOL = ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="quern-sock",
)


def _lookup_process_info(client_id: str | None) -> dict | None:
    """Process info for a client connection, live or just-disconnected.

    Resolves a pending socket lookup if one was started at connection time.
    The result replaces the future so the wait is paid once per connection
    rather than once per flow on it.
    """
    if not client_id:
        return None
    info = _client_process_info.get(client_id)
    if info is None:
        with _cache_lock:
            info = _recent_process_info.get(client_id)
    if info is None:
        return None
    future = info.get("future")
    if future is not None:
        try:
            # Bounded hard. By the time a flow is being serialised the lookup
            # has had the whole request to finish; waiting longer would trade
            # a missing attribution for a stalled response, which is the wrong
            # way round.
            pid, process_name = future.result(timeout=0.5)
        except TimeoutError:
            # Still running -- leave the future in place. Replacing it with
            # `None` here made one slow lookup poison the connection: the
            # first flow recorded the failure, and every later flow on the
            # same connection stayed unattributed even after the lookup had
            # finished. The pool has four workers, so a burst of local
            # connections queues and this is reachable rather than
            # theoretical.
            return None
        except Exception:
            pid = process_name = None
        info.clear()
        info["pid"] = pid
        info["process_name"] = process_name
    return info


# ---------------------------------------------------------------------------
# Local socket → PID, for devices that reach the proxy over the network
# ---------------------------------------------------------------------------

# Addresses that mean "this connection came from the machine the proxy runs
# on". An emulator's traffic is NATed by QEMU, so it arrives from one of these
# rather than from anything resembling the emulator's own 10.0.2.15.
_LOCAL_ADDRS: set[str] = set()


def _is_local_address(ip: str) -> bool:
    """Whether `ip` belongs to this host.

    Loopback plus whatever the host's own interfaces answer to. A physical
    phone arrives from its LAN address and takes none of the work below --
    which matters, because the socket lookup costs ~40ms and a phone is
    already attributable by that address.
    """
    if ip in ("127.0.0.1", "::1", "localhost"):
        return True
    if not _LOCAL_ADDRS:
        try:
            out = subprocess.run(
                ["ifconfig"], capture_output=True, text=True, timeout=5,
            ).stdout
            _LOCAL_ADDRS.update(re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", out))
        except Exception:
            _LOCAL_ADDRS.add("127.0.0.1")
    return ip in _LOCAL_ADDRS


def _pid_from_local_socket(writer) -> tuple[int | None, str | None]:
    # `writer` is mitmproxy's `asyncio.StreamWriter`; left untyped because
    # this module is loaded by `mitmdump -s` rather than imported, and adding
    # an `asyncio` import purely for an annotation pulls it into that path.
    """The process that opened this connection, when it came from this host.

    Returns `(None, None)` for anything it cannot establish, which is the
    common case and must stay cheap: a connection from a phone's LAN address
    never reaches the lookup.
    """
    try:
        peer = writer.get_extra_info("peername")
        if not (isinstance(peer, (tuple, list)) and len(peer) >= 2):
            return None, None
        ip, port = peer[0], int(peer[1])
        if not _is_local_address(ip):
            return None, None
        result = subprocess.run(
            # No state filter. `-sTCP:ESTABLISHED` lost connections that were
            # in some other state at the instant of the lookup -- two of ten
            # flows in a live run came back unattributed for that reason
            # alone. The `local->remote` match below is what disambiguates,
            # and a listening socket has no `->` in its name, so nothing is
            # admitted by dropping the filter.
            ["lsof", "-nP", f"-iTCP:{port}", "-Fpcn"],
            capture_output=True, text=True, timeout=5,
        )
        # Both ends of this connection are on this host -- the device's side
        # and the proxy's -- so `lsof` returns two processes for the port and
        # taking the first is a coin toss. It came up heads for one emulator
        # and tails for the next, which attributed six flows to the proxy's
        # own pid. Match the entry whose *local* address is the peer, which is
        # the only one that identifies who opened the connection.
        want = f"{ip}:{port}->"
        pid = name = None
        cur_pid = cur_name = None
        for line in result.stdout.splitlines():
            tag, value = line[:1], line[1:]
            if tag == "p":
                cur_pid, cur_name = int(value), None
            elif tag == "c":
                cur_name = value
            elif tag == "n" and value.startswith(want):
                pid, name = cur_pid, cur_name
                break
        return pid, name
    except Exception:
        return None, None


# ---------------------------------------------------------------------------
# PID → Android emulator serial
# ---------------------------------------------------------------------------

# qemu pid -> ("emulator-5554", monotonic time it was resolved)
#
# Time-limited and bounded, because a pid is not a stable name for a device.
# An emulator exits, the OS reuses its pid for a different QEMU instance on a
# different console port, and an unexpiring cache answers with the old serial
# -- attributing a flow to the wrong device with `device_of` calling it firm.
# The window is narrow and the failure is silent, which is the combination
# worth spending a re-lookup on.
_emulator_serial_cache: OrderedDict[int, tuple[str, float]] = OrderedDict()
_SERIAL_CACHE_TTL = 60.0
_SERIAL_CACHE_MAX = 256
# Console ports live in this range; the emulator takes an even one for the
# console and the next odd one for adb, which is why the serial is the even
# one. Bounding the search stops an unrelated listener being read as a serial.
_CONSOLE_PORT_RANGE = range(5554, 5684, 2)


def _is_emulator_process(pid: int) -> bool:
    """Whether this pid is actually an Android emulator.

    Checked by command line rather than by the port it listens on, because the
    port range is shared with whatever else a developer happens to run. The
    emulator is a QEMU binary launched with `-avd <name>`, and both halves are
    on its argv.
    """
    try:
        result = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return False
    command = result.stdout
    return "qemu" in command.lower() and "-avd" in command


def _emulator_serial_for_pid(pid: int) -> str | None:
    """`emulator-5554` for the QEMU process serving that emulator.

    An emulator's serial *is* its console port, and the process listens on it,
    so the port is recoverable from the process without asking adb. Measured:
    the QEMU process for `Pixel_6_Dev` listens on 5554 and 5555, and adb calls
    that device `emulator-5554`.

    The `-avd <name>` on its command line is the friendlier identifier but not
    the one quern keys on, so the port is what is returned.
    """
    now = time.monotonic()
    with _cache_lock:
        hit = _emulator_serial_cache.get(pid)
        if hit is not None and now - hit[1] < _SERIAL_CACHE_TTL:
            return hit[0]
    try:
        result = subprocess.run(
            ["lsof", "-nP", "-a", "-p", str(pid), "-iTCP", "-sTCP:LISTEN", "-Fn"],
            capture_output=True, text=True, timeout=5,
        )
        ports = {
            int(m.group(1))
            for m in re.finditer(r"^n.*?:(\d+)$", result.stdout, re.MULTILINE)
        }
        console = sorted(p for p in ports if p in _CONSOLE_PORT_RANGE)
        if not console:
            return None
        if not _is_emulator_process(pid):
            # A port in the range is a hint, not proof. Anything else
            # listening on an even port between 5554 and 5682 would otherwise
            # be handed an `emulator-NNNN` serial, and `device_of` treats a
            # serial as *firm* attribution -- so the wrong answer would be
            # reported with more confidence than the right one.
            return None
        serial = f"emulator-{console[0]}"
        with _cache_lock:
            _emulator_serial_cache[pid] = (serial, now)
            while len(_emulator_serial_cache) > _SERIAL_CACHE_MAX:
                _emulator_serial_cache.popitem(last=False)
        return serial
    except Exception:
        return None


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

#: How much of a TLS alert to keep. mitmproxy puts the raw receive buffer in
#: this string for an unparseable ClientHello -- two hex characters per byte
#: buffered, and it buffers until the hello is complete or the client gives up.
#: A client streaming junk produced a single 1.95 MB stdout line, which is past
#: the parent's 1 MB reader limit: `async for line in ...` then raises and
#: `_read_loop` exits for good, so all capture stops while mitmdump keeps
#: running. Every other writer here is capped for the same reason.
MAX_ALERT_LEN = 200

#: Alerts that mean the client rejected our *certificate*. `tls_failed_client`
#: fires for every client-side handshake failure, and most of them say nothing
#: about trust: a suspended app, a cancelled request or a flaky network all
#: abort a handshake, and mitmproxy's own triage logs those at INFO or not at
#: all. Reporting them as refusals would bury the real signal in noise from
#: ordinary traffic -- and name no host, since a connection that dies before
#: its ClientHello has no SNI.
_CERT_REJECTION_ALERTS = (
    "unknown ca", "bad certificate", "certificate unknown",
    "certificate expired", "certificate revoked", "unsupported certificate",
    "certificate required",
)

# Default timeout for held (intercepted) flows
DEFAULT_TIMEOUT_SECONDS = 30.0

#: Hosts quern never intercepts, whatever the bypass list says.
#:
#: quern's own update check talks to quern.dev, and urllib honours the macOS
#: system proxy -- so the moment quern configured that proxy, it began
#: man-in-the-middling its own update check, and the certificate stopped
#: verifying. quern created that condition, so quern clears it rather than
#: printing advice about it. Enforced in `tls_clienthello`, which sets
#: `ignore_connection` before TLS is terminated: true passthrough, with no
#: certificate replaced and so nothing to fail verification.
#:
#: Deliberately not seeded into `_bypass_patterns`. That list belongs to the
#: user and `clear_bypass` empties it, so a seed there would be silently
#: removable -- the same failure with an extra step between. The cost is that
#: quern's own site cannot be captured through quern, and debugging quern.dev
#: is not what this proxy is for.
ALWAYS_BYPASS: tuple[str, ...] = ("quern.dev", "*.quern.dev")


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
        "request_ms": (
            _delta("request_start", "request_complete")
            if hasattr(ts, "request_start") else None
        ),
        "response_ms": (
            _delta("response_start", "response_complete")
            if hasattr(ts, "response_start") else None
        ),
        "total_ms": (
            _delta("request_start", "response_complete")
            if hasattr(ts, "request_start") else None
        ),
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

        # Bypass patterns — hosts matching these are silently passed through
        self._bypass_patterns: list[str] = []
        self._bypass_lock = threading.Lock()

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

    def _is_bypassed(self, host: str) -> bool:
        """Check if a host matches any bypass pattern."""
        if any(fnmatch.fnmatch(host, p) for p in ALWAYS_BYPASS):
            return True
        with self._bypass_lock:
            for pattern in self._bypass_patterns:
                if fnmatch.fnmatch(host, pattern):
                    return True
        return False

    def tls_clienthello(self, data: Any) -> None:
        """Skip TLS interception for bypassed hosts.

        This is called before mitmproxy terminates TLS, so bypassed
        hosts get true passthrough — no cert replacement, no MITM.
        Essential for cert-pinned services like Apple's device
        verification endpoints.
        """
        sni = data.context.client.sni
        if sni and self._is_bypassed(sni):
            data.ignore_connection = True

    def tls_failed_client(self, data: Any) -> None:
        """A client refused the certificate we offered it.

        The most direct evidence there is that a device does not trust our CA,
        and until now the only hook that saw it was absent -- `error` fires for
        an `http.HTTPFlow`, and a handshake the client aborts never becomes
        one. So a rejection left no trace anywhere: no flow, no error, nothing
        in the log, and `proxy_status` still reporting no warnings.

        Measured: a simulator refused `www.apple.com`, said so on its own screen
        in plain language, and quern recorded nothing at all. See #156.

        Worth more than it looks for a *physical* device, which cannot be asked
        the way a simulator's TrustStore can. A client that rejects our
        certificate has demonstrated the answer.

        Bypassed hosts are skipped: their TLS is never terminated by us
        (`tls_clienthello` sets `ignore_connection`), so a failure there is
        between the client and the real server and says nothing about our CA.
        """
        try:
            client = data.context.client
            sni = getattr(client, "sni", None)
            if isinstance(sni, bytes):
                sni = sni.decode("utf-8", errors="replace")
            if sni and self._is_bypassed(sni):
                return
            # Same filter every other emitter applies. Without it this reports
            # hosts the user explicitly excluded from capture, and publishes
            # their names through `proxy_status`.
            if self._host_filter and sni != self._host_filter:
                return

            alert = str(getattr(client, "error", None) or "")
            if not any(a in alert.lower() for a in _CERT_REJECTION_ALERTS):
                return

            event = {
                "type": "tls_rejected",
                "sni": sni,
                "error": alert[:MAX_ALERT_LEN],
                "timestamp": time.time(),
            }
            # Identify the client the way `_serialize_flow` does. For a
            # simulator the peer address is 127.0.0.1 -- it shares the host's
            # network stack -- so the IP alone cannot say which device refused,
            # which is the case this hook exists for.
            client_id = getattr(client, "id", None)
            info = _lookup_process_info(client_id)
            if info:
                pid = info.get("pid")
                event["source_process"] = info.get("process_name")
                event["source_pid"] = pid
                if pid is not None:
                    event["simulator_udid"] = _resolve_simulator_udid(pid)
                    if not event.get("simulator_udid"):
                        serial = _emulator_serial_for_pid(pid)
                        if serial:
                            event["device_serial"] = serial
            peername = getattr(client, "peername", None)
            if isinstance(peername, (tuple, list)) and len(peername) >= 1:
                event["client_ip"] = peername[0]
            _write_json(event)
        except Exception as exc:  # pragma: no cover - defensive
            # Never raise out of a hook: an exception here would take down TLS
            # handling for every connection, to report a diagnostic.
            _write_json({"type": "error", "where": "tls_failed_client",
                         "detail": str(exc)})

    def request(self, flow: http.HTTPFlow) -> None:
        """Called when a request is received. Check mocks first, then intercept."""
        # Apply host filter
        if self._host_filter and flow.request.pretty_host != self._host_filter:
            return

        # Skip bypassed hosts
        if self._is_bypassed(flow.request.pretty_host):
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
        if self._is_bypassed(flow.request.pretty_host):
            return

        _write_json(self._serialize_flow(flow))

    def error(self, flow: http.HTTPFlow) -> None:
        """Called when a flow errors (connection refused, timeout, etc.)."""
        if self._host_filter and flow.request.pretty_host != self._host_filter:
            return
        if self._is_bypassed(flow.request.pretty_host):
            return

        _write_json(self._serialize_flow(flow))

    def client_disconnected(self, client) -> None:
        """Retire process info when a client disconnects.

        Retired rather than dropped: a flow on this connection may still be
        waiting to be serialised, and discarding the entry here is what made
        attribution depend on how promptly a client hung up.
        """
        info = _client_process_info.pop(client.id, None)
        if info is not None:
            with _cache_lock:
                _recent_process_info[client.id] = info
                while len(_recent_process_info) > _RECENT_PROCESS_INFO_MAX:
                    _recent_process_info.popitem(last=False)

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
        info = _lookup_process_info(client_id)
        if info:
            pid = info.get("pid")
            result["source_process"] = info.get("process_name")
            result["source_pid"] = pid
            if pid is not None:
                result["simulator_udid"] = _resolve_simulator_udid(pid)
                # The same pid, asked a different question. An emulator is not
                # a `launchd_sim` child so the walk above finds nothing, and
                # its flows carry the *host's* address so `client_ip` below
                # identifies nothing either -- every emulator on a machine
                # collapses to one address, which is also the machine's own.
                # The process that opened the socket is the one thing that
                # distinguishes them.
                if not result.get("simulator_udid"):
                    serial = _emulator_serial_for_pid(pid)
                    if serial:
                        result["device_serial"] = serial

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
                elif action == "set_bypass":
                    self._handle_set_bypass(cmd)
                elif action == "remove_bypass":
                    self._handle_remove_bypass(cmd)
                elif action == "clear_bypass":
                    self._handle_clear_bypass()

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


    def _handle_set_bypass(self, cmd: dict) -> None:
        """Add bypass patterns."""
        patterns = cmd.get("patterns", [])
        if isinstance(patterns, str):
            patterns = [patterns]

        with self._bypass_lock:
            for p in patterns:
                if p not in self._bypass_patterns:
                    self._bypass_patterns.append(p)

        _write_json({
            "type": "status",
            "event": "bypass_updated",
            "patterns": list(self._bypass_patterns),
            "timestamp": time.time(),
        })

    def _handle_remove_bypass(self, cmd: dict) -> None:
        """Remove specific bypass patterns."""
        patterns = cmd.get("patterns", [])
        if isinstance(patterns, str):
            patterns = [patterns]

        with self._bypass_lock:
            self._bypass_patterns = [
                p for p in self._bypass_patterns
                if p not in patterns
            ]

        _write_json({
            "type": "status",
            "event": "bypass_updated",
            "patterns": list(self._bypass_patterns),
            "timestamp": time.time(),
        })

    def _handle_clear_bypass(self) -> None:
        """Remove all bypass patterns."""
        with self._bypass_lock:
            self._bypass_patterns.clear()

        _write_json({
            "type": "status",
            "event": "bypass_cleared",
            "timestamp": time.time(),
        })


addons = [IOSDebugAddon()]
