# WebDriverAgent Guide

WebDriverAgent (WDA) is how Quern controls physical iOS devices. It's an open-source test runner that exposes an HTTP API for tapping, swiping, typing, and reading the accessibility tree. Quern builds, installs, and manages WDA automatically — but understanding how it works helps when things go sideways.

## What WDA Is

WDA is an XCTest bundle that runs on the device as a test runner process. It hosts an HTTP server (port 8100) that accepts commands like "tap at coordinates", "find element by label", "get the accessibility tree". Quern's `tap_element`, `type_text`, `get_screen_summary`, and related tools all talk to WDA under the hood.

On simulators, Quern uses `idb` (Facebook's iOS Development Bridge) instead, which talks to the accessibility framework directly. WDA is only needed for physical devices because idb doesn't support them.

## Setup

### First Time

Call `setup_wda` with the device's UDID. This:

1. Discovers your signing identities from Xcode preferences
2. Clones the WDA repo to `~/.quern/wda/repo/`
3. Customizes it (Quern app icon, display name "QuernDriver", bundle ID `dev.quern.driver`)
4. Builds with `xcodebuild build-for-testing`
5. Installs on the device

The built artifact is cached by team ID. Subsequent calls skip the build unless you pass `force: true` or change teams.

### Multiple Signing Identities

If you have more than one Apple Developer team in Xcode, `setup_wda` returns a list and asks you to pick:

```json
{
  "status": "needs_identity_selection",
  "identities": [
    {"team_id": "ABC123", "team_name": "Personal", "team_type": "Individual"},
    {"team_id": "XYZ789", "team_name": "Acme Corp", "team_type": "Organization"}
  ]
}
```

Call again with `team_id: "ABC123"` to proceed.

## Free vs Paid Developer Accounts

This matters more than you'd think.

### Paid Account ($99/year — Individual or Organization)

- Provisioning profiles last 1 year
- Wildcard App IDs (one ID covers all bundle identifiers)
- No app count limits
- WDA just works, indefinitely

### Free Account (Apple ID, no enrollment)

- Provisioning profiles expire after **7 days**
- No wildcard App IDs — each unique bundle identifier uses a slot
- **~3 active App ID slots** per 7-day rolling window
- WDA uses **2 slots** (`dev.quern.driver` + `dev.quern.driver.xctrunner`), leaving ~1 for your actual app
- Must re-run `setup_wda` with `force: true` every 7 days
- The device must explicitly trust the developer profile (see below)

### Device Trust

On both free and paid accounts, the first time you install a development app on a device, iOS requires the user to trust the developer profile:

**Settings > General > VPN & Device Management > [your developer name] > Trust**

If you skip this, WDA will fail to launch with an unhelpful error. The runner log will show something about being unable to launch the app.

## How the Driver Works

When you interact with a physical device, Quern automatically:

1. Checks if a WDA driver process is running for that device
2. If not, spawns `xcodebuild test-without-building` targeting the device
3. Waits for WDA's HTTP server to respond on port 8100
4. Routes your tool call through the WDA HTTP API

The driver process persists across server restarts (tracked by PID in `~/.quern/wda/wda-state.json`). You can manually start/stop it with `start_driver` and `stop_driver`.

### Auto-Recovery

WDA sessions can go stale (device locks, app crashes, timeout). Quern handles this automatically:

- **Invalid session**: If WDA returns "session does not exist", Quern creates a new session and retries
- **Connection error**: If WDA's HTTP server becomes unreachable (cable disconnected briefly, WDA crashed), Quern restarts the driver process and retries
- **Transport error**: Network-level failures trigger the same connection recovery

You shouldn't need to think about session management. If a tool call fails and retrying fixes it, that's the recovery system working.

## Designing Apps for WDA Automation

WDA interacts with your app through the accessibility tree. How you build your UI directly affects how well automation works.

### Do

- **Set `accessibilityIdentifier` on key interactive elements.** Buttons, text fields, switches, cells. This is the single most impactful thing you can do. Identifiers are stable across localizations and UI changes.

```swift
loginButton.accessibilityIdentifier = "login-submit-button"
emailField.accessibilityIdentifier = "login-email-field"
```

- **Use standard UIKit/SwiftUI controls.** They have built-in accessibility support. A `UIButton` is tappable and discoverable; a custom `UIView` with a tap gesture isn't (unless you add accessibility traits).

- **Keep screens reasonably simple.** A screen with 200+ interactive elements makes `get_ui_tree` slow and `tap_element` ambiguous. If you have a long list, accessibility identifiers on cells help disambiguation.

- **Use distinct labels.** If you have three buttons all labeled "Edit", `tap_element(label="Edit")` returns an ambiguous match. Either use identifiers or more descriptive labels.

### Don't

- **Don't use overlays/modals that cover the entire screen.** Full-screen overlays block WDA from seeing elements underneath. If you have a loading overlay, make sure it's dismissible or has a timeout.

- **Don't rely on complex custom gestures.** WDA supports tap, swipe, and long-press. It doesn't support pinch-to-zoom, 3D touch, or custom multi-finger gestures. If your app requires these, provide alternative navigation paths.

- **Don't make UI state depend on animations completing.** WDA can tap before an animation finishes. Use `wait_for_element` to wait for the target element to appear rather than guessing animation durations.

- **Don't put critical UI behind scroll views without identifiers.** WDA can scroll, but finding an element that's off-screen requires scrolling and re-checking the tree. Identifiers on scroll view content make `tap_element` work even when the element needs to be scrolled into view.

## Common WDA Failure Patterns

When WDA fails to start, Quern parses the runner log and returns a diagnosis. Here are the common ones:

| Error in Log | What It Means | Fix |
|---|---|---|
| "Supported platforms for the buildables is empty" | xctestrun file is invalid (expired profile, signing changed) | `setup_wda(force: true)` |
| "The device is locked" | Screen lock is on | Unlock the device |
| "Unable to launch" | App not trusted on device | Settings > VPN & Device Management > Trust |
| "application-identifier entitlement does not match" | WDA was reinstalled with different signing | `setup_wda(force: true)` |
| "No signing certificate" | Xcode doesn't have a valid cert for this team | Xcode > Settings > Accounts > Manage Certificates |
| "maximum number of apps for free development profiles" | Free account App ID limit reached | Wait for slots to expire (7 days) or use a paid account |
| "Device is not available" | Device disconnected | Reconnect USB cable |

Runner logs are at `~/.quern/wda/runner-<udid-prefix>.log`.

## Known Limitations

- **No side/power button.** Pressing the side button would kill the WDA process (it puts the app in background). WDA can't simulate it.
- **No brightness control.** No API exists for this. The workaround for dark-room testing: face the device down — USB still carries the video signal for live preview.
- **No mute switch.** The hardware mute switch isn't software-controllable.
- **No system UI interaction.** WDA can only interact with the frontmost app. It can't dismiss system alerts (except notification banners), interact with Control Center, or use Spotlight. There are partial workarounds via `press_button(button="home")` for specific scenarios.
- **Orientation control is app-level only.** WDA can set `XCUIDevice.shared.orientation` which rotates the app content, but doesn't physically rotate the screen. The CoreMediaIO preview always captures in the device's native orientation.
- **Performance on older devices.** The accessibility tree query (`/source`) can be slow on older devices (iPhone 8, iPad Air 2). Use `source_timeout` parameter to increase the timeout, or use the `skeleton` strategy for complex screens.
