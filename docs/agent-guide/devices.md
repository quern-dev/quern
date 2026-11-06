# Devices: physical iPhones, preview and the device pool

*`quern://guide/devices` -- part of the Quern agent guide. Start from `quern://guide`.*

Getting a device, driving physical iPhones, watching devices live, and changing device settings.
## Device Pool for Parallel Testing

Use `ensure_devices` to boot multiple simulators at once, then run different test scenarios on each in parallel. The first device in the result becomes the active device; pass explicit `udid` parameters to target the others.

**Active device**: After `resolve_device` or `ensure_devices`, the resolved device becomes the active device for all subsequent tool calls. You don't need to pass `udid` to every tool — it defaults to the active device. To switch, call `resolve_device` with new criteria, or pass `udid` directly to `resolve_device` (faster than re-matching by name when you already know the UDID — e.g., from a `list_devices` call). The active device is persisted in `~/.quern/active-device.json` and survives `quern stop` / `quern restart`, so within one agent session you don't have to re-resolve after a restart.

**It is yours alone.** Each agent session has its own active device (#254), keyed by the session id its MCP server sends, so another agent resolving a different device never redirects your calls. A session that hasn't chosen one yet doesn't inherit anyone else's: with several devices booted, an unqualified call is refused with "specify udid" until you resolve one or pass `udid`. This keeps agents from *redirecting* each other; it doesn't hand them *different* devices. Two agents that each ask `resolve_device` for "a simulator" can get the same one; to keep devices apart, reserve them (see "Sharing a server with other agents" below). Your choice is kept across a server restart for a day of inactivity. Callers that send no session (a plain curl, an MCP server older than #453) share one active device, as before.

**Default behavior**: `resolve_device` and `ensure_devices` default to a virtual device -- `type="simulator"` on iOS, an emulator on Android -- to prevent accidentally targeting physical devices (which may not have your app installed). Pass `type="device"` or `type="android_device"` explicitly to target physical devices.

**Which platform**: device requests are made on your project's platform -- `.quern/config.json`'s `"platform"`, else what the project is (an Xcode project is iOS, an Android app module Android). So in an iOS project `resolve_device(os_version="16")` means iOS 16, never Android 16, and the answer says so (`platform`, `platform_source`). Name a device on the other platform -- "Pixel 8" from an iOS project -- and it is used when it is the only match (or, with `count`, when there are no more matches than you asked for). In a project that builds for both, or none, a bare version both platforms have is refused with the matches on each side: the refusal is a 409 `ambiguous_platform` whose `retry_with` names the call to make: pass `platform="ios"` or `"android"`, or write the version as "iOS 16" or "Android 16".

## Controlling a device

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

## Working with Physical Devices

Physical iOS devices are supported for screenshots, UI automation, log capture, and crash reports. UI automation on a device always uses WebDriverAgent (WDA). A simulator uses the accessibility tree (sim-bridge / idb) by default, and can be switched to WDA with `start_driver` — that is the way to see the element types XCUITest will see (`xcui_type`), and `stop_driver` switches it back.

**First-time setup**: Call `setup_wda` with the device UDID. This builds and installs WDA on the device, which requires a valid Apple Developer signing identity. If multiple identities exist, the tool returns a list — call again with the chosen `team_id`. The app appears on the device as **Quern Driver**.

**After setup**: WDA auto-starts when you first interact with the device (screenshot, UI tree, tap, etc.). No need to manually call `start_driver` — it happens transparently. The driver idles out after 15 minutes of inactivity.

**What works the same**: `take_screenshot`, `get_screen_summary`, `get_ui_tree`, `tap_element`, `swipe`, `type_text`, `wait_for_element`, `install_app`, `launch_app`, `terminate_app`.

**What's different**:
- `boot_device` / `shutdown_device` — simulators only
- `set_location` — simulators only
- `grant_permission` — simulators only
- `start_device_logging` / `stop_device_logging` — on-demand log capture for physical devices (vs `start_simulator_logging` for simulators). Captures os_log and Logger output only — `print()` writes to stdout and is not captured. Both support `preset` parameter to apply ingestion filters at start time
- `get_latest_crash` with a `udid` parameter — pulls crash reports directly from the physical device
- `tap_element` taps only an element a tap can reach: its centre on screen and XCUITest reporting it hittable. Elements read through WDA carry `on_screen`. When every match is unreachable the not-found response lists them and why — scroll the element into view (`scroll_to_element`) rather than retrying the tap
- Reads made for `tap_element`, `get_element`, `wait_for_element` and `scroll_to_element` go through WDA at depth 12, which keeps long lists fast (measured: 3.9s instead of 34.5s on a 200-row list). A miss looks once more at the full depth (25) -- bounded, and only when the first read was quick -- and that holds when you pass `snapshot_depth` too: the depth you give is a floor, not a ceiling. Pass `deepen: false` for a read at exactly that depth. Every miss reports how deep it looked
- A physical iPhone that has never had `setup_wda` is a **400 naming `setup_wda`**, not a failure to retry: run setup first
- `preview_device` — opens a live macOS video preview window of the device screen: CoreMediaIO for USB-connected physical devices, and quern-media's MJPEG stream for booted simulators. Each device is independently controlled — add and remove individual previews without affecting others. Use `stop_preview` with a UDID to close one device, or without to close all. `preview_status` shows per-device breakdown and what can be previewed, booted simulators included, each tagged `kind`. Asking again for a device already being previewed answers `already_active` with the original `started_at`: the stream is reused, not restarted. A simulator answer carries `on_air`, so a reused preview that is reattaching is not read as live

## Proxy capture on a physical device

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

## Live Preview of Devices and Simulators

Open real-time video windows to see what's happening on USB-connected physical devices and on booted simulators. Each device is independently managed — no restart penalty after the initial 3-second CoreMediaIO discovery.

1. `preview_device` with a device UDID — adds that device's preview window
2. `preview_device` with another UDID — adds a second device (1s stagger, no rediscovery)
3. `preview_status` — see which devices are active vs. available. `available` always includes booted simulators (`kind: "simulator"`, `id` is the UDID `preview_device` takes). USB devices (`kind: "device"`) appear once the preview process is running, and their `id` is a CoreMediaIO id: preview a phone by its UDID from `list_devices`. `simulators_error` means simulators could not be listed, not that none are booted
4. `stop_preview` with a UDID — remove one device's preview, others stay running
5. `stop_preview` without a UDID — stop all previews and kill the process

**Key insight**: The preview process stays alive even if all windows are closed (by user or via `stop_preview` with UDID). Re-adding a device is instant — no 3s discovery delay. Only `stop_preview` without a UDID kills the process.

**Two routes, one tool.** A simulator is not a CoreMediaIO capture source, so it does not arrive the same way: `quern-media` reads its framebuffer directly and serves MJPEG on loopback, and the preview window opens on that stream. Pass a booted simulator's UDID to `preview_device` and it works; the difference matters only when something breaks, because the failure modes are unrelated.

**Limitations**: a simulator must already be booted, and `preview_device` with no UDID previews USB-connected physical devices only — it does not sweep up simulators, which would open a window for every booted one.

**OFF AIR.** A simulator window whose stream drops — the simulator shut down or rebooted, or `quern-media` restarted — stays open, greys out under an OFF AIR label, and reconnects on its own. While it is off air, `preview_status` reports `"on_air": false` for that simulator, so a frozen last frame is never reported as a live preview. Quitting Simulator.app shuts down every simulator, headless ones included.

**The person at the Mac can open simulators too.** The preview app's Devices menu lists the booted simulators alongside USB devices, and keeps the list current as simulators boot and shut down. A simulator picked there is opened by the server, so it appears in `preview_status` and `stop_preview` closes it like any other.

## Mistakes to avoid

**Hardcoding device UDIDs** — Use `resolve_device` with a name and let Quern find the right device. UDIDs differ across machines.

## When something is wrong

**"The screen summary is empty on a physical device"** — `element_count: 0` with no error, while `take_screenshot` plainly shows a populated screen. The accessibility tree and screenshots take different paths, so the device looks alive and unreadable at once.

The usual cause is that WDA's `/source` read exceeded its budget. WDA serializes requests, so the abandoned call keeps running on the device and the *next* one queues behind it — which is why retrying with a larger timeout can still fail, and why the failure looks permanent rather than intermittent. Give the device a moment to drain before retrying.

You no longer have to infer any of this. A read that timed out and fell back now says so: `get_screen_summary` and `get_ui_tree` return `source_timed_out: true` and a `degraded` string, and a post-action screen context carries them too. On a full-tree read, an `element_count` with no `degraded` beside it is a real count. That does not extend to `strategy: "skeleton"`, which never has a `degraded` field because you asked for a partial read — its count is the navigation chrome it returned, not what is on the screen. Quern also no longer treats a timeout as proof the runner is hung. WDA serialises requests, so a tree big enough to outlast any check window queues every liveness ping behind it — waiting longer can only ever distinguish slow from slower. A restart now needs evidence the runner is *gone*: a refused or reset connection, meaning nothing is listening. A ping that merely times out says something accepted it and is busy, and the runner is left alone — which matters because the restart reinstalls it through xcodebuild (#170).

The budgets are 20s for A13-era and older chips and 10s for everything newer. Both are sized from measurement with headroom: an iPhone 11 on iOS 26 takes about 10.2s to answer on its home screen, and an iPhone 15 Pro 4.4–5.3s depending on how dense the screen is. Expect these to move when *Xcode* moves rather than when the device does — the same iPhone 11 answered in 7.5s before WDA was rebuilt under Xcode 27, on identical hardware and the same screen.

Pass `source_timeout` (30–45) on `get_screen_summary` or `get_ui_tree` when a device is slower than that, or a screen is unusually dense. It does not restart anything. `strategy: "skeleton"` skips the `/source` read entirely and returns navigation chrome only, which is the right answer for maps with many pins.
