# Getting Started

## Install

```bash
git clone https://github.com/ArctiaN/quern.git
cd quern
./quern setup
```

Setup checks your environment and offers to install what's missing: Python dependencies, idb (simulator UI control), pymobiledevice3 (physical device support), mitmproxy (network interception), and Node.js (MCP wrapper). It prompts before installing anything.

### For Physical iOS Devices

If you'll be working with physical iPhones or iPads on iOS 17+, you'll also need the tunneld daemon:

```bash
./quern tunneld install    # Requires sudo — installs a LaunchDaemon
```

This creates persistent tunnels to connected iOS devices. Without it, physical device features on iOS 17+ won't work. See [Device Pool](device-pool.md) for why this exists.

### For Android

Install Android Studio (or the standalone SDK tools). Quern finds `adb` and `emulator` automatically from your PATH, `ANDROID_HOME`, or the standard SDK install locations. No additional setup needed.

## Start the Server

```bash
./quern start          # Runs in the background
./quern start -f       # Foreground mode (useful for troubleshooting the server itself)
```

On startup, Quern finds available ports, checks which tools are installed, and starts its log/proxy/crash adapters. It writes everything to `~/.quern/state.json`, which is how the MCP wrapper and CLI commands discover the server.

## Connect Your AI Agent

### Claude Code

```bash
./quern mcp-install
```

This registers Quern's MCP server in Claude Code's config. It'll be available on your next Claude Code session.

### Other MCP Clients

Point your client at: `node /path/to/quern/mcp/dist/index.js`

### Direct HTTP (for custom tooling)

Everything the MCP tools do is available via HTTP on the port shown in `./quern status`. The API key is at `~/.quern/api-key`.

## Server Lifecycle

```bash
./quern status         # Is it running? What port? What tools are available?
./quern stop           # Graceful shutdown
./quern restart        # Stop + start
```

The server runs as a background daemon. It survives terminal closure and persists until you explicitly stop it or reboot. If you change Quern's source code, restart the server to pick up changes.

## Configuration

Optional config at `~/.quern/config.json`:

```json
{
  "default_device_family": "iPhone",
  "local_capture": ["MobileSafari", "com.apple.WebKit.Networking"]
}
```

- **default_device_family**: When your agent asks for a device without specifying what kind, default to this. Usually "iPhone".
- **local_capture**: Process names for transparent network capture. Safari and WebKit networking are a good default — your agent can add your app's process name when it starts a debugging session.

## Verify It Works

After starting the server, run `./quern status` to confirm it's up. Then in your AI agent, try something like:

> "List my available devices"

or

> "Take a screenshot of the simulator"

If you see devices and get a screenshot, you're good to go.

## Where Things Live

| Path | What |
|---|---|
| `~/.quern/state.json` | How everything finds the server (ports, PID) |
| `~/.quern/config.json` | Your configuration (optional) |
| `~/.quern/server.log` | Server logs (daemon mode) |
| `~/.quern/api-key` | Authentication token |
| `~/.quern/device-pool.json` | Known devices and their state |
| `~/.quern/cert-state.json` | Proxy certificate installation state |
| `~/.quern/app-states/` | Saved app state checkpoints |
| `~/.quern/crashes/` | Pulled crash reports |
| `~/.quern/wda/` | WDA build cache and runner logs |

## Next Steps

- [Device Pool & Resolution](device-pool.md) — Understand how device selection works
- [Simulator Proxy Setup](ios-proxy-simulators.md) — Start capturing network traffic
- [Logging Best Practices](ios-logging.md) — Get useful logs from your app
