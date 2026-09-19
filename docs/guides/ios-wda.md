# WebDriverAgent Guide

WebDriverAgent (WDA) is how Quern controls physical iOS devices — tapping buttons, reading screen content, typing text, swiping. It runs on the device and exposes an API that your agent talks to. Quern manages WDA automatically, but understanding how it works helps when things go sideways.

## What You Need to Know

**On simulators**, Quern uses `idb` (Facebook's iOS Development Bridge) for UI automation. No setup needed — it works out of the box.

**On physical devices**, Quern needs WDA. Your agent will set it up the first time you interact with a physical device. Here's what happens and what might require your input.

## Setup

### First Time

Tell your agent you want to work with a physical device, and it will call `setup_wda`. This:

1. Discovers your signing identities from Xcode
2. Clones the WDA repo
3. Builds and installs it on the device as "QuernDriver"

The build is cached — subsequent sessions skip it unless something changes.

### Signing Identity Selection

If you have multiple Apple Developer teams in Xcode, your agent will ask which one to use. Pick the one you use for development on this device.

## Free vs Paid Developer Accounts

This matters more than you'd think.

### Paid Account ($99/year)

- Provisioning profiles last 1 year
- Wildcard App IDs (one covers everything)
- Quern Driver (WDA) just works, indefinitely
- No device trust step required — apps signed by paid accounts are trusted automatically

### Free Account (Apple ID, no enrollment fee)

- Profiles expire after **7 days**, so WDA must be re-set-up weekly
- No wildcard App IDs — each bundle identifier registers its own

There are **two separate budgets**, and they are often confused because Xcode's
error message mentions only one of them:

| budget | limit | what Quern Driver uses | cleared by |
|---|---|---|---|
| App IDs registered | 10 per rolling 7 days | 2 — `dev.quern.driver` and `dev.quern.driver.xctrunner` | waiting out the 7 days |
| free-signed apps installed on a device | 3 at once | **1** — the runner, shown as `QuernDriver` | deleting a free-signed app from the device |

Only one app is installed: the `.xctest` bundle ships *inside* the runner rather
than beside it. So Quern Driver costs you one of three device slots, leaving two
for your own app.

The error *"The maximum number of apps for free development profiles has been
reached"* is the **device** limit. Waiting does not clear it. Note also that
Xcode counts *offloaded* apps toward the three, so a device can look emptier
than the error suggests — check Settings > General > iPhone Storage.

**If you're on a free account**, tell your agent. It will warn you about slot limits and profile expiry. When Quern Driver stops working after 7 days, tell your agent to rebuild it:

> "Rebuild WDA — my profile expired"

#### Device Trust (Free Accounts Only)

Free developer accounts require you to manually trust the developer profile on the device before Quern Driver can run:

**Settings > General > VPN & Device Management > [your developer name] > Trust**

You need to do this on the device itself — it's a one-time step per developer identity per device. If Quern Driver won't launch at all and you're on a free account, this is almost certainly why. The runner log will show something about being unable to launch the app.

### How Your Agent Detects This

When WDA setup discovers your account type, it includes warnings in the response. Your agent should surface these to you — things like "this is a free account, profiles expire in 7 days" and "Quern Driver occupies 1 of your 3 device slots, leaving 2 for your own app."

## How the Driver Works

When your agent interacts with a physical device (tap, screenshot, read screen), Quern automatically:

1. Checks if a WDA driver process is running for that device
2. If not, launches one with `xcodebuild test-without-building`
3. Waits for WDA's HTTP server to respond
4. Routes the command through WDA

Step 2 **reinstalls the runner app every time**, even when it is already
installed and working. That is normally invisible, but it means the driver
cannot start whenever the device's install channel is unhealthy — see
"Starting WDA without reinstalling" below.

On iOS 17+, the connection goes through tunneld via IPv6. On older iOS, it uses a local port-forward over USB. This is transparent — you don't need to know or care which path is used.

### Auto-Recovery

WDA sessions go stale (device locks, app crashes, timeout). Quern handles this automatically:

- **Session expired**: Creates a new session and retries
- **Connection lost**: Restarts the driver process and retries
- **Transport error**: Same recovery as connection loss

If a command fails and succeeds on retry, that's the recovery system working as designed.

## Finding Elements

When your agent taps a button or reads screen content, it's finding UI elements through the accessibility tree. Understanding how this works helps you build apps that are easier for the agent to work with.

### What the Agent Looks For

Your agent prefers these strategies, in order:

1. **Accessibility identifier** — The fastest and most reliable. If your code sets `accessibilityIdentifier = "login-button"`, the agent finds it instantly.
2. **Label text** — The display text ("Sign In", "Settings"). Works well when labels are unique.
3. **Label + type** — When multiple elements share a label, adding the type narrows it down ("the Button labeled Edit", not "the StaticText labeled Edit").
4. **Advanced queries** — NSPredicate expressions and class chain syntax for complex cases. Your agent knows how to use these when simpler approaches fail.

### Common Element Types

| Type | What |
|---|---|
| `Button` | UIButton, SwiftUI Button |
| `TextField` | UITextField, SwiftUI TextField |
| `SecureTextField` | Password fields |
| `StaticText` | UILabel, SwiftUI Text |
| `Switch` | UISwitch, SwiftUI Toggle |
| `Cell` | Table/collection view cells |
| `NavigationBar` | Navigation bar container |
| `TabBar` | Tab bar container |
| `Alert` | System and custom alerts |
| `SearchField` | Search bars |

### Complex Screens

On screens with many elements (large lists, MapKit, complex collection views), the full accessibility tree query can be slow. Quern has a fallback "skeleton" strategy that queries just the top-level containers (navigation bars, tab bars, toolbars, alerts) and their immediate children. This is automatic — your agent gets usable results even when the full tree times out.

## Designing Apps for AI Automation

How you build your UI directly affects how well the agent can work with it.

### Do

- **Set `accessibilityIdentifier` on key interactive elements.** This is the single most impactful thing you can do. Identifiers are stable across localizations, UI redesigns, and dynamic content.

```swift
loginButton.accessibilityIdentifier = "login-submit-button"
emailField.accessibilityIdentifier = "login-email-field"
```

- **Use standard UIKit/SwiftUI controls.** They have built-in accessibility support. A `UIButton` is tappable and discoverable; a custom `UIView` with a tap gesture isn't (unless you add accessibility traits).

- **Use distinct labels.** Three buttons all labeled "Edit" means the agent can't tell them apart without identifiers or structural context.

### Don't

- **Don't present views over complex screens without truly replacing them.** If you push a simple modal over a complex screen (a list with hundreds of cells, a map view), the elements underneath still appear in the accessibility tree — even though the user can't see them. The agent sees a polluted tree full of irrelevant elements from the screen behind the modal, making it hard to find what's actually on screen. Use proper modal presentation (`.fullScreenCover` in SwiftUI, `modalPresentationStyle = .fullScreen` in UIKit) or remove the underlying view's accessibility when it's covered.

- **Don't rely on complex custom gestures.** WDA supports tap, swipe, and long-press. No pinch-to-zoom, 3D touch, or custom multi-finger gestures. Provide alternative navigation paths if your app uses these.

- **Don't make UI state depend on animations completing.** The agent can tap before an animation finishes. It uses `wait_for_element` to wait for targets to appear rather than guessing timing.

## Common Failure Patterns

When WDA fails to start, Quern parses the runner log and tells your agent what went wrong:

| What You'll Hear | What It Means | What to Do |
|---|---|---|
| "Profile expired" or "Supported platforms empty" | Signing profile is invalid | Tell your agent to rebuild WDA |
| "Device is locked" | Screen lock is on | Unlock the device |
| "App not trusted" | Developer profile not trusted (free accounts) | Settings > VPN & Device Management > Trust |
| "Entitlement mismatch" | WDA was reinstalled with different signing | Tell your agent to force-rebuild WDA |
| "No signing certificate" | Xcode doesn't have a valid cert | Xcode > Settings > Accounts > Manage Certificates |
| "Maximum number of apps" | 3 free-signed apps already installed on the device | Delete a free-signed app from the device (check Settings > General > iPhone Storage for offloaded ones too). **Waiting does not clear this** — that is the separate 10-App-IDs-per-7-days limit |
| "Device is not available" | Device disconnected | Reconnect USB cable |
| "Failed to install the app on the device", `IXRemoteErrorDomain`, "Connection interrupted" | The install channel is unhealthy. The runner itself is fine | Start it without reinstalling (below), or replug the device |
| "WDA did not become responsive" with a healthy runner log | Something local is in the way — often a stale port forward holding the port Quern forwards WDA to (iOS 16 and older; the first device gets 18100) | Check `lsof -nP -iTCP:18100` before blaming WDA |

Runner logs are at `~/.quern/wda/runner-<udid-prefix>.log` if you need to dig deeper.

### Starting WDA without reinstalling

Two failures look identical from the outside — "WDA did not become
responsive" — but mean opposite things. Check the runner log first:

- The log shows a **build or signing** problem → rebuild (`setup_wda`).
- The log shows **`Failed to install the app on the device`** with
  `IXRemoteErrorDomain` / `Connection interrupted` → do *not* rebuild.
  The runner is already installed and healthy; only the install step is
  failing, and every retry will fail the same way.

Confirm the runner is really installed:

```bash
xcrun devicectl device info apps --device <hardware-udid> | grep -i quern
# QuernDriver   dev.quern.driver.xctrunner   1.0   1
```

Then start it without going through `xcodebuild`:

```bash
pymobiledevice3 developer dvt xcuitest dev.quern.driver.xctrunner --udid <hardware-udid>
```

This drives the runner through `testmanagerd` directly, with no install
step. WDA answers `/status` in under ten seconds and Quern picks it up on
the next command — no restart of the server, no rebuild. Leave the process
running; it hosts the session.

Both commands take the hardware UDID (the `00008030-...` form shown by
`xcrun devicectl list devices`), not the CoreDevice identifier that Quern's
`list_devices` reports for the same device.

**Do not try `devicectl device process launch` on the runner.** It reports
success and nothing happens: an `.xctrunner` app is a stub, and XCTest
needs `testmanagerd` to attach and drive the bundle. That is exactly what
the `dvt xcuitest` command above provides.

## Known Limitations

- **No side/power button.** Would kill the WDA process. Can't simulate it.
- **No brightness control.** No API exists. Workaround for dark rooms: face the device down — USB still carries the video signal for live preview.
- **No mute switch.** Hardware switch, not software-controllable.
- **No system UI.** WDA can only interact with the frontmost app. Can't dismiss system alerts (except notification banners), open Control Center, or use Spotlight.
- **Orientation is app-level only.** WDA can rotate the app content but doesn't physically rotate the screen. The live preview always shows the native orientation.
- **Slow on older devices.** Accessibility tree queries on iPhone 8 / iPad Air 2 era devices can be slow. Your agent will increase timeouts or use the skeleton strategy automatically.
