# iOS Debug Server

An open-source system that captures iOS debug logs and makes them accessible to AI coding agents via HTTP API and MCP (Model Context Protocol).

**Status: Phase 1a — Minimum Viable Log Server (in development)**

## What This Does

iOS Debug Server captures logs from multiple sources, processes them into structured formats, and serves them through a local API that AI agents can consume. Instead of dumping raw console output into an AI's context window, it provides filtered, deduplicated, and summarized log digests designed to be token-efficient.

```
Your iOS Device
    │ USB
    ▼
iOS Debug Server (localhost:9100)
    │
    ├── Real-time SSE stream
    ├── Historical queries
    └── LLM-optimized summaries
    │
    ▼
AI Agent (Claude Code, Cursor, etc.)
```

## Quick Start

### Prerequisites

- macOS (Linux support planned)
- Python 3.11+
- libimobiledevice: `brew install libimobiledevice`
- A USB-connected iOS device

### Install & Run

```bash
# Clone and install
git clone https://github.com/YOUR_ORG/ios-debug-server.git
cd ios-debug-server
pip install -e ".[dev]"

# Start the server (filters to your app's process)
ios-debug-server --process MyApp

# The server prints your API key on startup
```

### Use the API

```bash
# Check health
curl http://localhost:9100/health

# Stream logs in real time (SSE)
curl -H "Authorization: Bearer YOUR_KEY" \
     http://localhost:9100/api/v1/logs/stream

# Query historical logs
curl -H "Authorization: Bearer YOUR_KEY" \
     "http://localhost:9100/api/v1/logs/query?level=error&limit=10"
```

### Connect to an AI Agent (MCP)

```bash
# Coming in Phase 1c
npx ios-debug-mcp
```

## Log Sources

| Source | Tool | Status |
|--------|------|--------|
| Device system log | `idevicesyslog` | ✅ Phase 1a |
| Structured OS log | `log stream --style json` | 🔜 Phase 1b |
| Crash reports | `idevicecrashreport` | 🔜 Phase 1c |
| Build output | `xcodebuild` | 🔜 Phase 1c |
| App-embedded drain | Custom Swift package | 🔜 Phase 1d |

## Roadmap

This is Phase 1 of a three-phase project:

- **Phase 1: Debug Logs** — Capture, process, and serve iOS debug logs (this repo)
- **Phase 2: Network Proxy** — mitmproxy integration for HTTP traffic inspection
- **Phase 3: Device Control** — WebDriverAgent + idb for AI-assisted UI testing

## License

MIT
