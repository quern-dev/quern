# Quern Agent Guide

**For**: AI agents using Quern MCP tools for mobile app debugging and testing
**Last Updated**: February 20, 2026

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

1. `ensure_server` — start or verify Quern is running
2. `resolve_device` — get a device to work with
3. `get_screen_summary` — see what's on screen
4. `proxy_status` — check if network capture is active

From there, use the right tool for your task.

---

## Core Principles

### 1. Prefer Structured Data for Decision-Making

Use `get_screen_summary` for a curated text description or `get_ui_tree` for the full accessibility hierarchy. These are cheaper, faster, and easier to reason about programmatically than screenshots.

Screenshots are still useful — for verifying visual layout, catching rendering bugs, documenting state for humans, or when you need to see something the accessibility tree doesn't capture. Use both, but reach for structured data first when you need to make decisions or find elements to interact with.

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

Similarly, `list_held_flows` supports a `timeout` parameter for long-polling — use it instead of repeatedly checking for intercepted flows.

---

### 6. Filter Aggressively

Logs, network flows, and UI trees can be huge. Always filter to what you need.

**Logs**: Filter by `level`, `process`, `search` text, and time range. Don't fetch 1,000 entries when 50 filtered ones will do.

**Flows**: Filter by `host`, `method`, `path_contains`, and `status_min`/`status_max`. Use `get_flow_summary` first to identify which hosts or patterns to investigate.

**UI Tree**: Use `get_screen_summary` with a reasonable `max_elements` limit. If you need detail, scope `get_ui_tree` with `children_of` to a specific container rather than fetching the full 500+ element hierarchy.

---

## Common Workflows

### Debugging Network Issues

1. Call `ensure_server`, then check `proxy_status` — start the proxy if it isn't running
2. Get a baseline with `get_flow_summary` to see current traffic patterns
3. Trigger the issue (tap a button, navigate to a screen)
4. Query for relevant flows — filter by method, path, or status code
5. Use `get_flow_detail` on the specific flow to inspect headers, request body, and response body

**Key insight**: Start with summary, trigger action, drill down to specific flows.

**Certificate verification**: If no flows are captured, verify the proxy certificate is installed on the simulator:
1. Call `verify_proxy_setup` — performs a ground-truth check by querying the simulator's TrustStore database. Defaults to **booted simulators only**; pass `state="all"` or `device_type="device"` to check shutdown sims or physical devices
2. Returns per-device `status`: `installed`, `not_installed`, `never_booted`, or `error`
3. Returns `erased_devices` — UDIDs where a previously installed cert is now missing (probable device erase)
4. If cert is missing, install it with: `xcrun simctl keychain <udid> add-root-cert ~/.mitmproxy/mitmproxy-ca-cert.pem`

---

### Debugging UI Issues

1. Call `get_screen_summary` to see current state
2. Trigger the issue (tap, swipe, type)
3. Call `get_screen_summary` again to see what changed
4. If the result is unexpected, use `get_ui_tree` (optionally scoped with `children_of`) to inspect the full hierarchy

**Key insight**: Use summary for quick checks, full tree when you need details.

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

**"I need to tap/interact with UI"**
- Known element: `tap_element` with label and element_type
- Coordinates (rare): `tap`
- Gesture: `swipe`
- Text input: focus the element, then `type_text`

**"I need to see network traffic"**
- Overview: `get_flow_summary`
- Specific requests: `query_flows` with filters
- Full detail: `get_flow_detail`
- Modify traffic: `set_intercept` + `release_flow` with modifications
- Mock responses: `set_mock`

**"I need to see logs"**
- Recent activity: `tail_logs`
- Overview: `get_log_summary`
- Specific search: `query_logs` with filters
- Errors only: `get_errors`

**"I need to control the device"**
- Boot: `boot_device` or `resolve_device` with auto_boot
- Install app: `install_app`
- Launch app: `launch_app`
- Screenshot: `take_screenshot`
- Location: `set_location`
- Permissions: `grant_permission`

---

## REST API Reference

When calling the HTTP API directly (without MCP), use these paths:

| MCP Tool             | HTTP Method | REST Path                              |
|----------------------|-------------|----------------------------------------|
| `ensure_server`      | GET         | `/health`                              |
| `tail_logs`          | GET         | `/api/v1/logs/query`                   |
| `query_logs`         | GET         | `/api/v1/logs/query`                   |
| `get_log_summary`    | GET         | `/api/v1/logs/summary`                 |
| `get_errors`         | GET         | `/api/v1/logs/errors`                  |
| `get_build_result`   | GET         | `/api/v1/builds/latest`                |
| `get_latest_crash`   | GET         | `/api/v1/crashes/latest`               |
| `set_log_filter`     | POST        | `/api/v1/logs/filter`                  |
| `list_log_sources`   | GET         | `/api/v1/logs/sources`                 |
| `query_flows`        | GET         | `/api/v1/proxy/flows`                  |
| `get_flow_detail`    | GET         | `/api/v1/proxy/flows/{id}`             |
| `get_flow_summary`   | GET         | `/api/v1/proxy/flows/summary`          |
| `proxy_status`       | GET         | `/api/v1/proxy/status`                 |
| `verify_proxy_setup` | POST        | `/api/v1/proxy/cert/verify`            |
| `start_proxy`        | POST        | `/api/v1/proxy/start`                  |
| `stop_proxy`         | POST        | `/api/v1/proxy/stop`                   |
| `set_intercept`      | POST        | `/api/v1/proxy/intercept`              |
| `clear_intercept`    | DELETE      | `/api/v1/proxy/intercept`              |
| `list_held_flows`    | GET         | `/api/v1/proxy/intercept/held`         |
| `release_flow`       | POST        | `/api/v1/proxy/intercept/release`      |
| `replay_flow`        | POST        | `/api/v1/proxy/replay/{id}`            |
| `set_mock`           | POST        | `/api/v1/proxy/mock`                   |
| `list_mocks`         | GET         | `/api/v1/proxy/mock`                   |
| `clear_mocks`        | DELETE      | `/api/v1/proxy/mock`                   |
| `list_devices`       | GET         | `/api/v1/device/list`                  |
| `boot_device`        | POST        | `/api/v1/device/boot`                  |
| `shutdown_device`    | POST        | `/api/v1/device/shutdown`              |
| `install_app`        | POST        | `/api/v1/device/app/install`           |
| `launch_app`         | POST        | `/api/v1/device/app/launch`            |
| `terminate_app`      | POST        | `/api/v1/device/app/terminate`         |
| `list_apps`          | GET         | `/api/v1/device/app/list`              |
| `take_screenshot`    | GET         | `/api/v1/device/screenshot`            |
| `get_ui_tree`        | GET         | `/api/v1/device/ui`                    |
| `get_element_state`  | GET         | `/api/v1/device/ui/element`            |
| `wait_for_element`   | POST        | `/api/v1/device/ui/wait-for-element`   |
| `get_screen_summary` | GET         | `/api/v1/device/screen-summary`        |
| `tap`                | POST        | `/api/v1/device/ui/tap`                |
| `tap_element`        | POST        | `/api/v1/device/ui/tap-element`        |
| `swipe`              | POST        | `/api/v1/device/ui/swipe`              |
| `type_text`          | POST        | `/api/v1/device/ui/type`               |
| `clear_text`         | POST        | `/api/v1/device/ui/clear`              |
| `press_button`       | POST        | `/api/v1/device/ui/press`              |
| `set_location`       | POST        | `/api/v1/device/location`              |
| `grant_permission`   | POST        | `/api/v1/device/permission`            |
| `list_device_pool`   | GET         | `/api/v1/devices/pool`                 |
| `claim_device`       | POST        | `/api/v1/devices/claim`                |
| `release_device`     | POST        | `/api/v1/devices/release`              |
| `resolve_device`     | POST        | `/api/v1/devices/resolve`              |
| `ensure_devices`     | POST        | `/api/v1/devices/ensure`               |
| `start_simulator_logging` | POST   | `/api/v1/device/logging/start`         |
| `stop_simulator_logging`  | POST   | `/api/v1/device/logging/stop`          |
| `start_device_logging`    | POST   | `/api/v1/device/logging/device/start`  |
| `stop_device_logging`     | POST   | `/api/v1/device/logging/device/stop`   |

---

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

Use `set_mock` to return synthetic responses for specific endpoints. This lets you create reliable, repeatable test scenarios — fixed user data, specific error conditions, or edge-case payloads — without depending on backend state.

Mock rules take priority over intercept rules. Clear them with `clear_mocks` when done.

---

### Device Pool for Parallel Testing

Use `ensure_devices` to boot and claim multiple simulators at once, then run different test scenarios on each in parallel. Each claimed device is isolated — no other session can use it until you call `release_device`. Always release devices when done to avoid resource exhaustion.

**Default behavior**: `resolve_device` and `ensure_devices` default to `type="simulator"` to prevent accidentally targeting physical devices (which may not have your app installed). Pass `type="device"` explicitly to target physical devices.

---

## Common Mistakes

**Not calling `ensure_server` first** — Tools fail with connection errors. Always start with `ensure_server`.

**Using only screenshots to understand UI state** — Screenshots work, but `get_screen_summary` and `get_ui_tree` are faster, cheaper, and return structured data you can act on directly. Use screenshots to complement structured data, not replace it.

**Forgetting element_type when label is ambiguous** — `tap_element(label="Cancel")` might match a StaticText instead of the Button. Specify `element_type="Button"` when the label might not be unique.

**Not filtering logs/flows** — Unfiltered queries return overwhelming amounts of data. Always filter by level, process, host, status code, or search text.

**Hardcoding device UDIDs** — Use `resolve_device` with a name and let Quern find the right device. UDIDs differ across machines.

**Client-side polling instead of server-side waiting** — Use `wait_for_element` instead of looping on `get_ui_tree`. Use `list_held_flows` with a timeout instead of polling for intercepted flows.

**Not clearing text before typing** — Use `clear_text` before `type_text` when a field has pre-existing content. Otherwise you'll append to whatever's already there.

**Confusing `tail_logs` and `query_logs`** — Use `tail_logs` for "show me recent stuff" (defaults to 50, newest first). Use `query_logs` for searching with filters and time ranges.

**Ignoring log source names** — `device` = physical device logs (on-demand, via `start_device_logging`), `simulator` = simulator unified logging (on-demand, via `start_simulator_logging`), `crash` = crash reports, `build` = xcodebuild output, `proxy` = network traffic. Legacy: `syslog` = idevicesyslog (disabled by default, opt-in with `--syslog`), `oslog` = macOS unified log (disabled by default, opt-in with `--oslog`).

**Using mock when you need intercept (or vice versa)** — Mocks return instant synthetic responses for stable test fixtures. Intercept pauses real requests for ad-hoc inspection and modification. Mock rules take priority over intercept.

**Not checking idb availability** — Device management and screenshots use `simctl` (always available with Xcode). UI inspection and interaction (`get_ui_tree`, `tap`, `swipe`, `type_text`, `clear_text`, `press_button`) require `idb`. Check `list_devices` response for tool availability.

**Holding flows too long** — Held flows auto-release after 30 seconds to prevent hanging clients. Use `list_held_flows` with `timeout` for long-polling instead of rapid polling.

---

## Performance Tips

**Use summaries before full queries.** Summaries are cheap and curated. Use them to decide what to investigate, then make targeted queries.

**Limit result counts.** Fetch 50 entries, not 10,000. You can always query for more if needed.

**Use cursors for incremental updates.** `get_log_summary` and `get_flow_summary` return a cursor. Pass it back with `since_cursor` to get only new activity since your last call — critical for token efficiency. The continuous monitoring pattern: call the summary tool, save the cursor, and on each subsequent check pass `since_cursor` to get a lightweight delta instead of re-fetching everything.

**Scope UI tree queries.** Use `get_ui_tree` with `children_of` to fetch a subtree instead of the full hierarchy. Use `get_screen_summary` with a reasonable `max_elements` limit.

---

## Troubleshooting

**"No element found matching label"** — The element may not exist, the label may be wrong, or multiple elements match. Use `get_screen_summary` to see what's actually on screen, then refine your query with the exact label and an element_type.

**"Proxy not running"** — Check with `proxy_status` and call `start_proxy` if needed.

**"No flows captured"** — The proxy may not be running, the device may not be configured to route through it, or the app may use certificate pinning. Check `proxy_status`, then `proxy_setup_guide` for device configuration steps.

**"Wait for element timed out"** — The element may never have appeared (a bug or wrong expectation), the timeout may be too short, or the label may differ from what you expect. Check what actually appeared with `get_screen_summary`.

---

## Summary

1. **Think in structured data**, not visuals
2. **Verify state before acting**
3. **Summarize first, drill down second**
4. **Filter aggressively** — logs, flows, UI
5. **Use accessibility over coordinates**
6. **Correlate across sources** — logs + network + UI = full picture
7. **Let the server wait**, don't poll client-side
