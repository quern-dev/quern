# Spike: can the accessibility tree see inside our webviews?

**Probe branch:** `spike/webview-a11y-probe` in the Geocaching iOS repo (disposable)
**App under test:** Geocaching iOS — used as the prototype hybrid app
**Date:** 2026-08-31
**Build:** Geocaching 10.19.0 (develop), iPhone 16 Pro / iOS 18.6 simulator
**Account:** `deptest18` (Basic)
**Screen:** Profile → Shareables (`AuthorizedWebView`, a plain `WKWebView`)

Answers the Phase 0 gate in [`hybrid-automation-design-notes.md`](./hybrid-automation-design-notes.md) §10:

> Measure whether the a11y tree alone would suffice — a week of investigation vs. a quarter of building.

## Answer

**The DOM is already bridged into accessibility. Quern just cannot see it on simulators.**

Two accessibility clients, same screen, same build, same moment:

| Query | Quern (`get_ui_tree`) | XCUITest |
|---|---|---|
| total elements | **5** | **47** |
| webViews | 0 | 3 |
| webview descendants | 0 | 42 |
| buttons | 0 | 5 |
| staticTexts | 0 | 11 |
| links | 0 | 1 |

Quern returns only native chrome: Application, nav Group, Back, Heading, `_Share button`.
`mode: "flat"` returns the same 5.

XCUITest returns the real content — `Learn more`, `Feedback`, `Settings`, `Received`,
`Your Shareables`, `Shareables from other players (1)`, `Filter`, `Newest first`,
`Cookies!`, `gamecoug` — plus four latent web dialogs that are not even on screen
(`Replace your existing report?`, `Remove this Shareable from your collection?`,
`Manage this Shareable`, `We received your report`).

## Root cause

`quern/server/device/controller_ui.py:132-133`:

> Android devices use U2Backend; iOS physical devices use WdaBackend; iOS simulators
> use SimBridgeBackend (preferred) or IdbBackend (fallback).

WDA is XCTest-based — the same plumbing that saw all 42 elements. It is only used for
**physical** iOS devices. On simulators Quern uses sim-bridge/idb, which do not surface
`WKWebView` descendants.

There is **no webview-specific filtering anywhere in Quern's source**, so nothing is being
deliberately discarded. The content never arrives from the backend.

## What this changes

- The design doc's §3 claim is **correct**: "WebKit does bridge the DOM into accessibility …
  simple links/buttons/static text are reachable."
- The knowledge base's claim that these screens are opaque is **correct for Quern**, but the
  cause is Quern's simulator backend, not the app and not WebKit.
- A large share of read-only automation — assert text, find and tap buttons, verify tab state —
  is reachable **today**, for far less than the agent-in-bundle work.
- The agent is still needed for what §3 says the a11y tree cannot give: DOM identity, testids,
  stable ordering, waiting on JS state. "Adequate for smoke assertions, not for flows."

Phase 0's week of investigation was worth it: the quarter of building is still justified for
flows, but it is no longer the only path to any webview automation at all.

## Not the cause

- **`isInspectable`** — never set anywhere in the app, so it defaults to `false` on iOS 16.4+.
  It gates Safari Web Inspector, **not** the accessibility bridge, which works regardless.
  Still worth setting in debug/internal builds (one line, keeps the §3 fallback open).
- **App-side suppression** — `WebWrapperView.makeUIView` is a bare `WKWebView()`. No
  `accessibilityElementsHidden`, no `isAccessibilityElement` overrides anywhere near it.
- **Content not loaded** — the screenshot shows everything rendered.

## VERIFIED: WDA works against a simulator and sees everything

Ran Quern's own WebDriverAgent checkout (`~/.quern/wda/WebDriverAgent`) against the **simulator**,
with the app on the same Shareables screen:

```bash
xcodebuild -project ~/.quern/wda/WebDriverAgent/WebDriverAgent.xcodeproj \
  -scheme WebDriverAgentRunner -destination 'id=<SIM-UDID>' \
  -skipPackagePluginValidation -skipMacroValidation CODE_SIGNING_ALLOWED=NO test
curl -s "http://localhost:8100/source?format=json"
```

WDA starts fine on a simulator (`"simulatorVersion": "18.6"`), needs no code signing, and returns:

| | Quern sim-bridge/idb | WDA, same sim, same screen |
|---|---|---|
| nodes | **5** | **63** |
| WebView nodes | 0 | **3** |
| Buttons | 0 | 7 |
| StaticTexts | 0 | 12 |
| Links | 0 | 1 |

Every rendered element is present: `Learn more`, `Feedback`, `Settings`, `Received`,
`Your Shareables` (exposed as a **Link**), `Filter`, `Newest first`, `Cookies!`, `gamecoug`.

**Note the depth: web content sits at depth 20-21.** Worth knowing for whatever consumes it —
any pruning or summarisation will need to handle deep nesting, and this is the token-budget
problem the design notes flag in §5.5, arriving earlier than expected.

## Where the gap actually is

Not in Quern's Python. `sim_bridge.describe_all` accepts `snapshot_depth` but never forwards it —
`_fetch_nested` sends `{"cmd": "describe-ui", "nested": True}` with no depth — so this is not a
depth cap being applied at that layer. The sim-bridge's own AXPTranslator walk does not descend
into `WKWebView` subtrees at all, and neither does idb. That is below Quern, in the native
component.

Which makes the pragmatic fix *not* "fix the native walk" but "use the backend that already
works."

## Suggested next step

**Verified above — WDA works on simulators.** What remains is a Quern change: allow
`WdaBackend` on simulators, either always or as a fallback when the sim-bridge tree contains a
`WKWebView`. `controller_ui._ui_backend` is the single decision point. The WDA tools
(`setup_wda`, `start_driver`, `stop_driver`) are also documented and gated as physical-device
only, so they would need to accept simulator UDIDs.

Trade-off worth measuring before committing: WDA is slower to start than sim-bridge and adds a
running xcodebuild process per device. A hybrid — sim-bridge by default, WDA when a webview is
detected — keeps the fast path for native screens.

This is a Quern fix, not an app fix, and it needs no web-bundle changes.

## Reproducing

The probe is `testWebViewAccessibilityProbe` in
`GeocachingUITests/Tests/Tabs/Profile/ProfileTests.swift` on the probe branch of the Geocaching
iOS repo. It is disposable —
added to an existing file so no pbxproj target change was needed.

```bash
xcodebuild -workspace Geocaching.xcworkspace -scheme "Internal (UI only)" -sdk iphonesimulator \
  -destination 'id=<UDID>' -skipPackagePluginValidation -skipMacroValidation \
  -only-testing:GeocachingUITests/ProfileTests/testWebViewAccessibilityProbe test
```

`-skipPackagePluginValidation` is required: without it the build fails on SwiftLint plugin
trust validation, and both xcodebuild and Quern's `build_and_install` report
"Build failed. 0 error(s)" with no cause.
