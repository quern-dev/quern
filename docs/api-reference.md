# API Reference

Every MCP tool and the HTTP endpoint behind it, plus the endpoints that have no tool.
This is the authoritative list — the README links here rather than duplicating it, and
agents can read it over MCP as `quern://api-reference`.

## Authentication

All endpoints require `Authorization: Bearer <key>` except these public paths: `/`,
`/health`, `/api/v1/health`, `/tools`, `/docs`, `/redoc`, `/openapi.json`, `/video-test`,
and `/api/v1/proxy/cert`.

`/api/v1/proxy/cert` is deliberately unauthenticated — devices and simulators fetch the
mitmproxy CA certificate from it during setup, before they hold a key. It serves only the
public CA certificate; no traffic, logs, or device state are reachable without a key.

The key lives at `~/.quern/api-key`; the server's URL and port are in `~/.quern/state.json`.

## Tools and their endpoints

### Server and updates

| MCP Tool | Method | Path | Description |
|---|---|---|---|
| `ensure_server` | GET | `/api/v1/system/update-status` | Most recent persisted update-check result |
| `update_quern` | POST | `/api/v1/system/update` | Launch `quern update` in a detached child; returns immediately |
| `set_update_channel` | PUT | `/api/v1/system/channel` | Set the update channel (`stable` or `beta`) |

### Logs

| MCP Tool | Method | Path | Description |
|---|---|---|---|
| `tail_logs` | GET | `/api/v1/logs/query` | Query logs with filters and pagination |
| `query_logs` | GET | `/api/v1/logs/query` | Query logs with filters and pagination |
| `get_log_summary` | GET | `/api/v1/logs/summary` | LLM-optimized summary with cursor support |
| `get_errors` | GET | `/api/v1/logs/errors` | Errors and crashes only |
| `get_build_result` | GET | `/api/v1/builds/latest` | Most recent build result |
| `parse_build_output` | POST | `/api/v1/builds/parse-file` | Parse a build log file from disk |
| `get_latest_crash` | GET | `/api/v1/crashes/latest` | Recent parsed crash reports |
| `set_log_filter` | POST | `/api/v1/logs/filter` | Reconfigure capture filters |
| `get_log_filter` | GET | `/api/v1/logs/filter` | Current ingestion filter config at all scopes (global, per-source, per-device) |
| `list_log_sources` | GET | `/api/v1/logs/sources` | Active log source adapters |
| `start_simulator_logging` | POST | `/api/v1/device/logging/start` | Start simulator log capture |
| `stop_simulator_logging` | POST | `/api/v1/device/logging/stop` | Stop simulator log capture |
| `start_device_logging` | POST | `/api/v1/device/logging/device/start` | Start physical device log capture |
| `stop_device_logging` | POST | `/api/v1/device/logging/device/stop` | Stop physical device log capture |
| `start_oslog_streaming` | POST | `/api/v1/logs/oslog/start` | Start streaming the host Mac's unified log |
| `stop_oslog_streaming` | POST | `/api/v1/logs/oslog/stop` | Stop host oslog streaming |

### Network proxy

| MCP Tool | Method | Path | Description |
|---|---|---|---|
| `query_flows` | GET | `/api/v1/proxy/flows` | Query captured flows |
| `get_flow_detail` | GET | `/api/v1/proxy/flows/{id}` | Full flow detail |
| `wait_for_flow` | POST | `/api/v1/proxy/flows/wait` | Block until a matching flow appears, or time out |
| `get_flow_summary` | GET | `/api/v1/proxy/flows/summary` | Traffic digest |
| `start_capture_session` | POST | `/api/v1/proxy/capture/start` | Start a capture session to bracket a UI action |
| `stop_capture_session` | POST | `/api/v1/proxy/capture/stop` | Stop the session and return only the flows from that window |
| `proxy_status` | GET | `/api/v1/proxy/status` | Proxy status and config |
| `start_proxy` | POST | `/api/v1/proxy/start` | Start the proxy |
| `stop_proxy` | POST | `/api/v1/proxy/stop` | Stop the proxy |
| `proxy_setup_guide` | GET | `/api/v1/proxy/setup-guide` | Device setup instructions |
| `verify_proxy_setup` | POST | `/api/v1/proxy/cert/verify` | Verify CA cert installation (defaults to booted simulators) |
| `install_proxy_cert` | POST | `/api/v1/proxy/cert/install` | Install CA certificate on simulators and emulators |
| `record_device_proxy_config` | POST | `/api/v1/proxy/device-proxy-config` | Record a physical device's Wi-Fi proxy config (per SSID) |
| `set_local_capture` | POST | `/api/v1/proxy/local-capture` | Set local capture process list |
| `set_bypass` | POST | `/api/v1/proxy/bypass` | Add domain patterns to the bypass allowlist |
| `clear_bypass` | DELETE | `/api/v1/proxy/bypass` | Remove bypass patterns, or clear all |
| `configure_system_proxy` | POST | `/api/v1/proxy/configure-system` | Auto-configure macOS system proxy |
| `unconfigure_system_proxy` | POST | `/api/v1/proxy/unconfigure-system` | Restore original proxy settings |

### Intercept and mock

| MCP Tool | Method | Path | Description |
|---|---|---|---|
| `set_intercept` | POST | `/api/v1/proxy/intercept` | Set intercept pattern |
| `clear_intercept` | DELETE | `/api/v1/proxy/intercept` | Clear intercept |
| `list_held_flows` | GET | `/api/v1/proxy/intercept/held` | List held flows |
| `release_flow` | POST | `/api/v1/proxy/intercept/release` | Release a held flow |
| `replay_flow` | POST | `/api/v1/proxy/replay/{id}` | Replay a captured flow |
| `set_mock` | POST | `/api/v1/proxy/mocks` | Add mock rule |
| `list_mocks` | GET | `/api/v1/proxy/mocks` | List mock rules |
| `update_mock` | PATCH | `/api/v1/proxy/mocks/{id}` | Update a mock rule's pattern and/or response |
| `clear_mocks` | DELETE | `/api/v1/proxy/mocks/{id}` | Delete a specific mock rule |

### Device

| MCP Tool | Method | Path | Description |
|---|---|---|---|
| `list_devices` | GET | `/api/v1/device/list` | List simulators, emulators, and physical devices |
| `boot_device` | POST | `/api/v1/device/boot` | Boot simulator |
| `shutdown_device` | POST | `/api/v1/device/shutdown` | Shutdown simulator |
| `erase_device` | POST | `/api/v1/device/erase` | Erase a simulator, resetting it to factory state |
| `install_app` | POST | `/api/v1/device/app/install` | Install app |
| `launch_app` | POST | `/api/v1/device/app/launch` | Launch app |
| `terminate_app` | POST | `/api/v1/device/app/terminate` | Terminate app |
| `uninstall_app` | POST | `/api/v1/device/app/uninstall` | Uninstall app |
| `list_apps` | GET | `/api/v1/device/app/list` | List installed apps |
| `build_and_install` | POST | `/api/v1/device/build-and-install` | Build an Xcode scheme and install it on one or more devices |

### UI interaction

| MCP Tool | Method | Path | Description |
|---|---|---|---|
| `get_ui_tree` | GET | `/api/v1/device/ui` | Accessibility tree |
| `get_element_state` | GET | `/api/v1/device/ui/element` | Query specific element state |
| `wait_for_element` | POST | `/api/v1/device/ui/wait-for-element` | Poll until element appears |
| `get_screen_summary` | GET | `/api/v1/device/screen-summary` | LLM-optimized screen description |
| `tap` | POST | `/api/v1/device/ui/tap` | Tap at coordinates |
| `tap_element` | POST | `/api/v1/device/ui/tap-element` | Tap element by label/identifier |
| `swipe` | POST | `/api/v1/device/ui/swipe` | Swipe gesture |
| `scroll_to_element` | POST | `/api/v1/device/ui/scroll-to-element` | Scroll a container until the target is in view, without tapping it |
| `get_web_content` | POST | `/api/v1/device/ui/web-content` | Read WKWebView content the accessibility tree cannot see (iOS simulator only) |
| `wait_for_settle` | POST | `/api/v1/device/ui/wait-settled` | Wait until the screen stops changing, by comparing successive screenshots |
| `type_text` | POST | `/api/v1/device/ui/type` | Type text |
| `clear_text` | POST | `/api/v1/device/ui/clear` | Clear text field |
| `press_button` | POST | `/api/v1/device/ui/press` | Press hardware button |

### Screenshots and preview

| MCP Tool | Method | Path | Description |
|---|---|---|---|
| `take_screenshot` | GET | `/api/v1/device/screenshot` | Capture screenshot |
| `take_annotated_screenshot` | GET | `/api/v1/device/screenshot/annotated` | Screenshot with accessibility overlays |
| `start_screenshot_timeline` | POST | `/api/v1/device/screenshot/timeline/start` | Auto-capture a screenshot after every UI action |
| `stop_screenshot_timeline` | POST | `/api/v1/device/screenshot/timeline/stop` | Stop the timeline and return its manifest |
| `get_screenshot_timeline` | GET | `/api/v1/device/screenshot/timeline` | Manifest of the active timeline, without stopping it |
| `preview_device` | POST | `/api/v1/device/preview/start` | Add a device preview (or all devices if no UDID) |
| `stop_preview` | POST | `/api/v1/device/preview/stop` | Remove a device preview (or stop all if no UDID) |
| `preview_status` | GET | `/api/v1/device/preview/status` | Per-device preview state and available devices |

### Device configuration

| MCP Tool | Method | Path | Description |
|---|---|---|---|
| `set_location` | POST | `/api/v1/device/location` | Set GPS location |
| `open_url` | POST | `/api/v1/device/open-url` | Open a URL via the platform's default handler (Android can target a package) |
| `grant_permission` | POST | `/api/v1/device/permission` | Grant app permission |
| `set_locale` | POST | `/api/v1/device/locale` | Set the system locale (Android) |
| `set_hardware_keyboard` | POST | `/api/v1/device/keyboard` | Attach/detach the simulated hardware keyboard (iOS simulators) |
| `set_font_scale` | POST | `/api/v1/device/font-scale` | Set the font scale (Android) |
| `set_display_density` | POST | `/api/v1/device/display-density` | Set the display density / DPI (Android) |

### App state and plist

| MCP Tool | Method | Path | Description |
|---|---|---|---|
| `save_app_state` | POST | `/api/v1/device/app/state/save` | Save a named checkpoint (data container + app groups, optionally the keychain) |
| `restore_app_state` | POST | `/api/v1/device/app/state/restore` | Restore a named checkpoint |
| `list_app_states` | GET | `/api/v1/device/app/state/list` | List saved checkpoints for a bundle ID |
| `delete_app_state` | DELETE | `/api/v1/device/app/state/{id}` | Delete a named checkpoint |
| `read_app_plist` | GET | `/api/v1/device/app/state/plist` | Read a plist, or a single key, from an app container |
| `set_app_plist_value` | POST | `/api/v1/device/app/state/plist` | Set a plist key |
| `set_app_plist_values` | POST | `/api/v1/device/app/state/plist/batch` | Set multiple plist keys in one call |
| `diff_app_plist` | GET | `/api/v1/device/app/state/plist/diff` | Compare a live plist against a saved checkpoint |
| `delete_app_plist_key` | DELETE | `/api/v1/device/app/state/plist/key` | Remove a key from a plist |
| `start_plist_watch` | POST | `/api/v1/device/app/state/plist/watch/start` | Poll a plist and emit per-key changes as log entries |
| `stop_plist_watch` | POST | `/api/v1/device/app/state/plist/watch/stop` | Stop polling a plist |
| `configure_plist_watch` | POST | `/api/v1/device/app/state/plist/watch/configure` | Save a persistent watch config for a bundle ID |
| `get_plist_watch_config` | GET | `/api/v1/device/app/state/plist/watch/config` | Read the persistent watch configuration |
| `unconfigure_plist_watch` | DELETE | `/api/v1/device/app/state/plist/watch/configure` | Remove a persistent watch config |

### Device pool

| MCP Tool | Method | Path | Description |
|---|---|---|---|
| `resolve_device` | POST | `/api/v1/devices/resolve` | Resolve a device by criteria *or* by explicit `udid` (sets active device) |
| `ensure_devices` | POST | `/api/v1/devices/ensure` | Ensure N devices matching criteria are booted |

### App knowledge and landmarks

| MCP Tool | Method | Path | Description |
|---|---|---|---|
| `init_app_knowledge` | — | — | Scaffolds or detects a `.quern/knowledge/` directory on disk; performs no server call |
| `load_landmarks` | POST | `/api/v1/landmarks/load` | Load landmarks from a knowledge base path or inline JSON. Returns `screens` count and a categorized `skipped[]` array (legacy-format files, stubs, malformed YAML). |
| `identify_screen` | POST | `/api/v1/landmarks/identify` | Match the live UI tree against loaded landmarks. Returns matched screen, confidence, and full per-landmark detail in `partial_matches`. |
| `list_landmarks` | GET | `/api/v1/landmarks` | List loaded landmark sets per app |
| `unload_landmarks` | DELETE | `/api/v1/landmarks` | Unload landmarks for an app or all apps |
| `validate_landmarks` | POST | `/api/v1/landmarks/validate` | Detect collisions between screens with overlapping landmark sets |

### Physical device (WDA)

| MCP Tool | Method | Path | Description |
|---|---|---|---|
| `setup_wda` | POST | `/api/v1/device/wda/setup` | Build and install WDA on physical device |
| `start_driver` | POST | `/api/v1/device/wda/start` | Start WDA driver |
| `stop_driver` | POST | `/api/v1/device/wda/stop` | Stop WDA driver |

## Endpoints with no MCP tool

Reachable over HTTP only — streaming endpoints (an MCP tool can't hold an SSE connection),
public probes, and a few operations the CLI uses directly.

| Method | Path | Description |
|---|---|---|
| DELETE | `/api/v1/proxy/mocks` | Clear all mock rules |
| GET | `/` | Redirects to `/docs` (public) |
| GET | `/api/v1/device/preview/devices` | List CoreMediaIO preview devices |
| GET | `/api/v1/device/video` | Live MJPEG video stream |
| GET | `/api/v1/health` | Same as `/health` (public) |
| GET | `/api/v1/logs/stream` | SSE real-time log stream |
| GET | `/api/v1/proxy/bypass` | List bypass patterns |
| GET | `/api/v1/proxy/cert` | Download CA certificate (public — see note above) |
| GET | `/api/v1/proxy/cert/status` | Check certificate installation status |
| GET | `/api/v1/proxy/flows/stream` | SSE real-time flow stream |
| GET | `/api/v1/system/channel` | Current update channel preference |
| GET | `/health` | Fast liveness ping (public). Does no device-tool probing — kept sub-millisecond so CLI health checks can't time out |
| GET | `/tools` | Device-tool availability and UI cache stats (public). Backs `quern doctor` and `quern status` |
| GET | `/video-test` | Internal preview test page (public) |
| POST | `/api/v1/builds/parse` | Submit xcodebuild output |
| POST | `/api/v1/device/active` | Set the active device by UDID |
| POST | `/api/v1/devices/refresh` | Refresh pool from simctl |
| POST | `/api/v1/proxy/filter` | Set proxy capture filters |
| POST | `/api/v1/proxy/intercept/release-all` | Release all held flows |
