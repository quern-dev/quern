# Working with Physical Devices

*`quern://guide/devices/physical` -- part of `quern://guide/devices` in the Quern agent guide. Start from `quern://guide`.*

Physical iOS devices are supported for screenshots, UI automation, log capture, and crash reports. UI automation on a device uses WebDriverAgent (WDA); on iOS 27 a server can read the screen without it (below). A simulator uses the accessibility tree (sim-bridge / idb) by default, and can be switched to WDA with `start_driver` — that is the way to see the element types XCUITest will see (`xcui_type`), and `stop_driver` switches it back.

**First-time setup**: Call `setup_wda` with the device UDID. This builds and installs WDA on the device, which requires a valid Apple Developer signing identity. If multiple identities exist, the tool returns a list — call again with the chosen `team_id`. The app appears on the device as **Quern Driver**.

**After setup**: WDA auto-starts when you first interact with the device (screenshot, UI tree, tap, etc.). No need to manually call `start_driver` — it happens transparently. The driver idles out after 15 minutes of inactivity.

**Screen reads without WDA (`backend: "coredevice"`)**: on iOS 27 and later, a server whose owner has set `"device_ui_reader": "coredevice"` in `~/.quern/config.json` reads a physical iPhone's screen from the accessibility tree the device sends over its tunnel, instead of asking WDA. That covers `get_ui_tree`, `get_screen_summary` and the reads behind `tap_element`, `wait_for_element` and the like, at about 2 s a screen. Taps and every other action still go through WDA. The tree is what VoiceOver can reach, so it differs from WDA's in three ways:
- only elements on screen, and nothing hidden under a toolbar;
- a row's inner texts merged into one label ("Wi-Fi, Home" where WDA has a button and two texts);
- the element names a simulator uses (`CheckBox` for a switch, `Heading` for a title).

So match rows by `identifier` rather than an exact `label`, and scroll until an element appears in the tree rather than until its frame is on screen. A read it cannot make falls back to WDA, and `backend` on the answer says which one served it.

**What works the same**: `take_screenshot`, `get_screen_summary`, `get_ui_tree`, `tap_element`, `swipe`, `type_text`, `wait_for_element`, `install_app`, `launch_app`, `terminate_app`.

**What's different**:
- `boot_device` / `shutdown_device` — simulators only
- `set_location` — simulators only
- `grant_permission` — simulators only
- `start_device_logging` / `stop_device_logging` — on-demand log capture for physical devices (vs `start_simulator_logging` for simulators). Captures os_log and Logger output only — `print()` writes to stdout and is not captured. Both support `preset` parameter to apply ingestion filters at start time
- `get_latest_crash` with a `udid` parameter — pulls crash reports directly from the physical device
- `tap_element` taps only an element a tap can reach: its centre on screen and XCUITest reporting it hittable. Elements read through WDA carry `on_screen`. When every match is unreachable the not-found response lists them and why — scroll the element into view (`scroll_to_element`) rather than retrying the tap
- Reads made for `tap_element`, `get_element_state`, `wait_for_element` and `scroll_to_element` go through WDA at depth 12, which keeps long lists fast (measured: 3.9s instead of 34.5s on a 200-row list). A miss looks once more at the full depth (25) -- bounded, and only when the first read was quick -- and that holds when you pass `snapshot_depth` too: the depth you give is a floor, not a ceiling. Pass `deepen: false` for a read at exactly that depth. Every miss reports how deep it looked
- A physical iPhone that has never had `setup_wda` is a **400 naming `setup_wda`**, not a failure to retry: run setup first
- `preview_device` — opens a live macOS video preview window of the device screen: CoreMediaIO for USB-connected physical devices, and quern-media's MJPEG stream for booted simulators. Each device is independently controlled — add and remove individual previews without affecting others. Use `stop_preview` with a UDID to close one device, or without to close all. `preview_status` shows per-device breakdown and what can be previewed, booted simulators included, each tagged `kind`. Asking again for a device already being previewed answers `already_active` with the original `started_at`: the stream is reused, not restarted. A simulator answer carries `on_air`, so a reused preview that is reattaching is not read as live
