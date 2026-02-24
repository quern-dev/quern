# Quern

**Eyes, ears, and hands for AI-assisted mobile development.**

Quern is a local debug server that lets AI coding agents — Claude Code, Cursor, Windsurf, and others — actually *see* what your app is doing. Logs, network traffic, crash reports, screenshots, UI state: instead of guessing from stack traces and stale error messages, your agent gets live, structured access to everything happening on the device.

No cloud. No telemetry. Just a daemon on your Mac that bridges the gap between "build succeeded" and "it actually works."

<!-- TODO: Hero image — screenshot of an agent session using Quern to diagnose a bug
     (e.g. agent reads a 500 response body via network proxy and identifies the issue)
     ![Agent session with Quern](docs/images/agent-session.png)
-->

> Supports iOS Simulators and physical iOS devices (via WebDriverAgent). Android emulator and device support is on the roadmap.

```
Simulator / Device
    │
Quern (localhost:9100)
    ├── Log capture (device, simulator, crash reports, build output)
    ├── Network proxy (intercept, mock, replay HTTP traffic)
    ├── Device control (boot, screenshot, tap, swipe, type)
    │
    ├── HTTP API ──→ Any tool or script
    └── MCP tools ──→ Claude Code, Cursor, etc.
```

## Why

AI agents are good at writing code. They're bad at knowing whether it worked. A build error gets caught; a silent API failure, a wrong screen, or a crash on launch usually doesn't — unless you paste logs back into the chat yourself.

Quern closes that loop. It gives agents direct, token-efficient access to everything they need to diagnose and fix problems autonomously: structured logs, network request/response pairs, parsed crash reports, screenshots, and the ability to interact with the running app.

Quern isn't a cloud testing platform. It's local infrastructure that makes the AI tools you already use actually effective at debugging.

- **Local-first** — No cloud, no accounts, no third-party API keys. Your code and logs never leave your machine.
- **Works with your existing AI** — Not a replacement for Claude, Cursor, or Codex. It makes them better by giving them live access to what your app is actually doing.
- **Built for your editor and CLI** — Designed for agents already in your workflow, not a separate QA portal.
- **Great for QA and SDET workflows** — Pair with an agent during manual testing. Let it intercept network calls, mock error responses, or verify analytics payloads while you drive the app — no more juggling Charles Proxy and a terminal.
- **Free and open source** — Apache 2.0 licensed. Run it forever on your own hardware.

<!-- TODO: Terminal recording (asciinema gif) — quern setup, quern start, then a short
     agent interaction showing log tailing or network inspection
     ![Quern in action](docs/images/demo.gif)
-->

## How to Use Quern

Once the server is installed and the MCP is registered, open your mobile project with your AI coding assistant (Claude Code, Cursor, etc.) and just ask it to do things:

> *"Boot an iPhone 16 simulator, make sure the proxy is capturing traffic, then build and install my app. Log in with testuser / password123 and give me a summary of all the API calls you see during login."*

The agent will use Quern's MCP tools to boot the simulator, configure the proxy, install your app, drive the UI to log in, and then query the captured network traffic — all without you touching the simulator or pasting logs into chat.

Other things you can ask:

- *"Take a screenshot and tell me what's on screen"*
- *"Mock the /api/users endpoint to return a 500 error and see how the app handles it"*
- *"Find the last crash report and figure out what caused it"*
- *"Set up the proxy on my physical iPhone and capture traffic while I browse"*
- *"Show me what analytics events get sent when I open the settings screen"*
- *"Run the app on 3 simulators in parallel and compare the network traffic"*

## Quick Start

### Prerequisites

- macOS with Xcode installed
- Python 3.11+
- Node.js 18+ (for MCP server)
- Optional: `idb` for UI automation (`brew install idb-companion`)
- Optional: `pymobiledevice3` for physical device support (`pipx install pymobiledevice3`)
- Optional: [mitmproxy-macos](https://github.com/mitmproxy/mitmproxy-macos) for local capture mode (transparent simulator traffic capture without system proxy)

### Install

```bash
git clone <repo-url>
cd quern
./quern setup                # creates venv, installs deps, checks tools, adds quern to ~/.local/bin
./quern mcp-install          # builds MCP server, adds to ~/.claude.json
```

That's it — `setup` creates the `.venv`, installs everything into it, and verifies system dependencies. `mcp-install` builds the TypeScript MCP server and registers it with Claude Code. Setup will offer to add `~/.local/bin` to your PATH, allowing you to use `quern <command>` from anywhere instead of `./quern` from the project folder.

### Run

```bash
quern start                # start as a background daemon
quern start -f             # run in the foreground (Ctrl-C to stop)
quern status               # check status
quern stop                 # stop
quern update               # pull latest changes and rebuild
```

The server prints connection info on startup — URL, API key, and proxy port. All state is stored in `~/.quern/`:

| File | Purpose |
|------|---------|
| `state.json` | Running instance info (port, PID, API key) — deleted on stop |
| `cert-state.json` | Per-device certificate installation state — persists across restarts |
| `device-pool.json` | Device pool claim/release state — persists across restarts |
| `config.json` | Local capture settings and other configuration |
| `api-key` | Persistent API key |
| `server.log` | Daemon log output |

### Connect via MCP

```bash
quern mcp-install           # adds quern-debug to ~/.claude.json
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

## What It Does

### Log Capture

Captures from multiple sources simultaneously, deduplicates, and stores in a ring buffer (10,000 entries).

| Source | Tool | What it captures | Mode |
|--------|------|-------------------|------|
| Physical device logs | `pymobiledevice3 syslog` | os_log, Logger, NSLog from physical devices | On-demand (`start_device_logging`) |
| Simulator logs | `simctl log stream` | os_log, Logger, NSLog from simulators | On-demand (`start_simulator_logging`) |
| Crash reports | `idevicecrashreport` | Parsed crash reports with stack traces | Always on |
| Build output | `xcodebuild` | Errors, warnings, test results | Always on |
| Device syslog (legacy) | `idevicesyslog` | Unfiltered system + app log messages | Opt-in (`--syslog`) |
| macOS unified log | `log stream` | Structured OS log entries | Opt-in (`--oslog`) |

### Network Proxy

Spawns `mitmdump` as a subprocess to capture HTTP/HTTPS traffic (port 9101 by default).

- **Query flows** — filter by host, method, status code, path, or simulator UDID
- **Inspect details** — full headers and bodies for any captured request
- **Intercept** — pause matching requests, inspect, modify, release
- **Mock** — return synthetic responses without hitting the real server
- **Replay** — re-send a previously captured request
- **Local capture** — transparently capture simulator traffic per-process via mitmproxy's macOS System Extension, without configuring a system proxy. Each flow is tagged with the originating simulator's UDID for per-simulator filtering
- **System proxy** — auto-configures macOS network settings to route traffic through the proxy (for physical devices or non-simulator traffic)
- **Certificate management** — check, install, and verify mitmproxy CA certificates
- **LLM summaries** — traffic digests grouped by host with error highlights

**Proxy setup for simulators:**

| Mode | Setup | Pros | Cons |
|------|-------|------|------|
| Local capture (recommended) | `quern enable-local-capture` + approve macOS System Extension | Zero config per-simulator, per-simulator flow tagging, no system proxy needed | Requires [mitmproxy-macos](https://github.com/mitmproxy/mitmproxy-macos) and one-time macOS permission approval |
| System proxy | `configure_system_proxy` / `unconfigure_system_proxy` | No extra software | Affects all Mac traffic, must remember to unconfigure when done |

Local capture requires approving the **Mitmproxy Redirector** system extension in **System Settings > Privacy & Security** on first use.

### Device Control

Manage iOS simulators and physical devices, and interact with running apps.

- **Device management** — list, boot, shutdown simulators; discover physical devices
- **App management** — install, launch, terminate, uninstall, list apps
- **Screenshots** — capture with configurable scale and format, annotated screenshots with accessibility overlays
<!-- TODO: Annotated screenshot example — show a real app with the accessibility overlay
     ![Annotated screenshot](docs/images/annotated-screenshot.png)
-->
- **UI inspection** — accessibility tree, element state queries, wait-for-element polling, screen summaries
- **Interaction** — tap (by element label or coordinates), swipe, type text, clear text, press hardware buttons
- **Configuration** — set GPS location, grant permissions
- **Device pool** — claim/release devices for parallel test execution

**Simulator UI automation** uses [idb](https://fbidb.io/) (`brew install idb-companion`). Device management and screenshots use `xcrun simctl` (always available with Xcode).

**Physical device UI automation** uses [WebDriverAgent](https://github.com/appium/WebDriverAgent) (WDA), which Quern builds and deploys automatically via `setup_wda`. WDA requires a valid Apple Developer signing identity. Once set up, the WDA driver auto-starts on first interaction and idles out after 15 minutes of inactivity. The app appears on the device as **Quern Driver**.

### Process Lifecycle

Startup is idempotent — running `start` when a server is already running is a no-op. Port conflicts are handled automatically by scanning upward. The MCP server is auto-rebuilt on start when the TypeScript source is newer than the compiled output.

```bash
quern setup                  # Check environment, install deps
quern start                  # Daemonize
quern start -f               # Foreground
quern stop                   # Graceful shutdown
quern restart                # Stop + start
quern status                 # Show PID, URL, uptime
quern update                 # Pull latest changes, reinstall deps, rebuild MCP
quern regenerate-key         # New API key
quern mcp-install            # Register MCP server with Claude Code
quern enable-local-capture   # Enable transparent simulator traffic capture
quern disable-local-capture  # Disable local capture
```

`~/.quern/state.json` is the single source of truth for discovering a running instance.

## MCP Tools

63 tools available via MCP:

| Category | Tools |
|----------|-------|
| Server | `ensure_server` |
| Logs | `tail_logs`, `query_logs`, `get_log_summary`, `get_errors`, `get_build_result`, `parse_build_output`, `get_latest_crash`, `set_log_filter`, `list_log_sources`, `start_simulator_logging`, `stop_simulator_logging`, `start_device_logging`, `stop_device_logging` |
| Network | `query_flows`, `wait_for_flow`, `get_flow_detail`, `get_flow_summary`, `proxy_status`, `start_proxy`, `stop_proxy`, `proxy_setup_guide`, `verify_proxy_setup`, `set_local_capture` |
| System Proxy | `configure_system_proxy`, `unconfigure_system_proxy` |
| Intercept & Mock | `set_intercept`, `clear_intercept`, `list_held_flows`, `release_flow`, `replay_flow`, `set_mock`, `list_mocks`, `clear_mocks` |
| Device | `list_devices`, `boot_device`, `shutdown_device`, `install_app`, `launch_app`, `terminate_app`, `uninstall_app`, `list_apps` |
| UI | `take_screenshot`, `get_ui_tree`, `get_element_state`, `wait_for_element`, `get_screen_summary`, `tap`, `tap_element`, `swipe`, `type_text`, `clear_text`, `press_button` |
| Config | `set_location`, `grant_permission` |
| Device Pool | `list_device_pool`, `claim_device`, `release_device`, `resolve_device`, `ensure_devices` |
| Physical Device | `setup_wda`, `start_driver`, `stop_driver` |

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
| POST | `/api/v1/proxy/configure-system` | Auto-configure macOS system proxy |
| POST | `/api/v1/proxy/unconfigure-system` | Restore original proxy settings |
| POST | `/api/v1/proxy/filter` | Set proxy capture filters |
| GET | `/api/v1/proxy/flows` | Query captured flows |
| GET | `/api/v1/proxy/flows/{id}` | Full flow detail |
| GET | `/api/v1/proxy/flows/summary` | Traffic digest |
| POST | `/api/v1/proxy/intercept` | Set intercept pattern |
| DELETE | `/api/v1/proxy/intercept` | Clear intercept |
| GET | `/api/v1/proxy/intercept/held` | List held flows |
| POST | `/api/v1/proxy/intercept/release` | Release a held flow |
| POST | `/api/v1/proxy/intercept/release-all` | Release all held flows |
| POST | `/api/v1/proxy/replay/{id}` | Replay a captured flow |
| POST | `/api/v1/proxy/mocks` | Add mock rule |
| GET | `/api/v1/proxy/mocks` | List mock rules |
| DELETE | `/api/v1/proxy/mocks/{rule_id}` | Delete a specific mock rule |
| DELETE | `/api/v1/proxy/mocks` | Clear all mock rules |
| GET | `/api/v1/proxy/cert` | Download CA certificate |
| GET | `/api/v1/proxy/cert/status` | Check certificate installation status |
| POST | `/api/v1/proxy/cert/verify` | Verify CA cert installation (defaults to booted simulators) |
| POST | `/api/v1/proxy/cert/install` | Install CA certificate |
| GET | `/api/v1/proxy/setup-guide` | Device setup instructions |
| POST | `/api/v1/proxy/local-capture` | Set local capture process list |

### Device Control

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/device/list` | List simulators |
| POST | `/api/v1/device/boot` | Boot simulator |
| POST | `/api/v1/device/shutdown` | Shutdown simulator |
| POST | `/api/v1/device/app/install` | Install app |
| POST | `/api/v1/device/app/launch` | Launch app |
| POST | `/api/v1/device/app/terminate` | Terminate app |
| POST | `/api/v1/device/app/uninstall` | Uninstall app |
| GET | `/api/v1/device/app/list` | List installed apps |
| GET | `/api/v1/device/screenshot` | Capture screenshot |
| GET | `/api/v1/device/screenshot/annotated` | Screenshot with accessibility overlays |
| GET | `/api/v1/device/ui` | Accessibility tree |
| GET | `/api/v1/device/ui/element` | Query specific element state |
| POST | `/api/v1/device/ui/wait-for-element` | Poll until element appears |
| GET | `/api/v1/device/screen-summary` | LLM-optimized screen description |
| POST | `/api/v1/device/ui/tap` | Tap at coordinates |
| POST | `/api/v1/device/ui/tap-element` | Tap element by label/identifier |
| POST | `/api/v1/device/ui/swipe` | Swipe gesture |
| POST | `/api/v1/device/ui/type` | Type text |
| POST | `/api/v1/device/ui/clear` | Clear text field |
| POST | `/api/v1/device/ui/press` | Press hardware button |
| POST | `/api/v1/device/location` | Set GPS location |
| POST | `/api/v1/device/permission` | Grant app permission |
| POST | `/api/v1/device/logging/start` | Start simulator log capture |
| POST | `/api/v1/device/logging/stop` | Stop simulator log capture |
| POST | `/api/v1/device/logging/device/start` | Start physical device log capture |
| POST | `/api/v1/device/logging/device/stop` | Stop physical device log capture |
| POST | `/api/v1/device/wda/setup` | Build and install WDA on physical device |
| POST | `/api/v1/device/wda/start` | Start WDA driver |
| POST | `/api/v1/device/wda/stop` | Stop WDA driver |

### Device Pool

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/device/pool` | List pool status |
| POST | `/api/v1/device/claim` | Claim a device for exclusive use |
| POST | `/api/v1/device/release` | Release a claimed device |
| POST | `/api/v1/device/cleanup` | Release stale claims |
| POST | `/api/v1/device/refresh` | Refresh pool from simctl |
| POST | `/api/v1/device/resolve` | Resolve a device by requirements |
| POST | `/api/v1/device/ensure` | Ensure devices matching requirements exist |

## Architecture

```
server/
  main.py              Entry point, CLI, FastAPI app
  config.py            API key management
  lifecycle/           Daemon, state.json, port scanning, watchdog, setup, updater
  sources/             Log source adapters (device, simulator, syslog, oslog, crash, build, proxy)
  processing/          Deduplicator, classifier, summarizer
  storage/             Ring buffer
  proxy/               mitmproxy addon, flow store, system proxy, cert management
  device/              Simulator control (simctl, idb) + physical device control (WDA, pymobiledevice3), device pool
  api/                 HTTP route handlers
mcp/                   MCP server (TypeScript)
tests/                 855+ tests
```

## Development

```bash
# Run tests (venv auto-detected)
.venv/bin/pytest tests/ -v

# Build MCP server
cd mcp && npm run build

# Run with debug logging
quern start -f --verbose
```

## License

Apache 2.0. See [LICENSE](LICENSE).
