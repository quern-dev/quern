# Hybrid App UI Automation — Design Notes & Decisions

**Status:** working notes, pre-implementation
**Scope:** webview automation for a standalone player-to-player transfer feature; Quern harness expansion; BLE proximity testing
**Date:** 2026-07-28

---

## 1. Problem statement

Native UI automation (XCUITest, Espresso/Compose) cannot reach content inside our webviews. The feature under test is essentially standalone, with one external dependency: a phone-to-phone Bluetooth LE connection used to **discover and identify nearby players**. The "transfer" itself happens over cell/wifi — BLE is only proximity and identity.

Constraints established during discussion:

| Constraint | Consequence |
|---|---|
| We control all primary webviews | Ship the agent in the bundle; skip injection machinery |
| Iterable-triggered modal webviews exist but are not targets | Treat as interference: suppress, mock, guard |
| Web and mobile have **independent CI pipelines** | Contract testing with a published manifest + pre-deploy gate |
| App version tail is **open-ended** (rare force-updates) | Web contract must be **additive-only, permanently** |
| BLE will be mocked as a roster of test players | Multi-device orchestration drops out of scope |

---

## 2. The technique worth stealing from Appium

Appium's context switching is still fully supported (documented through the current 3.x docs). The valuable insight is *how* it works: it is **not** an extension of native accessibility. Appium opens a **second out-of-band channel directly into the web engine** and multiplexes both channels behind one session.

- **Android:** the WebView exposes a unix domain socket when web contents debugging is enabled. Appium forwards it via `adb` and drives a **version-matched chromedriver over CDP**. Chromedriver/WebView version matching is the perennial failure mode.
- **iOS:** communication goes over the **WebKit remote debugger protocol**, with real-device transport via `appium-ios-device` (historically `ios_webkit_debug_proxy`). Hard gate: the webview must be debuggable — if Safari's remote debugger can't see it, neither can the driver.
- **Atoms:** a portable JS verb layer (from Selenium) implementing find / click / getText / isDisplayed etc.
- **`nativeWebTap`:** translates DOM coordinates into native screen coordinates and performs a *real* native tap. Exists because a synthetic JS `.click()` skips real hit-testing, doesn't dismiss the keyboard, and doesn't trigger native scroll interception.

**Three transferable pieces:** (1) a channel into the engine, (2) a portable JS verb layer, (3) a router presenting one unified surface.

---

## 3. Platform-native equivalents

### Espresso / Compose — mostly already solved

**Espresso-Web** is the atoms approach shipped in-process: it reuses WebDriver atoms via a JavaScript bridge into the WebView. Requires `forceJavascriptEnabled()` since it works exclusively through the JS engine. No chromedriver, no version matching, no ADB forwarding.

Caveats:
- Atoms are frozen at an old Selenium vintage; complex CSS/XPath can misbehave.
- Failure messages are close to useless.
- Only knows `android.webkit.WebView`. Under Compose (`AndroidView { WebView(...) }`) it still works — it's a real WebView in the hierarchy — but there's usually no `R.id`, so match on `isAssignableFrom(WebView::class.java)`.
- Espresso and `composeTestRule` coexist in the same instrumentation, so mixed native/web flows are fine.
- **Idling gap:** `waitForIdle` knows nothing about page loads. Needs an idling resource off `WebViewClient.onPageFinished` plus a JS readiness probe.

### XCUITest — nothing equivalent

The accessibility tree is not sufficient. WebKit does bridge the DOM into accessibility, so `XCUIElementTypeWebView` has descendants and simple links/buttons/static text are reachable — but you lose DOM identity, testids, stable ordering, and any ability to wait on JS state. Adequate for smoke assertions, not for flows.

Two real options:
1. **In-app debug bridge — CHOSEN.** A debug-only listener inside the app; harness talks to it over localhost, and it calls into the WKWebView. See §5.
2. **Talk `webinspectord` directly — fallback only.** Same channel Appium uses, no app changes, but it means reimplementing `appium-remote-debugger`. Note the modern gate: **since iOS 16.4 `isInspectable` defaults to `false`** and must be opted into per-`WKWebView` (plus Web Inspector enabled in Settings on devices; always on for simulators).

**Do set `isInspectable = true` in debug/internal builds regardless** — one line, gives humans Safari Web Inspector, and keeps the fallback path open. Never in release: it's a real exposure surface.

---

## 4. DECISION: ship the web agent *in the web bundle*

**This is the centerpiece decision.** Because we own all primary webviews, we do not need injection at all.

The agent is a small TypeScript bundle compiled into the web app, exposing `window.__quern`. It is:

- **Part of the same bundle as production**, gated behind a build flag or an activation param (`?quernAgent=1`) — *not* a separate build. This avoids the test target being structurally different from prod.
- **Inert until activated.** No listeners, no globals beyond the namespace, no behavioural difference when off.

### Verb surface

```
query(selector)            -> [handle]        // pierces shadow roots
describe(handle)           -> { role, name, text, enabled, visible, rect }
click(handle)
setValue(handle, text)
getText(handle)
isVisible(handle)
scrollIntoView(handle)
rectInViewport(handle)     -> { x, y, w, h }  // CSS px, viewport-relative
waitFor(predicate, timeout)
snapshot(options)          -> pruned semantic tree
verifyContract(manifest)   -> contract check results (see §8)
```

Plus console/error capture: `window.onerror`, `unhandledrejection`, and `console.*` buffered for retrieval.

### What this eliminates

- Proxy response rewriting and CSP header rewriting
- The `file://` / bundled-content problem
- Espresso-Web's frozen-atoms limitation
- chromedriver version matching
- Most of the iOS bridge complexity — it shrinks from "delivery mechanism for a large JS payload" to a thin JSON transport

**Proxy-based agent injection is explicitly out of scope.** Keep the proxy for what it's uniquely good at: response mocking and capture sessions.

**Not covered:** third-party webviews (Iterable, payment, OAuth). Those fall back to the accessibility tree plus native coordinate taps. See §7.

---

## 5. How Quern communicates with the agent during interactions

```
Claude Code
    │  MCP
    ▼
  Quern  ──── transport ────►  in-app bridge  ──►  window.__quern  ──►  DOM
    │                              (per platform)
    └──── native automation ───►  XCUITest / UiAutomator / Espresso
```

### 5.1 Transports

**iOS (primary):** debug-build-only listener inside the app. Quern → localhost HTTP/socket → app → **`callAsyncJavaScript`** on the target `WKWebView` → agent verb → JSON result back.

Use `callAsyncJavaScript` rather than `evaluateJavaScript` specifically because it:
- awaits promises properly (essential for `waitFor` and settling),
- takes a **frame** parameter (solves iframes),
- takes a **content world** parameter (isolated world, so the agent can't be perturbed by page script).

**Android (primary):** `WebView.evaluateJavascript` from instrumentation, in-process. Same JSON protocol.

**Fallback (either platform):** WebKit remote debugger / CDP from the host, used only if a build cannot carry the bridge.

### 5.2 Wire protocol

JSON envelope with **correlation IDs**, since everything is async and multiple calls may be outstanding:

```json
{ "id": "q-0417", "verb": "query", "args": { "selector": "[data-testid=transfer-confirm]" },
  "webContextId": "wv-1", "frame": null, "timeoutMs": 5000 }
```

Responses carry the same `id`, a status, and either a result or a structured error (`not_found`, `ambiguous`, `not_visible`, `timeout`, `detached`). Structured error kinds matter — they're what let the agent and the contract gate produce actionable messages rather than "element not found."

### 5.3 Handle model — non-modal, no context switching

**Do not copy Appium's modal context switch.** A stateful "which mode am I in" toggle is workable for a human writing a test file and actively bad for an LLM agent: Claude loses track of the current mode, and the meaning of every tool silently changes underneath it.

Instead:

- `get_ui_tree` returns **one unified tree**. Webview subtrees are expanded inline and marked `kind: "web"`, carrying `webContextId`, owning native frame, and selector.
- **Handles are opaque and self-describing.** `tap_element` resolves a DOM node without the caller needing to know it is one.
- Mode becomes an implementation detail of handle resolution, never agent-visible state.
- `list_web_contexts` exists for diagnostics only.
- `get_ui_tree(scope: webContextId)` allows scoping for token control.

This is where Quern can beat Appium specifically for agent consumption.

### 5.4 Coordinate conversion and tap semantics

`rectInViewport` returns CSS pixels; Quern converts to device screen coordinates using:

- the webview's frame in screen coordinates (native side knows this),
- `window.devicePixelRatio` / CSS-px scale,
- current scroll offsets,
- any pinch zoom,
- on iOS additionally `WKWebView` `contentInset` and safe-area insets.

**`tap_element` on a web node defaults to a native tap at converted coordinates** — real hit-testing, keyboard dismissal, native scroll interception all behave correctly. `mode: "js"` is an explicit opt-in escape hatch. This mirrors Appium's `nativeWebTap` and is not optional polish; JS clicks silently diverge from user behaviour.

### 5.5 Token budget — the actual constraint

Capability is the easy part; DOM trees are enormous. `snapshot()` must prune aggressively, mirroring whatever `get_screen_summary` already does natively:

- interactive + text-bearing nodes only
- collapse wrapper elements
- prefer `data-testid` / `aria-label` / role over generated class soup
- cap depth
- dedupe repeated list items with a count
- scope to a single web context on request

### 5.6 Integration with existing Quern subsystems

- **Logs:** pipe agent-captured console errors, `onerror`, and `unhandledrejection` into the existing log pipeline as a new source, so `get_errors` covers web failures alongside native ones.
- **Screenshots:** `take_annotated_screenshot` annotates DOM elements too, via §5.4 conversion. Cheapest grounding mechanism available to an agent.
- **Landmarks:** web screen signatures = URL pattern + testid presence. `identify_screen` then becomes the detector for "the web team shipped a change that broke the flow" — arguably more valuable for web than native, since web changes without an app release.
- **Proxy:** `set_mock` for deterministic web content; `start_capture_session` bracketing a web interaction to answer "what did that tap actually request."

### 5.7 New / changed Quern tool surface

| Tool | Change |
|---|---|
| `get_ui_tree` | inline web subtree expansion, `kind: "web"`, `scope` param |
| `get_screen_summary` | web pruning parity |
| `tap_element` | resolves web handles; native-coordinate tap default, `mode: "js"` opt-in |
| `wait_for_element` | accepts web predicates via agent `waitFor` |
| `take_annotated_screenshot` | annotates DOM elements |
| `list_web_contexts` | new, diagnostics only |
| `export_web_contract` | new — derives contract manifest from landmarks (§8) |
| log sources | new webview console source |

---

## 6. Testid convention — do this first

Agree **one testid convention shared across native and web** before any harness work. If every interactive element carries a stable ID from the same namespace on both sides:

- the agent's `query` becomes trivial,
- landmark signatures are uniform across native and web screens,
- the accessibility-tree fallback becomes usable for webviews we don't own,
- Espresso-Web's weak locators stop mattering.

A week of coordination that removes the flakiness tax which makes most hybrid suites miserable. **Do not do this without the enforcement gate in §8** — a convention without enforcement decays.

---

## 7. Iterable modal webviews

Interference, not targets. Goal is determinism. Three layers, all of them:

1. **Suppress at the SDK.** iOS: `isAutoDisplayPaused`. Android: `setAutoDisplayPaused(true)` — stops automatic display while keeping the local queue in sync; `setAutoDisplayPaused(false)` resumes. Gate on a UI-test launch argument. Note that while paused you can still call `showMessage` manually, so the same switch gives deliberate coverage of the modal path. (`inAppConfigDisplayInterval` is a weaker variant of the same idea; default is 30s.)

2. **Mock at the proxy as belt-and-braces.** SDK pausing depends on app wiring being correct on every launch path, and in-app messages arrive via **silent push** with the SDK showing them on foreground — timing is not ours to control. The queue is pulled via `GET /api/inApp/getMessages`. Mock it empty with `set_mock`: scenario-scoped, no app change, no launch-arg plumbing. **Invert it** — return a fixed message — for a deterministic Iterable test instead of "send a campaign and hope."

3. **Guard anyway.** Suppression will occasionally fail; you want a diagnosable failure. Build a **generic interstitial guard**: a landmark set for "unexpected modal appeared," checked before and after actions, with an associated dismiss. Iterable close controls resolve to `iterable://dismiss`, handled internally by the SDK. Reached via accessibility tree + native coordinate tap.

**Cannot inject into these.** The SDK owns the webview and doesn't set `isInspectable`. One theoretical escape hatch: the message HTML arrives inside the `getMessages` payload rather than being fetched at display time, so rewriting that payload is the only place the agent could be slipped in. Verify against the SDK version before relying on it.

---

## 8. Contract testing across independent pipelines

Mobile suite = **consumer**; web app = **provider**. This is consumer-driven contract testing (cf. Pact), and the mechanism is a published artifact plus a pre-deploy gate — not a shared pipeline.

**Governing constraint: the gate must be fast and web-native or the web team will route around it.** Anything requiring a simulator, a mobile build artifact, or device boot gets marked flaky and disabled within a month.

### Tiers

- **Tier 1 — per-PR in the web repo.** Headless Playwright against the built bundle, calling `__quern.verifyContract(manifest)`. Sub-minute, no devices, entirely inside existing web tooling. Catches the overwhelming majority: renamed testids, restructured components.
- **Tier 2 — pre-deploy back-check.** Same harness against deployed staging/canary, asserting **semantics** not presence: resolves to exactly one node, visible, enabled, expected role. "Present but behind an overlay" and "selector now matches three nodes" are real breakages a presence check waves through.
- **Tier 3 — nightly / RC.** Real Quern run on device against staging. The only tier that catches what selectors structurally cannot: unreachable-without-scrolling layout changes, native↔web handoff breaks. Fine at nightly cadence, fatal as a PR gate.

### Manifest

- **Generate it, don't maintain it.** Derive from loaded landmark sets via `export_web_contract`. A hand-kept list drifts from what the suite actually asserts within a couple of sprints, at which point the gate is theatre.
- Publish as plain JSON, distributed as an npm package (path of least resistance — the consumer is a web repo). Renovate/Dependabot handles freshness; fail if the pinned version falls more than N releases behind.

### The open-tail rule

Because the version tail is open-ended, **the contract is additive-only, permanently.** A selector can never be removed — there is always some install depending on it. Practical consequences:

- One monotonically growing union, not a per-version matrix. Simpler to enforce.
- Every testid mobile depends on is **permanent API surface from the moment it ships**. Keep the depended-on set small and semantic (`transfer-confirm`, not a structural path). Be stingy about minting new ones.
- If this becomes untenable, the fix is architectural: **version the content endpoint** (`/v3/transfer`) so old installs keep receiving old content. Converts an unbounded compatibility surface into a bounded set of retirable bundles. Much smaller lift for a standalone feature than app-wide. Worth deciding before the union has 200 entries.

### Governance

- **Actionable failure messages.** Distinguish missing / present-but-hidden / present-but-ambiguous / moved-URL, and name the mobile flow that depends on it. A web dev who can't tell what they broke will assume the gate is wrong.
- **Sanctioned way to break it.** Without an escape hatch the gate is a hostage situation and gets disabled during the first urgent release. Treat selectors as a deprecated API: a PR marks one deprecated with an expiry, mobile has N releases to migrate, the selector survives until then. Web can always ship; they just can't *silently* remove.

---

## 9. Bluetooth / proximity testing

### Reframe

BLE is only a **trigger** producing "peer X is near you at strength Y." The transfer is a network operation. Therefore **most of this feature needs no radios at all** — UI, transfer handshake, error paths, and the network call are all testable against a faked peer event.

### Platform reality (asymmetric)

- **iOS Simulator has no Bluetooth, period.** Core Bluetooth requires real devices; on the simulator the central manager reports powered-off. Nordic's **CoreBluetoothMock** exists for this and drops in with minimal refactoring — but historically supports **only the central manager**, with connection events and L2CAP unsupported. Phone-to-phone means each device is simultaneously central *and* peripheral, so our own seam is likely needed regardless. **Verify before relying on it.**
- **Android emulators can do real BLE to each other.** The emulator's virtual Bluetooth controller is **netsim** (built on RootCanal); multiple clients on the same netsim process talk over a virtual radio link layer. Google notes it is recent and still evolving, and docs drift from emulator versions.

Do not let Android's stronger position set the strategy — it produces coverage that can't be mirrored on iOS. Build around the seam; treat netsim as a bonus Android-side integration check.

### DECISION: mock a BLE state containing test players; seam at the *peer* level

Abstract `PeerDiscovery` exposing `peersInRange: [(peerId, rssi, lastSeen)]` — **not** a wrapper around `CBCentralManager` / `BluetoothAdapter`. Rationale:

- The fake is trivial and **platform-identical**, which matters because one harness drives both platforms.
- Tests read like the product, not like GATT plumbing.
- It's the level where the real bugs live: threshold hysteresis, peer churn, stale peers lingering after someone walks away, RSSI flapping at the boundary, duplicate peer IDs.

**Consequence: multi-device orchestration drops out of scope.** One device plus a fake roster covers discovery UI, threshold logic, transfer initiation, and every error path. If the receiving player must actively accept, stub the *server* responses representing that acceptance rather than booting a second phone — bilateral coverage on a single device, using the proxy we already have.

### Three design requirements for the mock

1. **Scriptable timeline, not a static snapshot.** A static roster of N players tests the happy path and nothing else. The bugs are in transitions: a peer appears mid-scroll, RSSI drifts across the threshold and back, a peer vanishes while the confirm sheet is open, the roster churns as the transfer commits. The fake needs mutable state drivable at runtime — the difference between a fixture and a test instrument.

2. **Provision BLE roster and server-side players together, atomically.** If `player-42` appears in the mocked BLE state but the backend has never heard of them, failures look like Bluetooth bugs and are actually fixture mismatches. Make the fixture one named artifact spanning both sides: peer roster + seeded accounts (or + the `set_mock` responses for player lookup).

3. **Assume the mock will drift from the radio; build the defence now.** This is the real cost of mocking. Fakes emit polite, well-ordered events. Real BLE stacks emit duplicates, out-of-order callbacks, peers appearing with null identity that populate a beat later, unknown-RSSI sentinels, and callbacks on unexpected threads. A well-behaved fake produces a green suite over a broken product.
   - **Make the fake hostile by default:** duplicates, reordering, unknown signal values, rapid churn. If the app only works against a polite mock, that's a day-one finding.
   - **Record real traces and replay them.** Instrument two real phones for a handful of sessions, capture the actual event stream at the seam, keep those recordings as fixtures alongside hand-written ones.

### Quern surface for proximity

Frame it next to `set_location` — Quern already simulates GPS, and peer proximity is the same category of environment simulation. RSSI sweeps are the analogue of dragging the location pin, and sweeping the threshold is a test that is *impossible* with real radios without physically walking around.

| Tool | Notes |
|---|---|
| `set_peer_state` | declarative initial roster |
| `add_peer` / `remove_peer` / `set_peer_rssi` | live mutation during a running test |
| `replay_peer_trace` | recorded sessions — **mirrors the existing `replay_flow` pattern exactly** |

Same conceptual model as the proxy, so it should feel native to the harness rather than bolted on.

**Implementation strictness:** inject the fake *as* the real implementation of the seam in test builds. Do not let test code reach past the seam to poke UI directly — that tests the double instead of the app and destroys the property that makes record-and-replay work.

### Highest-value tests

- **In-flight bilateral states via the proxy.** `set_intercept` + `release_flow` holds the transfer request mid-flight so pending state can be asserted on both sides: double-submit, timeout, one side committing while the other doesn't. These are the bugs this class of feature actually ships, and they're miserable to reproduce manually.
- **Fault injection:** BT handshake succeeds, network call fails.
- **`start_capture_session`** bracketing the tap for the causal chain.
- **Permissions:** `grant_permission` already covers the Android 12+ scan/advertise/connect split and the iOS Bluetooth prompt. Denied-permission paths are real user states and cheap to cover.
- **One real-hardware nightly test:** two phones, one discovery, assert the emitted event stream conforms to the shape the fakes produce. A contract test on the seam — the canary for mock drift.
- **Adversarial:** does the server independently verify proximity, or trust a client-asserted "this peer is near me"? If the latter, the fake-peer injection built for testing is also the spoofing tool. Worth knowing deliberately.

---

## 10. Sequencing

| Phase | Work |
|---|---|
| 0 | Testid convention (§6) + manifest export + Tier-1 Playwright gate (§8). Cheap, protects everything after it. |
| 0 | Spike: agent evaluating in one real webview per platform via cheapest transport. Measure whether the a11y tree alone would suffice — a week of investigation vs. a quarter of building. |
| 1 | Agent in bundle (§4) + iOS in-app bridge + Android `evaluateJavascript` transport (§5.1–5.2) |
| 1 | `get_ui_tree` inline expansion, handle resolution, native-coordinate tap (§5.3–5.4). Unblocks most flows. |
| 2 | Pruning/summary parity, console log source, annotated screenshots (§5.5–5.6) |
| 2 | Peer seam + `set_peer_state` + hostile-by-default fake (§9) |
| 3 | Interstitial guard (§7), web landmarks, Tier-2/3 gates |
| 3 | `replay_peer_trace` + recorded traces; nightly real-hardware seam contract test |
| — | **Dropped:** proxy-based agent injection; multi-device choreography (revisit only if bilateral tests can't be stubbed) |

---

## 11. Open questions

1. **Is peer identity carried in the BLE payload** (advertisement data / GATT read) **or is BLE only a proximity signal with identity resolved server-side from an ephemeral token?** Determines whether the fake must carry identity payloads and versioning, and how much of the trust boundary is exercisable without radios.
2. Does the receiving player have to **actively accept**? Confirms whether server-stubbing fully replaces a second device.
3. Does **CoreBluetoothMock** cover the peripheral role adequately, or do we build our own seam end-to-end? (Assume the latter until proven.)
4. Appetite for **versioning the content endpoint** (§8) given the open-ended tail.
5. Is **netsim** viable on our Android CI hosts, or is it nightly-on-hardware only?

---

## 12. References

- Appium — Managing Contexts: https://appium.io/docs/en/3.4/guides/context/
- Appium XCUITest driver — installation & webview requirements: https://appium.github.io/appium-xcuitest-driver/10.14/installation/
- Appium XCUITest driver — web & context commands (atoms, native web tap): https://deepwiki.com/appium/appium-xcuitest-driver/4.3-web-and-context-commands
- WebdriverIO `switchContext`: https://webdriver.io/docs/api/mobile/switchContext/
- Espresso Web: https://developer.android.com/training/testing/espresso/web
- WebKit — Enabling the Inspection of Web Content in Apps (`isInspectable`, iOS 16.4+): https://webkit.org/blog/13936/enabling-the-inspection-of-web-content-in-apps/
- Iterable — In-App Messages on Android (`setAutoDisplayPaused`): https://support.iterable.com/hc/en-us/articles/360035537231-In-App-Messages-on-Android
- Iterable — In-App Messages on iOS (`isAutoDisplayPaused`, silent push): https://support.iterable.com/hc/en-us/articles/360035536791-In-App-Messages-on-iOS
- Iterable — Testing & Troubleshooting In-App Messages (`GET /api/inApp/getMessages`): https://support.iterable.com/hc/en-us/articles/360035623391-Testing-and-Troubleshooting-In-App-Messages
- Apple TN2295 — Core Bluetooth in the iOS Simulator: https://developer.apple.com/library/archive/technotes/tn2295/_index.html
- Nordic CoreBluetoothMock: https://devzone.nordicsemi.com/guides/short-range-guides/b/bluetooth-low-energy/posts/ios-corebluetooth-mock
- Bumble — Android emulator Bluetooth / netsim / RootCanal: https://google.github.io/bumble/platforms/android.html
