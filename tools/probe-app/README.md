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
./build.sh --scene            # the scene-lifecycle bundle (see below)
```

Bundle id: `com.quern.probe`.

### On a real iPhone

```sh
./build.sh --device <device-udid>            # build + sign
./build.sh --device <device-udid> --install  # and install it
```

`<device-udid>` is the hardware udid from `xcrun devicectl list devices`, not
the identifier Quern shows for the same phone.

Same sources and same `Info.plist` as the simulator build, so a test means the
same thing on both; the build adds `MinimumOSVersion` and
`CFBundleSupportedPlatforms`, which only device installs require.

Signing needs a **development** provisioning profile that lists the device.
`find-profile.py` finds one, preferring a profile issued for `com.quern.probe`
over a wildcard `TEAM.*` team profile, and rejecting any profile with no device
list — that is a distribution profile, which signs and installs and then fails
to launch. If nothing matches it says so and tells you the fix: open any iOS
project in Xcode once with your account signed in, which is enough to get a
wildcard team profile covering your registered devices.

Nothing here is checked in or configured: the profile, the team and the signing
certificate are all discovered at build time, because a pasted team id and
profile uuid both go stale — profiles expire yearly and Xcode reissues them
with a new uuid.

Why it was simulator-only until now: the build targeted the `iphonesimulator`
SDK with an `-ios16.0-simulator` triple and never signed anything. That suffix
is the whole difference between a bundle the simulator loads and one a phone
does, and the mismatch surfaces at install as a signature error.

## Tabs

| Tab | Identifier | Exercises |
|---|---|---|
| Text | `tab_text` | Typing fidelity per keyboard type (`field_default`, `field_url`, `field_email`, `field_secure`); `text_event_log` echoes the last UITextField delegate event |
| Controls | `tab_controls` | Element state and value-aware taps: `control_switch`, `control_slider`, `control_segment`, `control_stepper`; alert/sheet dismissal via `control_show_alert` / `control_show_sheet` |
| Scroll | `tab_scroll` | Scroll/swipe and scroll-to-element against 200 stable rows (`row_0` … `row_199`) |
| Links | `tab_links` | Deep link landing surface — `link_count`, `link_last_uri` |
| Logs | `tab_logs` | Every iOS logging path (`print`, `NSLog`, `os_log` at four levels and with a subsystem, Swift `Logger` at four levels), idle until `log_start` |
| Location | *(in More)* | `set_location` / simulated movement — live lat/lon/speed labels and an update counter |
| Web | *(in More)* | `WKWebView` with fixed local content and named DOM ids; `isInspectable` on 16.4+ |
| Diag | *(in More)* | Crash (uncaught exception, `fatalError`) and main-thread hang |

The tab bar itself doubles as a fixture for hidden tab-bar-children probing.

**Tab order is load bearing.** An iPhone tab bar shows five items and moves the
rest into a More list, which keeps its own navigation stack and is markedly
harder to drive. The five the self-test exercises on every run are on the bar;
Location, Web and Diag are reached through More by `goto()`, which handles the
list, the nav stack, and the fact that "More" names two different elements — the
tab (a `RadioButton`) and the back button (a `Button`).

## Logging

Absorbed from the standalone LogTester app, which lived outside version control
while `server/sources/device_log.py` documented its parsing regex against
LogTester's output. Emitters are idle until started: a fixture that logs
continuously pollutes the log stream during every other test in the same app,
and log volume is itself under test.

## Deep links

`quernprobe://` is registered in `Info.plist`. `link_count` is the assertion
target rather than the tool's response, for the reason spelled out in
`DeepLinkStore.swift`: on Android `open_url` reports success regardless (#78),
and even on iOS a link that opens Safari instead of the app still "succeeds".

Two things the fixture made visible that are worth knowing before writing deep
link automation:

* **iOS puts up a confirmation alert** — *Open in "QuernProbe"?* — for a
  custom-scheme link. Until it is answered it blocks every other query, so a run
  that dies mid-test leaves the simulator wedged behind it.
* **A cold-launch link arrives differently.** It comes through
  `launchOptions[.url]`, not `application(_:open:options:)`, which only fires
  for an already-running app. Handling one and not the other loses the link
  silently.

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
