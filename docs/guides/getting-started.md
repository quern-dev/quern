# Getting Started

Installing Quern, understanding how it runs, and getting your first debug session going.

## Install

```bash
git clone https://github.com/ArctiaN/quern.git
cd quern
./quern setup
```

`setup` checks your environment and installs what's missing:

- **Python 3.11+** in a virtual environment
- **Xcode Command Line Tools** (for simctl, devicectl)
- **idb** + **idb-companion** (Facebook's iOS Development Bridge, for simulator UI)
- **pymobiledevice3** (for physical device screenshots, logs, tunneling)
- **mitmproxy** (for network interception)
- **Node.js 18+** (for the MCP wrapper)

It'll prompt before installing anything. If a tool is already installed, it skips it.

### For Physical iOS Devices

If you plan to work with physical iPhones/iPads on iOS 17+:

```bash
./quern tunneld install    # Installs the tunneld LaunchDaemon (requires sudo)
```

This sets up the persistent tunnel daemon that pymobiledevice3 needs for iOS 17+ device communication. See [Device Pool](device-pool.md) for details on why this exists.

### For Android

Install Android Studio (or just the Android SDK command-line tools). Quern finds `adb` and `emulator` automatically from your PATH, `ANDROID_HOME`, or the standard SDK locations. See [Android Getting Started](android-getting-started.md) for details.

## Start the Server

```bash
./quern start          # Daemon mode (background)
./quern start -f       # Foreground mode (see logs in terminal)
```

Foreground mode is useful when debugging the server itself. Daemon mode is for normal use — it runs in the background and survives terminal closure.

### What Happens on Start

1. **Port resolution**: Finds available ports for the HTTP API (default 9100) and proxy (default 9101). If occupied, scans upward.
2. **MCP build**: Rebuilds the TypeScript MCP wrapper if sources changed.
3. **Tool discovery**: Checks which tools are available (simctl, idb, adb, pymobiledevice3, mitmproxy) and logs the results.
4. **Adapter startup**: Starts log adapters, proxy, crash watcher based on configuration.
5. **State file**: Writes `~/.quern/state.json` with ports, PID, and configuration. This is how everything discovers the server.

### Startup Flags

```
--host HOST           Bind host (default: 0.0.0.0)
--port PORT           HTTP API port (default: 9100)
--proxy-port PORT     Proxy listen port (default: 9101)
-p, --process NAME    Filter logs to a specific process
--buffer-size N       Ring buffer size (default: 10,000 entries)
--no-proxy            Disable mitmproxy
--no-crash            Disable crash watcher
--on-crash CMD        Shell command to run on crash (JSON piped to stdin)
-v, --verbose         Debug logging
```

## Connect an AI Agent

### Claude Code

```bash
./quern mcp-install
```

This registers Quern's MCP server in `~/.claude.json`. Claude Code will discover it automatically on next launch.

### Other MCP Clients

Point your MCP client at:
```
node /path/to/quern/mcp/dist/index.js
```

The MCP wrapper reads `~/.quern/state.json` to find the server's port and API key.

### Direct HTTP

Everything the MCP tools do is available via HTTP:

```bash
API_KEY=$(cat ~/.quern/api-key)
curl -H "Authorization: Bearer $API_KEY" http://localhost:9100/api/v1/device/list
```

## Server Lifecycle

```bash
./quern status         # Is it running? What port? What tools are available?
./quern stop           # Graceful shutdown (restores system proxy if configured)
./quern restart        # Stop + start
```

### State File

`~/.quern/state.json` is the single source of truth. All consumers — CLI, MCP, scripts — discover the server through it:

```json
{
  "pid": 12345,
  "server_port": 9100,
  "proxy_port": 9101,
  "local_ip": "192.168.1.100",
  "api_key": "...",
  "proxy_enabled": true,
  "started_at": "2026-03-09T14:30:00Z"
}
```

Never hardcode ports. Always read from the state file.

### API Key

Auto-generated on first run, stored at `~/.quern/api-key` with owner-only permissions. Required on every HTTP request as `Authorization: Bearer <key>`. Regenerate with:

```bash
./quern regenerate-key
```

## Configuration

Optional config at `~/.quern/config.json`:

```json
{
  "default_device_family": "iPhone",
  "local_capture": ["MobileSafari", "com.apple.WebKit.Networking"]
}
```

- **default_device_family**: When `resolve_device` is called without specifying a family, default to this. Usually "iPhone".
- **local_capture**: Process names for transparent proxy capture via mitmproxy's System Extension. Set this to avoid configuring the system proxy.

## Verify It Works

After starting the server, the quickest verification:

```bash
./quern status
```

Should show the server running with tool availability. Then from your AI agent:

```
list_devices            # See available simulators/devices
resolve_device          # Get the default device
take_screenshot         # Capture the screen
```

If you see devices and get a screenshot, you're good.

## File Locations

| Path | What |
|---|---|
| `~/.quern/state.json` | Server discovery (ports, PID, API key) |
| `~/.quern/api-key` | Authentication token |
| `~/.quern/config.json` | Optional user configuration |
| `~/.quern/server.log` | Server logs (daemon mode) |
| `~/.quern/device-pool.json` | Device pool state |
| `~/.quern/cert-state.json` | Proxy certificate installation state |
| `~/.quern/app-states/` | Saved app state checkpoints |
| `~/.quern/crashes/` | Pulled crash reports |
| `~/.quern/wda/` | WDA build cache and runner logs |
| `~/.quern/bin/` | Compiled tools (preview app) |

## Next Steps

- [Device Pool & Resolution](device-pool.md) — How device selection works
- [Simulator Proxy Setup](ios-proxy-simulators.md) — Capture network traffic
- [Logging Best Practices](ios-logging.md) — Get useful logs from your app
- [Network Debugging Patterns](network-debugging.md) — Mock, intercept, replay
