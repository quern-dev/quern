# Quern User Guides

How to get the most out of AI-assisted mobile development and testing with Quern. These guides are for *you* — the human working with an AI agent. They cover what you need to know, what decisions require your input, and the practical details that help you guide your agent effectively.

For the AI agent's reference, see the [Agent Guide](../agent-guide.md). For installation and API reference, see the [README](../../README.md).

---

## Getting Started

- [Installation & Setup](getting-started.md) — Get Quern running and connected to your AI agent
- [Device Pool & Resolution](device-pool.md) — How Quern manages devices, the iOS 17+ complexity it hides, and what you should know
- [Build & Install](build-and-install.md) — Building and deploying to multiple devices at once

---

## iOS

### Network Proxy
- [Simulator Proxy Setup](ios-proxy-simulators.md) — Capturing network traffic from simulators
- [Physical Device Proxy Setup](ios-proxy-physical-devices.md) — Capturing traffic from real iPhones and iPads

### Logs and Diagnostics
- [Logging Best Practices](ios-logging.md) — Making your app's logs useful for AI-assisted debugging

### Physical Devices
- [WebDriverAgent Guide](ios-wda.md) — What you need to know about physical device automation
- [Live Video Preview](ios-preview.md) — Real-time screen mirroring over USB

### Simulators
- [App State Management](app-state.md) — Saving and restoring app state for reproducible debugging

---

## Android

- [Getting Started with Android](android-getting-started.md) — What's supported, emulator setup, image types
- [Android Proxy Setup](android-proxy.md) — Network traffic capture on emulators and physical devices
- [Logcat Integration](android-logging.md) — How Android logs flow into Quern

---

## React Native

- [React Native Logging](react-native-logging.md) — Route JS logs through os_log with `@quern/react-native-os-logger` for structured, filterable logs in Quern

---

## Cross-Platform

- [Network Debugging Patterns](network-debugging.md) — Mocking, intercepting, and replaying network traffic
- [Deep Link Testing](deep-link-testing.md) — Custom URL schemes vs universal links, testing both paths, common verification failures
- [App Knowledge Base](app-knowledge.md) — Give your agent a pre-built map of your app: screens, navigation, alerts, state flags

---

## Workflow Guides

Real-world scenarios showing how the pieces fit together.

- [Testing a New API Integration](workflow-api-testing.md) — Verify requests, mock error responses, sweep status codes
- [Investigating a Crash](workflow-crash-investigation.md) — From crash report to root cause using logs, network, and reproduction
- [Multi-Device Testing](workflow-multi-device.md) — Boot a fleet, build once, test across screen sizes and OS versions
- [Physical Device Setup from Zero](workflow-physical-device-setup.md) — The complete first-time flow: trust, WDA, proxy, preview
- [Onboarding onto a Project](workflow-onboarding.md) — From git clone to productive in an hour
- [Location Simulation](workflow-location-testing.md) — GPS routes, geofences, and multi-device coordination (rideshare, delivery)
- [Agent-Generated Test Scripts](workflow-test-scripts.md) — Write once, run forever, bring the agent back only when things break
- [Building an App Knowledge Base](workflow-app-knowledge.md) — The complete guided tour: from first launch to saved checkpoints and executable test flows
