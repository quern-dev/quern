# Physical Device Proxy Cert Setup

Step-by-step WDA UI automation flow for installing the mitmproxy CA cert and configuring the Wi-Fi proxy on a physical iOS device. Validated on iPhone 12, iOS 26.3.

---

## Prerequisites

1. **The proxy serves the certificate, so route the device first.** The quern API listens on 127.0.0.1 only (#254), out of a phone's reach. mitmproxy answers `http://mitm.it` itself, for any device routed through it, with the same CA quern installs on simulators -- so the Wi-Fi proxy is configured first (Phases 1-2), then the certificate fetched (Phases 3-5). Plain-HTTP pages load before the certificate is trusted. With two Macs running quern, whichever proxy the device points at answers, with that Mac's CA.

2. **No API key is needed from Safari**: `mitm.it` is answered by the proxy, not the API. (A server started with `--host 0.0.0.0` also serves `/api/v1/proxy/cert` directly, unauthenticated -- `setup-guide` then gives that URL and the old order.)

3. **Cert download defaults to `.cer`** (`application/x-x509-ca-cert`) which triggers iOS's "Install Profile" dialog. The old `application/x-pem-file` was silently ignored by iOS.

4. **WDA must be set up** on the device. If it fails with `0xe8008001`:
   ```bash
   rm -rf ~/.quern/wda/build
   # Edit ~/.quern/wda-state.json — remove build_team_id, built_at, installs, runners
   ```
   Then call `setup_wda` again. This error means a build from another machine (synced via iCloud/Dropbox) is installed — it's signed with that machine's cert and won't verify on the current one.

---

## Automated Script Outline

```python
status     = proxy_status()
# The proxy serves the certificate: it must be running before Phase 1, or
# Safari has nothing to fetch mitm.it from once the Wi-Fi proxy points at it.
if status["status"] != "running":
    start_proxy()
    status = proxy_status()
local_ip   = status["local_ip"]             # fallback default-route IP
proxy_port = status["port"]                  # 9101 unless started on another port
cert_url   = "http://mitm.it/cert/cer"       # served by the proxy, once routed
# NOTE: The actual proxy host used in Wi-Fi settings is derived by subnet
# matching in record_device_proxy_config — you don't need to compute it manually.
```

### Phase 1 — Configure Wi-Fi proxy

Before starting, read the current SSID and device IP from Settings > Wi-Fi (or from the network detail screen you're about to open). You'll need both for Phase 2.

```python
launch_app("com.apple.Preferences")
tap_element(identifier="com.apple.settings.wifi")   # "Wi-Fi, <network>"

# Read the connected SSID from the top of the screen (StaticText near the checkmark)
# and note the device IP — visible on the next screen.

# Tap ⓘ info button next to the connected network
tap_element(label="More Info", element_type="Button")

# Read IP Address field (e.g. "192.168.31.139") — needed for record_device_proxy_config
# Use get_screen_summary to extract it.

# Scroll to "Configure Proxy" using left-edge swipe
while "Configure Proxy" not on screen:
    swipe(start_x=30, start_y=700, end_x=30, end_y=300)

# NOTE: WDA only finds this as StaticText, not Button or Cell
tap_element(label="Configure Proxy", element_type="StaticText")
tap_element(label="Manual", element_type="StaticText")   # same quirk

# The proxy host is the Mac IP on the same subnet as the device.
# record_device_proxy_config derives it automatically — use local_ip as a
# placeholder here, then call record_device_proxy_config to get the real value.
tap_element(label="Server", element_type="TextField")
type_text(local_ip)

tap_element(label="Port", element_type="TextField")
type_text(str(proxy_port))

tap_element(label="Save", element_type="Button")

# Back at network detail — "Configure Proxy: Manual" ✓
```

### Phase 2 — Record the proxy config

Call this after Phase 1 completes. It derives the correct Mac interface IP by
subnet-matching the device's `client_ip`, stores the config per SSID, and
enables per-device flow filtering.

```python
ssid      = "<network name>"   # e.g. "Lilypad" — from Settings > Wi-Fi
client_ip = "<device IP>"      # e.g. "192.168.31.139" — from network detail screen

result = record_device_proxy_config(udid=udid, ssid=ssid, client_ip=client_ip)
# result["wifi_proxy_host"] is the Mac IP that was actually recorded.
# If it differs from what you typed in Phase 1, go back and correct it in Settings.
```

If the Mac is on multiple interfaces (e.g. Wi-Fi + Ethernet), `record_device_proxy_config`
will pick the interface on the same /24 subnet as the device — always the right one,
regardless of routing tables or interface names.

### Phase 3 — Download cert via Safari

```python
launch_app("com.apple.mobilesafari")
tap_element(label="Address", element_type="TextField")
type_text(cert_url + "\n")

# iOS prompt: "This website is trying to download a configuration profile."
tap_element(label="Allow", element_type="Button")

# iOS confirmation: "Profile Downloaded"
tap_element(label="Close", element_type="Button")
```

### Phase 4 — Install profile in Settings

```python
launch_app("com.apple.Preferences")

# "Profile Downloaded" banner appears at top of Settings root
tap_element(label="Profile Downloaded", element_type="Button")

# "Install Profile" screen
tap_element(label="Install", element_type="Button")   # top-right nav button

# Warning screen: "Unmanaged Root Certificate"
tap_element(label="Install", element_type="Button")   # top-right nav button again

# Alert: "Install Profile — Installing this profile will change settings on your iPhone."
# Two "Install" buttons exist at this point — use coordinates for the alert button
tap(x=269, y=481)

# "Profile Installed ✓" screen — navigate away
tap_element(label="Done", element_type="Button")      # or the ✓ checkmark top-right
```

### Phase 5 — Enable full trust

```python
launch_app("com.apple.Preferences")
tap_element(label="General", element_type="Button")   # scroll down if not visible
tap_element(label="About", element_type="Cell")

# Scroll to Certificate Trust Settings using left-edge swipe
# (avoid center — long-press on IP address text triggers "Copy" popup)
while "Certificate Trust Settings" not on screen:
    swipe(start_x=30, start_y=700, end_x=30, end_y=300)

tap_element(label="Certificate Trust Settings", element_type="Cell")

# mitmproxy toggle is OFF — tap to enable
tap_element(label="mitmproxy", element_type="Switch")

# Alert: "Root Certificate — Warning: enabling this certificate..."
tap_element(label="Continue", element_type="Button")

# mitmproxy toggle is now ON (green) ✓
```

---

## WDA Quirks Reference

| Situation | What doesn't work | What works |
|-----------|------------------|------------|
| Tap "Configure Proxy" row | `element_type="Button"`, `"Cell"` | `element_type="StaticText"` |
| Tap "Manual" option | `element_type="Button"` | `element_type="StaticText"` |
| Final Install alert (two buttons) | `tap_element(label="Install")` — ambiguous | `tap(x=269, y=481)` — lower button |
| Scroll on About / network detail pages | Center swipe — triggers "Copy" on IP fields | Swipe from `x=30` (left edge) |
| Deep UI tree on Settings screens | `get_ui_tree` returns shallow results | `get_screen_summary` + `tap_element` by label |

## Ports

| Port | What it is |
|------|------------|
| `9110` | Quern API server: 127.0.0.1 by default (#254); `--host 0.0.0.0` exposes it, with a warning |
| `9101` | mitmproxy proxy listener: the device points here, and fetches the cert from `mitm.it` through it |

The device's Wi-Fi proxy points to **9101**; the cert download goes through it.
