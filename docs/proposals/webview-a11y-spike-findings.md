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

---

# Follow-up spike: Safari Web Inspector on the simulator

**Result: the full DOM channel works today, off the shelf, and it does not disturb the
accessibility bridge.**

## What it took

1. One line in the app, on the internal build only:

   ```swift
   #if INTERNAL
   webView.isInspectable = true   // defaults to false since iOS 16.4
   #endif
   ```

   **Use `#if INTERNAL`, not `#if DEBUG`.** This codebase compiles with `-DINTERNAL` and does
   not define `DEBUG`; a `#if DEBUG` guard silently compiles the line away, the build still
   succeeds, and no inspectable target ever appears. Verify the symbol landed — and note Xcode 16
   puts app code in `Geocaching.debug.dylib`, not the thin launcher binary, so check the dylib.

2. Google's `ios_webkit_debug_proxy` (not an Apple tool; `brew install ios-webkit-debug-proxy`),
   pointed at the simulator's socket:

   ```bash
   SOCK=$(lsof -U | grep -o "/private/tmp/com.apple.launchd.[^ ]*/com.apple.webinspectord_sim.socket" | head -1)
   ios_webkit_debug_proxy -s "unix:$SOCK" -c null:9221,:9222-9250
   curl -s http://localhost:9222/json
   ```

   The `-s/--simulator-webinspector` flag is required. Without it the proxy enumerates over
   usbmux, finds no devices, and reports `ssl recv failed`.

3. A WebSocket client speaking the **Target-wrapped** WebKit protocol. Flat
   `Runtime.evaluate` fails with `'Runtime' domain was not found`; commands must be wrapped in
   `Target.sendMessageToTarget` with the `targetId` from `Target.targetCreated`, and replies
   arrive inside `Target.dispatchMessageFromTarget`. Roughly 20 lines — this is the quirk
   `appium-remote-debugger` exists to absorb, but it is not a large lift.

## What it gives

Against Shareables, live:

| | |
|---|---|
| `document.title` | `Shareables` |
| `document.readyState` | `complete` — **JS readiness, which the a11y tree cannot express** |
| `location.href` | `https://staging.geocaching.com/play/shareables/received?hideLayout=true` |
| elements with `id` | 116 |
| **`[data-testid]`** | **0** |
| buttons | `Learn more`, `Feedback`, `Settings`, `Filter`, plus hidden modal content (`Share experience feedback`, `Cancel`, `Submit`, `Ok`) |

Arbitrary JS evaluation, the real DOM, and console access — everything §3 lists as missing from
the accessibility tree.

**The zero testids confirm §6.** "Testid convention — do this first" is not premature: the page
ships none today. Selection would fall back to `id` attributes (116 present) or CSS/text, which
is exactly the brittleness the convention is meant to remove.

## This channel does not corrupt the accessibility bridge

Important, because it distinguishes the two options at runtime.

**WDA does corrupt it.** After running WDA (and the XCUITest probe), Quern's `get_ui_tree`
returned a bare Application with one element. Killing WDA did not restore it; only a full
simulator reboot did. This matches the known post-XCUITest AX-bridge corruption.

**Web Inspector does not.** After a full session of proxy + WebSocket + JS evaluation, Quern
still returned its normal 5 native elements and `identify_screen` matched Shareables at
confidence `exact`.

That has a direct design consequence: **a runtime hybrid that switches between sim-bridge and
WDA per screen is probably unworkable**, since starting WDA poisons the native tree for the rest
of the boot. Either pick one backend per session, or prefer the Web Inspector channel precisely
because it composes with sim-bridge instead of displacing it.

Open question worth answering before building either: is the corruption caused by WDA
specifically, or by any XCTest process starting and stopping on the simulator?

## Revised cost ladder

| Tier | Cost | Gives | Disturbs a11y bridge |
|---|---|---|---|
| 1. WDA backend on simulators | Quern change only | roles, labels, taps | **Yes — poisons it until reboot** |
| 2. Web Inspector via iwdp | 1 app line + ~20 lines of protocol glue | DOM, testids, JS eval, console, readiness | **No** |
| 3. Agent in the web bundle | web-team coordination + bundle change | cleanest API, transport-independent | No |

Tier 2 turns out to be far cheaper than "fallback only" implied, and is the only option that
composes cleanly with the existing simulator backend.

## Choosing a WebKit protocol client

Three candidates, evaluated 2026-08-31.

| | `ios_webkit_debug_proxy` | `appium-remote-debugger` | `pymobiledevice3` |
|---|---|---|---|
| Origin | Google | Appium | community |
| Language | C binary | TypeScript library | Python |
| Version | 1.9.2 (Jul 2025) | 17.4.1 (published 2026-08-31) | 7.7.1 installed, 11.3.0 latest |
| Activity | ~annual releases, last commit Jun 2025, 20 open issues | very active | active |
| **Simulator support** | **Yes — `-s/--simulator-webinspector`** | Yes (28 source references) | **No** |
| Interface | HTTP + WebSocket daemon, language-neutral | library, must be embedded | CLI + library (`cdp`, `js-shell`, `opened-tabs`) |
| Already a Quern dependency | no | no | **yes** |

**`ios_webkit_debug_proxy` is the only one proven working against a simulator**, and it was
proven here rather than assumed — see the session above. It is also the easiest fit for a Python
server: it exposes an HTTP endpoint for target discovery and a WebSocket per page, so Quern can
drive it without adding a language runtime.

Its drawbacks are real but manageable: an extra binary dependency, a slow release cadence, and a
surface that is CDP-*shaped* rather than CDP — the `Target.sendMessageToTarget` wrapping still
has to be handled on our side.

**`appium-remote-debugger` is the better reference implementation than dependency.** It is
actively maintained and handles the protocol's quirks properly, but it is a Node library rather
than a daemon. Adopting it means putting Node in Quern's runtime path, which is a large cost for
one feature. Reading it to get the protocol details right is worthwhile regardless.

**`pymobiledevice3` would be the ideal fit if it supported simulators.** It is already a Quern
dependency, it is Python, and its `webinspector` subcommand already exposes exactly the surface
we want (`cdp`, `js-shell`, `opened-tabs`). But it reaches devices over usbmux/RSD/tunnels only:
run `pymobiledevice3 webinspector opened-tabs` against a booted simulator and it raises rather
than finding anything.

**That gap is worth considering as an upstream contribution.** Simulator web inspection is a
Unix domain socket at
`/private/tmp/com.apple.launchd.*/com.apple.webinspectord_sim.socket` — no usbmux, no pairing,
no developer disk image. Teaching `pymobiledevice3` to attach to it would give Quern a
Python-native path with no new runtime dependency, and would likely be useful to that project's
other consumers. Until then, `ios_webkit_debug_proxy` is the pragmatic choice.

## Can this channel actually drive the webview?

Yes, through JavaScript — not through synthetic input events. Domains probed on this build:

| Domain | Available |
|---|---|
| `Runtime`, `DOM`, `Page`, `Console`, `Network` | yes |
| **`Input`** (`dispatchMouseEvent` / `dispatchKeyEvent`) | **absent** |
| **`Automation`** (WebDriver-style actions) | **absent** |

Verified end to end: evaluating `[...document.querySelectorAll('button')].find(b => b.innerText.trim() === 'Filter').click()`
opened the Filter dropdown on the device, confirmed by screenshot.

**Works well:** buttons and links via `.click()`, form fill via `.value` plus `input`/`change`
events, scrolling via `scrollIntoView()`, and waiting on arbitrary JS state — the last of which
the accessibility tree cannot do at all.

**Weak or unsafe:**

- **Press-and-drag** must be hand-dispatched as a pointer/touch event sequence. Whether it works
  depends on which event family the component listens to. No hit-testing, no momentum, no native
  gesture recognizers.
- **`.click()` does not respect reality.** It fires on elements that are covered, scrolled out of
  view, or visually disabled. Tests can pass against broken UI. This is precisely why §5.4 treats
  `nativeWebTap` as mandatory rather than polish.

**So the hybrid in §5.4 is the right shape, and it is now cheap:** use this channel to locate the
element and read `getBoundingClientRect()`, then perform a real native tap through Quern's
existing input path. Quern already taps well; the missing piece was only knowing where.

**One integration wrinkle:** converting DOM coordinates to screen coordinates needs the
`WKWebView`'s frame in screen space, and the sim-bridge tree does not expose the webview node at
all — it returns five elements, none of them the webview. That frame has to come from somewhere
else (WDA exposes it, or derive it from the window and nav-bar geometry).
