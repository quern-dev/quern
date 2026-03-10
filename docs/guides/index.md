# Quern Developer Guides

Practical knowledge for getting the most out of Quern. These guides cover the real-world details, gotchas, and patterns that go beyond the API reference.

For AI agent workflows, see the [Agent Guide](../agent-guide.md). For installation and API reference, see the [README](../../README.md).

---

## Getting Started

- [Installation & Setup](getting-started.md) — Install, configure, connect your AI agent, server lifecycle
- [Device Pool & Resolution](device-pool.md) — How Quern finds devices, UDID translation, iOS 17+ differences, Android in the pool
- [Build & Install](build-and-install.md) — Architecture-aware builds, multi-device install, OS version checks

---

## iOS

### Network Proxy
- [Simulator Proxy Setup](ios-proxy-simulators.md) — Local capture, certificates, per-simulator flow tagging
- [Physical Device Proxy Setup](ios-proxy-physical-devices.md) — Wi-Fi proxy, split-tunnel VPN, multi-network tracking

### Logs and Diagnostics
- [Logging Best Practices](ios-logging.md) — os.log vs print(), ingestion filters, presets, crash reports, build summaries

### Physical Devices
- [WebDriverAgent Guide](ios-wda.md) — Setup, element selectors, free vs paid accounts, designing apps for automation, limitations
- [Live Video Preview](ios-preview.md) — CoreMediaIO screen capture, multi-device, orientation

---

## Android

- [Getting Started with Android](android-getting-started.md) — What works, what doesn't yet, emulator image types
- [Android Proxy Setup](android-proxy.md) — Cert installation options, networkSecurityConfig, emulator vs physical
- [Logcat Integration](android-logging.md) — Format, filtering, level mapping

---

## Cross-Platform

- [Network Debugging Patterns](network-debugging.md) — Mocking, intercepting, replaying, flow summaries
- [App State Management](app-state.md) — Save/restore state, plist manipulation
