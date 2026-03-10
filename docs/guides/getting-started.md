# Getting Started

## Prerequisites

You need two things before installing:

- **Xcode** — Full Xcode from the App Store, not just the Command Line Tools. You need it for simulators, `xcodebuild` (app and WDA builds), signing, and provisioning. Sign in to your Apple Developer account in Xcode > Settings > Accounts to manage signing identities.
- **Homebrew** — [brew.sh](https://brew.sh). Quern uses Homebrew to install most of its dependencies (idb, mitmproxy, libimobiledevice, etc.). Setup will not install Homebrew itself — if it's missing, you'll be told.
- **Android Studio** (optional) — Only needed if you're working with Android. Provides `adb`, `emulator`, and the SDK tools. Quern auto-discovers these from your PATH or the standard SDK install locations.

## Install

```bash
curl -fsSL https://quern.dev/install.sh | bash
```

That's it. The installer clones Quern to `~/.local/share/quern`, then runs `quern setup`, which does everything else:

1. **Installs Python** (via Homebrew) if you don't already have Python 3.11+, then creates a virtual environment and installs server dependencies
2. **Checks for system tools** and offers to install what's missing:
   - **Xcode Command Line Tools** — for `simctl` (simulator control) and `devicectl` (iOS 17+ devices)
   - **idb + idb-companion** — for simulator UI automation (accessibility tree, tapping, typing)
   - **pymobiledevice3** — for physical device screenshots, logs, and tunneling
   - **mitmproxy** — for network traffic interception
   - **Node.js** — for the MCP wrapper
3. **Builds the MCP server** (TypeScript → JavaScript)
4. **Registers the MCP server** with Claude Code (writes to `~/.claude.json`)
5. **Adds `quern` to your PATH** (via `~/.local/bin`)
6. **Tracks what it installed** in `~/.quern/installed-by-setup.json` so `quern uninstall` can cleanly remove only what it added

Setup prompts before installing anything — it won't surprise you with Homebrew packages you didn't agree to. If a tool is already installed, it skips it.

When everything's ready, you'll see a summary like this:

```
──────────────────────────────────────────────────
  Quern Setup Summary
──────────────────────────────────────────────────
  ✓ Platform: macOS 26.3.1
  ✓ Homebrew: Homebrew 5.0.16
  ✓ Python: 3.12.12
  ✓ Virtual env: ~/.local/share/quern/.venv
  ✓ libimobiledevice: idevicesyslog 1.4.0
  ✓ ideviceinstaller: ideviceinstaller 1.2.0
  ✓ Xcode CLI Tools: Installed (simctl available)
  ✓ mitmdump: 12.2.1
  ✓ Node.js: v20.19.5
  ✓ idb_companion: installed
  ✓ idb (fb-idb): installed
  ✓ pymobiledevice3: 7.7.1
  ✓ tunneld: Running on http://127.0.0.1:49151
  ✓ VPN Detection: No active VPN detected
  ✓ mitmproxy CA Cert: ~/.mitmproxy/mitmproxy-ca-cert.pem
  ✓ Crash dialog: Disabled (crash reports still saved to disk)
  ✓ Wrapper script: Installed to ~/.local/bin/quern
  ✓ MCP server: Built successfully
──────────────────────────────────────────────────
  All checks passed — ready to go!
```

Not everything needs to be green. If you're only working with simulators, you won't have tunneld or libimobiledevice — that's fine. Setup tells you what's missing and what it affects.

<details>
<summary>Manual install (for contributors / development)</summary>

```bash
git clone https://github.com/quern-dev/quern.git
cd quern
./quern setup
./quern mcp-install
```

</details>

### For Physical iOS Devices (iOS 17+)

Setup automatically installs the **tunneld** LaunchDaemon (it will ask for your sudo password). This creates persistent tunnels to connected iOS 17+ devices — required for screenshots, logs, and WDA on modern iPhones. See [Device Pool](device-pool.md) for why this exists.

### For Android

Install Android Studio (or the standalone SDK tools). Quern finds `adb` and `emulator` automatically from your PATH, `ANDROID_HOME`, or the standard SDK install locations. No additional Quern setup needed.

## Start the Server

```bash
quern start              # Runs in the background
quern start -f           # Foreground mode (useful for troubleshooting the server itself)
```

On startup, Quern finds available ports, checks which tools are installed, and starts its log/proxy/crash adapters. It writes everything to `~/.quern/state.json`, which is how the MCP wrapper and CLI discover the server.

## Verify It Works

After starting, run `quern status` to confirm it's up and see which tools were detected. Then in your AI agent, try:

> "List my available devices"

or

> "Take a screenshot of the simulator and show me in Preview"

If you see devices and get a screenshot, you're good to go.

## Using Quern with Your AI Agent

### Claude Code

If you ran the installer, the MCP server is already registered. Just open Claude Code in your project and start asking:

> "Boot an iPhone 16 simulator, install my app, and show me what API calls happen during login"

If you installed manually, run `quern mcp-install` to register.

### Other MCP Clients

Point your client at: `node ~/.local/share/quern/mcp/dist/index.js`

The MCP wrapper auto-discovers the running server via `~/.quern/state.json` — no URL or API key configuration needed.

## Server Lifecycle

```bash
quern status             # Is it running? What port? What tools are available?
quern stop               # Graceful shutdown
quern restart            # Stop + start
quern update             # Pull latest changes, reinstall deps, rebuild
quern uninstall          # Remove Quern and everything setup installed
```

The server runs as a background daemon. It survives terminal closure and persists until you explicitly stop it or reboot. If Quern's source code is updated (via `quern update` or `git pull`), restart the server to pick up changes.

## Configuration

Optional config at `~/.quern/config.json`:

```json
{
  "default_device_family": "iPhone",
  "local_capture": ["MobileSafari", "com.apple.WebKit.Networking"]
}
```

- **default_device_family**: When your agent asks for a device without specifying what kind, default to this. Usually "iPhone".
- **local_capture**: Process names for transparent network capture. Safari and WebKit are a good default — your agent can add your app's process name when it starts a debugging session.

## Where Things Live

| Path | What |
|---|---|
| `~/.local/share/quern/` | Source code (installed by curl script) |
| `~/.local/bin/quern` | CLI wrapper (on your PATH) |
| `~/.quern/state.json` | How everything finds the server (ports, PID) |
| `~/.quern/config.json` | Your configuration (optional) |
| `~/.quern/server.log` | Server logs (daemon mode) |
| `~/.quern/api-key` | Authentication token |
| `~/.quern/installed-by-setup.json` | What setup installed (for clean uninstall) |
| `~/.quern/device-pool.json` | Known devices and their state |
| `~/.quern/cert-state.json` | Proxy certificate installation state |
| `~/.quern/app-states/` | Saved app state checkpoints |
| `~/.quern/crashes/` | Pulled crash reports |
| `~/.quern/wda/` | WDA build cache and runner logs |

## Next Steps

- [Device Pool & Resolution](device-pool.md) — Understand how device selection works
- [Simulator Proxy Setup](ios-proxy-simulators.md) — Start capturing network traffic
- [Logging Best Practices](ios-logging.md) — Get useful logs from your app
- [API Testing Workflow](workflow-api-testing.md) — A real end-to-end example
