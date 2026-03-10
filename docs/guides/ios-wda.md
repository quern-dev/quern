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

- Profiles expire after **7 days**
- No wildcard App IDs — each bundle identifier uses a slot
- **~3 active App ID slots** per 7-day window
- Quern Driver uses **2 slots** (`dev.quern.driver` + `dev.quern.driver.xctrunner`), leaving ~1 for your actual app
- Must re-setup WDA every 7 days

**If you're on a free account**, tell your agent. It will warn you about slot limits and profile expiry. When Quern Driver stops working after 7 days, tell your agent to rebuild it:

> "Rebuild WDA — my profile expired"

#### Device Trust (Free Accounts Only)

Free developer accounts require you to manually trust the developer profile on the device before Quern Driver can run:

**Settings > General > VPN & Device Management > [your developer name] > Trust**

You need to do this on the device itself — it's a one-time step per developer identity per device. If Quern Driver won't launch at all and you're on a free account, this is almost certainly why. The runner log will show something about being unable to launch the app.

### How Your Agent Detects This

When WDA setup discovers your account type, it includes warnings in the response. Your agent should surface these to you — things like "this is a free account, profiles expire in 7 days" and "Quern Driver is using 2 of your ~3 App ID slots."

## How the Driver Works

When your agent interacts with a physical device (tap, screenshot, read screen), Quern automatically:

1. Checks if a WDA driver process is running for that device
2. If not, launches one
3. Waits for WDA's HTTP server to respond
4. Routes the command through WDA

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
| "Maximum number of apps" | Free account slot limit | Wait 7 days for slots to free up, or use a paid account |
| "Device is not available" | Device disconnected | Reconnect USB cable |

Runner logs are at `~/.quern/wda/runner-<udid-prefix>.log` if you need to dig deeper.

## Known Limitations

- **No side/power button.** Would kill the WDA process. Can't simulate it.
- **No brightness control.** No API exists. Workaround for dark rooms: face the device down — USB still carries the video signal for live preview.
- **No mute switch.** Hardware switch, not software-controllable.
- **No system UI.** WDA can only interact with the frontmost app. Can't dismiss system alerts (except notification banners), open Control Center, or use Spotlight.
- **Orientation is app-level only.** WDA can rotate the app content but doesn't physically rotate the screen. The live preview always shows the native orientation.
- **Slow on older devices.** Accessibility tree queries on iPhone 8 / iPad Air 2 era devices can be slow. Your agent will increase timeouts or use the skeleton strategy automatically.
