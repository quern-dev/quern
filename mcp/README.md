# Quern Debug MCP Server

MCP (Model Context Protocol) server that wraps the Quern Debug Server HTTP API, letting AI agents query iOS device logs, intercept network traffic, control simulators, and more.

## Prerequisites

- The Python Quern Debug Server must be running (`quern start`)
- Node.js 18+

## Usage

### With Claude Desktop

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "quern-debug": {
      "command": "npx",
      "args": ["-y", "quern-debug-mcp"]
    }
  }
}
```

### With Cursor

Add to your MCP settings:

```json
{
  "quern-debug": {
    "command": "npx",
    "args": ["-y", "quern-debug-mcp"]
  }
}
```

### From Source

```bash
cd mcp
npm install
npm run build
node dist/index.js
```

## Configuration

| Environment Variable | Default | Description |
|---|---|---|
| `QUERN_DEBUG_SERVER_URL` | `http://127.0.0.1:9100` | Python server URL |

The server URL and API key are discovered automatically from `~/.quern/state.json` and `~/.quern/api-key`.

## Source Layout

```
src/
├── index.ts              Entrypoint: server creation, resources, main()
├── config.ts             State discovery, API key resolution
├── http.ts               HTTP request helper + health probe
└── tools/
    ├── logs.ts           Log query, summary, errors, builds, crashes
    ├── proxy.ts          Network proxy control + flow queries
    ├── intercept.ts      Intercept, replay, and mock rules
    ├── device.ts         Simulator control + UI interaction
    ├── device-pool.ts    Device pool claim/release/resolve
    ├── simulator-log.ts  Simulator log capture
    └── device-log.ts     Physical device log capture
```

## Tools

### Logs (10 tools)

| Tool | Description |
|---|---|
| `ensure_server` | Start server if needed, return connection info |
| `tail_logs` | Show recent log entries (most recent first) |
| `query_logs` | Full-featured log search with time ranges and text search |
| `get_log_summary` | AI-optimized summary with cursor-based delta polling |
| `get_errors` | Error-level entries and crash reports |
| `get_build_result` | Most recent parsed xcodebuild result |
| `parse_build_output` | Parse an xcodebuild log file into structured results |
| `get_latest_crash` | Recent crash reports with parsed details |
| `set_log_filter` | Reconfigure capture filters |
| `list_log_sources` | List active log source adapters |

### Proxy (11 tools)

| Tool | Description |
|---|---|
| `query_flows` | Query captured HTTP flows with filters |
| `wait_for_flow` | Block until a matching HTTP flow appears |
| `get_flow_detail` | Full request/response detail for a flow |
| `proxy_status` | Check proxy state and configuration |
| `verify_proxy_setup` | Verify mitmproxy CA cert on simulators |
| `start_proxy` | Start mitmproxy network capture |
| `stop_proxy` | Stop mitmproxy and restore system proxy |
| `get_flow_summary` | LLM-optimized traffic summary with cursor polling |
| `proxy_setup_guide` | Device proxy configuration instructions |
| `configure_system_proxy` | Route traffic through mitmproxy |
| `unconfigure_system_proxy` | Restore system proxy to pre-Quern state |

### Intercept & Mock (8 tools)

| Tool | Description |
|---|---|
| `set_intercept` | Hold matching requests for inspection |
| `clear_intercept` | Release all held flows |
| `list_held_flows` | List intercepted flows (supports long-polling) |
| `release_flow` | Release a held flow, optionally modifying it |
| `replay_flow` | Replay a captured flow through the proxy |
| `set_mock` | Add a mock response rule |
| `list_mocks` | List active mock rules |
| `clear_mocks` | Remove mock rules |

### Device Control (20 tools)

| Tool | Description |
|---|---|
| `list_devices` | List simulators and tool availability |
| `boot_device` | Boot a simulator by UDID or name |
| `shutdown_device` | Shutdown a simulator |
| `install_app` | Install .app/.ipa on a simulator |
| `launch_app` | Launch an app by bundle ID |
| `terminate_app` | Terminate a running app |
| `list_apps` | List installed apps |
| `take_screenshot` | Capture simulator screenshot |
| `get_ui_tree` | Full accessibility tree |
| `get_element_state` | Single element state lookup |
| `wait_for_element` | Server-side element polling |
| `get_screen_summary` | LLM-optimized screen description |
| `tap` | Tap at coordinates |
| `tap_element` | Tap element by label/identifier |
| `swipe` | Swipe gesture |
| `type_text` | Type into focused field |
| `clear_text` | Clear focused field |
| `press_button` | Press hardware button |
| `set_location` | Set simulated GPS location |
| `grant_permission` | Grant app permission |

### Device Pool (5 tools)

| Tool | Description |
|---|---|
| `list_device_pool` | List devices with claim status |
| `claim_device` | Claim device for exclusive use |
| `release_device` | Release claimed device |
| `resolve_device` | Smart find + optional claim |
| `ensure_devices` | Boot N devices for parallel testing |

### Simulator Logging (2 tools)

| Tool | Description |
|---|---|
| `start_simulator_logging` | Capture os_log/Logger/NSLog from a simulator |
| `stop_simulator_logging` | Stop simulator log capture |

### Device Logging (2 tools)

| Tool | Description |
|---|---|
| `start_device_logging` | Capture os_log/Logger/NSLog from a physical device |
| `stop_device_logging` | Stop physical device log capture |

## Resources

| URI | Description |
|---|---|
| `quern://guide` | Agent guide: workflows, tool selection, REST API reference |
| `quern://troubleshooting` | iOS error patterns and crash report reading guide |
