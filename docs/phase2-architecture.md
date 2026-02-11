# Phase 2: Network Proxy Integration

## Architecture Document — v0.1

---

## 1. Vision

Add network traffic inspection and manipulation capabilities to the Quern debug server by integrating mitmproxy as a managed subprocess. An AI agent should be able to see every HTTP/HTTPS request an app makes, inspect full request/response details, filter traffic by host or path, replay requests, and set up intercept rules — all through the same HTTP API and MCP interface used for logs.

The key insight driving the hybrid storage design: network events appear naturally in the log stream ("POST /api/v1/login → 401 Unauthorized, 234ms") alongside console output, while the full flow details (headers, bodies, timing) live in a dedicated flow store the AI can drill into when needed.

---

## 2. How mitmproxy Works (Background)

mitmproxy is a free, open-source HTTPS proxy. It performs TLS interception by generating certificates on-the-fly signed by its own CA. For iOS, the device must be configured to route traffic through the proxy and trust the mitmproxy CA certificate.

The tool ships in three forms:
- **mitmproxy** — interactive TUI
- **mitmweb** — web-based UI
- **mitmdump** — headless, scriptable — **this is what we use**

`mitmdump` accepts Python addon scripts via `-s script.py`. The addon receives callbacks for every stage of a request/response lifecycle. We write a custom addon that serializes captured flows to stdout as JSON lines, and our proxy source adapter reads them — exactly mirroring the idevicesyslog pattern.

---

## 3. Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                     Quern Debug Server (port 9100)                  │
│                                                                   │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │                     HTTP API Layer                        │    │
│  │                                                           │    │
│  │  ── Existing (Phase 1) ──────────────────────────────     │    │
│  │  GET  /api/v1/logs/stream         (SSE, now includes      │    │
│  │  GET  /api/v1/logs/query           network summary events)│    │
│  │  GET  /api/v1/logs/summary                                │    │
│  │  GET  /api/v1/logs/errors                                 │    │
│  │                                                           │    │
│  │  ── New (Phase 2) ───────────────────────────────────     │    │
│  │  GET  /api/v1/proxy/status        Proxy status & config   │    │
│  │  POST /api/v1/proxy/start         Start the proxy         │    │
│  │  POST /api/v1/proxy/stop          Stop the proxy          │    │
│  │  GET  /api/v1/proxy/flows         Query captured flows    │    │
│  │  GET  /api/v1/proxy/flows/{id}    Full flow detail        │    │
│  │  GET  /api/v1/proxy/flows/summary LLM-optimized digest    │    │
│  │  POST /api/v1/proxy/intercept     Set intercept rules     │    │
│  │  DELETE /api/v1/proxy/intercept   Clear intercept rules   │    │
│  │  POST /api/v1/proxy/replay/{id}   Replay a captured flow  │    │
│  │  GET  /api/v1/proxy/cert          Download CA certificate │    │
│  │  GET  /api/v1/proxy/setup-guide   Device setup instructions│   │
│  └──────────────────────┬───────────────────────────────────┘    │
│                          │                                        │
│  ┌──────────────────────▼───────────────────────────────────┐    │
│  │                    Hybrid Storage                          │    │
│  │                                                           │    │
│  │  ┌─────────────────────────┐  ┌────────────────────────┐  │    │
│  │  │   Ring Buffer (Phase 1) │  │   Flow Store (Phase 2) │  │    │
│  │  │                         │  │                         │  │    │
│  │  │  Log entries             │  │  Full HTTP flows       │  │    │
│  │  │  + network summary       │  │  - Request headers     │  │    │
│  │  │    events injected by    │  │  - Request body        │  │    │
│  │  │    proxy adapter         │  │  - Response headers    │  │    │
│  │  │                         │  │  - Response body        │  │    │
│  │  │  "POST /api/login       │  │  - Timing breakdown    │  │    │
│  │  │   → 401 (234ms)"        │  │  - TLS info            │  │    │
│  │  │                         │  │  - Flow ID for linking  │  │    │
│  │  └─────────────────────────┘  └────────────────────────┘  │    │
│  └──────────────────────────────────────────────────────────┘    │
│                                                                   │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │                   Source Adapters                          │    │
│  │                                                           │    │
│  │  ┌─────────────┐  ┌──────────────┐  ┌────────────────┐   │    │
│  │  │ idevicesyslog│  │ log stream   │  │ Crash Reporter │   │    │
│  │  │ (Phase 1)    │  │ (Phase 1)    │  │ (Phase 1)      │   │    │
│  │  └─────────────┘  └──────────────┘  └────────────────┘   │    │
│  │                                                           │    │
│  │  ┌──────────────────────────────────────────────────┐     │    │
│  │  │              Proxy Adapter (Phase 2)              │     │    │
│  │  │                                                   │     │    │
│  │  │  Spawns: mitmdump -s addon.py --set port=8080    │     │    │
│  │  │  Reads: JSON lines from stdout                    │     │    │
│  │  │  Emits: summary LogEntry → ring buffer            │     │    │
│  │  │         full FlowRecord → flow store              │     │    │
│  │  │  Control: writes commands to stdin                 │     │    │
│  │  └──────────────────────────────────────────────────┘     │    │
│  └──────────────────────────────────────────────────────────┘    │
│                                                                   │
│           spawns as subprocess                                    │
│                  │                                                │
│                  ▼                                                │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │              mitmdump process                              │    │
│  │                                                           │    │
│  │  addon.py (our custom script)                             │    │
│  │  ├── request() callback → serialize to JSON → stdout      │    │
│  │  ├── response() callback → serialize to JSON → stdout     │    │
│  │  ├── error() callback → serialize to JSON → stdout        │    │
│  │  └── reads stdin for control commands (intercept rules,   │    │
│  │      filter changes, etc.)                                │    │
│  │                                                           │    │
│  │  Listens on: 0.0.0.0:8080 (configurable)                 │    │
│  │  CA cert: ~/.mitmproxy/mitmproxy-ca-cert.pem              │    │
│  └──────────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────┘

iOS Device (configured to use proxy at <host_ip>:8080)
    │
    └── All HTTP/HTTPS traffic routes through mitmdump
```

---

## 4. The Addon Script

The addon script (`server/proxy/addon.py`) is the bridge between mitmdump and our server. It runs inside the mitmdump process and communicates via stdout (JSON lines) and stdin (control commands).

### 4.1 Output Format (stdout → our server)

Each completed flow is serialized as a single JSON line:

```json
{
  "type": "flow",
  "id": "f_abc123",
  "timestamp": "2026-02-10T14:23:01.234Z",
  "request": {
    "method": "POST",
    "url": "https://api.example.com/v1/login",
    "host": "api.example.com",
    "path": "/v1/login",
    "headers": {"Content-Type": "application/json", "Authorization": "Bearer ..."},
    "body": "{\"email\":\"user@example.com\",\"password\":\"...\"}",
    "body_size": 56,
    "timestamp_start": 1707314581.234,
    "timestamp_end": 1707314581.240
  },
  "response": {
    "status_code": 401,
    "reason": "Unauthorized",
    "headers": {"Content-Type": "application/json"},
    "body": "{\"error\":\"invalid_credentials\"}",
    "body_size": 35,
    "timestamp_start": 1707314581.450,
    "timestamp_end": 1707314581.468
  },
  "timing": {
    "dns_ms": null,
    "connect_ms": 12.4,
    "tls_ms": 45.2,
    "request_ms": 6.0,
    "response_ms": 18.0,
    "total_ms": 234.0
  },
  "tls": {
    "version": "TLSv1.3",
    "sni": "api.example.com"
  },
  "error": null
}
```

For flows that error (connection refused, timeout, etc.):

```json
{
  "type": "flow",
  "id": "f_def456",
  "timestamp": "2026-02-10T14:23:02.000Z",
  "request": {
    "method": "GET",
    "url": "http://localhost:8090/health",
    "host": "localhost",
    "path": "/health",
    "headers": {},
    "body": null,
    "body_size": 0
  },
  "response": null,
  "timing": {"total_ms": 5002.0},
  "tls": null,
  "error": "Connection refused"
}
```

The addon also emits status events:

```json
{"type": "status", "event": "started", "port": 8080, "timestamp": "..."}
{"type": "status", "event": "client_connected", "address": "192.168.1.42", "timestamp": "..."}
{"type": "status", "event": "error", "message": "Address already in use", "timestamp": "..."}
```

### 4.2 Input Format (our server → stdin)

Control commands are JSON lines written to mitmdump's stdin:

```json
{"command": "set_intercept", "pattern": "~u /api/v1/login & ~m POST"}
{"command": "clear_intercept"}
{"command": "set_filter", "pattern": "~d api.example.com"}
{"command": "clear_filter"}
```

These use mitmproxy's flow filter syntax, which is well-documented and powerful.

### 4.3 Body Handling

HTTP bodies can be large (images, files, etc.). The addon applies these rules:
- Bodies ≤ 100KB: included inline as string (text) or base64 (binary)
- Bodies > 100KB: truncated, with `body_truncated: true` and `body_full_size: N` fields
- Binary content types (images, protobuf, etc.): stored as base64 with `body_encoding: "base64"`
- The AI agent can request full bodies via the flow detail endpoint when needed

---

## 5. Data Models

### 5.1 FlowRecord (Full Flow — lives in Flow Store)

```python
class FlowRequest(BaseModel):
    method: str
    url: str
    host: str
    path: str
    headers: dict[str, str]
    body: str | None = None
    body_size: int = 0
    body_truncated: bool = False
    body_encoding: str = "utf-8"  # "utf-8" or "base64"

class FlowResponse(BaseModel):
    status_code: int
    reason: str
    headers: dict[str, str]
    body: str | None = None
    body_size: int = 0
    body_truncated: bool = False
    body_encoding: str = "utf-8"

class FlowTiming(BaseModel):
    dns_ms: float | None = None
    connect_ms: float | None = None
    tls_ms: float | None = None
    request_ms: float | None = None
    response_ms: float | None = None
    total_ms: float

class FlowRecord(BaseModel):
    id: str
    timestamp: datetime
    device_id: str = "default"
    request: FlowRequest
    response: FlowResponse | None = None
    timing: FlowTiming
    tls: dict[str, str] | None = None
    error: str | None = None
    tags: list[str] = []  # e.g., ["auth", "slow", "error"]
```

### 5.2 Summary LogEntry (lives in Ring Buffer alongside logs)

When the proxy adapter receives a completed flow, it creates *both*:
1. A `FlowRecord` → stored in the flow store
2. A `LogEntry` → stored in the ring buffer

The LogEntry is a one-line summary:

```python
LogEntry(
    id="f_abc123",              # same ID as FlowRecord for linking
    timestamp=flow.timestamp,
    device_id="default",
    process="network",          # special process name for proxy entries
    subsystem=flow.request.host,  # host as subsystem for filtering
    category="proxy",
    level=LogLevel.ERROR if status >= 400 else LogLevel.INFO,
    message="POST /v1/login → 401 Unauthorized (234ms, 35B)",
    source=LogSource.PROXY,     # new enum value
    raw=""
)
```

This means:
- `tail_logs` naturally shows network events alongside app logs
- `get_log_summary` includes network errors in its digest
- `query_logs` with `source=proxy` isolates network traffic
- `query_logs` with `search="401"` finds both app log errors and network 401s

---

## 6. Flow Store

A dedicated in-memory store for full flow records, separate from the log ring buffer.

```python
class FlowStore:
    """In-memory store for HTTP flow records."""
    
    def __init__(self, max_size: int = 5_000):
        self._flows: OrderedDict[str, FlowRecord]  # id → FlowRecord
        self._max_size: int
    
    async def add(self, flow: FlowRecord) -> None: ...
    async def get(self, flow_id: str) -> FlowRecord | None: ...
    async def query(self, params: FlowQueryParams) -> tuple[list[FlowRecord], int]: ...
    async def clear(self) -> None: ...
```

### Query Parameters

```python
class FlowQueryParams(BaseModel):
    host: str | None = None          # filter by host
    path_contains: str | None = None  # filter by path substring
    method: str | None = None         # GET, POST, etc.
    status_min: int | None = None     # minimum status code
    status_max: int | None = None     # maximum status code
    has_error: bool | None = None     # only flows with connection errors
    since: datetime | None = None
    until: datetime | None = None
    device_id: str = "default"
    limit: int = 100
    offset: int = 0
```

---

## 7. HTTP API Design

### 7.1 Proxy Control

```
GET /api/v1/proxy/status
```

Returns proxy state, configuration, and connection info.

```json
{
  "status": "running",
  "port": 8080,
  "listen_host": "0.0.0.0",
  "started_at": "2026-02-10T14:00:00Z",
  "flows_captured": 156,
  "active_intercept": null,
  "active_filter": null,
  "connected_clients": ["192.168.1.42"]
}
```

```
POST /api/v1/proxy/start
```

Start the proxy (if not already running). Optional body for configuration:

```json
{
  "port": 8080,
  "listen_host": "0.0.0.0",
  "upstream_proxy": null
}
```

```
POST /api/v1/proxy/stop
```

Stop the proxy subprocess.

### 7.2 Flow Inspection

```
GET /api/v1/proxy/flows
```

Query captured flows.

**Query parameters:**
| Parameter      | Type     | Description                                    |
|----------------|----------|------------------------------------------------|
| `host`         | string   | Filter by host (e.g., `api.example.com`)       |
| `path_contains`| string   | Filter by path substring (e.g., `/login`)      |
| `method`       | string   | Filter by HTTP method                          |
| `status_min`   | integer  | Minimum response status code                   |
| `status_max`   | integer  | Maximum response status code                   |
| `has_error`    | boolean  | Only flows with connection errors              |
| `since`        | ISO8601  | Flows after this timestamp                     |
| `until`        | ISO8601  | Flows before this timestamp                    |
| `limit`        | integer  | Max results (default: 50, max: 500)            |
| `offset`       | integer  | Pagination offset                              |

**Response:**
```json
{
  "flows": [
    {
      "id": "f_abc123",
      "timestamp": "2026-02-10T14:23:01.234Z",
      "method": "POST",
      "url": "https://api.example.com/v1/login",
      "host": "api.example.com",
      "path": "/v1/login",
      "status_code": 401,
      "reason": "Unauthorized",
      "total_ms": 234.0,
      "request_size": 56,
      "response_size": 35,
      "error": null,
      "tags": ["auth", "error"]
    }
  ],
  "total": 156,
  "has_more": true
}
```

Note: The list response is intentionally compact — no bodies or full headers. Use the detail endpoint for those.

```
GET /api/v1/proxy/flows/{id}
```

Full flow detail including headers and bodies.

```json
{
  "id": "f_abc123",
  "timestamp": "2026-02-10T14:23:01.234Z",
  "request": {
    "method": "POST",
    "url": "https://api.example.com/v1/login",
    "headers": {
      "Content-Type": "application/json",
      "Authorization": "Bearer eyJ..."
    },
    "body": "{\"email\":\"user@example.com\",\"password\":\"...\"}",
    "body_size": 56
  },
  "response": {
    "status_code": 401,
    "reason": "Unauthorized",
    "headers": {
      "Content-Type": "application/json",
      "X-Request-Id": "req-789"
    },
    "body": "{\"error\":\"invalid_credentials\",\"message\":\"Email or password is incorrect\"}",
    "body_size": 75
  },
  "timing": {
    "dns_ms": null,
    "connect_ms": 12.4,
    "tls_ms": 45.2,
    "request_ms": 6.0,
    "response_ms": 18.0,
    "total_ms": 234.0
  },
  "tls": {
    "version": "TLSv1.3",
    "sni": "api.example.com"
  },
  "error": null
}
```

### 7.3 LLM-Optimized Flow Summary

```
GET /api/v1/proxy/flows/summary
```

Like the log summary endpoint, but for network traffic. Designed to answer "what network activity just happened?"

**Query parameters:**
| Parameter      | Type     | Description                                    |
|----------------|----------|------------------------------------------------|
| `window`       | string   | Time window: `30s`, `1m`, `5m`, `15m`          |
| `host`         | string   | Focus on specific host                         |
| `since_cursor` | string   | Cursor from previous summary for delta mode    |

**Response:**
```json
{
  "window": "5m",
  "generated_at": "2026-02-10T14:28:00Z",
  "cursor": "c_1707314880234",
  "summary": "In the last 5 minutes, the app made 47 HTTP requests to 3 hosts. api.example.com received 38 requests (35 succeeded, 3 returned 401). The 401s were all POST /v1/login attempts between 14:23:01 and 14:23:03, followed by a successful token refresh. cdn.example.com served 8 image requests, all 200 OK. One request to localhost:8090 failed with connection refused.",
  "total_flows": 47,
  "by_host": [
    {
      "host": "api.example.com",
      "total": 38,
      "success": 35,
      "client_error": 3,
      "server_error": 0,
      "avg_latency_ms": 180
    },
    {
      "host": "cdn.example.com",
      "total": 8,
      "success": 8,
      "client_error": 0,
      "server_error": 0,
      "avg_latency_ms": 45
    },
    {
      "host": "localhost:8090",
      "total": 1,
      "success": 0,
      "client_error": 0,
      "server_error": 0,
      "connection_errors": 1,
      "avg_latency_ms": null
    }
  ],
  "errors": [
    {
      "pattern": "POST /v1/login → 401",
      "count": 3,
      "first_seen": "2026-02-10T14:23:01Z",
      "last_seen": "2026-02-10T14:23:03Z"
    },
    {
      "pattern": "GET localhost:8090/health → Connection refused",
      "count": 1,
      "first_seen": "2026-02-10T14:23:02Z"
    }
  ],
  "slow_requests": [
    {
      "method": "POST",
      "url": "/v1/user/profile/upload",
      "total_ms": 2340,
      "status_code": 200
    }
  ]
}
```

### 7.4 Traffic Interception

```
POST /api/v1/proxy/intercept
```

Set an intercept rule. Matched flows are held by mitmproxy until released or modified.

```json
{
  "pattern": "~u /api/v1/login & ~m POST",
  "action": "hold"
}
```

Intercept patterns use mitmproxy's filter expression syntax:
- `~u regex` — URL matches regex
- `~m METHOD` — HTTP method
- `~d domain` — domain
- `~s` — response (intercept after server responds)
- `~q` — request (intercept before sending to server)
- Combine with `&` (and), `|` (or), `!` (not)

```
DELETE /api/v1/proxy/intercept
```

Clear all intercept rules.

### 7.5 Replay

```
POST /api/v1/proxy/replay/{id}
```

Replay a previously captured request. Optionally modify it before sending.

```json
{
  "modify_headers": {"Authorization": "Bearer new_token"},
  "modify_body": null
}
```

### 7.6 Setup Assistance

```
GET /api/v1/proxy/cert
```

Returns the mitmproxy CA certificate for installation on the iOS device.

```
GET /api/v1/proxy/setup-guide
```

Returns step-by-step instructions for configuring the iOS device to use the proxy. Includes the host machine's local IP address (auto-detected) and the proxy port.

```json
{
  "proxy_host": "192.168.1.100",
  "proxy_port": 8080,
  "cert_install_url": "http://mitm.it",
  "steps": [
    "1. On your iOS device, go to Settings → Wi-Fi → tap your network → Configure Proxy → Manual",
    "2. Set Server to 192.168.1.100 and Port to 8080",
    "3. Open Safari and visit http://mitm.it to download the CA certificate",
    "4. Go to Settings → General → VPN & Device Management → install the mitmproxy cert",
    "5. Go to Settings → General → About → Certificate Trust Settings → enable mitmproxy"
  ]
}
```

---

## 8. MCP Tools (additions to existing MCP server)

| Tool Name              | Description                                      | Maps To                         |
|------------------------|--------------------------------------------------|---------------------------------|
| `list_flows`           | Query captured HTTP flows                        | `GET /proxy/flows`              |
| `get_flow_detail`      | Get full request/response for a flow             | `GET /proxy/flows/{id}`         |
| `get_flow_summary`     | LLM-optimized network traffic digest             | `GET /proxy/flows/summary`      |
| `proxy_status`         | Check if proxy is running, how many flows, etc.  | `GET /proxy/status`             |
| `start_proxy`          | Start the proxy                                  | `POST /proxy/start`             |
| `stop_proxy`           | Stop the proxy                                   | `POST /proxy/stop`              |
| `set_intercept`        | Set a traffic intercept rule                     | `POST /proxy/intercept`         |
| `clear_intercept`      | Clear intercept rules                            | `DELETE /proxy/intercept`       |
| `replay_flow`          | Replay a captured request                        | `POST /proxy/replay/{id}`       |
| `proxy_setup_guide`    | Get device configuration instructions            | `GET /proxy/setup-guide`        |

The existing log-focused tools (`tail_logs`, `query_logs`, `get_log_summary`, `get_errors`) automatically include proxy summary events because those are injected into the shared ring buffer.

---

## 9. Proxy Source Adapter

Following the same pattern as Phase 1 source adapters:

```python
class ProxyAdapter(BaseSourceAdapter):
    """Manages mitmdump subprocess and captures HTTP flows."""
    
    def __init__(
        self,
        device_id: str = "default",
        on_entry: EntryCallback | None = None,   # for log ring buffer
        flow_store: FlowStore | None = None,       # for full flow storage
        proxy_port: int = 8080,
        listen_host: str = "0.0.0.0",
    ):
        super().__init__(
            adapter_id="proxy",
            adapter_type="mitmproxy",
            device_id=device_id,
            on_entry=on_entry,
        )
        self.flow_store = flow_store
        self.proxy_port = proxy_port
        self.listen_host = listen_host
        self._process: asyncio.subprocess.Process | None = None
    
    async def start(self) -> None:
        """Spawn mitmdump with our addon script."""
        addon_path = Path(__file__).parent.parent / "proxy" / "addon.py"
        cmd = [
            "mitmdump",
            "-s", str(addon_path),
            "--listen-host", self.listen_host,
            "--listen-port", str(self.proxy_port),
            "--set", "flow_detail=true",
            "--quiet",  # suppress mitmdump's own output
        ]
        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # start read loop (same pattern as SyslogAdapter)
    
    async def _handle_flow(self, data: dict) -> None:
        """Process a captured flow: store full record + emit summary log entry."""
        flow = FlowRecord(**data)
        
        # 1. Store full flow in flow store
        if self.flow_store:
            await self.flow_store.add(flow)
        
        # 2. Emit summary as a log entry into the ring buffer
        summary_msg = self._format_summary(flow)
        level = self._classify_level(flow)
        entry = LogEntry(
            id=flow.id,
            timestamp=flow.timestamp,
            device_id=self.device_id,
            process="network",
            subsystem=flow.request.host,
            category="proxy",
            level=level,
            message=summary_msg,
            source=LogSource.PROXY,
        )
        await self.emit(entry)
    
    def _format_summary(self, flow: FlowRecord) -> str:
        """Format a flow as a one-line summary for the log stream."""
        if flow.error:
            return f"{flow.request.method} {flow.request.path} → {flow.error} ({flow.timing.total_ms:.0f}ms)"
        if flow.response:
            return (
                f"{flow.request.method} {flow.request.path} "
                f"→ {flow.response.status_code} {flow.response.reason} "
                f"({flow.timing.total_ms:.0f}ms, {flow.response.body_size}B)"
            )
        return f"{flow.request.method} {flow.request.path} → pending"
    
    def _classify_level(self, flow: FlowRecord) -> LogLevel:
        """Classify flow as log level based on outcome."""
        if flow.error:
            return LogLevel.ERROR
        if flow.response:
            if flow.response.status_code >= 500:
                return LogLevel.ERROR
            if flow.response.status_code >= 400:
                return LogLevel.WARNING
        return LogLevel.INFO
    
    async def send_command(self, command: dict) -> None:
        """Send a control command to the mitmdump subprocess via stdin."""
        if self._process and self._process.stdin:
            line = json.dumps(command) + "\n"
            self._process.stdin.write(line.encode())
            await self._process.stdin.drain()
```

---

## 10. iOS Device Configuration

The proxy requires two things on the iOS device:

### 10.1 HTTP Proxy Setting

Settings → Wi-Fi → [network] → Configure Proxy → Manual
- Server: host machine's local IP (e.g., 192.168.1.100)
- Port: 8080 (our default)

### 10.2 CA Certificate Trust

For HTTPS interception, the device must trust mitmproxy's CA:

1. With proxy configured, visit `http://mitm.it` in Safari
2. Download and install the profile
3. Settings → General → About → Certificate Trust Settings → enable mitmproxy

This is standard mitmproxy setup. Our `GET /proxy/setup-guide` endpoint automates the instructions with the correct IP and port filled in.

**Certificate location:** mitmproxy auto-generates its CA on first run at `~/.mitmproxy/mitmproxy-ca-cert.pem`. Our `GET /proxy/cert` endpoint serves this file directly for alternative installation methods (e.g., MDM, Apple Configurator).

---

## 11. Technology Choices

### mitmdump version

**Recommendation: mitmproxy >= 10.0**

The addon API is stable and well-documented. Version 10+ supports Python 3.12 and has improved performance. Install via `pip install mitmproxy` (it's a Python package, which is convenient since our server is also Python).

### Addon communication

**stdout/stdin JSON lines.** This matches our existing subprocess pattern perfectly. mitmdump's addon runs in the same process as the proxy, and we use `sys.stdout` (not `print()`) to write JSON lines. On our server side, the ProxyAdapter reads these lines from the subprocess stdout just like SyslogAdapter reads idevicesyslog output.

### Flow Store sizing

**Default: 5,000 flows.** Flows are larger than log entries (they contain headers and bodies), so we keep fewer of them. At ~10KB average per flow, that's roughly 50MB of memory, which is acceptable.

---

## 12. Project Structure (new files)

```
server/
  proxy/                         # New: proxy-related code
    __init__.py
    addon.py                     # mitmproxy addon script (runs inside mitmdump)
    flow_store.py                # In-memory flow record store
  sources/
    proxy.py                     # ProxyAdapter (spawns/manages mitmdump)
  api/
    proxy.py                     # /proxy/* route handlers
  models.py                      # + FlowRecord, FlowRequest, FlowResponse, etc.

mcp/src/
  index.ts                       # + new proxy-related tools

tests/
  test_flow_store.py
  test_proxy_adapter.py
  test_proxy_api.py
  fixtures/
    flow_sample.json
```

---

## 13. Implementation Phases

### Phase 2a: Addon & Adapter (Week 1)

1. Write `server/proxy/addon.py` — mitmproxy addon that serializes flows to stdout
2. Implement `FlowStore` in `server/proxy/flow_store.py`
3. Implement `ProxyAdapter` in `server/sources/proxy.py`
4. Add `LogSource.PROXY` to models
5. Add flow data models to `server/models.py`
6. Test addon script independently with `mitmdump -s addon.py`

**Milestone:** Run mitmdump with our addon, make HTTP requests through it, see JSON lines on stdout.

### Phase 2b: API Endpoints (Week 2)

1. Create `server/api/proxy.py` with all proxy routes
2. Wire proxy adapter into `server/main.py` lifespan
3. Add `--proxy` / `--no-proxy` and `--proxy-port` CLI flags
4. Implement flow query, detail, and summary endpoints
5. Implement proxy start/stop/status endpoints
6. Implement setup-guide and cert endpoints

**Milestone:** Start the server with `--proxy`, configure iOS device, see flows appearing in both `/proxy/flows` and `/logs/stream`.

### Phase 2c: Intercept, Replay & MCP (Week 3)

1. Implement intercept rule management (stdin commands to addon)
2. Implement replay endpoint
3. Add proxy tools to the MCP server
4. Add flow summary generation (template-based, like log summaries)
5. Integration tests

**Milestone:** AI agent can query network traffic, see flow summaries alongside log summaries, and replay requests via MCP tools.

### Phase 2d: Polish (Week 4)

1. Body handling refinements (truncation, binary detection, base64)
2. Auto-tagging flows (auth, slow, error, redirect)
3. Flow correlation with log entries (match by timestamp + request ID)
4. Update CLAUDE.md and documentation

---

## 14. Design Decisions (Resolved)

1. **Integration pattern:** mitmdump as a subprocess, matching the idevicesyslog pattern. Single entry point via `quern-debug-server`.

2. **Storage:** Hybrid. Summary log entries in the shared ring buffer (visible in log stream, included in log summaries). Full flow records in a dedicated FlowStore (queryable via /proxy/flows endpoints).

3. **Communication:** stdout JSON lines from addon → server (flows), stdin JSON lines from server → addon (control commands). Same async readline pattern used throughout Phase 1.

4. **Body handling:** Inline for ≤ 100KB, truncated for larger. Binary content base64-encoded. Full bodies available via detail endpoint on demand.

5. **Proxy lifecycle:** The proxy is opt-in (`--proxy` flag) and can be started/stopped at runtime via API endpoints. It does not start by default since it requires device configuration.

6. **mitmproxy as a pip dependency:** Since our server is already Python, mitmproxy installs via pip alongside our other dependencies. No separate installation step.

7. **Level classification for flows:** 5xx → ERROR, 4xx → WARNING, 2xx/3xx → INFO, connection errors → ERROR. This means the log summary naturally highlights failing requests.
