# Phase 1: iOS Debug Log Capture & AI Context System

## Architecture Document — v0.1

---

## 1. Vision

Build an open-source system that captures iOS debug logs from multiple sources, processes them into structured and AI-consumable formats, and exposes them through a local HTTP API with a thin MCP wrapper. An AI agent should be able to tail logs in real time, query historical logs, receive filtered/summarized digests, and correlate log events with UI state — all without Xcode running.

This is the foundation layer. Phases 2 (network proxy) and 3 (device control) will plug into the same API surface.

---

## 2. Log Sources

iOS debug information comes from several distinct channels. We need to capture all of them.

### 2.1 Device System Log (`idevicesyslog`)

**What it is:** The unified device log stream, equivalent to what you see in Xcode's console or Console.app. Includes all processes, not just your app.

**Tool:** `idevicesyslog` from [libimobiledevice](https://github.com/libimobiledevice/libimobiledevice)

**Key capabilities:**
- Streams logs over USB in real time (no WiFi pairing needed for most setups)
- Can filter by process name (`-p MyApp`)
- Supports matching (`-m "keyword"`) and exclusion (`-e "noise"`)
- Works with both debug and release builds
- No Xcode dependency — runs standalone on macOS and Linux

**Raw output format:**
```
Feb  7 14:23:01 iPhone MyApp(CoreFoundation)[1234] <Notice>: viewDidLoad called for HomeViewController
Feb  7 14:23:01 iPhone MyApp[1234] <Error>: Failed to fetch user profile: HTTP 401
Feb  7 14:23:02 iPhone kernel(Sandbox)[0] <Notice>: MyApp(1234) deny(1) mach-lookup com.apple.something
```

**Parsing strategy:** Each line follows the pattern:
```
{date} {device} {process}({subsystem})[{pid}] <{level}>: {message}
```

We'll parse this into structured records:
```json
{
  "timestamp": "2026-02-07T14:23:01.000Z",
  "device_id": "default",
  "device": "iPhone",
  "process": "MyApp",
  "subsystem": "CoreFoundation",
  "pid": 1234,
  "level": "Error",
  "message": "Failed to fetch user profile: HTTP 401",
  "source": "syslog"
}
```

### 2.2 OSLog / Unified Logging (`log stream`)

**What it is:** Apple's structured logging system (iOS 10+). Richer than syslog — includes categories, subsystems, and activity tracing.

**Tool:** `log stream` on macOS (requires the device to be connected)

**Key capabilities:**
- Filter by subsystem: `--predicate 'subsystem == "com.myapp.networking"'`
- Filter by category: `--predicate 'category == "auth"'`
- Filter by log level: `--level debug` (shows debug and above)
- Output as JSON: `--style json` (this is huge for us)
- Includes signpost data for performance tracing

**JSON output format (simplified):**
```json
{
  "traceID": 1234567890,
  "eventMessage": "Request completed in 234ms",
  "eventType": "logEvent",
  "source": null,
  "formatString": "Request completed in %{public}dms",
  "subsystem": "com.myapp.networking",
  "category": "performance",
  "timestamp": "2026-02-07 14:23:01.234567-0800",
  "machTimestamp": 1234567890,
  "messageType": "Default",
  "processID": 1234,
  "processImagePath": "/private/var/containers/Bundle/Application/.../MyApp",
  "senderImagePath": "/usr/lib/libsystem_trace.dylib"
}
```

**Advantage over idevicesyslog:** Already structured, includes subsystem/category, can be filtered at the source level (reducing noise dramatically).

**Limitation:** Only works on macOS (not Linux). Requires the device to be paired.

### 2.3 Crash Logs & Exception Reports

**What they are:** Structured reports generated when the app crashes, including symbolicated stack traces (for debug builds), exception type, and thread state.

**Location:** Pulled from device via `idevicecrashreport` (libimobiledevice) or from `~/Library/Logs/CrashReporter/` after Xcode syncs them.

**Strategy:** Poll periodically or watch the directory. Parse the `.ips` (JSON) or `.crash` (plain text) format. These are high-signal, low-noise — every crash log is worth surfacing to the AI.

### 2.4 Build Output (xcodebuild)

**What it is:** Compiler warnings, errors, linker output, and test results from `xcodebuild`.

**Tool:** `xcodebuild build` or `xcodebuild test` piped through `xcbeautify` or `xcpretty` for structured output.

**Strategy:** When the AI triggers a build (or we detect one), capture the full output. Parse it into:
- Build errors (file, line, column, message)
- Warnings (same structure)
- Test results (pass/fail per test case, duration, failure message)

This is less about streaming and more about capturing discrete build events.

### 2.5 App-Embedded Log Drain (Optional Enhancement)

**What it is:** A lightweight logging library embedded in the debug build that sends structured logs directly to our server over a local socket.

**Why:** `idevicesyslog` and `log stream` capture OS-level logs, but the app itself has richer context — it knows about view controller lifecycle, user actions, state machine transitions, network request/response bodies, etc.

**Implementation sketch:**
```swift
// In the app's debug configuration
import Foundation

class DebugLogDrain {
    static let shared = DebugLogDrain()
    private var socket: URLSessionWebSocketTask?
    
    func configure(serverURL: URL = URL(string: "ws://192.168.1.X:9100/logs")!) {
        socket = URLSession.shared.webSocketTask(with: serverURL)
        socket?.resume()
    }
    
    func log(_ message: String, category: String = "general", 
             level: String = "info", metadata: [String: Any]? = nil) {
        let entry: [String: Any] = [
            "timestamp": ISO8601DateFormatter().string(from: Date()),
            "category": category,
            "level": level,
            "message": message,
            "metadata": metadata ?? [:],
            "source": "app_drain"
        ]
        if let data = try? JSONSerialization.data(withJSONObject: entry),
           let text = String(data: data, encoding: .utf8) {
            socket?.send(.string(text)) { _ in }
        }
    }
}
```

This is optional but powerful — it gives the AI the richest possible context. We'd provide a drop-in Swift package.

---

## 3. System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        AI Agent                                  │
│                 (Claude Code, Cursor, etc.)                       │
│                                                                   │
│  ┌─────────────┐     ┌──────────────────────────────────────┐    │
│  │  MCP Client  │────▶│  MCP Server (thin stdio wrapper)     │    │
│  └─────────────┘     │  Translates MCP tools → HTTP calls    │    │
│                       └──────────────┬───────────────────────┘    │
└──────────────────────────────────────┼────────────────────────────┘
                                       │ HTTP
                                       ▼
┌──────────────────────────────────────────────────────────────────┐
│                    Log Server (port 9100)                         │
│                                                                   │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │                     HTTP API Layer                        │    │
│  │                                                           │    │
│  │  GET  /api/v1/logs/stream          SSE real-time stream   │    │
│  │  GET  /api/v1/logs/query           Historical query       │    │
│  │  GET  /api/v1/logs/summary         LLM-optimized digest   │    │
│  │  GET  /api/v1/logs/errors          Errors & crashes only  │    │
│  │  GET  /api/v1/logs/sources         List active sources    │    │
│  │  POST /api/v1/logs/filter          Set active filters     │    │
│  │  GET  /api/v1/builds/latest        Last build result      │    │
│  │  GET  /api/v1/crashes/latest       Recent crash reports   │    │
│  │  GET  /api/v1/health               Server health check    │    │
│  └──────────────────────┬───────────────────────────────────┘    │
│                          │                                        │
│  ┌──────────────────────▼───────────────────────────────────┐    │
│  │                  Processing Pipeline                      │    │
│  │                                                           │    │
│  │  1. Parse raw lines → structured records                  │    │
│  │  2. Classify (error / warning / info / debug / noise)     │    │
│  │  3. Deduplicate (suppress repeated identical messages)    │    │
│  │  4. Correlate (group related entries by request ID, etc.) │    │
│  │  5. Store in ring buffer (last N entries, configurable)   │    │
│  │  6. Generate rolling summaries on demand                  │    │
│  └──────────────────────┬───────────────────────────────────┘    │
│                          │                                        │
│  ┌──────────────────────▼───────────────────────────────────┐    │
│  │                   Source Adapters                          │    │
│  │                                                           │    │
│  │  ┌─────────────┐  ┌──────────────┐  ┌────────────────┐   │    │
│  │  │ idevicesyslog│  │ log stream   │  │ Crash Reporter │   │    │
│  │  │ (subprocess) │  │ (subprocess) │  │ (file watcher) │   │    │
│  │  └──────┬──────┘  └──────┬───────┘  └───────┬────────┘   │    │
│  │         │                │                   │            │    │
│  │  ┌──────┴──────┐  ┌──────┴───────┐  ┌───────┴────────┐   │    │
│  │  │ xcodebuild  │  │  WebSocket   │  │  (Future:      │   │    │
│  │  │ (subprocess) │  │  (app drain) │  │   Phase 2/3)   │   │    │
│  │  └─────────────┘  └──────────────┘  └────────────────┘   │    │
│  └──────────────────────────────────────────────────────────┘    │
│                                                                   │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │                   Ring Buffer Store                        │    │
│  │                                                           │    │
│  │  Configurable size (default: 10,000 entries)              │    │
│  │  Indexed by: timestamp, level, source, process, category  │    │
│  │  Optional: persist to SQLite for cross-session queries    │    │
│  └──────────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────┘
```

---

## 4. HTTP API Design

### 4.1 Authentication

All endpoints (except `GET /api/v1/health`) require authentication via one of:

- Header: `Authorization: Bearer <api-key>`
- Header: `X-API-Key: <api-key>`

The API key is generated on first server start and stored at `~/.ios-debug-server/api-key`. The MCP server reads this file automatically. Unauthenticated requests receive `401 Unauthorized`.

```bash
# The key is printed on first start and can be retrieved:
cat ~/.ios-debug-server/api-key

# Or regenerated:
ios-debug-server regenerate-key
```

### 4.2 Log Streaming (SSE)

```
GET /api/v1/logs/stream
```

Server-Sent Events endpoint for real-time log tailing. Supports query parameters for filtering at the source.

**Query parameters:**
| Parameter     | Type     | Description                                      |
|---------------|----------|--------------------------------------------------|
| `level`       | string   | Minimum level: `debug`, `info`, `notice`, `warning`, `error`, `fault` |
| `process`     | string   | Filter by process name (e.g., `MyApp`)           |
| `subsystem`   | string   | Filter by OSLog subsystem                        |
| `category`    | string   | Filter by OSLog category                         |
| `source`      | string   | Filter by source adapter: `syslog`, `oslog`, `crash`, `build`, `app_drain` |
| `match`       | string   | Include only lines matching this substring        |
| `exclude`     | string   | Exclude lines matching this substring             |

**SSE event format:**
```
event: log
data: {"timestamp":"2026-02-07T14:23:01.234Z","level":"error","process":"MyApp","subsystem":"com.myapp.networking","category":"auth","message":"Failed to fetch user profile: HTTP 401","source":"oslog","pid":1234}

event: log
data: {"timestamp":"2026-02-07T14:23:01.500Z","level":"info","process":"MyApp","message":"Retrying with refreshed token...","source":"syslog","pid":1234}

event: heartbeat
data: {"time":"2026-02-07T14:23:05.000Z","buffer_size":4523,"sources_active":["syslog","oslog"]}
```

### 4.3 Historical Query

```
GET /api/v1/logs/query
```

Query the ring buffer for historical entries.

**Query parameters:**
| Parameter     | Type     | Description                                      |
|---------------|----------|--------------------------------------------------|
| `since`       | ISO8601  | Entries after this timestamp                     |
| `until`       | ISO8601  | Entries before this timestamp                    |
| `level`       | string   | Minimum level filter                             |
| `process`     | string   | Process name filter                              |
| `search`      | string   | Full-text search across messages                 |
| `limit`       | integer  | Max results (default: 100, max: 1000)            |
| `offset`      | integer  | Pagination offset                                |

**Response:**
```json
{
  "entries": [
    {
      "id": "a1b2c3d4",
      "timestamp": "2026-02-07T14:23:01.234Z",
      "level": "error",
      "process": "MyApp",
      "subsystem": "com.myapp.networking",
      "category": "auth",
      "message": "Failed to fetch user profile: HTTP 401",
      "source": "oslog",
      "pid": 1234
    }
  ],
  "total": 47,
  "has_more": true
}
```

### 4.4 LLM-Optimized Summary

```
GET /api/v1/logs/summary
```

This is the killer feature. Instead of dumping raw logs into an AI's context window (expensive and noisy), we pre-digest them into a compact, actionable summary.

**Query parameters:**
| Parameter     | Type     | Description                                      |
|---------------|----------|--------------------------------------------------|
| `window`      | string   | Time window: `30s`, `1m`, `5m`, `15m`, `1h`     |
| `process`     | string   | Focus on specific process                        |
| `format`      | string   | `prose` (default) or `structured`                |
| `since_cursor`| string   | Opaque cursor from a previous summary response — returns only new entries since that point |

**Prose response (default):**
```json
{
  "window": "5m",
  "generated_at": "2026-02-07T14:28:00Z",
  "cursor": "c_1707314880234",
  "summary": "In the last 5 minutes, MyApp logged 234 entries. 3 errors occurred, all related to authentication: the app failed to refresh the OAuth token 3 times (HTTP 401 from /api/v1/user/profile) before succeeding on the 4th attempt at 14:25:33. There were 12 warnings about AutoLayout constraint conflicts in FeedViewController. Memory usage appears stable — no low-memory warnings. The app launched successfully at 14:23:00 and loaded HomeViewController → FeedViewController → ProfileViewController in sequence.",
  "error_count": 3,
  "warning_count": 12,
  "total_count": 234,
  "top_issues": [
    {
      "pattern": "HTTP 401 on /api/v1/user/profile",
      "count": 3,
      "first_seen": "2026-02-07T14:23:01Z",
      "last_seen": "2026-02-07T14:25:30Z",
      "resolved": true
    },
    {
      "pattern": "AutoLayout constraint conflict in FeedViewController",
      "count": 12,
      "first_seen": "2026-02-07T14:24:00Z",
      "last_seen": "2026-02-07T14:27:55Z",
      "resolved": false
    }
  ]
}
```

**How the summary is generated:**
This does NOT require calling an LLM. We generate it programmatically:
1. Count entries by level
2. Group errors by message pattern (fuzzy dedup)
3. Detect sequences (e.g., repeated errors then success = "resolved")
4. Identify the most common warning patterns
5. Note any crash reports in the window
6. Summarize app lifecycle events (launch, backgrounding, foregrounding)
7. Compose into natural language using templates

If we want to get fancier later, we can optionally pass the structured data to a local LLM for more nuanced summaries, but template-based is the right starting point.

### 4.5 Errors & Crashes

```
GET /api/v1/logs/errors
```

Returns only error-level entries and crash reports. Designed for the common AI agent pattern of "did anything go wrong?"

**Query parameters:**
| Parameter     | Type     | Description                                      |
|---------------|----------|--------------------------------------------------|
| `since`       | ISO8601  | Entries after this timestamp                     |
| `include_crashes` | boolean | Include parsed crash reports (default: true) |
| `limit`       | integer  | Max results (default: 50)                        |

### 4.6 Build Results

```
GET /api/v1/builds/latest
```

Returns the most recent build output, parsed into structured results.

**Response:**
```json
{
  "build_id": "b1234",
  "started_at": "2026-02-07T14:20:00Z",
  "finished_at": "2026-02-07T14:22:30Z",
  "status": "succeeded",
  "scheme": "MyApp",
  "configuration": "Debug",
  "errors": [],
  "warnings": [
    {
      "file": "FeedViewController.swift",
      "line": 47,
      "column": 12,
      "message": "Result of call to 'fetchItems()' is unused"
    }
  ],
  "tests": {
    "total": 42,
    "passed": 41,
    "failed": 1,
    "failures": [
      {
        "test": "testUserLogin",
        "class": "AuthTests",
        "message": "XCTAssertEqual failed: expected 200, got 401",
        "duration_ms": 1234
      }
    ]
  }
}
```

### 4.7 Source Management

```
GET /api/v1/logs/sources
```

List active log sources and their status.

```json
{
  "sources": [
    {
      "id": "syslog",
      "type": "idevicesyslog",
      "status": "streaming",
      "device": "iPhone 15 Pro (UDID: abc123...)",
      "entries_captured": 4523,
      "started_at": "2026-02-07T14:00:00Z"
    },
    {
      "id": "oslog",
      "type": "log_stream",
      "status": "streaming",
      "filter": "subsystem == 'com.myapp.*'",
      "entries_captured": 1247,
      "started_at": "2026-02-07T14:00:00Z"
    },
    {
      "id": "crash_watcher",
      "type": "crash_reporter",
      "status": "watching",
      "last_crash": null
    }
  ]
}
```

```
POST /api/v1/logs/filter
```

Dynamically reconfigure what gets captured without restarting.

```json
{
  "source": "syslog",
  "process": "MyApp",
  "exclude_patterns": ["CoreLocation", "AVFoundation"]
}
```

---

## 5. MCP Server (Thin Wrapper)

Following the pattern validated by MobAI, the MCP server is a minimal stdio-transport adapter. It translates MCP tool calls into HTTP requests to the log server.

### Available MCP Tools

| Tool Name              | Description                                      | Maps To                    |
|------------------------|--------------------------------------------------|----------------------------|
| `tail_logs`            | Stream recent logs (returns last N + subscribes) | `GET /logs/stream`         |
| `query_logs`           | Search historical logs                           | `GET /logs/query`          |
| `get_log_summary`      | Get LLM-optimized digest                         | `GET /logs/summary`        |
| `get_errors`           | Get recent errors and crashes                    | `GET /logs/errors`         |
| `get_build_result`     | Get latest build output                          | `GET /builds/latest`       |
| `get_latest_crash`     | Get most recent crash report                     | `GET /crashes/latest`      |
| `set_log_filter`       | Update capture filters                           | `POST /logs/filter`        |
| `list_log_sources`     | Show active log sources                          | `GET /logs/sources`        |

### MCP Resources (Context Documents)

| Resource URI                    | Description                                  |
|---------------------------------|----------------------------------------------|
| `logserver://guide`             | How to use the log tools effectively         |
| `logserver://troubleshooting`   | Common iOS error patterns and what they mean |

---

## 6. Technology Choices

### Log Server Runtime

**Recommendation: Python (FastAPI + uvicorn)**

Rationale:
- FastAPI has native SSE support, async subprocess management, and auto-generated OpenAPI docs
- Same language as mitmproxy (Phase 2), enabling tight integration later
- asyncio subprocess handling is clean for managing idevicesyslog/log stream child processes
- Rich ecosystem for text parsing and pattern matching

Alternative considered: Node.js (good for SSE, but Python is better for Phase 2 alignment). Go (fast but overkill for a local-only server, and less flexible for rapid iteration).

### MCP Server Runtime

**Recommendation: TypeScript/Node.js**

Rationale:
- MCP SDK is best-supported in TypeScript
- `npx` distribution (zero-install) following the pattern MobAI validated
- Only ~100 lines of code needed — it's just HTTP fetch calls

### Storage

**Recommendation: In-memory ring buffer + optional SQLite**

- Default: Ring buffer of 10,000 entries in memory (fast, no dependencies)
- Optional: SQLite via `aiosqlite` for persistent cross-session history
- Crash reports always written to disk (they're rare and high-value)

### Dependencies

| Dependency        | Purpose                        | Install                     |
|-------------------|--------------------------------|-----------------------------|
| libimobiledevice  | `idevicesyslog`, device comms  | `brew install libimobiledevice` |
| Python 3.11+      | Log server runtime             | System or pyenv             |
| FastAPI + uvicorn  | HTTP server                    | `pip install fastapi uvicorn` |
| Node.js 18+       | MCP server                     | System or nvm               |
| xcbeautify        | Build output parsing           | `brew install xcbeautify`   |

---

## 7. Project Structure

```
ios-debug-server/
├── README.md
├── LICENSE                          # MIT
├── pyproject.toml                   # Python project config
│
├── server/                          # Python log server
│   ├── __init__.py
│   ├── main.py                      # FastAPI app, entry point
│   ├── config.py                    # Server configuration
│   │
│   ├── sources/                     # Log source adapters
│   │   ├── __init__.py
│   │   ├── base.py                  # Abstract source adapter
│   │   ├── syslog.py                # idevicesyslog adapter
│   │   ├── oslog.py                 # log stream adapter
│   │   ├── crash.py                 # Crash report watcher
│   │   ├── build.py                 # xcodebuild output parser
│   │   └── app_drain.py             # WebSocket receiver for app-embedded logs
│   │
│   ├── processing/                  # Log processing pipeline
│   │   ├── __init__.py
│   │   ├── parser.py                # Raw line → structured record
│   │   ├── classifier.py            # Level classification & noise detection
│   │   ├── deduplicator.py          # Suppress repeated messages
│   │   ├── correlator.py            # Group related entries
│   │   └── summarizer.py            # Generate LLM-optimized summaries
│   │
│   ├── storage/                     # Log storage
│   │   ├── __init__.py
│   │   ├── ring_buffer.py           # In-memory ring buffer
│   │   └── sqlite_store.py          # Optional persistent storage
│   │
│   └── api/                         # API route handlers
│       ├── __init__.py
│       ├── logs.py                  # /logs/* endpoints
│       ├── builds.py                # /builds/* endpoints
│       ├── crashes.py               # /crashes/* endpoints
│       └── sources.py               # /sources/* endpoints
│
├── mcp/                             # MCP server (TypeScript)
│   ├── package.json
│   ├── tsconfig.json
│   └── src/
│       └── index.ts                 # Thin MCP-to-HTTP adapter
│
├── swift-package/                   # Optional app-embedded log drain
│   ├── Package.swift
│   └── Sources/
│       └── DebugLogDrain/
│           └── DebugLogDrain.swift
│
└── tests/
    ├── test_parser.py
    ├── test_summarizer.py
    ├── test_ring_buffer.py
    └── fixtures/                    # Sample log data for testing
        ├── syslog_sample.txt
        ├── oslog_sample.json
        └── crash_sample.ips
```

---

## 8. Implementation Phases

### Phase 1a: Minimum Viable Log Server (Week 1-2)

Goal: Stream idevicesyslog output through an HTTP API.

1. Implement the `syslog` source adapter (spawn `idevicesyslog -p MyApp`, parse stdout)
2. Implement the ring buffer store
3. Build the FastAPI server with `/logs/stream` (SSE) and `/logs/query` endpoints
4. Add `/health` endpoint
5. Basic CLI to start/stop the server

**Milestone:** An AI agent can `curl` the SSE endpoint and see live device logs.

### Phase 1b: Structured Logging & Summaries (Week 3)

Goal: Add OSLog support and the summary engine.

1. Implement the `oslog` source adapter (`log stream --style json`)
2. Build the processing pipeline (parser, classifier, deduplicator)
3. Implement the `/logs/summary` endpoint with template-based generation
4. Add the `/logs/errors` convenience endpoint

**Milestone:** An AI agent can ask "what went wrong in the last 5 minutes?" and get a useful answer.

### Phase 1c: MCP Wrapper & Build Integration (Week 4)

Goal: Full MCP integration and build output capture.

1. Build the TypeScript MCP server
2. Implement the `build` source adapter (xcodebuild output parsing)
3. Add crash report watching
4. Publish MCP server as npm package
5. Write the guide/troubleshooting MCP resources

**Milestone:** Claude Code can use MCP tools to query logs, check build results, and read crash reports.

### Phase 1d: Polish & App Drain (Week 5+)

Goal: Optional enhancements.

1. Build the Swift package for app-embedded log drain
2. Add SQLite persistent storage option
3. Log correlation engine (group entries by request ID / trace ID)
4. Web dashboard for visual log browsing (nice to have, not critical)

---

## 9. Design Decisions (Resolved)

1. **Linux support:** macOS-only at launch. `log stream` (OSLog) is macOS-exclusive and that's fine for now. However, `idevicesyslog` works on Linux, so we'll keep the source adapter abstraction clean enough that Linux support via syslog-only mode can be added later without refactoring. No Linux-hostile patterns (e.g., no hardcoded macOS paths without fallbacks).

2. **Multi-device:** Single-device first. However, the internal architecture will use a `device_id` parameter throughout — source adapters, storage, and API routes will all accept a device identifier even though we only support one initially. When multi-device lands, it's additive rather than a rewrite. The API contract will include `device_id` in responses from day one.

3. **Log persistence:** Ephemeral ring buffer as the default. SQLite persistence is a future enhancement. Crash reports are the exception — they're always written to disk since they're rare and high-value. The storage layer uses an abstract interface so swapping in SQLite later is a clean substitution.

4. **"What's new" mode:** Yes. The `/api/v1/logs/summary` endpoint will support a `since_cursor` parameter. Each summary response includes a `cursor` value (opaque, timestamp-based). Passing `since_cursor` on the next request returns only a summary of entries that arrived after that point. This enables the AI pattern: "check logs" → get summary + cursor → do some work → "what's new?" with cursor → get delta summary. This is significantly more token-efficient than re-summarizing the full window each time.

5. **API key:** Yes. The server will generate a random API key on first start and write it to `~/.ios-debug-server/api-key`. All endpoints require `Authorization: Bearer <key>` or `X-API-Key: <key>` header. The MCP server reads the same file automatically. This prevents accidental interference from other tools hitting localhost:9100 but doesn't pretend to be real security (it's localhost-only anyway). The key can be regenerated via CLI command.
