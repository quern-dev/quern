# API Reference

Every MCP tool and the HTTP endpoint behind it, plus the endpoints that have no tool.
This is the authoritative list — the README links here rather than duplicating it, and
agents can read it over MCP as `quern://api-reference`.

## Authentication

All endpoints require `Authorization: Bearer <key>` except these public paths: `/`,
`/health`, `/api/v1/health`, `/tools`, `/docs`, `/redoc`, `/openapi.json`,
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
| `get_trace` | GET | `/api/v1/trace` | Quern's actions with the flows and log lines each one caused |
| `get_log_summary` | GET | `/api/v1/logs/summary` | LLM-optimized summary with cursor support |
| `get_errors` | GET | `/api/v1/logs/errors` | Errors and crashes only |
| `get_build_result` | GET | `/api/v1/builds/latest` | Most recent build result |
| `parse_build_output` | POST | `/api/v1/builds/parse-file` | Parse a build log file from disk |
| `get_latest_crash` | GET | `/api/v1/crashes/latest` | Recent parsed crash reports; with `udid`, fetched from an iPhone (USB, the last `days`, default 3) or an Android device first |
| `clear_crashes` | DELETE | `/api/v1/crashes` | Delete the crash reports quern stored on the Mac, for one `udid` or all; the device keeps its own |
| `clear_device_crashes` | POST | `/api/v1/crashes/device/clear` | Permanently delete every crash report on an iPhone (USB); Android and simulators are refused with the reason |
| `set_log_filter` | POST | `/api/v1/logs/filter` | Reconfigure capture filters |
| `get_log_filter` | GET | `/api/v1/logs/filter` | Current ingestion filter config at all scopes (global, per-source, per-device) |
| `list_log_sources` | GET | `/api/v1/logs/sources` | Active log source adapters, and what each log buffer holds and has evicted |
| `start_simulator_logging` | POST | `/api/v1/device/logging/start` | Start simulator log capture |
| `stop_simulator_logging` | POST | `/api/v1/device/logging/stop` | Stop simulator log capture |
| `start_device_logging` | POST | `/api/v1/device/logging/device/start` | Start physical device log capture |
| `stop_device_logging` | POST | `/api/v1/device/logging/device/stop` | Stop physical device log capture |
| `start_oslog_streaming` | POST | `/api/v1/logs/oslog/start` | Start streaming the host Mac's unified log |
| `stop_oslog_streaming` | POST | `/api/v1/logs/oslog/stop` | Stop host oslog streaming |

**An empty answer is not always a true negative.** Logs live in fixed-size
buffers that evict their oldest entries: a shared one, and one each for crash
reports and quern's own logs. An unfiltered simulator can turn the shared buffer over in seconds.
`query_logs`, `tail_logs`, `get_errors` and `get_log_summary` therefore return
`truncated` and `complete_after`. `truncated: true` means entries stamped inside
the requested window were evicted before the call, so the result *may* be
missing some. `false` is a guarantee that nothing in the window was lost. For
a tail (`tail_logs`, or `query_logs` with `tail`) it means something narrower:
the entries returned really are the newest N, though older ones may have been
evicted. Entries removed by a filter change are not evictions; the call that
changed the filter reports them as `purged`.
`complete_after` is the time after which nothing has been evicted. The check is
narrowed by the query's `source` and `level`, so shed debug lines do not flag a
search for errors. `get_trace` reports the same thing as
`log_window_truncated` and `action_window_truncated`. `list_log_sources`
returns a `buffers` object with each buffer's capacity, intake, evictions by
source, and the oldest entry it still holds. A source's `entries_captured` is
intake, not retention.

The live streams, `/logs/stream` and `/proxy/flows/stream`, tell a client
that falls behind what it missed. They send a `dropped` event at most once a
second, with the count, the running `total_dropped`, and `missed_from` /
`missed_to`: the span missed since the previous notice, so consecutive
notices do not overlap. Backfill it with `query_logs` on the log stream and
`query_flows` on the flow stream. Only entries matching the stream's own
filter are counted. Every heartbeat also carries `total_dropped`.

Crash reports have a buffer of their own, so a busy source cannot evict them.
On Android, while capture is running, the logcat adapter emits a crash entry for
each Java crash, native crash and ANR as it happens. Find them with `query_logs`
(`source=crash`) or `get_errors`. `get_latest_crash` reads Android crash reports
too; see below.

**Summary cursors follow arrival order.** `get_log_summary` and
`get_flow_summary` return a `cursor`; pass it back as `since_cursor` to get only
what arrived since. The cursor counts arrivals, not timestamps, so the next delta
also includes entries stamped earlier than the last summary: a device clock
running ahead, a crash report written after the crash, a request that started
before the summary and finished after it. A cursor from before a server restart,
or one ahead of anything the server has numbered, comes back with
`cursor_reset: true`, and the summary then covers the requested window. On the
flow summary, a string that is not a cursor at all is refused with 400. Older timestamp cursors are still accepted, and the response always
returns an arrival cursor.

**Where it crashed.** Each crash report carries `app_frame`: where in the app's own
code it happened, with source file and line where the report has them.
- **Which frame.** It comes from an uncaught exception's backtrace when the report has
  one, and otherwise from the crashing thread. A crash reporter's signal handler and
  the app's entry point are skipped.
- **Source lines.** A simulator's Debug build has them, because macOS resolves the
  frames on the Mac. A phone names the function for a Debug build and gives only an
  offset for a stripped one.
- **`reason`** is the report's own explanation: an uncaught exception's reason, or
  Android's abort message or root cause. A Swift `fatalError` writes its message to
  the app's log, not the report, so query the logs for it.
- **`killed_by`** names the process whose signal ended the app, when it was not
  the app itself, such as a kill from a shell or `devicectl`. It is decided by
  pid, because the name is truncated to 32 characters. In that case `app_frame`
  is null, because the frames only say where the app was waiting. A watchdog or
  memory termination is not a kill: it keeps its frames, which are where the app
  hung, and its explanation goes in `reason`.
- **`.crash` text reports** (iOS 14 and older) get neither `killed_by` nor the
  exception backtrace. Their frames are the crashed thread's.

The response is compact by default: `app_frame`, `reason`, `killed_by`, the top
frames, and the app's `bundle_id`, `app_version` and `build_version`.
- **`detail=true`** adds `frames` (the exception's backtrace or the crashing thread,
  each frame with image, offset, symbol and source line) and `images` (the UUID and
  load address of each binary they point into). Symbolicating a frame needs these.
- **`include_raw=true`** adds `raw_text`.

A frame counts as the app's when:
- **iOS:** its binary is inside the app bundle.
- **Android native:** it was installed with the app.
- **Java:** its class is in the app's package, or in a shorter prefix of it (a
  Debug build's `.debug` suffix, or a module), falling back to excluding platform
  and common-library packages.

**Crash reports on both platforms.** `get_latest_crash` with a `udid` fetches
that device's crashes first. An iPhone is read over USB with `pymobiledevice3`:
the reports dated within the last `days` (default 3), and `pull` says what it
left on the phone (`older_on_device`, `oldest_on_device`, and a `note` pointing at
`days` and `clear_device_crashes`). An Android device or emulator is read from its DropBox,
which needs no root, and yields Java crashes, native crashes and ANRs; each
report's `kind` says which. The response's `pull` says whether the fetch
happened: `pulled`, `skipped` (with the reason, for example an iPhone that is not
on USB) or `failed` (with the error). Only `pulled` means the list reflects the
device (for an iPhone, within the pull's window; `older_on_device` says what lay beyond it). A `failed` Android pull can still add the reports it did read, for
example when one DropBox tag could not be read, or when the device's timezone
could not be read and records without a time of their own were skipped. A
simulator is `skipped`: its crash reports are written on the Mac and read from
`~/Library/Logs/DiagnosticReports` continuously, unless the server was started
with `--no-simulator-crashes`, which the reason then says. With a `udid`, the
list is that device's crashes, plus reports quern cannot place on any device. A
simulator's crash names its simulator by the app's path, or for a system app or
extension (which runs from the runtime volume) by the report's `coalitionName`,
so it is listed under that simulator only. A crash of one of the Mac's own
processes has `mac_process: true` and is listed only without a `udid`.
`raw_text` is left out of the response unless
`include_raw=true`: it runs to about a thousand tokens of JSON per crash. For an
iOS report the whole file is on disk at `file_path`. An Android report has no
file, and `file_path` names its DropBox record.

Crash reports are left on the iPhone (the pull copies, it never deletes), so a
pull does not take them away from Xcode or Finder. Each phone's reports are kept in
`~/.quern/crashes/devices/<device id>/`, so after a restart they are listed
against the right phone again, as an Android device's DropBox history is. A crash from before the server started,
on either platform, is listed but does not become a new log entry or run the
on-crash hook.
The hook runs for every newer crash, including one logcat already reported.

**Clearing.** `clear_crashes` deletes quern's stored copies on the Mac, for one
`udid` or all: only the files quern's own pulls wrote, under
`~/.quern/crashes/devices/`. Nothing else in the crash directory is deleted, and
never `~/Library/Logs/DiagnosticReports`. It does not clear the device: a later
pull lists again whatever the device still holds within its window, without
logging it as a new crash. An unknown `udid` is a 404, and an empty one a 400.
Pulled reports not copied for 30 days are also removed automatically, at start-up
and hourly; set `crash_retention_days` in `~/.quern/config.json` (0 keeps them
forever). The removals are logged in the server log only.

`clear_device_crashes` permanently deletes an iPhone's own crash reports, for
Xcode and Finder too: every `.ips`/`.crash` report at the top of its crash
directory, each by name. DiagnosticLogs (sysdiagnose archives) and other files
are left; `pymobiledevice3 crash clear` would remove them too, and is not used.
It returns how many it removed, how many remain and any that failed. It refuses a
phone matched to USB by name rather than by its hardware UDID (#323), Android (an
unrooted device's DropBox can only be read), and a simulator, whose reports are
files on the Mac. A phone with a long history lists more slowly; that cost has
not been measured.

On Android, `pull.open_dialogs` lists processes showing a crash ("keeps
stopping") dialog right now (`kind: "crash"`), and processes Android is treating
as not responding (`kind: "anr"`). The second starts when Android notices, about
13 seconds before the ANR dialog and its report appear (measured on API 32), and
lasts until the dialog is answered. While a crash dialog is open, Android drops
every further crash of that process, with no report and no log line, so no new
reports does not mean it stopped crashing. Dismiss the dialog or force-stop the
app. `[]` means none; `null` means it was not checked (iOS, or a pull that could
not run at all) or the process listing could not be read. A pull that is `failed`
only because some DropBox tags could not be read still reports it.

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
| `start_proxy` | POST | `/api/v1/proxy/start` | Start the proxy. With `system_proxy: true` it also configures the macOS system proxy and takes the same certificate check as `configure_system_proxy` — **428** when a booted simulator does not trust the CA; pass `skip_cert_check` to proceed. Starting the listener alone is never refused |
| `stop_proxy` | POST | `/api/v1/proxy/stop` | Stop the proxy |
| `proxy_setup_guide` | GET | `/api/v1/proxy/setup-guide` | Device setup instructions |
| `verify_proxy_setup` | POST | `/api/v1/proxy/cert/verify` | Verify CA cert installation (defaults to booted simulators) |
| `install_proxy_cert` | POST | `/api/v1/proxy/cert/install` | Install CA certificate on simulators and emulators |
| `record_device_proxy_config` | POST | `/api/v1/proxy/device-proxy-config` | Record a physical device's Wi-Fi proxy config (per SSID) |
| `set_local_capture` | POST | `/api/v1/proxy/local-capture` | Set local capture process list — sets it, but always keeps a minimum (`MobileSafari`, `com.apple.WebKit.Networking`) so webview and OAuth traffic is not silently lost. `capture_added` and `capture_removed` on the response say what changed. `only: true` captures exactly the list given. The **flag** is not stored, but the **list is** — it is written to `config.json` like any other, and start-up later widens it back to include the minimum, because nothing on disk records that you meant it narrowly. So `only` is a one-shot narrowing of the running server, not a temporary view: it permanently replaces whatever list was configured before. Returns **428** when enabling capture and a booted simulator does not trust the mitmproxy CA; pass `skip_cert_check` to proceed anyway. Disabling (empty list) is never refused |
| `set_bypass` | POST | `/api/v1/proxy/bypass` | Add domain patterns to the bypass allowlist |
| `clear_bypass` | DELETE | `/api/v1/proxy/bypass` | Remove bypass patterns, or clear all |
| `configure_system_proxy` | POST | `/api/v1/proxy/configure-system` | Auto-configure macOS system proxy. Returns **428** when a booted simulator does not trust the mitmproxy CA; pass `skip_cert_check` to proceed anyway |
| `unconfigure_system_proxy` | POST | `/api/v1/proxy/unconfigure-system` | Restore original proxy settings |

**Flow answers say when the store has evicted.** The flow store holds 5,000
flows and evicts the oldest-completed at capacity. `query_flows`,
`get_flow_summary`, `wait_for_flow` and `stop_capture_session` return
`truncated` and `complete_after`, with the meaning they have on the log tools.
They are narrowed by `simulator_udid`, `client_ip` or `device_serial` when the
query filters on one. Prefer `device_serial` for Android: an emulator's traffic
arrives from the host's own address because QEMU NATs it, so every emulator on
a machine shares one `client_ip` and filtering on it cannot tell them apart.
The serial is resolved from the process that owns the host socket, so it
distinguishes two emulators where nothing else can. `truncated` covers counts as well as entries: a page can hold the newest
flows and still carry a `total` that is short by what was evicted. To ask only
about recent traffic, pass `since` (for example, just before the action you
triggered); evictions from before it do not flag the answer. `proxy_status`
reports the store's capacity, intake (`added`), evictions and the span it still
holds in `flow_store`. `flows_captured` is only what survived.

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
| `build_and_install` | POST | `/api/v1/device/build-and-install` | Build an Xcode scheme and install it on one or more devices. Each successful build is recorded in `build_records` and in `~/.quern/build-records/`: bundle id, version, configuration and each binary's UUID, kept after the next build overwrites DerivedData. A device build also keeps dSYMs of the binaries this build compiled, and those Xcode or a vendor supplied with matching UUIDs, for the newest 10 device builds of each scheme; records are kept 30 days. Each binary's `dwarf` is the file inside its dSYM to pass to `atos -o`: one dSYM can cover several binaries (`MyApp.app.dSYM` holds the app and its `.debug.dylib`), and `atos` given the bundle can read the wrong one and resolve nothing. A dSYM that could not be made is said per binary (`dsym_error`), and never fails the build. `skip_plugin_validation=true` builds even when a Swift package plug-in or macro has not been approved in Xcode (`-skipPackagePluginValidation -skipMacroValidation`); it is off by default, and a build refused for that says so in its errors |

### UI interaction

| MCP Tool | Method | Path | Description |
|---|---|---|---|
| `get_ui_tree` | GET | `/api/v1/device/ui` | Accessibility tree |
| `get_element_state` | GET | `/api/v1/device/ui/element` | Query specific element state |
| `wait_for_element` | POST | `/api/v1/device/ui/wait-for-element` | Poll until element appears |
| `get_screen_summary` | GET | `/api/v1/device/screen-summary` | LLM-optimized screen description |
| `tap` | POST | `/api/v1/device/ui/tap` | Tap at coordinates |
| `restore_simulator_input` | POST | `/api/v1/device/ui/restore-input` | Take a simulator's touch, button and keyboard services back from Xcode 27's Device Hub. Restarts SpringBoard, so running apps are killed |
| `tap_element` | POST | `/api/v1/device/ui/tap-element` | Tap element by label/identifier. With `scroll_to_find` (the default) this can sweep for a long time; a client that disconnects abandons it rather than leaving it running. The request is closed **499** server-side, which a caller that has hung up does not receive — the observable effect is that the device stops being driven |
| `swipe` | POST | `/api/v1/device/ui/swipe` | Swipe gesture |
| `scroll_to_element` | POST | `/api/v1/device/ui/scroll-to-element` | Scroll a container until the target is in view, without tapping it. On iOS, bounded by a wall-clock deadline as well as `max_swipes`. A client that disconnects abandons the sweep instead of leaving it driving the device. The request is closed **499** server-side; a caller that has hung up does not receive it, so the observable effect is that the device is released |
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
| GET | `/tools` | Device-tool availability and UI cache stats (public). Backs `quern doctor` and `quern status`. Probes are bounded and run concurrently; the response also re-syncs the server's simulator UI backend |
| GET | `/api/v1/device/tools/sites` | Per-install-site tool versions and provenance. Authenticated: it returns absolute paths, which `/tools` deliberately does not |
| POST | `/api/v1/builds/parse` | Submit xcodebuild output |
| POST | `/api/v1/device/active` | Set the active device by UDID |
| POST | `/api/v1/devices/refresh` | Refresh pool from simctl |
| POST | `/api/v1/proxy/filter` | Set proxy capture filters |
| POST | `/api/v1/proxy/intercept/release-all` | Release all held flows |
