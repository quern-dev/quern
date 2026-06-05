# QuernProbe

A deterministic, offline UIKit playground app for exercising, testing, and
demoing Quern's device-automation features. No Xcode project — it compiles
with bare `swiftc` into a hand-assembled `.app` bundle, the same
compile-on-demand philosophy as `tools/sim-bridge.swift`.

## Why

Verifying Quern features against OS apps (Safari, Settings) is fragile:
address-bar focus behavior, autofill prompts, and web-view hit testing all
shift between iOS versions. QuernProbe is a fixed target — every interactive
element carries a stable accessibility identifier, content is deterministic,
and nothing touches the network. It was born as the controlled fixture that
isolated the HID shift-drop bug (see `fix/sim-bridge-keyboard-modifier-drop`).

## Build & install

```sh
./build.sh                    # build only → build/QuernProbe.app
./build.sh --install          # build + install on the booted simulator
./build.sh --install <udid>   # build + install on a specific simulator
```

Bundle id: `com.quern.probe`.

## Tabs

| Tab | Identifier | Exercises |
|---|---|---|
| Text | `tab_text` | Typing fidelity per keyboard type (`field_default`, `field_url`, `field_email`, `field_secure`); `text_event_log` echoes the last UITextField delegate event |
| Location | `tab_location` | `set_location` / simulated movement — live lat/lon/speed labels (`location_lat`, ...) and an update counter (`location_count`) |
| Controls | `tab_controls` | Element state and value-aware taps: `control_switch`, `control_slider`, `control_segment`, `control_stepper`; alert/sheet dismissal via `control_show_alert` / `control_show_sheet` |
| Scroll | `tab_scroll` | Scroll/swipe and scroll-to-element against 200 stable rows (`row_0` … `row_199`) |

The tab bar itself doubles as a fixture for hidden tab-bar-children probing.

## Self-test

Drives the app through Quern's REST API — identifier-based interaction only,
no coordinates:

```sh
python3 selftest.py [--udid UDID]
```

Covers: shift-character typing fidelity, `set_hardware_keyboard` toggling,
tab navigation, switch state via value-aware tap, scroll-tab row presence.
Requires a running Quern server (`quern start`) and a booted simulator.

## Roadmap

- Hidden tab-bar/menu children probe coverage (idb #767 regression fixture)
- Location movement assertions (update counter + coordinate deltas)
- Alert/sheet dismissal flows in the self-test
- Scripted demo tour of Quern features against this app
