# Quern Agent Guide

**For**: AI agents using Quern MCP tools for mobile app debugging and testing

This page is the overview: how to start, the principles behind the tools, and which tool fits which job. The detail lives in topic guides, and each topic is split into short pages, one per subject, each its own MCP resource. A topic's address (`quern://guide/ui`) is its contents list: one line per page saying what it answers. The table below names the exact page for each common job, so you can usually go straight there.

| Read | When |
|---|---|
| `quern://guide/ui` | Reading the screen, tapping, gestures, web views, scrolling to an element, landmarks and screen identification |
| `quern://guide/network` | Capturing traffic, local capture and the system proxy, certificates, mocks, intercepts, a slow network on a simulator |
| `quern://guide/devices` | Choosing a device, physical iPhones and WDA, live preview, app state and plists, device settings, foldables, builds |
| `quern://guide/logs` | App logs and whether quern can see them, crashes, recordings, `get_trace`, screenshot timelines |
| `quern://guide/performance` | CPU and memory, hangs and janky frames, profiles, thread stacks, a slow network or a hot phone |
| `quern://guide/sharing` | A server other agents use too, reservations, client keys, other Macs (`use_remote`) |
| `quern://app-knowledge-guide` | Building or updating an app's knowledge base with the user |
| `quern://troubleshooting` | iOS error patterns in app logs, and reading crash reports |
| `quern://api-reference` | Every tool's HTTP endpoint, when calling the REST API directly |

---

## Quick Start Checklist

Every Quern session should start with:

1. `resolve_device` — get a device to work with
2. `get_screen_summary` — see what's on screen
3. `proxy_status` — check if network capture is active

From there, use the right tool for your task. If any tool fails with a connection error, call `ensure_server` to verify the server is running and auto-start it if needed.

---

## Core Principles

1. **Prefer structured data for decisions.** `get_screen_summary` for a curated description, `get_ui_tree` for the full hierarchy. Screenshots are for checking what something looks like, not for deciding what to tap.
2. **Prefer accessibility over coordinates.** `tap_element` with a label and an `element_type` survives layout changes; a coordinate `tap` is for gestures that aren't tied to an element. A refused tap says why and what to do (`quern://guide/ui`).
3. **Summarize first, drill down second.** Start with `get_log_summary`, `get_flow_summary` and `get_screen_summary`, then filter to what they showed you.
4. **Verify state before acting.** Don't assume the proxy is running or an element exists: check, since state changes between calls.
5. **Let the server wait.** `wait_for_element`, `wait_for_flow`, `wait_for_settle` and `list_held_flows` with a `timeout` instead of polling in a loop.
6. **Filter aggressively.** Logs by level, process and text; flows by host, method and status; the UI tree with `max_elements` or `children_of`. The summary tools return a `cursor`: pass it back as `since_cursor` for only what's new.
7. **Correlate across sources.** Humans struggle to correlate millisecond-level timing across logs, network, and UI. You don't. After an action, read logs, flows and the screen for the same narrow window, or let `get_trace` join them for you.

---

## Which tool for which job

| You want to | Reach for | Read |
|---|---|---|
| See what's on screen | `get_screen_summary`, then `get_ui_tree`; `take_annotated_screenshot` when an element can't be found | `quern://guide/ui/reading` |
| Read a web view | `get_web_content` (iOS simulators; elsewhere the UI tree covers it) | `quern://guide/ui/debugging` |
| Tap, type, swipe | `tap_element`, `type_text` (after `clear_text`), `swipe`, `gesture` | `quern://guide/ui/tapping` |
| Reach an element off screen | `tap_element` (sweeps when the screen scrolls), `scroll_to_element` | `quern://guide/ui/off-screen` |
| Know which screen you're on | `identify_screen`, or `get_screen_summary(identify=true)` | `quern://guide/ui/landmarks` |
| Take a screenshot after every action | `start_screenshot_timeline` / `stop_screenshot_timeline` | `quern://guide/logs/screenshot-timelines` |
| See network traffic | `get_flow_summary`, `query_flows`, `get_flow_detail`; bracket an action with `start_capture_session`; capture an app by process name with `add_session_capture` (every simulator's, so filter by `simulator_udid`) | `quern://guide/network/capture` |
| Capture a physical iPhone's traffic | `proxy_setup_guide`, `record_device_proxy_config`, then `wait_for_flow` (ask before installing the CA) | `quern://guide/devices/physical-proxy` |
| Fake a response | `set_mock`, `update_mock` | `quern://guide/network/mocks` |
| Hold or change a live request | `set_intercept`, `list_held_flows`, `release_flow`, `clear_intercept` | `quern://guide/network/intercept` |
| Read app logs | `get_log_summary`, `query_logs`, `tail_logs`, `get_errors` | `quern://guide/logs/reading` |
| See what an action caused | `get_trace` | `quern://guide/logs/trace` |
| Investigate a crash | `get_latest_crash`, then logs and flows around it | `quern://guide/logs/crashes` |
| Keep a whole run | `start_recording` / `stop_recording` | `quern://guide/logs/bug-reports` |
| Measure an app | `get_performance`, `start_profile` / `stop_profile`, `get_thread_stacks` | `quern://guide/performance/which-tool` |
| Test under a bad network or a hot phone | `set_device_condition` (physical iPhones) | `quern://guide/performance/device-conditions` |
| Slow the network on a simulator | hold requests with `set_intercept`, or the Mac's Network Link Conditioner (ask first) | `quern://guide/network/slow-network` |
| Get a device | `resolve_device`, `ensure_devices` | `quern://guide/devices/pool` |
| Install, launch or stop an app | `install_app`, `launch_app`, `terminate_app` | `quern://guide/devices/controlling` |
| Build and install an app | `build_and_install` | `quern://guide/devices/builds` |
| Change location, permissions or locale | `set_location`, `grant_permission`, `set_simulator_setting`; `set_locale` (Android and physical iPhones, not simulators) | `quern://guide/devices/settings` |
| Start an app from a known state | `save_app_state` / `restore_app_state`, plist tools (simulators) | `quern://guide/devices/checkpoints` |
| Share a server with other agents | `reserve_devices`, `release_devices` | `quern://guide/sharing/shared-server` |
| Use another Mac's server | `list_remotes`, `use_remote` | `quern://guide/sharing/remote-servers` |

---

## Common Mistakes

**Not calling `ensure_server` when tools fail** — If tools fail with connection errors, call `ensure_server` — it checks server health and auto-starts if needed.

**Using only screenshots to understand UI state** — Screenshots work, but `get_screen_summary` and `get_ui_tree` are faster, cheaper, and return structured data you can act on directly. Use screenshots to complement structured data, not replace it.

**Not filtering logs/flows** — Unfiltered queries return overwhelming amounts of data. Always filter by level, process, host, status code, or search text.

**Client-side polling instead of server-side waiting** — Use `wait_for_element` instead of looping on `get_ui_tree`. Use `wait_for_flow` instead of polling `query_flows` after triggering a UI action. Use `list_held_flows` with a timeout instead of polling for intercepted flows.

**Retrying a refusal.** Many answers that look like errors are answers: a launch that can't start on this runtime, a proxy refusal over an untrusted certificate, a tap refused as obstructed, a 403 that is the server owner's setting. The response says what to do instead. Read it before calling again.

---

## Troubleshooting

**Tools missing or misbehaving?** `quern doctor` reports device-tool availability and venv sync, plus the version, provenance and upgrade command for the tools quern tracks as install sites (`pymobiledevice3`, `idb`, `mitmproxy`, `adb`, `libimobiledevice`, `node`, and quern's own `trace_processor` for Android profiles). `simctl` and `devicectl` ship inside Xcode and are reported as available or not, without version detail, and `GET /tools` returns the
same data over HTTP. Both are deliberately kept off the `/health` path so the
liveness probe stays sub-millisecond — do not expect `/health` to tell you
whether a tool is installed.

`/tools` is read-only about your machine, but it is not inert about the server:
it re-checks which backend serves simulator UI automation and adopts the answer.
That is the point — it means an Xcode upgrade under a running server is noticed
the next time anyone asks, rather than leaving every tap routed to a backend
that stopped working. Each probe is bounded, so a wedged tool no longer holds up
the response.

A tool reported as unavailable may be installed but not answering; the two are
not yet distinguishable in this response. If one is unexpectedly missing, check
`~/.quern/server.log` for a probe timeout before assuming it is uninstalled.

The UI, network and devices guides end with what goes wrong in their area: a tap that misses in `quern://guide/ui`, no flows captured in `quern://guide/network`, an empty screen summary on a physical device in `quern://guide/devices`.

---

## Calling the REST API directly

Every tool's method and path, and the endpoints with no tool (SSE streams, public probes), are in `quern://api-reference`.
Everything needs `Authorization: Bearer <key>` from `~/.quern/api-key` (or a
client key from `quern clients add`, which also needs `X-Quern-Session` on
every request), except `/`, `/health`, `/api/v1/health`, `/tools`, `/docs`,
`/redoc`, `/openapi.json`, `/api/v1/proxy/cert`, and the two pairing routes a client calls before it has a key,
`POST /api/v1/remote/pair/hello` and `POST /api/v1/remote/pair/finish`.
