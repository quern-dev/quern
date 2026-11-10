# Proxy capture on a physical device

*`quern://guide/devices/physical-proxy` -- part of `quern://guide/devices` in the Quern agent guide. Start from `quern://guide`.*

**Physical device proxy capture**: Physical devices need their Wi-Fi proxy configured manually in Settings. The full setup flow is: configure the Wi-Fi proxy → install the cert (the device downloads it from `http://mitm.it/cert/cer`, which only the proxy serves, so the proxy comes first) → trust the cert → call `record_device_proxy_config`. `proxy_setup_guide` gives the exact steps. **Ask the user before installing or trusting the certificate**, unless `proxy_status` shows `auto_install_cert: true`, which is their standing yes: then install it and tell them you did. A trusted root CA persists on the phone after the capture ends, so it is theirs to decide, as on simulators. After that, filter flows by the device's `client_ip`.

- `record_device_proxy_config(udid, ssid, client_ip)` — records the config per Wi-Fi network (SSID). Automatically derives the correct Mac interface IP by finding the interface on the same /24 subnet as the device's `client_ip`. This handles multi-interface Macs correctly (e.g. Wi-Fi + Ethernet active simultaneously).
- Call it again whenever you move to a different network — each SSID is tracked independently, so configs for home and work don't overwrite each other.
- `proxy_status` shows `wifi_proxy_configs` (keyed by SSID), `wifi_proxy_stale` (true if no stored network's proxy_host matches the current Mac IP), and `active_wifi_network` (the currently matching SSID). If `wifi_proxy_stale: true`, reconfigure the device's Wi-Fi proxy and call `record_device_proxy_config` again.
- `proxy_status` also includes `network_state` (refreshed by a ~15s background poll) — current SSID/IP, plus `last_changed_at` and `last_change_reason` fields populated when the laptop's network identity shifts. When you see `last_changed_at` is recent and a physical device is in play, expect `wifi_proxy_stale: true` to follow — and run the autonomous reconfiguration flow below.
- The steps below drive the whole reconfiguration through WDA.

**Autonomous proxy reconfiguration (cert already installed)**: When `wifi_proxy_stale: true` or `wifi_proxy_configs: null`, you can update the device's proxy settings fully via WDA without user intervention:

1. `proxy_status` — confirm proxy is running, note `local_ip`
2. `resolve_device(name=..., type="device")` — connect to the physical device
3. `launch_app(bundle_id="com.apple.Preferences")` — open Settings
4. `tap_element(label="Wi-Fi")` — navigate to Wi-Fi settings
5. `tap_element(label="More Info", element_type="Button")` — open the connected network's detail page
6. Read the device IP from the `IP Address` row (use `take_screenshot` or `get_screen_summary`)
7. Scroll down to find the **HTTP Proxy** section, tap `Configure Proxy` (StaticText). If it is **Off** (this network was never configured), choose **Manual** and fill in Port as well as Server: the proxy's port from `proxy_status`. If it is already Manual, check the Port as well as the Server in step 8 before saving: Save returns to the network's detail page, so a field left stale means opening the editor again
8. Verify the current Server value — if stale, tap the Server field to focus it:
   - The Server TextField is not discoverable by label in `get_ui_tree`. Use `take_annotated_screenshot` to find its bounding box, calculate its center as a fraction of the screen, convert to device logical points, then `tap` those coordinates.
   - `clear_text()` → `type_text("192.168.x.x")` with the correct Mac IP, and the same for Port if it differs from `proxy_status`
   - Then, with both fields right, `tap_element(label="Save", element_type="Button")`
9. `record_device_proxy_config(udid=..., ssid=..., client_ip=<device IP from step 6>)` — the correct Mac interface IP is derived automatically
10. `proxy_status` — verify `wifi_proxy_stale: false` and `active_wifi_network` is set
11. `launch_app(bundle_id="com.apple.mobilesafari", udid=...)` — open Safari to generate traffic
12. `wait_for_flow(client_ip=<device IP>)` — confirm traffic from the device is reaching the proxy. If no flow arrives within the timeout, the cert is likely not trusted on the device (Settings → General → About → Certificate Trust Settings) or the proxy port is unreachable from the device's network.

**Note on the Server field coordinates**: The proxy settings screen on iOS 26 does not expose text fields in the standard accessibility tree. Always use `take_annotated_screenshot` before tapping — the annotated view shows the TextField bounding box even when `get_ui_tree` doesn't. The field typically sits at ~40% screen height; divide the bounding box center (in image pixels) by the image dimensions and multiply by the device's logical point size (e.g. 390×844 for iPhone 12).
