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
- xcbeautify (build output parsing)

## Implementation Phases

We are currently building **Phase 1a: Minimum Viable Log Server**.

### Phase 1a — MVP (current)
- [x] Project scaffolding
- [ ] `idevicesyslog` source adapter (spawn subprocess, parse stdout)
- [ ] Ring buffer storage
- [ ] FastAPI server with `/logs/stream` (SSE) and `/logs/query`
- [ ] API key auth middleware
- [ ] `/health` endpoint
- [ ] CLI entry point to start/stop server

### Phase 1b — Structured Logging & Summaries
- [ ] `log stream` (OSLog) source adapter
- [ ] Processing pipeline (parser, classifier, deduplicator)
- [ ] `/logs/summary` endpoint with template-based generation + cursor support
- [ ] `/logs/errors` convenience endpoint

### Phase 1c — MCP Wrapper & Build Integration
- [ ] TypeScript MCP server (`mcp/`)
- [ ] `xcodebuild` output parser source adapter
- [ ] Crash report watcher source adapter
- [ ] npm package for MCP server
- [ ] MCP resources (guide, troubleshooting docs)

### Phase 1d — Polish & App Drain
- [ ] Swift package for app-embedded log drain (`swift-package/`)
- [ ] SQLite persistent storage option
- [ ] Log correlation engine (group by request/trace ID)

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

## Key Endpoints (Phase 1a target)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check (no auth required) |
| GET | `/api/v1/logs/stream` | SSE real-time log stream with filters |
| GET | `/api/v1/logs/query` | Historical log query with pagination |
| GET | `/api/v1/logs/sources` | List active log sources |
| POST | `/api/v1/logs/filter` | Reconfigure capture filters |

## Full Architecture Doc

See `docs/phase1-architecture.md` for the complete architecture document including all API schemas, log source details, and the processing pipeline design.
