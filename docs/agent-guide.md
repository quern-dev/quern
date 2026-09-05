# Quern Agent Guide

**For**: AI agents using Quern MCP tools for mobile app debugging and testing
**Last Updated**: March 1, 2026

---

## Philosophy

Quern is your sensory and motor interface to mobile apps. The tools give you three capabilities:

- **Eyes**: See UI state, network traffic, logs
- **Ears**: Hear app events, crashes, errors
- **Hands**: Control UI, intercept network, trigger actions

The tools should feel natural — you don't think about mechanics, you just look, listen, and act.

---

## Quick Start Checklist

Every Quern session should start with:

1. `resolve_device` — get a device to work with
2. `get_screen_summary` — see what's on screen
3. `proxy_status` — check if network capture is active

From there, use the right tool for your task. If any tool fails with a connection error, call `ensure_server` to verify the server is running and auto-start it if needed.

---

## Core Principles

### 1. Prefer Structured Data for Decision-Making

Use `get_screen_summary` for a curated text description or `get_ui_tree` for the full accessibility hierarchy. These are cheaper, faster, and easier to reason about programmatically than screenshots.

Screenshots are still useful — for verifying visual layout, catching rendering bugs, documenting state for humans, or when you need to see something the accessibility tree doesn't capture. Use both, but reach for structured data first when you need to make decisions or find elements to interact with.

Both `take_screenshot` and `take_annotated_screenshot` support a `save_path` parameter to write the image to disk instead of returning base64 — useful when handing off files to other tools (e.g., opening in Preview) or attaching to reports.

---

### 2. Prefer Accessibility Over Coordinates

Use `tap_element` with a label and element type instead of `tap` with raw coordinates. Accessibility-based taps work across screen sizes, survive layout changes, and are self-documenting.

**When coordinates are OK**: Gestures that aren't tied to specific elements (swipe to refresh, drag to reorder).

---

### 3. Summarize First, Drill Down Second

Don't start with `query_logs(limit=10000)` or `query_flows(limit=1000)`. Start with the summary tools:

- `get_log_summary` — overview of recent log activity
- `get_flow_summary` — traffic grouped by host with error highlights
- `get_screen_summary` — curated interactive elements on screen

Then drill down based on what you learned: filter logs by a specific error message, query flows for a specific failing host, or scope the UI tree to a specific container.

---

### 4. Verify State Before Acting

Don't assume the proxy is running — check with `proxy_status` first. Don't assume an element exists — check with `get_screen_summary` before tapping. State can change between tool calls. Always verify before acting.

---

### 5. Use Server-Side Waiting, Not Client-Side Polling

Use `wait_for_element` instead of calling `get_ui_tree` in a loop. It polls server-side at sub-second intervals, returns immediately on match, handles timeouts, and uses fewer API round-trips.

Use `wait_for_flow` after triggering a UI action to observe the resulting network request — it blocks until a matching flow appears or times out. Auto-sets `since` to 5 seconds before the call to catch flows that completed between the action and the wait. Use `list_held_flows` with a `timeout` when you need to intercept and *modify* flows.

---

### 6. Filter Aggressively

Logs, network flows, and UI trees can be huge. Always filter to what you need.

**Logs**: Filter by `level`, `process`, `search` text, and time range. Don't fetch 1,000 entries when 50 filtered ones will do. For sustained debugging, use `set_log_filter` to drop noise at ingestion — the `device-quiet` preset removes common system daemons, or combine `process` + `subsystems` includes for app-only output with zero framework noise. Setting a process filter automatically restarts running log adapters with subprocess-level filtering, cutting noise at the source. You can also apply presets at start time: `start_device_logging(process: "MyApp", preset: "device-quiet")`.

**Flows**: Filter by `host` (single), `hosts` (list), `exclude_hosts` (list), `method`, `path_contains`, and `status_min`/`status_max`. Use `detail="summary"` to get compact results (method/url/status/timing only) that stay within token limits. Use `get_flow_summary` first to identify which hosts or patterns to investigate.

**UI Tree**: Use `get_screen_summary` with a reasonable `max_elements` limit. If you need detail, scope `get_ui_tree` with `children_of` to a specific container rather than fetching the full 500+ element hierarchy.

---

## Common Workflows

### Debugging Network Issues

1. Check `proxy_status` — start the proxy if it isn't running
2. Get a baseline with `get_flow_summary` to see current traffic patterns
3. Trigger the issue (tap a button, navigate to a screen)
4. Query for relevant flows — filter by method, path, status code, or `simulator_udid`
5. Use `get_flow_detail` on the specific flow to inspect headers, request body, and response body
6. To re-send a captured request (e.g., after fixing a backend issue), use `replay_flow` with the flow ID

**Key insight**: Start with summary, trigger action, drill down to specific flows.

**Capture sessions for test fixtures**: To isolate the API calls for a specific UI action, use `start_capture_session` before the action and `stop_capture_session` after. This returns only the flows from that time window, with built-in host filtering and compact summaries. Use `exclude_hosts` to filter out analytics/SDK noise, or `hosts` to capture only specific API domains.

**Per-simulator flow filtering**: When local capture is enabled, each flow is tagged with the originating simulator's UDID. Use `simulator_udid` in `query_flows`, `get_flow_summary`, and `start_capture_session` to see only traffic from a specific simulator — essential when running parallel tests.

**Proxy modes for simulators**:

- **Local capture (recommended)**: Uses mitmproxy's macOS System Extension to transparently capture simulator traffic without configuring a system proxy. Each simulator's flows are tagged with its UDID. Check `proxy_status` — if `local_capture` is non-empty, simulator traffic is already being captured. The user configures which processes to capture via `quern enable-local-capture <process_name>` (the process name is typically the Xcode target name). Use `set_local_capture` to change the process list at runtime without restarting the server.
- **System proxy**: Configures macOS-wide proxy settings. Use `configure_system_proxy` to start capturing and `unconfigure_system_proxy` when done. Affects all Mac traffic — always unconfigure when finished.

**Certificate verification**: If no flows are captured, verify the proxy certificate is installed on the simulator:
1. Call `verify_proxy_setup` — performs a ground-truth check by querying the simulator's TrustStore database. Defaults to **booted simulators only**; pass `state="all"` or `device_type="device"` to check shutdown sims or physical devices
2. Returns per-device `status`: `installed`, `not_installed`, `never_booted`, or `error`
3. Returns `erased_devices` — UDIDs where a previously installed cert is now missing (probable device erase)
4. If cert is missing, install it with: `xcrun simctl keychain <udid> add-root-cert ~/.mitmproxy/mitmproxy-ca-cert.pem`

**Physical device proxy capture**: Physical devices need their Wi-Fi proxy configured manually in Settings. The full setup flow is: install cert → trust cert → configure Wi-Fi proxy → call `record_device_proxy_config`. After that, filter flows by the device's `client_ip`.

- `record_device_proxy_config(udid, ssid, client_ip)` — records the config per Wi-Fi network (SSID). Automatically derives the correct Mac interface IP by finding the interface on the same /24 subnet as the device's `client_ip`. This handles multi-interface Macs correctly (e.g. Wi-Fi + Ethernet active simultaneously).
- Call it again whenever you move to a different network — each SSID is tracked independently, so configs for home and work don't overwrite each other.
- `proxy_status` shows `wifi_proxy_configs` (keyed by SSID), `wifi_proxy_stale` (true if no stored network's proxy_host matches the current Mac IP), and `active_wifi_network` (the currently matching SSID). If `wifi_proxy_stale: true`, reconfigure the device's Wi-Fi proxy and call `record_device_proxy_config` again.
- `proxy_status` also includes `network_state` (refreshed by a ~15s background poll) — current SSID/IP, plus `last_changed_at` and `last_change_reason` fields populated when the laptop's network identity shifts. When you see `last_changed_at` is recent and a physical device is in play, expect `wifi_proxy_stale: true` to follow — and run the autonomous reconfiguration flow below.
- See `docs/physical-device-cert-setup.md` for the full WDA automation script.

**Autonomous proxy reconfiguration (cert already installed)**: When `wifi_proxy_stale: true` or `wifi_proxy_configs: null`, you can update the device's proxy settings fully via WDA without user intervention:

1. `proxy_status` — confirm proxy is running, note `local_ip`
2. `resolve_device(name=..., type="device")` — connect to the physical device
3. `launch_app(bundle_id="com.apple.Preferences")` — open Settings
4. `tap_element(label="Wi-Fi")` — navigate to Wi-Fi settings
5. `tap_element(label="More Info", element_type="Button")` — open the connected network's detail page
6. Read the device IP from the `IP Address` row (use `take_screenshot` or `get_screen_summary`)
7. Scroll down to find the **HTTP Proxy** section, tap `Configure Proxy` (StaticText)
8. Verify the current Server value — if stale, tap the Server field to focus it:
   - The Server TextField is not discoverable by label in `get_ui_tree`. Use `take_annotated_screenshot` to find its bounding box, calculate its center as a fraction of the screen, convert to device logical points, then `tap` those coordinates.
   - `clear_text()` → `type_text("192.168.x.x")` with the correct Mac IP
   - `tap_element(label="Save", element_type="Button")`
9. `record_device_proxy_config(udid=..., ssid=..., client_ip=<device IP from step 6>)` — the correct Mac interface IP is derived automatically
10. `proxy_status` — verify `wifi_proxy_stale: false` and `active_wifi_network` is set
11. `launch_app(bundle_id="com.apple.mobilesafari", udid=...)` — open Safari to generate traffic
12. `wait_for_flow(client_ip=<device IP>)` — confirm traffic from the device is reaching the proxy. If no flow arrives within the timeout, the cert is likely not trusted on the device (Settings → General → About → Certificate Trust Settings) or the proxy port is unreachable from the device's network.

**Note on the Server field coordinates**: The proxy settings screen on iOS 26 does not expose text fields in the standard accessibility tree. Always use `take_annotated_screenshot` before tapping — the annotated view shows the TextField bounding box even when `get_ui_tree` doesn't. The field typically sits at ~40% screen height; divide the bounding box center (in image pixels) by the image dimensions and multiply by the device's logical point size (e.g. 390×844 for iPhone 12).

---

### Debugging UI Issues

1. Call `get_screen_summary` to see current state
2. Trigger the issue (tap, swipe, type)
3. Call `get_screen_summary` again to see what changed
4. If the result is unexpected, use `get_ui_tree` (optionally scoped with `children_of`) to inspect the full hierarchy

**Key insight**: Use summary for quick checks, full tree when you need details.

**If the element isn't on screen**: reach for `tap_element` (which auto-scrolls) or `scroll_to_element` rather than a manual `swipe` loop. Be aware that reading the full UI tree can itself scroll the content — on Android's `CoordinatorLayout`/`RecyclerView` screens the accessibility traversal a dump performs pushes top controls out of view before your tap lands. Both scroll paths avoid the dump for exactly this reason, so prefer them over "dump, read coordinates, tap".

**Debugging the platform normalizer**: When `tap_element` or a landmark match doesn't behave as expected and you suspect the underlying source attributes aren't surfacing correctly (e.g. an Android tab that doesn't appear `selected`), call `get_ui_tree` with `include_raw=true` to get the raw provider attributes (full uiautomator2 XML on Android) on each element under `extra_attrs`. This is faster than dropping to `adb shell uiautomator dump` and stays inside the Quern API surface.

---

### Identifying Screens with Landmarks

When the question is "what screen am I on right now?" — for verifying navigation, gating actions, or driving recipe-style workflows — landmarks give you a deterministic answer that doesn't depend on label parsing or model swaps.

**Why use landmarks instead of just reading `get_screen_summary`?**

- **Deterministic.** Quern matches the live UI tree against authored selectors and returns `matched: <screen_name>` with `confidence: exact | ambiguous | none`. No prose interpretation.
- **Cross-platform.** The same landmarks work on iOS (sim-bridge / idb / WDA depending on device) and Android (uiautomator2) — selection state, identifiers, and labels are normalized to a single schema.
- **Stable across LLMs.** A workflow that asks "is this the cart screen?" gets the same answer regardless of which model is driving.
- **Authored once, reused everywhere.** The screen knowledge base is the source of truth; agents don't re-discover screen identity on every call.

**Workflow:**

1. Load landmarks from the app's knowledge base:
   ```
   load_landmarks(app="org.example.myapp", path="/Users/dev/myapp/.quern/knowledge")
   ```
   Or pass landmarks inline (useful for ad-hoc identification):
   ```
   load_landmarks(app="...", landmarks={"home-tab": [{"element": "RadioButton", "identifier": "tab.home", "selected": true}]})
   ```
2. Call `identify_screen(app="...")` or — more often — pass `identify=true` to `get_screen_summary` to fold identification into a call you were already making.
3. Read the response. On a match, you get `matched`, `confidence: "exact"`, and `matched_landmarks` with per-landmark hit/miss detail. On no match, `partial_matches` lists every evaluated screen sorted by descending match count, *each with its full per-landmark results* — you can see exactly which selectors failed without re-running.

**Landmark schema** (in screen frontmatter or inline JSON):

| Field | Purpose |
|---|---|
| `element` | Element type, required (e.g. `Button`, `RadioButton`, `navigationBar`) |
| `identifier` | Accessibility identifier, exact match (preferred — locale-independent) |
| `label` | Label text, case-insensitive exact match |
| `label_contains` | Substring match for elements with dynamic content in their label |
| `absent: true` | Element must NOT be present (use sparingly — rare) |
| `selected: true` | For tabs/switches/radios/checkboxes, element must be in the on/active state. Distinguishes "the Home tab is the selected one" from "a Home tab exists." |

**A knowledge base with no landmarks:** If `load_landmarks` returns `screens: 0` and a populated `skipped[]`, each entry says why. `legacy_format` means the file uses `identify_by:`, the field that preceded `landmarks:` and that the loader has never evaluated; the original entries come back in the response, so the rename can be done from it directly (keep `element`/`identifier`/`label`/`label_contains`/`absent`, turn `value: "1"` into `selected: true`, drop the rest). Check the result against the running app rather than translating blind — a knowledge base old enough to use that field is old enough to have drifted.

**Validating before relying on landmarks**: Run `validate_landmarks(app="...")` after loading. Reports collisions (two screens whose landmark sets overlap — one could be mistaken for the other) and screens with no landmarks. Fix collisions by adding a distinguishing element to one of the screens.

**When `confidence: "none"` on a known-good screen**: the landmarks are likely stale — the app has shipped UI changes since the knowledge base was authored. Surface this to the user before continuing; downstream automation built on top of stale landmarks will silently produce wrong results (you'll act on the wrong screen and misreport state). The fix is to navigate to the screen, run `get_ui_tree`, and re-author the landmarks block from what's actually there. See "Keeping Landmarks in Sync" in the knowledge-base authoring guide.

---

### Debugging Crashes

1. Check recent crashes with `get_latest_crash`
2. Get logs around the crash time — query for error-level entries in the seconds leading up to the crash
3. Check network activity around the same time with `query_flows`
4. Correlate: crash report + logs + network activity = full picture

**Key insight**: Crashes leave traces in multiple places. Cross-referencing sources is where you find root causes.

**Crash discovery**: Simulator crash reports are automatically picked up from `~/Library/Logs/DiagnosticReports/` (enabled by default). The macOS crash dialog can be disabled via `./quern setup` or manually with `defaults write com.apple.CrashReporter DialogType none` — crash reports are still written to disk.

**Crash hooks**: Use `--on-crash '<command>'` to run a shell command whenever a crash is detected. The full `CrashReport` JSON is piped to the command's stdin. The hook runs in the background with a 60-second timeout and never blocks the server. Example:

```bash
./quern start --on-crash 'cat > /tmp/last_crash.json'
```

---

### Working with Physical Devices

Physical iOS devices are supported for screenshots, UI automation, log capture, and crash reports. The key difference from simulators is that UI automation uses WebDriverAgent (WDA) instead of the simulator backend (sim-bridge / idb).

**First-time setup**: Call `setup_wda` with the device UDID. This builds and installs WDA on the device, which requires a valid Apple Developer signing identity. If multiple identities exist, the tool returns a list — call again with the chosen `team_id`. The app appears on the device as **Quern Driver**.

**After setup**: WDA auto-starts when you first interact with the device (screenshot, UI tree, tap, etc.). No need to manually call `start_driver` — it happens transparently. The driver idles out after 15 minutes of inactivity.

**What works the same**: `take_screenshot`, `get_screen_summary`, `get_ui_tree`, `tap_element`, `swipe`, `type_text`, `wait_for_element`, `install_app`, `launch_app`, `terminate_app`.

**What's different**:
- `boot_device` / `shutdown_device` — simulators only
- `set_location` — simulators only
- `grant_permission` — simulators only
- `start_device_logging` / `stop_device_logging` — on-demand log capture for physical devices (vs `start_simulator_logging` for simulators). Captures os_log and Logger output only — `print()` writes to stdout and is not captured. Both support `preset` parameter to apply ingestion filters at start time
- `get_latest_crash` with a `udid` parameter — pulls crash reports directly from the physical device
- `preview_device` — opens a live macOS video preview window of the device screen via CoreMediaIO (USB-connected physical devices only, not simulators). Each device is independently controlled — add and remove individual previews without affecting others. Use `stop_preview` with a UDID to close one device, or without to close all. `preview_status` shows per-device breakdown and available devices

---

### Live Preview of Physical Devices

Open real-time video windows to see what's happening on USB-connected physical devices. Each device is independently managed — no restart penalty after the initial 3-second CoreMediaIO discovery.

1. `preview_device` with a device UDID — adds that device's preview window
2. `preview_device` with another UDID — adds a second device (1s stagger, no rediscovery)
3. `preview_status` — see which devices are active vs. available
4. `stop_preview` with a UDID — remove one device's preview, others stay running
5. `stop_preview` without a UDID — stop all previews and kill the process

**Key insight**: The preview process stays alive even if all windows are closed (by user or via `stop_preview` with UDID). Re-adding a device is instant — no 3s discovery delay. Only `stop_preview` without a UDID kills the process.

**Limitations**: USB-connected physical devices only. Simulators are not CoreMediaIO screen capture sources.

---

### Reproducing Bug Reports

1. Use `get_screen_summary` to verify your starting state
2. Follow the reported steps using `tap_element`, `type_text`, `swipe`, etc.
3. After each step, call `get_screen_summary` to verify the expected state before continuing — this catches where the reproduction diverges from expectations
4. Check logs and network flows alongside UI state to build the full picture
5. If the bug reproduces, capture a diagnostic bundle: screenshot, logs, network flows, and UI tree

**Key insight**: Verify state at each step. The step where expected and actual diverge is where the bug lives.

---

## Tool Selection Guide

**"I need to see what's on screen"**
- Quick overview: `get_screen_summary`
- Full detail: `get_ui_tree`
- Visual for humans: `take_screenshot`
- Accessibility overlay: `take_annotated_screenshot` — draws bounding boxes on interactive elements, useful for debugging why `tap_element` can't find an element

**"I need to tap/interact with UI"**
- Known element: `tap_element` with label and element_type
- Coordinates (rare): `tap`
- Gesture: `swipe`
- Text input: focus the element, then `type_text`

**"The element I want is scrolled off-screen"**
- `tap_element` already handles this: on a miss it scrolls the target into view and retries. Pass `scroll_to_find: false` to fail fast instead.
- Need it visible but *not* tapped (asserting on it, screenshotting it): `scroll_to_element`
- Both work on Android and iOS (simulator and physical), and both drive a bounded swipe loop that re-checks the target by selector rather than dumping the UI tree — a tree dump can itself scroll the target away, which is the bug this design exists to avoid
- Neither is a substitute for `swipe` when you want to scroll a *screen* rather than reach a known element

**"I need to see network traffic"**
- Overview: `get_flow_summary` (use `simulator_udid` to filter by simulator)
- Specific requests: `query_flows` with filters (`hosts`, `exclude_hosts`, `detail="summary"`)
- Bracket a UI action: `start_capture_session` → action → `stop_capture_session`
- Full detail: `get_flow_detail`
- Modify traffic: `set_intercept` + `release_flow` with modifications
- Mock responses: `set_mock`
- Replay a request: `replay_flow`
- Check capture mode: `proxy_status` — look at `local_capture` field

**"I need to see logs"**
- Recent activity: `tail_logs`
- Overview: `get_log_summary`
- Specific search: `query_logs` with filters
- Errors only: `get_errors`
- Reduce noise at start: `start_device_logging(process: "MyApp", preset: "device-quiet")` — applies subprocess-level process filter and ingestion preset in one call
- Reduce noise mid-session: `set_log_filter(source: "device", process: "MyApp")` — automatically restarts the adapter with subprocess-level filtering and purges old entries
- **App-only mode** (zero noise): First `tail_logs` with the process filter to discover your app's subsystem name, then lock it down with `set_log_filter(source: "device", process: "MyApp", subsystems: ["MyApp.debug.dylib"])`. This eliminates all framework noise (UIKitCore, CFNetwork, Security) and shows only your code's os_log output.

**"I need to control the device"**
- Boot: `boot_device` or `resolve_device` with auto_boot
- Install app: `install_app`
- Launch app: `launch_app`
- Terminate app: `terminate_app`
- Uninstall app: `uninstall_app`
- List installed apps: `list_apps`
- Screenshot: `take_screenshot`
- Location: `set_location` (simulators only)
- Permissions: `grant_permission` (simulators only)
- Physical device setup: `setup_wda` (first time only)

---

## REST API Reference

When calling the HTTP API directly instead of going through MCP, the complete
mapping — every tool, its method and path, and the endpoints that have no tool
(SSE streams, public probes) — is in `docs/api-reference.md`, also readable over
MCP as the `quern://api-reference` resource.

The paths you will reach for most:

| Purpose | Method | Path |
|---|---|---|
| Liveness probe | GET | `/health` (public, sub-millisecond) |
| Device-tool availability | GET | `/tools` (public) |
| Query logs | GET | `/api/v1/logs/query` |
| Log summary | GET | `/api/v1/logs/summary` |
| Live log stream | GET | `/api/v1/logs/stream` (SSE — no MCP equivalent) |
| Query flows | GET | `/api/v1/proxy/flows` |
| Flow summary | GET | `/api/v1/proxy/flows/summary` |
| Live flow stream | GET | `/api/v1/proxy/flows/stream` (SSE — no MCP equivalent) |
| Screen summary | GET | `/api/v1/device/screen-summary` |
| Tap an element | POST | `/api/v1/device/ui/tap-element` |
| Scroll to an element | POST | `/api/v1/device/ui/scroll-to-element` |
| Resolve a device | POST | `/api/v1/devices/resolve` |

Everything needs `Authorization: Bearer <key>` from `~/.quern/api-key`, except
`/`, `/health`, `/api/v1/health`, `/tools`, `/docs`, `/redoc`, `/openapi.json`,
`/video-test`, and `/api/v1/proxy/cert`.

## Advanced Patterns

### Correlation

Humans struggle to correlate millisecond-level timing across logs, network, and UI. You don't. After triggering an action, query logs, flows, and UI state for the same narrow time window. Events that occur within milliseconds of each other are almost certainly related — this lets you trace causation across system boundaries.

---

### Intercept-Modify-Release for Testing Edge Cases

Test error handling without breaking the backend:

1. Set up an intercept pattern matching the target endpoint (e.g., `~d api.example.com & ~m POST`)
2. Trigger the action in the app
3. Wait for the request to be held with `list_held_flows` (use the timeout parameter)
4. Release the flow with modifications — change the status code to 500, inject an error body, or alter headers
5. Observe how the app handles the modified response via `get_screen_summary` and `query_logs`

Use this to test error handling, slow network conditions, and malformed responses without needing backend changes.

---

### Mocking for Deterministic Testing

Use `set_mock` to return synthetic responses for specific endpoints. This lets you create reliable, repeatable test scenarios — fixed user data, specific error conditions, or edge-case payloads — without depending on backend state. Use `update_mock` to modify an existing rule's pattern or response without deleting and recreating it.

Mock rules take priority over intercept rules. Clear them with `clear_mocks` when done.

**Filter pattern syntax:** Mock and intercept patterns use mitmproxy filter expressions. Common operators: `~d` (domain), `~u` (URL/path — use this for path matching), `~m` (method), `~c` (status code), `~h` (header), `~t` (content-type), `~b` (body). Combine with `&` (and), `|` (or), `!` (not). Examples: `"~d api.example.com & ~u /v1/users"`, `"~m POST & ~d api.example.com & ~u /v1/login"`. Note: `~p` is not a valid operator — use `~u` for path matching.

---

### Device Pool for Parallel Testing

Use `ensure_devices` to boot multiple simulators at once, then run different test scenarios on each in parallel. The first device in the result becomes the active device; pass explicit `udid` parameters to target the others.

**Active device**: After `resolve_device` or `ensure_devices`, the resolved device becomes the active device for all subsequent tool calls. You don't need to pass `udid` to every tool — it defaults to the active device. To switch, call `resolve_device` with new criteria, or pass `udid` directly to `resolve_device` (faster than re-matching by name when you already know the UDID — e.g., from a `list_devices` call). The active device is persisted in `~/.quern/active-device.json` and survives `quern stop` / `quern restart`, so you don't have to re-resolve at the start of every session.

**Default behavior**: `resolve_device` and `ensure_devices` default to `type="simulator"` to prevent accidentally targeting physical devices (which may not have your app installed). Pass `type="device"` explicitly to target physical devices.

---

## Common Mistakes

**Not calling `ensure_server` when tools fail** — If tools fail with connection errors, call `ensure_server` — it checks server health and auto-starts if needed.

**Using only screenshots to understand UI state** — Screenshots work, but `get_screen_summary` and `get_ui_tree` are faster, cheaper, and return structured data you can act on directly. Use screenshots to complement structured data, not replace it.

**Forgetting element_type when label is ambiguous** — `tap_element(label="Cancel")` might match a StaticText instead of the Button. Specify `element_type="Button"` when the label might not be unique.

**Using `get_ui_tree` to debug missing elements** — When `tap_element` can't find an element, use `take_annotated_screenshot` to visually see what the accessibility tree detects overlaid on the actual screen. It's faster than reading through the full UI tree and immediately shows mismatches between visual layout and accessibility labels.

**Not filtering logs/flows** — Unfiltered queries return overwhelming amounts of data. Always filter by level, process, host, status code, or search text.

**Hardcoding device UDIDs** — Use `resolve_device` with a name and let Quern find the right device. UDIDs differ across machines.

**Client-side polling instead of server-side waiting** — Use `wait_for_element` instead of looping on `get_ui_tree`. Use `wait_for_flow` instead of polling `query_flows` after triggering a UI action. Use `list_held_flows` with a timeout instead of polling for intercepted flows.

**Not clearing text before typing** — Use `clear_text` before `type_text` when a field has pre-existing content. Otherwise you'll append to whatever's already there.

**Confusing `tail_logs` and `query_logs`** — Use `tail_logs` for "show me recent stuff" (defaults to 50, newest first). Use `query_logs` for searching with filters and time ranges.

**Ignoring log source names** — `device` = physical device logs (on-demand, via `start_device_logging`), `simulator` = simulator unified logging (on-demand, via `start_simulator_logging`), `oslog` = host Mac unified logging (on-demand, via `start_oslog_streaming` — useful for capturing dev tool logs like Vite or webpack that write to os_log), `crash` = crash reports, `build` = xcodebuild output, `proxy` = network traffic. Legacy: `syslog` = idevicesyslog (disabled by default, opt-in with `--syslog`).

**Using mock when you need intercept (or vice versa)** — Mocks return instant synthetic responses for stable test fixtures. Intercept pauses real requests for ad-hoc inspection and modification. Mock rules take priority over intercept.

**Not checking simulator backend availability** — Device management and screenshots use `simctl` (always available with Xcode). Simulator UI automation (`get_ui_tree`, `tap`, `swipe`, `type_text`, `clear_text`, `press_button`) requires either sim-bridge (Xcode 26+ / Apple Silicon, preferred) or idb (fallback). Physical device UI automation uses WDA (auto-started). Check `list_devices` response for tool availability.

**Not using per-simulator flow filtering** — When local capture is enabled, flows are tagged with the originating simulator's UDID. Always pass `simulator_udid` when querying flows during parallel testing — otherwise you'll see traffic from all simulators mixed together.

**Leaving system proxy configured** — If you use `configure_system_proxy`, always call `unconfigure_system_proxy` when done. Forgetting this breaks the user's browser. With local capture enabled, you typically don't need the system proxy for simulator traffic at all.

**Holding flows too long** — Held flows auto-release after 30 seconds to prevent hanging clients. Use `list_held_flows` with `timeout` for long-polling instead of rapid polling.

---

## Performance Tips

**Use summaries before full queries.** Summaries are cheap and curated. Use them to decide what to investigate, then make targeted queries.

**Limit result counts.** Fetch 50 entries, not 10,000. You can always query for more if needed.

**Use cursors for incremental updates.** `get_log_summary` and `get_flow_summary` return a cursor. Pass it back with `since_cursor` to get only new activity since your last call — critical for token efficiency. The continuous monitoring pattern: call the summary tool, save the cursor, and on each subsequent check pass `since_cursor` to get a lightweight delta instead of re-fetching everything. For one-off actions where you need all flows from a specific time window, use `start_capture_session` / `stop_capture_session` instead — they handle the time-bracketing and filtering automatically.

**Scope UI tree queries.** Use `get_ui_tree` with `children_of` to fetch a subtree instead of the full hierarchy. Use `get_screen_summary` with a reasonable `max_elements` limit.

---

## Troubleshooting

**Tools missing or misbehaving?** `quern doctor` reports device-tool availability and venv sync, plus the version, provenance and upgrade command for the tools quern tracks as install sites (`pymobiledevice3`, `idb`, `mitmproxy`, `adb`, `libimobiledevice`, `node`). `simctl` and `devicectl` ship inside Xcode and are reported as available or not, without version detail
(adb, simctl, idb, devicectl, pymobiledevice3) as read-only diagnostics, and
`GET /tools` returns the same data over HTTP. Both are deliberately kept off the
`/health` path so the liveness probe stays sub-millisecond — do not expect
`/health` to tell you whether a tool is installed.

**"No element found matching label"** — The element may not exist, the label may be wrong, or multiple elements match. Use `get_screen_summary` to see what's actually on screen, then refine your query with the exact label and an element_type.

**"Proxy not running"** — Check with `proxy_status` and call `start_proxy` if needed.

**"No flows captured"** — Check `proxy_status`. If `local_capture` is non-empty, simulator traffic should be captured automatically — verify certs with `verify_proxy_setup`. If local capture is not enabled, the device may not be configured to route through the proxy. Check `proxy_setup_guide` for device configuration steps. Also check for certificate pinning in the app.

**"Wait for element timed out"** — The element may never have appeared (a bug or wrong expectation), the timeout may be too short, or the label may differ from what you expect. Check what actually appeared with `get_screen_summary`.

**"Tap isn't registering / wrong element activated"** — Your coordinates may be off. Use `take_annotated_screenshot` to see element bounding boxes overlaid on the actual screen. Find your target element's bounding box, express its center as a fraction of the screen image dimensions, then multiply by the device's logical point size (e.g. iPhone 12: 390×844 pt, iPhone 15 Pro: 393×852 pt) to get the correct tap coordinates. This is the reliable calibration technique when `tap_element` can't find the element and coordinate taps are missing.

---

## iOS Logging Best Practices for App Developers

Quern captures logs from Apple's unified logging system. How your app emits logs directly affects what Quern can capture and filter. These recommendations help developers get the most out of remote log capture.

### Use os.Logger, not print()

Swift's `print()` writes to stdout, which is **not captured** by the unified logging system. On a physical device, `print()` output is invisible to Quern (and to Console.app). Always use `os.Logger` instead:

```swift
import os

// Create a logger with your app's subsystem and a category
let logger = Logger(subsystem: "com.example.myapp", category: "networking")

// Log at appropriate levels
logger.notice("Request started: \(url.absoluteString, privacy: .public)")
logger.error("Request failed: \(error.localizedDescription, privacy: .public)")
```

The subsystem and category are what make Quern's filtering powerful — they let agents filter to exactly your code's output with zero framework noise.

### Choose the right log level

| Level | Use for | Quern visibility |
|-------|---------|-----------------|
| `.debug` | Verbose development traces, variable dumps | Captured but high volume — filter aggressively |
| `.info` | Routine operations (request started, cache hit) | Good default for most app events |
| `.notice` | Significant events worth seeing in production (login, navigation, state changes) | **Recommended default** — survives aggressive level filters |
| `.error` | Failures that need attention (network errors, decode failures) | Always visible in `get_errors` |
| `.fault` | Invariant violations, "this should never happen" | Always visible, triggers crash-adjacent alerts |

**Recommended default: `.notice`**. It's high enough to survive `min_level` filters but doesn't imply something is wrong. Use `.info` for chatty operational logs, `.error` only for actual failures.

### Mark strings as .public for debugging

By default, the unified logging system redacts dynamic string interpolations as `<private>` on physical devices. This makes logs useless for debugging. Mark values you need to see:

```swift
// BAD — shows as "<private>" on device
logger.notice("User tapped \(buttonName)")

// GOOD — visible in Quern logs
logger.notice("User tapped \(buttonName, privacy: .public)")
```

**When to use `.public`**: Any value you'd want to see while debugging — URLs, view names, error messages, state descriptions. Don't mark genuinely sensitive data (auth tokens, passwords, PII) as public.

### Structure your subsystems for filtering

Use a consistent subsystem hierarchy so agents can filter at the right granularity:

```swift
// Top-level subsystem for the app
Logger(subsystem: "com.example.myapp", category: "general")

// Feature-specific loggers
Logger(subsystem: "com.example.myapp", category: "networking")
Logger(subsystem: "com.example.myapp", category: "auth")
Logger(subsystem: "com.example.myapp", category: "ui")
```

With this structure, an agent can filter to your entire app with `subsystems: ["com.example.myapp"]` or drill into a specific category with `query_logs(search: "auth")`.

### What Quern captures vs. what it doesn't

| Output method | Simulator | Physical device |
|--------------|-----------|-----------------|
| `os.Logger` / `Logger` | Captured | Captured |
| `NSLog()` | Captured | Captured (routes through unified logging on iOS 10+) |
| `print()` / `debugPrint()` | Not captured | Not captured |
| `dump()` | Not captured | Not captured |

If your app uses `print()` extensively and you can't change it immediately, consider adding a `freopen` redirect at app launch to route stdout to os_log — but migrating to `os.Logger` is the proper fix.

---

## Summary

1. **Think in structured data**, not visuals
2. **Verify state before acting**
3. **Summarize first, drill down second**
4. **Filter aggressively** — logs, flows, UI
5. **Use accessibility over coordinates**
6. **Correlate across sources** — logs + network + UI = full picture
7. **Let the server wait**, don't poll client-side
