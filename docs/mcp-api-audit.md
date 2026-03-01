# MCP & API Audit — Agent Debugging Workflow Gaps

*2026-02-22 — updated 2026-02-28*

Audit of the Quern MCP tool surface and HTTP API, evaluated against typical agent debugging workflows for iOS development.

## Current Tool Inventory

- **Logs & Diagnostics**: 12 tools (tail, query, summary, errors, crashes, build result, log sources, filters, simulator/device logging)
- **Network Proxy**: 16 tools (status, start/stop, system proxy, local capture, flows, intercept, mocks, replay)
- **Device Control**: 13 tools (list, boot/shutdown, install/launch/terminate/uninstall, screenshot, location, permissions, buttons, build_and_install)
- **UI Automation**: 9 tools (ui tree, element state, wait for element, screen summary, tap, tap element, swipe, type, clear)
- **Device Pool**: 5 tools (list, claim, release, resolve, ensure)
- **App State**: 7 tools (save, restore, list, delete checkpoints; read/set/delete plist keys)
- **WDA (Physical)**: 3 tools (setup, start/stop driver)

---

## Critical Gaps

### ~~1. No build tool~~ ✅ Done

~~Every debug cycle requires a rebuild, but there's no MCP tool to trigger `xcodebuild` directly.~~ Added `build_and_install` MCP tool — builds an Xcode scheme and installs on one or more devices/simulators. Handles mixed physical+simulator targets by building once per architecture concurrently. Build output parsing also available via `get_build_result` and `parse_build_output`.

### ~~2. No `install_proxy_cert` MCP tool~~ ✅ Done

~~The HTTP endpoint exists (`POST /api/v1/proxy/cert/install`) but isn't exposed via MCP.~~ Added in `2e07d37`. Also clarified that local capture mode doesn't require simulator reboot after cert install.

### ~~3. No "wait for flow" tool~~ ✅ Done

~~After triggering a UI action, agents must poll `query_flows` to see the resulting network request.~~ Added `wait_for_flow` MCP tool with server-side polling, auto `since` lookback, and all flow filter parameters. Exposed via `POST /api/v1/proxy/flows/wait`.

### ~~4. Physical device `launch_app` doesn't use WDA~~ ✅ Done

~~`launch_app` uses `devicectl` for physical devices, which opens the app outside WDA's session context. WDA can't see or interact with the app afterward.~~ `launch_app` and `terminate_app` now use WDA's `activate` and `terminate` endpoints for physical devices, with devicectl fallback when WDA isn't available.

---

## Medium Gaps

### ~~5. No app data reset~~ ✅ Done

~~Can't clear caches, UserDefaults, or databases between test runs.~~ Full app state checkpoint system implemented: `save_app_state`, `restore_app_state`, `list_app_states`, `delete_app_state`. Also added plist inspection/mutation: `read_app_plist`, `set_app_plist_value`, `delete_app_plist_key`. Simulator only. Agents can save a clean-state checkpoint and restore it between test runs.

### ~~6. No annotated screenshot MCP tool~~ ✅ Done

~~`GET /api/v1/device/screenshot/annotated` overlays accessibility information on screenshots. Exists in HTTP API but not exposed via MCP.~~ Added `take_annotated_screenshot` MCP tool wrapping the existing endpoint.

### 7. `set_log_filter` is a stub

The MCP tool exists but the HTTP handler returns "Filter reconfiguration not yet implemented." Confusing for agents that try to use it. Should either be implemented or removed.

### ~~8. No mock update~~ ✅ Done

~~To change a mock response, agents must `clear_mocks(rule_id=...)` then `set_mock` again.~~ Added `update_mock` MCP tool — takes `rule_id` plus optional `pattern`, `status_code`, `headers`, `body`. Exposed via `PATCH /api/v1/proxy/mocks/{rule_id}`. Fixed race condition where `list_mocks` returned empty after `update_mock` — the addon's status echo was wiping the adapter's rule mirror. Fix: per-rule echoes are now ignored (caller is the single writer).

### 9. HTTP endpoints without MCP tools

| HTTP Endpoint | Purpose |
|---|---|
| ~~`POST /builds/parse`~~ | ~~Submit xcodebuild output for parsing~~ ✅ (via `parse_build_output` / `POST /builds/parse-file`) |
| ~~`POST /proxy/cert/install`~~ | ~~Install CA cert on simulator(s)~~ ✅ |
| `GET /proxy/cert/status` | Cert installation status (cached) |
| ~~`GET /device/screenshot/annotated`~~ | ~~Screenshot with accessibility overlays~~ ✅ |
| `POST /devices/cleanup` | Clean up stale device claims |
| `POST /devices/refresh` | Refresh pool from simctl |

---

## Nice to Have

### 10. No UI diff between snapshots
No way to compare two UI tree snapshots to see what changed. Agents must call `get_ui_tree` twice and diff manually.

### 11. No scroll-until-found
If an element is off-screen, the agent must manually swipe and re-check. A `scroll_to_element(label, direction)` compound action would help.

### 12. No CPU/memory/FPS metrics
No access to Instruments-level performance data. Would require substantial implementation (Instruments integration or DTXConnection).

### 13. No single "restart app" command
Must `terminate_app` then `launch_app`. Minor friction.

### 14. No timing/measurement tool
No way to measure "how long did this screen take to load" or "time between tap and UI update." Agents can approximate with timestamps between tool calls, but that includes MCP overhead.

---

## Workflow Coverage Summary

| Workflow | Coverage | Blocker |
|---|---|---|
| App crashes on launch | Good | ~~No build tool~~ ✅, no wait-for-crash |
| API returns wrong data | Good | ~~No wait-for-flow~~ ✅ |
| UI element not appearing | Good | ~~Annotated screenshot not in MCP~~ ✅ |
| Different API responses | Good | ~~No app data reset~~ ✅ |
| Fix bug and verify | Good | ~~No build tool~~ ✅ |
| Performance issue | Partial | No metrics, no timing |

---

## Priority Recommendation

1. ~~**Build integration** — unblocks the most common workflow~~ ✅
2. ~~**`install_proxy_cert` MCP tool** — low effort, wraps existing endpoint~~ ✅
3. ~~**`wait_for_flow` tool** — completes the network debugging loop~~ ✅
4. ~~**WDA activate for physical devices** — spec already written (`docs/wda-activate-app-spec.md`)~~ ✅
5. ~~**Annotated screenshot MCP tool** — low effort, wraps existing endpoint~~ ✅
6. **Fix or remove `set_log_filter` stub** — avoid confusing agents
