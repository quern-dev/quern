# MCP & API Audit — Agent Debugging Workflow Gaps

*2026-02-22*

Audit of the Quern MCP tool surface and HTTP API, evaluated against typical agent debugging workflows for iOS development.

## Current Tool Inventory

- **Logs & Diagnostics**: 12 tools (tail, query, summary, errors, crashes, build result, log sources, filters, simulator/device logging)
- **Network Proxy**: 16 tools (status, start/stop, system proxy, local capture, flows, intercept, mocks, replay)
- **Device Control**: 12 tools (list, boot/shutdown, install/launch/terminate/uninstall, screenshot, location, permissions, buttons)
- **UI Automation**: 9 tools (ui tree, element state, wait for element, screen summary, tap, tap element, swipe, type, clear)
- **Device Pool**: 5 tools (list, claim, release, resolve, ensure)
- **WDA (Physical)**: 3 tools (setup, start/stop driver)

---

## Critical Gaps

### 1. No build tool (partially addressed)

Every debug cycle requires a rebuild, but there's no MCP tool to trigger `xcodebuild` directly.

The parser is now exposed:
- `get_build_result` reads the most recent parsed build
- `parse_build_output` MCP tool accepts a build log file path and returns structured errors/warnings/test results
- Agents run `xcodebuild ... 2>&1 > /tmp/build.log` via Bash, then call `parse_build_output(file_path="/tmp/build.log")`

**Still needed (intentionally deferred):**
- `build_app` tool: runs `xcodebuild build` with scheme/workspace — deferred because xcodebuild's flag surface area is massive and agents can already run it via Bash
- `run_tests` tool: runs `xcodebuild test` — same rationale

### ~~2. No `install_proxy_cert` MCP tool~~ ✅ Done

~~The HTTP endpoint exists (`POST /api/v1/proxy/cert/install`) but isn't exposed via MCP.~~ Added in `2e07d37`. Also clarified that local capture mode doesn't require simulator reboot after cert install.

### ~~3. No "wait for flow" tool~~ ✅ Done

~~After triggering a UI action, agents must poll `query_flows` to see the resulting network request.~~ Added `wait_for_flow` MCP tool with server-side polling, auto `since` lookback, and all flow filter parameters. Exposed via `POST /api/v1/proxy/flows/wait`.

### 4. Physical device `launch_app` doesn't use WDA

`launch_app` uses `devicectl` for physical devices, which opens the app outside WDA's session context. WDA can't see or interact with the app afterward. Should use WDA's `POST /session/{id}/wda/apps/activate` instead.

See: `docs/wda-activate-app-spec.md`

---

## Medium Gaps

### 5. No app data reset

Can't clear caches, UserDefaults, or databases between test runs. Important for mock-based testing where you need a clean slate. Possible approaches:
- Expose simctl app container path, let agent delete contents
- `reset_app_data(bundle_id, udid)` that removes the app's data container
- At minimum, `uninstall_app` + `install_app` works but is slow

### 6. No annotated screenshot MCP tool

`GET /api/v1/device/screenshot/annotated` overlays accessibility information on screenshots. Exists in HTTP API but not exposed via MCP. Very useful for visual debugging of "element not appearing" issues.

### 7. `set_log_filter` is a stub

The MCP tool exists but the HTTP handler returns "Filter reconfiguration not yet implemented." Confusing for agents that try to use it. Should either be implemented or removed.

### 8. No mock update

To change a mock response, agents must `clear_mocks(rule_id=...)` then `set_mock` again. An `update_mock(rule_id, ...)` would reduce friction for Workflow 4 (testing different API responses).

### 9. HTTP endpoints without MCP tools

| HTTP Endpoint | Purpose |
|---|---|
| ~~`POST /builds/parse`~~ | ~~Submit xcodebuild output for parsing~~ ✅ (via `parse_build_output` / `POST /builds/parse-file`) |
| ~~`POST /proxy/cert/install`~~ | ~~Install CA cert on simulator(s)~~ ✅ |
| `GET /proxy/cert/status` | Cert installation status (cached) |
| `GET /device/screenshot/annotated` | Screenshot with accessibility overlays |
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
| App crashes on launch | Good (after crash) | No build tool, no wait-for-crash |
| API returns wrong data | Good | ~~No wait-for-flow~~ ✅ |
| UI element not appearing | Good | Annotated screenshot not in MCP |
| Different API responses | Good | No app data reset |
| Fix bug and verify | Partial | **No build tool** |
| Performance issue | Partial | No metrics, no timing |

---

## Priority Recommendation

1. **Build integration** — unblocks the most common workflow
2. ~~**`install_proxy_cert` MCP tool** — low effort, wraps existing endpoint~~ ✅
3. ~~**`wait_for_flow` tool** — completes the network debugging loop~~ ✅
4. **WDA activate for physical devices** — spec already written
5. **Annotated screenshot MCP tool** — low effort, wraps existing endpoint
6. **Fix or remove `set_log_filter` stub** — avoid confusing agents
