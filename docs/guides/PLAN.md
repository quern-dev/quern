# Documentation Plan

## Goal
Human-facing knowledge base (separate from agent-guide.md which is AI-focused).
Practical knowledge, tips, gotchas, the stuff that lives in the developer's head.

## Structure

### docs/guides/index.md — Home / TOC
Links to all topic pages, organized by platform then topic.

### iOS Guides

1. **ios-proxy-simulators.md** — Simulator proxy setup
   - Local capture mode (recommended) vs system proxy
   - Certificate installation (automatic via simctl)
   - Per-simulator flow tagging/filtering
   - Parallel test runners with isolated traffic

2. **ios-proxy-physical-devices.md** — Physical device proxy setup
   - Wi-Fi proxy configuration
   - Certificate install + trust (WDA automation or manual)
   - Split-tunnel VPN bridge scenario:
     - Mac on VPN via ethernet (corp subnet), phone on Wi-Fi (different subnet)
     - Quern auto-detects the correct interface via client_ip subnet matching
     - Works at home (VPN + Wi-Fi) and at office (ethernet + public Wi-Fi)
     - SUPER HANDY but dangerous — you're proxying through a machine on the corp VPN
     - USE WISELY — don't leak corp traffic through the proxy
   - Multi-network tracking (home vs work SSIDs stored independently)
   - wifi_proxy_stale detection and auto-reconfiguration

3. **ios-logging.md** — Logs and diagnostics
   - os.log vs print() — why os.log is better for debugging
   - print() diverter pattern: redirect print() to os.log so it shows up in Quern
   - Log levels and filtering best practices
   - device-quiet preset and custom presets
   - Process-level filtering (subprocess-level, not post-hoc)
   - Crash reports: automatic discovery, crash hooks, cross-referencing with logs/flows
   - Build output capture and summaries

4. **ios-wda.md** — WebDriverAgent tips and tricks
   - What WDA is and why it exists (no idb on physical devices)
   - Setup: paid vs free Apple developer accounts
   - Free account limitations: 7-day profiles, 3 App ID slots (WDA uses 2!)
   - Device trust requirement (Settings > VPN & Device Management)
   - Session recovery: auto-recovery on invalid session, connection errors
   - Element selectors: label vs identifier vs class chain vs predicate
   - Designing apps for WDA automation:
     - Set accessibility identifiers on key elements
     - Avoid dense screens with many similar elements
     - Avoid overlays/modals that block the accessibility tree
     - Keep navigation predictable (tab bars, standard back buttons)
     - Avoid custom gesture recognizers that conflict with WDA taps
   - Known limitations: no side/power button, can't control brightness, mute switch
   - Runner log diagnostics and common failure patterns

5. **ios-preview.md** — Live video preview for physical devices
   - CoreMediaIO screen capture (USB only, not Wi-Fi)
   - Multi-device: stagger AVCaptureSession starts by 1s
   - Orientation handling (app rotates, preview needs transform)
   - Future: click-to-tap overlay

### Android Guides

6. **android-getting-started.md** — Android support overview
   - What works in Phase 1: device discovery, app lifecycle, screenshots, logcat, emulator boot
   - What's not yet supported: UI automation, advanced log filtering
   - Google APIs vs Google Play emulator images — and why it matters

7. **android-proxy.md** — Android proxy/cert setup
   - Rootable emulators (Google APIs / dev-keys): fully automated system cert
     - API < 34: adb remount technique
     - API >= 34: nsenter APEX injection technique
     - Cert is non-persistent (lost on reboot), tool handles re-injection
   - Non-rootable emulators (Google Play): manual user cert + networkSecurityConfig
     - The networkSecurityConfig XML snippet (debug-only)
     - Why this is actually the right approach for app development
   - Physical devices: same as non-rootable emulators (need root/Magisk for system cert)
   - HTTP proxy: 10.0.2.2:9101 for emulators, Wi-Fi proxy for physical
   - SDK tool discovery: Quern finds adb/emulator from PATH, ANDROID_HOME, well-known locations

8. **android-logging.md** — Logcat integration
   - threadtime format parsing
   - Level mapping (V/D→DEBUG, I→INFO, W→WARNING, E→ERROR, F/A→FAULT)
   - Tag and process filtering
   - Buffer clearing on adapter start

### Cross-Platform

9. **network-debugging.md** — Network proxy patterns
   - Mocking responses (set_mock with mitmproxy filter syntax)
   - Intercepting and modifying live traffic (set_intercept + release_flow)
   - Replaying captured requests
   - Flow summaries with cursor-based delta updates
   - Common filter patterns (~d, ~u, ~m, ~c, ~b — NOT ~p)

10. **app-state.md** — App state management
    - Save/restore app state for reproducible debugging
    - Plist reading/writing for tweaking app behavior

## Style
- Practical, conversational, developer-to-developer
- Include real examples and gotchas
- "Here's what actually happens" not "the API accepts parameters"
- Match the existing README.md tone (direct, no-nonsense, slightly opinionated)

## Existing Content to Reference (don't duplicate)
- agent-guide.md — AI agent workflows (keep separate)
- physical-device-cert-setup.md — WDA automation steps for cert install
- troubleshooting.md — iOS error patterns and crash report reading
- README.md — Installation, quick start, API reference
