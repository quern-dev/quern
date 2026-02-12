# Quern Debug Server

## What This Project Is

An open-source system that captures debug logs from multiple sources, processes them into structured and AI-consumable formats, and exposes them through a local HTTP API with a thin MCP wrapper. This is Phase 1 of a three-phase project to build a comprehensive AI-assisted iOS debugging and testing environment.

Phase 2 (network proxy via mitmproxy) and Phase 3 (remote device inspection/control via WebDriverAgent) will build on this foundation.

## Architecture Overview

```
AI Agent (Claude Code, Cursor, etc.)
    │
    ├── via MCP ──→ mcp/ (thin TypeScript stdio wrapper)
    │                  │
    └── via HTTP ──→ server/ (Python FastAPI, port 9100)
                       │
                       ├── sources/   → spawn idevicesyslog, log stream, watch crashes
                       ├── processing/ → parse, classify, deduplicate, summarize
                       ├── storage/   → in-memory ring buffer (SQLite later)
                       └── api/       → HTTP routes + SSE streaming
```

The MCP server is intentionally thin — just translates MCP tool calls into HTTP requests. All logic lives in the Python server. This follows the pattern validated by MobAI's architecture.

## Key Design Decisions

- **macOS-only at launch.** `log stream` (OSLog) is macOS-exclusive. `idevicesyslog` works on Linux but we're not targeting it yet. Avoid macOS-hostile patterns so Linux support can be added later.
- **Single device first, multi-device later.** All internal interfaces accept a `device_id` parameter (defaults to `"default"`). API responses include `device_id`. When multi-device lands, it's additive.
- **Ephemeral storage by default.** In-memory ring buffer (10,000 entries). Crash reports always persist to disk. SQLite is a future enhancement. The storage layer uses an abstract interface for easy substitution.
- **Cursor-based "what's new" summaries.** `/api/v1/logs/summary` returns a `cursor` and accepts `since_cursor` for delta summaries. This is critical for token-efficient AI workflows.
- **API key auth.** Auto-generated on first start, stored at `~/.quern/api-key`. Required on all endpoints except `/health`. Supports `Authorization: Bearer <key>` or `X-API-Key: <key>`.
- **LLM summaries are template-based, not LLM-generated.** The summary endpoint groups errors by pattern, detects resolution sequences, and composes natural language via templates. No external LLM calls needed.
- **Python (FastAPI) for the server** — chosen for Phase 2 alignment (mitmproxy is Python) and strong async subprocess support.
- **TypeScript for the MCP server** — best MCP SDK support, `npx` distribution.

## Tech Stack

- Python 3.11+ with FastAPI + uvicorn (server)
- TypeScript + Node.js 18+ (MCP wrapper)
- libimobiledevice (`idevicesyslog`, `idevicecrashreport`)
- macOS `log` command (`log stream --style json`)
- Pure-Python xcodebuild output parsing (no xcbeautify dependency)

## Implementation Phases

We are currently building **Phase 1d: Polish & App Drain**.

### Phase 1a — MVP (complete)
- [x] Project scaffolding
- [x] `idevicesyslog` source adapter (spawn subprocess, parse stdout)
- [x] Ring buffer storage
- [x] FastAPI server with `/logs/stream` (SSE) and `/logs/query`
- [x] API key auth middleware
- [x] `/health` endpoint
- [x] CLI entry point to start/stop server

### Phase 1b — Structured Logging & Summaries (complete)
- [x] `log stream` (OSLog) source adapter
- [x] Processing pipeline (classifier, deduplicator)
- [x] `/logs/summary` endpoint with template-based generation + cursor support
- [x] `/logs/errors` convenience endpoint

### Phase 1c — MCP Wrapper & Build Integration (complete)
- [x] TypeScript MCP server (`mcp/`)
- [x] `xcodebuild` output parser source adapter
- [x] Crash report watcher source adapter
- [x] npm package for MCP server
- [x] MCP resources (guide, troubleshooting docs)

### Phase 1d — Polish & App Drain - Deferred (prioritizing Phase 2; app-embedded log drain will be revisited after proxy integration)"
- [ deferred ] Swift package for app-embedded log drain (`swift-package/`)
- [ deferred ] SQLite persistent storage option
- [ deferred ] Log correlation engine (group by request/trace ID)

## Project Structure

```
server/              Python FastAPI log server
  sources/           Log source adapters (idevicesyslog, oslog, crash, build, app_drain)
  processing/        Pipeline: parser → classifier → deduplicator → correlator → summarizer
  storage/           Ring buffer (+ SQLite later)
  api/               HTTP route handlers
  lifecycle/         Daemon, state.json, port scanning, watchdog, setup
mcp/                 Thin TypeScript MCP-to-HTTP adapter
swift-package/       Optional app-embedded log drain (Swift)
tests/               Python tests + fixtures
docs/                Architecture docs, API reference
  docs/quern-roadmap.md                    Project roadmap (living document, status-tagged)
  docs/quern-app-graph-and-dsl-design.md   App graph + test DSL design (early design)
  docs/test-results/                       Captured test scenario results
  docs/bugs/                               Known bug write-ups
```

## Code Conventions

- Python: Use `async`/`await` throughout. Type hints on all function signatures. Use `pydantic` models for API request/response schemas.
- Imports: Group as stdlib → third-party → local, separated by blank lines.
- Source adapters: All inherit from `BaseSourceAdapter` (see `server/sources/base.py`). Must implement `start()`, `stop()`, and emit entries via callback.
- Error handling: Source adapters must not crash the server. Catch exceptions, log them, and continue.
- Testing: Use pytest with async support (`pytest-asyncio`). Fixture log data lives in `tests/fixtures/`.

## API Base URL

`http://127.0.0.1:9100/api/v1`

## Key Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check (no auth required) |
| GET | `/api/v1/logs/stream` | SSE real-time log stream with filters |
| GET | `/api/v1/logs/query` | Historical log query with pagination |
| GET | `/api/v1/logs/summary` | LLM-optimized summary with cursor support |
| GET | `/api/v1/logs/errors` | Errors and crashes only |
| GET | `/api/v1/logs/sources` | List active log sources |
| POST | `/api/v1/logs/filter` | Reconfigure capture filters |
| GET | `/api/v1/crashes/latest` | Recent crash reports with parsed details |
| GET | `/api/v1/builds/latest` | Most recent parsed build result |
| POST | `/api/v1/builds/parse` | Submit xcodebuild output for parsing |

## Key Documents

- `docs/phase1-architecture.md` — Complete Phase 1 architecture (API schemas, log sources, processing pipeline)
- `docs/phase2-architecture.md` — Phase 2 proxy architecture (addon protocol, flow models, API schemas)
- `docs/phase3-architecture.md` — Phase 3 device control architecture (simctl, idb, UI automation)
- `docs/phase4a-architecture.md` — Phase 4a lifecycle management (daemon, state.json, watchdog)
- `docs/quern-roadmap.md` — Project roadmap with status tags (complete/in-progress/designed/conceptual/seed). Covers core platform, developer experience, agent intelligence (app graph, test DSL, AI test runner), infrastructure/scale, and platform expansion.
- `docs/quern-app-graph-and-dsl-design.md` — Design doc for the app graph (screen/transition model, marker-based identity, pre-commit validation) and test DSL (two-level abstraction, graph-aware `navigate_to`, parameterized data). Includes agent reasoning use cases and proxy-driven fault injection.

## Phase 2: Network Proxy (mitmproxy Integration)

### Overview

Phase 2 adds network traffic inspection by integrating mitmproxy as a managed subprocess. The proxy adapter follows the same pattern as Phase 1 source adapters: spawn mitmdump, read JSON lines from stdout, emit structured data. The key design is hybrid storage — compact summary events go into the shared log ring buffer (so log queries and summaries naturally include network events), while full flow records (headers, bodies, timing) go into a dedicated FlowStore.

### Architecture

```
quern-debug-server (port 9100)
    │
    ├── Ring Buffer (logs + network summary events)
    ├── Flow Store (full HTTP flow records, separate)
    │
    └── spawns mitmdump subprocess
         ├── addon.py serializes flows → stdout (JSON lines)
         └── reads control commands ← stdin (JSON lines)
```

### Key Design Decisions

- **Subprocess pattern:** mitmdump as subprocess, same as idevicesyslog. Single entry point.
- **Hybrid storage:** Summary LogEntries in ring buffer, full FlowRecords in FlowStore (5,000 max).
- **stdout/stdin communication:** Addon writes JSON lines to stdout, reads commands from stdin.
- **Body handling:** Inline ≤ 100KB, truncated above. Binary → base64. Full bodies via detail endpoint.
- **Proxy is opt-in:** `--proxy` flag to enable. Can start/stop at runtime via API.
- **Level classification:** 5xx → ERROR, 4xx → WARNING, 2xx/3xx → INFO, connection errors → ERROR.
- **LogSource.PROXY:** New enum value. Network summary events appear with `source=proxy` in log queries.
- **process="network":** Network events use process name "network" and `subsystem=<host>` for filtering.

### New Files (Phase 2)

```
server/proxy/addon.py          mitmproxy addon (runs inside mitmdump)
server/proxy/flow_store.py     In-memory flow record store
server/sources/proxy.py        ProxyAdapter (spawns/manages mitmdump)
server/api/proxy.py            /proxy/* route handlers
```

### New API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/proxy/status` | Proxy status & config |
| POST | `/api/v1/proxy/start` | Start the proxy |
| POST | `/api/v1/proxy/stop` | Stop the proxy |
| GET | `/api/v1/proxy/flows` | Query captured flows |
| GET | `/api/v1/proxy/flows/{id}` | Full flow detail (headers, bodies) |
| GET | `/api/v1/proxy/flows/summary` | LLM-optimized traffic digest |
| POST | `/api/v1/proxy/intercept` | Set intercept rules |
| DELETE | `/api/v1/proxy/intercept` | Clear intercept rules |
| POST | `/api/v1/proxy/replay/{id}` | Replay a captured flow |
| GET | `/api/v1/proxy/cert` | Download CA certificate |
| GET | `/api/v1/proxy/setup-guide` | Device setup instructions |

### New MCP Tools

| Tool | Maps To | Description |
|------|---------|-------------|
| `list_flows` | GET /proxy/flows | Query captured HTTP flows |
| `get_flow_detail` | GET /proxy/flows/{id} | Full request/response detail |
| `get_flow_summary` | GET /proxy/flows/summary | LLM-optimized traffic digest |
| `proxy_status` | GET /proxy/status | Check proxy state |
| `start_proxy` | POST /proxy/start | Start the proxy |
| `stop_proxy` | POST /proxy/stop | Stop the proxy |
| `set_intercept` | POST /proxy/intercept | Set intercept rules |
| `clear_intercept` | DELETE /proxy/intercept | Clear intercept rules |
| `replay_flow` | POST /proxy/replay/{id} | Replay captured request |
| `proxy_setup_guide` | GET /proxy/setup-guide | Device config instructions |

### Implementation Checklist

#### Phase 2a — Addon & Adapter
- [ ] `server/proxy/addon.py` — mitmproxy addon, serialize flows to stdout JSON lines
- [ ] `server/proxy/flow_store.py` — in-memory FlowStore with query support
- [ ] `server/sources/proxy.py` — ProxyAdapter (spawn mitmdump, read stdout, emit entries)
- [ ] Add `LogSource.PROXY` enum value to models
- [ ] Add flow data models (FlowRecord, FlowRequest, FlowResponse, FlowTiming, FlowQueryParams)
- [ ] Test addon independently with `mitmdump -s addon.py`

#### Phase 2b — API Endpoints
- [ ] `server/api/proxy.py` — all proxy route handlers
- [ ] Wire ProxyAdapter into `server/main.py` lifespan
- [ ] CLI flags: `--proxy` / `--no-proxy`, `--proxy-port`
- [ ] Flow query, detail, summary endpoints
- [ ] Proxy start/stop/status endpoints
- [ ] Setup-guide and cert endpoints

#### Phase 2c — Intercept, Replay & MCP
- [ ] Intercept rule management (stdin commands to addon)
- [ ] Replay endpoint
- [ ] Add proxy tools to MCP server
- [ ] Flow summary generation (template-based)
- [ ] Integration tests

#### Phase 2d — Polish
- [ ] Body handling refinements (truncation, binary detection, base64)
- [ ] Auto-tagging flows (auth, slow, error, redirect)
- [ ] Flow-to-log correlation (timestamp + request ID matching)
- [ ] Update documentation

### Dependencies

Add to pyproject.toml:
```
"mitmproxy>=10.0",
```

### Full Architecture Doc

See `docs/phase2-architecture.md` for complete details including addon protocol, data models, API schemas, and iOS device setup instructions.



### Phase 3 Context: Device Inspection & Control

**Architecture Reference**
The full Phase 3 architecture document is at docs/phase3-architecture.md. It contains the complete design rationale, API surface, data models, backend interfaces, and implementation phases. Read it before starting implementation — it covers the "why" behind decisions (tool selection, backend separation, active device concept, tap-element workflow, screenshot strategies) that this file summarizes as implementation rules.

**What it does:** Adds remote iOS simulator inspection and control — list devices, boot/shutdown, install/launch apps, take screenshots, read accessibility trees, tap/swipe/type — all via the same HTTP API + MCP wrapper pattern.

**Architecture:** Unlike Phases 1-2 (long-running subprocesses), Phase 3 dispatches individual CLI commands:
- `xcrun simctl` — device/app management, screenshots (always available with Xcode)
- `idb` (Facebook iOS Development Bridge) — UI automation: accessibility tree, tap, swipe, type (requires separate install)

**Key pattern:** DeviceController orchestrator routes operations to SimctlBackend or IdbBackend based on what's needed. Each backend calls its CLI tool as an async subprocess.

---

### File Layout

```
server/device/controller.py    — DeviceController: orchestrates backends, active device tracking
server/device/simctl.py        — SimctlBackend: xcrun simctl subprocess calls
server/device/idb.py           — IdbBackend: idb subprocess calls for UI automation
server/device/ui_tree.py       — Parse idb describe-all output → UITree model
server/device/screenshots.py   — Screenshot capture, scaling (Pillow), annotation
server/api/device.py           — FastAPI route handlers for /api/v1/device/*
server/models.py               — DeviceInfo, AppInfo, UIElement, UITree, request models
mcp/src/index.ts               — 18 new MCP tools for device control
```

---

### Implementation Rules

**Subprocess pattern:**
```python
async def _run_simctl(self, *args: str) -> tuple[str, str]:
    proc = await asyncio.create_subprocess_exec(
        "xcrun", "simctl", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise DeviceError(f"simctl {args[0]} failed: {stderr.decode().strip()}")
    return stdout.decode(), stderr.decode()
```

Same pattern for idb. Always async subprocess, always check returncode, always decode and strip.

**Active device resolution order:**
1. Explicit `udid` parameter → use it, update active
2. Stored `self._active_udid` → use it
3. Auto-detect: exactly 1 booted simulator → use it, update active
4. 0 booted → error "No booted simulator"
5. 2+ booted → error "Multiple simulators booted, specify udid"

**simctl JSON parsing:** `xcrun simctl list devices --json` returns:
```json
{
  "devices": {
    "com.apple.CoreSimulator.SimRuntime.iOS-18-2": [
      {"udid": "...", "name": "iPhone 16 Pro", "state": "Booted", "isAvailable": true}
    ]
  }
}
```
Flatten the runtime-keyed dict into a single list. Extract OS version from the runtime key.

**idb describe-all output parsing:** The output format varies by idb version. Parse conservatively — build UIElement tree from whatever structure idb provides. If parsing fails, return raw text in a fallback field rather than crashing.

**Screenshot handling:**
- `simctl io <udid> screenshot <path>` writes PNG to a temp file
- Use Pillow to scale (default 0.5x) and optionally convert to JPEG
- For annotated screenshots: overlay red bounding boxes + labels on interactive elements from the accessibility tree
- Return bytes with correct Content-Type header

**tap-element flow:**
1. Get UI tree via idb
2. Search by label OR identifier (not both)
3. Optionally filter by element_type
4. 0 matches → 404
5. 1 match → calculate center from frame, tap it
6. 2+ matches → return 200 with `"status": "ambiguous"` and all matches (let AI agent choose)

**Tool availability checking:**
```python
async def is_available(self, tool: str) -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            "which", tool,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        return proc.returncode == 0
    except Exception:
        return False
```

Report in GET /device/list response so the AI agent knows what it can do.

---

### API Routes (server/api/device.py)

All routes under `/api/v1/device/`. The DeviceController is instantiated once and shared (same pattern as proxy source — stored on app state or as module-level singleton).

**Device management (simctl):**
- `GET /list` — list simulators, include tool availability
- `POST /boot` — boot simulator by udid or name
- `POST /shutdown` — shutdown simulator

**App management (simctl):**
- `POST /app/install` — install .app/.ipa
- `POST /app/launch` — launch by bundle_id
- `POST /app/terminate` — terminate by bundle_id
- `GET /app/list` — list installed apps

**Inspection (simctl + idb):**
- `GET /screenshot` — capture screenshot (params: format, scale, quality)
- `GET /screenshot/annotated` — screenshot with accessibility overlays
- `GET /ui` — full accessibility tree JSON
- `GET /screen-summary` — LLM-optimized text description

**Interaction (idb):**
- `POST /ui/tap` — tap at x,y
- `POST /ui/tap-element` — find element by label/id, tap its center
- `POST /ui/swipe` — swipe gesture
- `POST /ui/type` — type text into focused field
- `POST /ui/press` — press hardware button

**Configuration (simctl):**
- `POST /location` — set GPS coordinates
- `POST /permission` — grant app permission

---

### MCP Tools (mcp/src/index.ts)

18 new tools. Follow existing patterns exactly:
- Each tool calls the HTTP API via fetch
- Return structured JSON responses
- Include `udid` as optional param (server resolves active device)

Tool names: `list_devices`, `boot_device`, `shutdown_device`, `install_app`, `launch_app`, `terminate_app`, `list_apps`, `take_screenshot`, `get_ui_tree`, `get_screen_summary`, `tap`, `tap_element`, `swipe`, `type_text`, `press_button`, `set_location`, `grant_permission`

For `take_screenshot`: the MCP tool should return the image as base64-encoded string with a mime type field, since MCP tools return JSON.

---

### Models to Add (server/models.py)

Add these to the existing models file:
- `DeviceType` (enum: simulator, device)
- `DeviceState` (enum: booted, shutdown, booting)
- `DeviceInfo` — udid, name, state, device_type, os_version, runtime, is_available
- `AppInfo` — bundle_id, name, app_type, architecture, install_type, process_state
- `UIElement` — type, label, identifier, value, frame, enabled, visible, traits, children
- `UITree` — app_bundle_id, root, element_count, timestamp + helper methods (flatten, find_by_label, find_by_type)
- `TapRequest` — x, y, udid?
- `TapElementRequest` — label?, identifier?, element_type?, udid?
- `SwipeRequest` — start_x, start_y, end_x, end_y, duration, udid?
- `TypeTextRequest` — text, udid?
- `PressButtonRequest` — button, udid?
- `SetLocationRequest` — latitude, longitude, udid?
- `GrantPermissionRequest` — bundle_id, permission, udid?
- `InstallAppRequest` — app_path, udid?
- `LaunchAppRequest` — bundle_id, udid?
- `BootDeviceRequest` — udid?, name?
- `ShutdownDeviceRequest` — udid
- `DeviceError` (exception class)

---

### Testing Strategy

**Unit tests with mocked subprocesses.** Never call real simctl/idb in tests.

Mock pattern:
```python
@pytest.fixture
def mock_subprocess():
    with patch("asyncio.create_subprocess_exec") as mock:
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"output", b""))
        proc.returncode = 0
        mock.return_value = proc
        yield mock, proc
```

**Fixture files:**
- `tests/fixtures/simctl_list_output.json` — real `simctl list devices --json` output
- `tests/fixtures/idb_describe_all_output.txt` — real `idb ui describe-all` output
- Capture these from your actual machine for accuracy

**Test categories:**
1. SimctlBackend — parsing simctl JSON, command construction, error handling
2. IdbBackend — command construction, error handling
3. UITree parsing — idb output → UIElement tree, find_by_label, find_by_type, flatten
4. DeviceController — active device resolution logic, backend routing
5. API routes — request handling, response formats, error responses
6. Screenshots — scaling, format conversion (mock Pillow)
7. tap-element — 0/1/multiple match scenarios

---

### Dependencies to Add

In pyproject.toml:
```
"Pillow>=10.0",
```

---

### Error Handling

Custom exception:
```python
class DeviceError(Exception):
    """Raised when a device operation fails."""
    def __init__(self, message: str, tool: str = "unknown"):
        self.tool = tool
        super().__init__(message)
```

Map to HTTP responses:
- DeviceError from simctl/idb → 500 with tool name and stderr message
- Tool not available → 503 with install instructions
- No booted device → 400 with helpful message
- Element not found (tap-element) → 404
- Ambiguous element (tap-element) → 200 with status "ambiguous"

---

### Integration with Phases 1 & 2

Phase 3 doesn't change any Phase 1/2 code. It adds new routes under `/api/v1/device/` and new MCP tools. The router is registered alongside existing routers in the FastAPI app.

The power is in the AI agent combining all three:
```
tail_logs → see error → get_screen_summary → understand UI state → 
get_flow_summary → inspect network request → tap_element("Retry") → 
tail_logs → verify error resolved
```

---

### Phase 3 Sub-phases

- **3a:** SimctlBackend + DeviceController + screenshots + device management API + tests
- **3b:** IdbBackend + accessibility tree parsing + screen-summary + tap-element + tests
- **3c:** All UI interaction endpoints + MCP tools + annotated screenshots + integration tests
- **3d (deferred):** Physical device support via devicectl + WDA



### Phase 4a Context: Process Lifecycle Management

**What it does:** Makes server and proxy startup idempotent, adds daemon mode, and establishes `~/.quern/state.json` as the single source of truth for all tools and agents.

**The core problem this solves:** Agents currently waste time fighting process management — detecting running instances, killing them, restarting. After 4a, `quern-debug-server start` always does the right thing and `ensure_server` MCP tool is all an agent needs.

---

### File Layout

```
server/lifecycle/__init__.py
server/lifecycle/state.py       — Read/write/validate state.json with file locking
server/lifecycle/ports.py       — Port scanning and availability checking
server/lifecycle/daemon.py      — Fork, setsid, stdio redirect, signal handlers
server/lifecycle/watchdog.py    — Async monitor for proxy subprocess health
```

---

### Key Constants

```python
CONFIG_DIR = Path.home() / ".quern"
STATE_FILE = CONFIG_DIR / "state.json"
LOG_FILE = CONFIG_DIR / "server.log"
DEFAULT_SERVER_PORT = 9100
DEFAULT_PROXY_PORT = 9101  # NOT 8080 — that's retired
MAX_PORT_SCAN = 20
```

---

### Critical Implementation Rules

**State file is the contract.** Every consumer (CLI, MCP, shell scripts, CI) discovers the server by reading `~/.quern/state.json`. Never hardcode ports. Never use environment variables for discovery.

**Idempotent start pattern:**
```python
existing = read_state()
if existing and health_check(existing["server_port"]):
    print(f"Server already running on port {existing['server_port']}")
    sys.exit(0)
if existing:
    # Stale — clean up
    remove_state()
# Proceed to start
```

**Port scanning:** Always try preferred port first, scan upward on conflict. Server port first, then proxy port starts scanning from `server_port + 1`.

**Daemon fork pattern:**
```python
pid = os.fork()
if pid > 0:
    # Parent: wait for health check, print status, exit
    wait_for_health(timeout=5.0)
    print_status()
    sys.exit(0)
# Child: become session leader, redirect stdio, run server
os.setsid()
redirect_stdio_to_log()
run_server()
```

**File locking on state.json:** Use `fcntl.flock()` for all reads (LOCK_SH) and writes (LOCK_EX) to prevent races between concurrent tools.

**Signal handling in daemon:**
```python
def handle_sigterm(signum, frame):
    # 1. Stop accepting requests
    # 2. SIGTERM proxy child, wait 2s, SIGKILL if needed
    # 3. Remove state.json
    # 4. sys.exit(0)
```

**Proxy watchdog:** Async task that checks `proxy_process.returncode` every 1s. If proxy dies unexpectedly, set status to "crashed" and update state.json. Do NOT auto-restart.

---

### CLI Subcommands

The `cli()` function in `main.py` changes from a single-command entry point to subcommands:

```
quern-debug-server start [--no-proxy] [--port N] [--proxy-port N] [--foreground] [--verbose]
quern-debug-server stop
quern-debug-server restart [OPTIONS]
quern-debug-server status
```

`--foreground` skips daemonization — essential for debugging the server itself. All existing behavior (direct `quern-debug-server` with no subcommand) should remain as an alias for `start --foreground` for backward compatibility.

---

### MCP Changes

**New tool: `ensure_server`**
- Reads state.json, health checks, starts if needed
- Returns: server_url, proxy_port, api_key, devices
- This is the ONLY tool agents should use to start the server

**Updated: `server_status`**
- Same info as ensure_server but never starts the server
- Returns `{"status": "not_running"}` if no instance found

**Updated: MCP guide resource**
- Tell agents to call `ensure_server` first, never start manually

---

### Testing Approach

Process lifecycle tests are inherently integration-level. Use `subprocess.run` to invoke CLI commands and verify:
- `start` creates state.json and daemon process
- `start` again is idempotent (exit 0, no new process)
- `stop` removes state.json and kills process
- `status` returns correct info
- Port conflict → successful scan to next port
- Stale state.json → cleaned up on next `start`

Unit-test state.py and ports.py in isolation. Daemon.py and watchdog.py need process-level tests.