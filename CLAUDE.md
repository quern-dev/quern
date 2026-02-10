# iOS Debug Server

## What This Project Is

An open-source system that captures iOS debug logs from multiple sources, processes them into structured and AI-consumable formats, and exposes them through a local HTTP API with a thin MCP wrapper. This is Phase 1 of a three-phase project to build a comprehensive AI-assisted iOS debugging and testing environment.

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
- **API key auth.** Auto-generated on first start, stored at `~/.ios-debug-server/api-key`. Required on all endpoints except `/health`. Supports `Authorization: Bearer <key>` or `X-API-Key: <key>`.
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
mcp/                 Thin TypeScript MCP-to-HTTP adapter
swift-package/       Optional app-embedded log drain (Swift)
tests/               Python tests + fixtures
docs/                Architecture docs, API reference
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

## Full Architecture Doc

See `docs/phase1-architecture.md` for the complete architecture document including all API schemas, log source details, and the processing pipeline design.

## Phase 2: Network Proxy (mitmproxy Integration)

### Overview

Phase 2 adds network traffic inspection by integrating mitmproxy as a managed subprocess. The proxy adapter follows the same pattern as Phase 1 source adapters: spawn mitmdump, read JSON lines from stdout, emit structured data. The key design is hybrid storage — compact summary events go into the shared log ring buffer (so log queries and summaries naturally include network events), while full flow records (headers, bodies, timing) go into a dedicated FlowStore.

### Architecture

```
ios-debug-server (port 9100)
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