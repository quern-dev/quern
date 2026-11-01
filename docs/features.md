# What Quern does

The detail behind the [README's overview](../README.md#what-quern-does): every log source, everything the proxy can do and how to point devices at it, and the device-control surface with the automation each platform uses. The [user guides](guides/index.md) walk through each area task by task.

## Log Capture

Captures from multiple sources simultaneously, deduplicates, and stores in a ring buffer (10,000 entries).

| Source | Tool | What it captures | Mode |
|--------|------|-------------------|------|
| Physical device logs | `pymobiledevice3 syslog` | os_log, Logger, NSLog from physical devices | On-demand (`start_device_logging`) |
| Android device / emulator logs | `adb logcat` | App and system logs from Android devices and emulators, tagged `source="logcat"` | On-demand (`start_device_logging`) |
| Simulator logs | `simctl log stream` | os_log, Logger, NSLog from simulators | On-demand (`start_simulator_logging`) |
| Crash reports | `pymobiledevice3 crash` (iPhone), `dumpsys dropbox` (Android) | Parsed crash reports with stack traces | Always on |
| Build output | `xcodebuild` | Errors, warnings, test results | Always on |
| Device syslog (legacy) | `idevicesyslog` | Unfiltered system + app log messages | Opt-in (`--syslog`) |
| macOS unified log | `log stream` | os_log from any host Mac process | On-demand (`start_oslog_streaming`) |

## Network Proxy

Spawns `mitmdump` as a subprocess to capture HTTP/HTTPS traffic (port 9101 by default).

- **Query flows** — filter by host, method, status code, path, or simulator UDID
- **Inspect details** — full headers and bodies for any captured request
- **Intercept** — pause matching requests, inspect, modify, release
- **Mock** — return synthetic responses without hitting the real server; rules can be updated in place without tearing down and re-adding them
- **Replay** — re-send a previously captured request
- **Capture sessions** — bracket a single UI action and get back only the flows it produced, instead of filtering them out of everything else
- **Bypass** — exclude domains from capture with an allowlist, so analytics and telemetry noise never enters the flow store
- **Local capture** — transparently capture simulator traffic per-process via mitmproxy's macOS System Extension, without configuring a system proxy. Each flow is tagged with the originating simulator's UDID for per-simulator filtering
- **Session capture** — capture one app on a simulator you hold through a separate session proxy, without changing the Mac-wide list or restarting anything (`add_session_capture`). It's how a client key, or an agent on another Mac, captures its own traffic
- **System proxy** — auto-configures macOS network settings to route traffic through the proxy (for physical devices or non-simulator traffic)
- **Certificate management** — check, install, and verify mitmproxy CA certificates. Trust is read from the device every time rather than from a record, so an erased simulator is noticed rather than reported as still trusting the CA. Quern asks before installing one: capturing through a device that does not trust the CA fails every HTTPS request with nothing pointing at the proxy, so enabling capture refuses in that state rather than creating it. `quern set-auto-install-cert on` answers the question once
- **Refused handshakes are recorded** — a client that rejects the certificate never becomes a flow, so it used to leave no trace anywhere. `proxy_status` now reports each rejection with the host, the process and simulator that refused, and the TLS alert verbatim — which is what separates "this device does not trust the CA" from "this app pins its certificate", two causes with identical symptoms and different fixes
- **LLM summaries** — traffic digests grouped by host with error highlights

**Proxy setup for simulators:**

| Mode | Setup | Pros | Cons |
|------|-------|------|------|
| Local capture (recommended) | `quern enable-local-capture` + approve macOS System Extension | Zero config per-simulator, per-simulator flow tagging, no system proxy needed | Requires [mitmproxy-macos](https://github.com/mitmproxy/mitmproxy-macos) and one-time macOS permission approval |
| System proxy | `configure_system_proxy` / `unconfigure_system_proxy` | No extra software | Affects all Mac traffic, must remember to unconfigure when done |

Local capture requires approving the **Mitmproxy Redirector** system extension in **System Settings > Privacy & Security** on first use.

With no arguments, `enable-local-capture` captures web traffic — `MobileSafari` and `com.apple.WebKit.Networking`. Naming processes **keeps that minimum and adds yours**, because a webview's requests leave through WebKit and an OAuth hand-off goes through Safari:

```bash
quern enable-local-capture MyApp                                # your app, plus the web-view minimum
quern enable-local-capture MyApp com.apple.WebKit.Networking    # your app and web views
```

The process name is usually the target name in Xcode. Name the process that actually makes the requests: Safari's traffic and every in-app web view's leave through `com.apple.WebKit.Networking`, so `MobileSafari` on its own captures nothing. The command prints what a change drops, since removing a process otherwise looks exactly like adding one.

Entries are mitmproxy's local-mode syntax, not only names: a name matches any part of the process's executable path, a bare number is a PID, and `!` excludes. Put exclusions *after* an include (`MyApp '!12345'`, quoted so the shell leaves the `!` alone). A list that *starts* with an exclusion captures every process on the Mac except those excluded, so it is refused unless you pass `--whole-mac`.

You can also change the list at runtime with the `set_local_capture` MCP tool. That restarts the proxy, not the server: mocks, bypass patterns and the intercept are kept, but open connections reset and held flows are released. To capture one app on one simulator you hold without any restart, use `add_session_capture`.

**Proxy setup for physical devices:** Configure the device's Wi-Fi proxy in Settings, then call `record_device_proxy_config` with the SSID and device IP. Quern automatically finds the correct Mac interface IP by subnet-matching, so it works correctly even when multiple interfaces are active. Configs are stored per SSID — switching between home and work networks just works. `proxy_status` shows `wifi_proxy_stale` per device if the stored config no longer matches the current network, and `network_state` (refreshed by a ~15s background poll) reports the current SSID/IP plus a `last_changed_at` timestamp so the response surfaces *when* the network shifted, not just that it's currently mismatched. When the laptop and physical devices travel together between locations, this lets agents notice the change and prompt for proxy reconfiguration without anyone having to remember to ask.

## Device Control

Manage iOS simulators and physical devices, and interact with running apps.

- **Device management** — list, boot, shutdown, and erase simulators and Android emulators; discover physical devices
- **App management** — install, launch, terminate, uninstall, list apps; build an Xcode scheme and install it across several devices in one call
- **Screenshots** — capture with configurable scale and format, annotated screenshots with accessibility overlays, and screenshot timelines that auto-capture after every UI action so a whole run can be reviewed frame by frame
- **Live preview** — real-time video windows for USB-connected physical devices and booted simulators, independently per-device
<!-- TODO: Annotated screenshot example — show a real app with the accessibility overlay
     ![Annotated screenshot](images/annotated-screenshot.png)
-->
- **UI inspection** — accessibility tree, element state queries, wait-for-element polling, screen summaries
- **Interaction** — tap (by element label or coordinates), long press, swipe (including edge swipes: back, home, Control Centre), pinch, rotate, two-finger pan, double tap and two-finger tap (iOS, and Android through scrcpy), scroll a container until a target is in view, type text, clear text, press hardware buttons
- **Configuration** — set GPS location, grant permissions, open URLs and deep links, attach or detach the simulated hardware keyboard (iOS), and set locale, font scale, and display density (Android)
- **App state checkpoints** — save and restore a named snapshot of an app's data container and app groups, so a test can start from a seeded state instead of driving the UI there every time. Auth tokens live in the simulator keychain, *outside* every app container, so a checkpoint restores logged-out unless you pass `include_keychain` — which additionally requires the device to be shut down, since the keychain is a WAL-mode SQLite database held open by `securityd`
- **Plist inspection** — read, diff, set, and delete defaults inside a simulator app's container, or watch a plist and have per-key changes land in the log pipeline alongside app logs and proxy flows
- **Device pool** — smart device resolution with active device tracking for multi-device workflows
- **Shared and remote servers** — reserve devices so agents sharing a server don't collide, pair another Mac (`quern pair` / `quern connect`), see each paired Mac's free devices (`quern remote list`, `list_remotes`), and switch an agent to one (`use_remote`) with its mocks, session capture and knowledge base following. See the [remote server guides](guides/remote-setup.md)

**Simulator UI automation** uses a native Swift helper, `sim-bridge`, that talks to `CoreSimulator` / `SimulatorKit` / `AccessibilityPlatformTranslation` directly — no daemon, no subprocess per call. It's built automatically on first use and requires Xcode 26+ on Apple Silicon. On Intel Macs or older Xcode, Quern falls back to [idb](https://fbidb.io/) (`brew install idb-companion` + `pip install fb-idb`); `quern setup` handles the install in that case. Device management and screenshots use `xcrun simctl` (always available with Xcode).

**Physical device UI automation** uses [WebDriverAgent](https://github.com/appium/appium-ios/tree/main/packages/webdriveragent) (WDA), which Quern builds and deploys automatically via `setup_wda`, at the version it pins. WDA requires a valid Apple Developer signing identity. Once set up, the WDA driver auto-starts on first interaction and idles out after 15 minutes of inactivity. The app appears on the device as **Quern Driver**.
