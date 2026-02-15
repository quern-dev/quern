# Quern Debug Server

A local server that captures iOS simulator logs, network traffic, crash reports, and build output — then exposes everything through an HTTP API and MCP tools designed for AI coding agents.

```
iOS Simulator
    │
Quern Debug Server (localhost:9100)
    ├── Log capture (syslog, oslog, crash reports, build output)
    ├── Network proxy (mitmproxy — intercept, mock, replay)
    ├── Device control (boot, screenshot, tap, swipe, type)
    │
    ├── HTTP API ──→ Any tool or script
    └── MCP tools ──→ Claude Code, Cursor, etc.
```

## Quick Start

### Prerequisites

- macOS with Xcode installed
- Python 3.11+
- Node.js 18+ (for MCP server)
- Optional: `idb` for UI automation (`brew install idb-companion`)
- Optional: `libimobiledevice` for physical device logs (`brew install libimobiledevice`)

### Install

```bash
git clone <repo-url>
cd quern
python3 -m server setup      # creates venv, installs deps, checks tools
cd mcp && npm install && npm run build && cd ..
```

That's it — `setup` creates the `.venv`, installs everything into it, and verifies system dependencies (Xcode, idb, mitmproxy, etc.). All commands use `python3 -m server` which auto-detects the venv.

### Run

```bash
# Start as a background daemon
python3 -m server start

# Or run in the foreground (Ctrl-C to stop)
python3 -m server start -f

# Check status
python3 -m server status

# Stop
python3 -m server stop
```

The server prints connection info on startup — URL, API key, and proxy port. All state is stored in `~/.quern/`:

| File | Purpose |
|------|---------|
| `state.json` | Running instance info (port, PID, API key) |
| `api-key` | Persistent API key |
| `server.log` | Daemon log output |

### Connect via MCP

Add to your MCP client config (e.g. Claude Code `~/.claude.json`):

```json
{
  "mcpServers": {
    "quern-debug": {
      "command": "node",
      "args": ["/path/to/quern-debug-server/mcp/dist/index.js"]
    }
  }
}
```

The MCP server auto-discovers the running server via `state.json` — no URL or API key configuration needed.

**Tip:** Call the `ensure_server` MCP tool first. It starts the server if it isn't running and returns connection info.

### Use the HTTP API

```bash
API_KEY=$(cat ~/.quern/api-key)

# Health check (no auth)
curl http://localhost:9100/health

# Tail recent logs
curl -H "Authorization: Bearer $API_KEY" \
     "http://localhost:9100/api/v1/logs/query?limit=20&level=error"

# Get an LLM-optimized summary
curl -H "Authorization: Bearer $API_KEY" \
     "http://localhost:9100/api/v1/logs/summary?window=5m"
```

## Features

### Log Capture

Captures from multiple sources simultaneously, deduplicates, and stores in a ring buffer (10,000 entries).

| Source | Tool | What it captures |
|--------|------|------------------|
| Device syslog | `idevicesyslog` | System and app log messages |
| macOS unified log | `log stream` | Structured OS log entries |
| Crash reports | `idevicecrashreport` | Parsed crash reports with stack traces |
| Build output | `xcodebuild` | Errors, warnings, test results |

### Network Proxy

Spawns `mitmdump` as a subprocess to capture HTTP/HTTPS traffic (port 9101 by default).

- **Query flows** — filter by host, method, status code, path
- **Inspect details** — full headers and bodies for any captured request
- **Intercept** — pause matching requests, inspect, modify, release
- **Mock** — return synthetic responses without hitting the real server
- **Replay** — re-send a previously captured request
- **LLM summaries** — traffic digests grouped by host with error highlights

### Device Control

Manage iOS simulators and interact with running apps.

- **Device management** — list, boot, shutdown simulators
- **App management** — install, launch, terminate, list apps
- **Screenshots** — capture with configurable scale and format
- **UI inspection** — accessibility tree, screen summaries
- **Interaction** — tap (by element label or coordinates), swipe, type text, press hardware buttons
- **Configuration** — set GPS location, grant permissions

Device management and screenshots use `xcrun simctl` (always available with Xcode). UI automation requires [idb](https://fbidb.io/).

### Process Lifecycle

Startup is idempotent — running `start` when a server is already running is a no-op. Port conflicts are handled automatically by scanning upward.

```bash
python3 -m server start          # Daemonize
python3 -m server start -f       # Foreground
python3 -m server stop            # Graceful shutdown
python3 -m server restart         # Stop + start
python3 -m server status          # Show PID, URL, uptime
python3 -m server regenerate-key  # New API key
```

`~/.quern/state.json` is the single source of truth for discovering a running instance.

## MCP Tools

23 tools available via MCP:

| Category | Tools |
|----------|-------|
| Server | `ensure_server` |
| Logs | `tail_logs`, `query_logs`, `get_log_summary`, `get_errors`, `get_build_result`, `get_latest_crash`, `set_log_filter`, `list_log_sources` |
| Network | `query_flows`, `get_flow_detail`, `get_flow_summary`, `proxy_status`, `start_proxy`, `stop_proxy`, `proxy_setup_guide` |
| Intercept & Mock | `set_intercept`, `clear_intercept`, `list_held_flows`, `release_flow`, `replay_flow`, `set_mock`, `list_mocks`, `clear_mocks` |
| Device | `list_devices`, `boot_device`, `shutdown_device`, `install_app`, `launch_app`, `terminate_app`, `list_apps` |
| UI | `take_screenshot`, `get_ui_tree`, `get_screen_summary`, `tap`, `tap_element`, `swipe`, `type_text`, `press_button` |
| Config | `set_location`, `grant_permission` |

## API Endpoints

All endpoints require `Authorization: Bearer <key>` except `/health`.

### Logs

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| GET | `/api/v1/logs/query` | Query logs with filters and pagination |
| GET | `/api/v1/logs/stream` | SSE real-time log stream |
| GET | `/api/v1/logs/summary` | LLM-optimized summary with cursor support |
| GET | `/api/v1/logs/errors` | Errors and crashes only |
| GET | `/api/v1/logs/sources` | Active log source adapters |
| POST | `/api/v1/logs/filter` | Reconfigure capture filters |
| GET | `/api/v1/crashes/latest` | Recent parsed crash reports |
| GET | `/api/v1/builds/latest` | Most recent build result |
| POST | `/api/v1/builds/parse` | Submit xcodebuild output |

### Network Proxy

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/proxy/status` | Proxy status and config |
| POST | `/api/v1/proxy/start` | Start the proxy |
| POST | `/api/v1/proxy/stop` | Stop the proxy |
| GET | `/api/v1/proxy/flows` | Query captured flows |
| GET | `/api/v1/proxy/flows/{id}` | Full flow detail |
| GET | `/api/v1/proxy/flows/summary` | Traffic digest |
| POST | `/api/v1/proxy/intercept` | Set intercept pattern |
| DELETE | `/api/v1/proxy/intercept` | Clear intercept |
| GET | `/api/v1/proxy/intercept/held` | List held flows |
| POST | `/api/v1/proxy/intercept/release` | Release a held flow |
| POST | `/api/v1/proxy/replay/{id}` | Replay a captured flow |
| POST | `/api/v1/proxy/mocks` | Add mock rule |
| GET | `/api/v1/proxy/mocks` | List mock rules |
| DELETE | `/api/v1/proxy/mocks` | Clear mock rules |
| GET | `/api/v1/proxy/setup-guide` | Device setup instructions |
| GET | `/api/v1/proxy/cert` | Download CA certificate |

### Device Control

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/device/list` | List simulators |
| POST | `/api/v1/device/boot` | Boot simulator |
| POST | `/api/v1/device/shutdown` | Shutdown simulator |
| POST | `/api/v1/device/app/install` | Install app |
| POST | `/api/v1/device/app/launch` | Launch app |
| POST | `/api/v1/device/app/terminate` | Terminate app |
| GET | `/api/v1/device/app/list` | List installed apps |
| GET | `/api/v1/device/screenshot` | Capture screenshot |
| GET | `/api/v1/device/ui` | Accessibility tree |
| GET | `/api/v1/device/screen-summary` | LLM-optimized screen description |
| POST | `/api/v1/device/ui/tap` | Tap at coordinates |
| POST | `/api/v1/device/ui/tap-element` | Tap element by label/identifier |
| POST | `/api/v1/device/ui/swipe` | Swipe gesture |
| POST | `/api/v1/device/ui/type` | Type text |
| POST | `/api/v1/device/ui/press` | Press hardware button |
| POST | `/api/v1/device/location` | Set GPS location |
| POST | `/api/v1/device/permission` | Grant app permission |

## Architecture

```
server/
  main.py              Entry point, CLI, FastAPI app
  config.py            API key management
  lifecycle/           Daemon, state.json, port scanning, watchdog
  sources/             Log source adapters (syslog, oslog, crash, build, proxy)
  processing/          Deduplicator, classifier, summarizer
  storage/             Ring buffer
  proxy/               mitmproxy addon and flow store
  device/              Simulator control (simctl, idb backends)
  api/                 HTTP route handlers
mcp/                   MCP server (TypeScript)
tests/                 594 tests
```

## Development

```bash
# Run tests (venv auto-detected)
.venv/bin/pytest tests/ -v

# Build MCP server
cd mcp && npm run build

# Run with debug logging
python3 -m server start -f --verbose
```

## License

MIT with Commons Clause — free to use and modify, but not for commercial resale. See [LICENSE](LICENSE).
