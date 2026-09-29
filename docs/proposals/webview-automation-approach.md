# Webview Automation: the in-bundle agent we did not ship

**Status:** decided and shipped, the other way. Web content is reached through
WebKit's Web Inspector protocol (`server/device/webinspector.py`, landed
2026-09-01 in 0.15.0). The alternative described here — a Quern agent compiled
into the app's own web bundle — was designed in some detail and then abandoned.
**Why this file exists:** the rejected design is more attractive than it looks,
and someone will propose it again. This is the argument against it, and the
measurement that settled it.
**Supersedes:** `docs/proposals/hybrid-automation-design-notes.md` and
`webview-a11y-spike-findings.md`, drafted 2026-08-31 to 09-01 and not merged.
Those, and the working log alongside them, were deleted rather than merged once
this shipped; their measured findings are in §4 below and the three questions
they left unmeasured are #332.

---

## 1. The problem

The accessibility tree does not descend into `WKWebView` on simulators, so web
content is invisible to sim-bridge and idb. Measured on one screen, one build,
one moment, two accessibility clients:

| Query | Quern (`get_ui_tree`) | XCUITest |
|---|---|---|
| total elements | 5 | 47 |
| webViews | 0 | 3 |
| webview descendants | 0 | 42 |
| buttons | 0 | 5 |
| staticTexts | 0 | 11 |
| links | 0 | 1 |

That table is the whole reason the design changed, and it is the one piece of
the original notes worth carrying forward verbatim. The DOM *is* bridged into
accessibility; Quern simply could not see it from where it was standing.

## 2. What we considered

An agent shipped **inside the app's own web bundle**, exposing `window.__quern`,
gated behind a build flag or an activation parameter, inert until switched on.
It would have offered a verb surface roughly like:

```
query(selector) -> [handle]      // pierces shadow roots
describe(handle) -> { role, name, text, enabled, visible, rect }
click / setValue / getText / isVisible / scrollIntoView
rectInViewport(handle) -> { x, y, w, h }
waitFor(predicate, timeout)
snapshot(options) -> pruned semantic tree
```

The reasoning came from how Appium does context switching, which is worth
understanding regardless: it does not extend native accessibility, it opens a
**second out-of-band channel into the web engine** and multiplexes both behind
one session. Three transferable pieces — a channel into the engine, a portable
JS verb layer, and a router presenting one surface.

It was attractive because we own the primary webviews, so no injection is
needed, and because a verb layer inside the page can answer questions the
accessibility tree cannot: shadow roots, viewport-relative geometry, console
and error capture.

## 3. Why we did not

**It puts Quern inside the app under test.** Even inert and flag-gated, the
agent ships in the same bundle as production code. The test target stops being
structurally identical to what users run, which is the property that makes a
passing test mean anything. A build flag is a promise, not a guarantee.

**It makes every app integrate before Quern is useful.** The agent has to be
built into each web bundle, kept in step with Quern's protocol, and activated
correctly. A debugging tool that requires the thing being debugged to adopt it
first is a much harder sell than one that works on an unmodified build.

**The measurement showed a cheaper channel already existed.** WebKit exposes the
Web Inspector protocol — the one Safari's Develop menu uses — and on a simulator
it is a plain unix socket with no usbmux, lockdown, pairing or developer disk
image. `pymobiledevice3.ServiceConnection` already speaks the wire format.

So the shipped approach reaches the same DOM with nothing added to the app, and
works against builds that have never heard of Quern.

## 4. What we gave up

Honest accounting, because the rejected design was not strictly worse:

- **No in-page verb layer.** Shadow-root piercing, viewport geometry and
  `waitFor` predicates are now Quern's problem on the far side of the protocol
  rather than a page-local helper's.
- **No console or error capture from inside the page** by that route.
- **Simulator-first.** The unix socket is a simulator convenience; physical
  devices need the usbmux path, which is a different transport.
- **A contract-testing idea went with it** — verifying a manifest of testids
  from inside the page — which has no home in the current design.
- **Vendor webviews are out of reach entirely.** The protocol needs the app to
  set `isInspectable`, and an SDK that owns its own `WKWebView` never does.
  Measured on Iterable's in-app message modal, one screen and one moment, two
  clients:

  | Client | Sees |
  |---|---|
  | sim-bridge / idb | **1 element** — a bare `Application` |
  | WDA (XCTest) | **77 nodes**, 3 WebViews, every probe string |

  So the loss is narrower than it first appears, and in the opposite direction
  from the rest of this section: accessibility needs no opt-in, and XCTest
  resolves the content with real types — headings as StaticText, links as
  **Link**, buttons as **Button**, so they are tappable as well as readable.
  For a vendor modal that is usually enough; you rarely want DOM identity
  inside someone else's dialog, you want the dismiss button. What is genuinely
  gone is DOM access and JS state for any webview the app does not own.

If a future need makes the in-page agent worth revisiting, the thing that
changed our minds was not the design's merits. It was that a non-invasive
channel turned out to already exist.

**A collapsed tree has two causes, and they are distinguishable without a
screenshot.** A presented web modal and a bridge poisoned by XCUITest (#66)
both reduce the tree to one element, which reads identically at a glance:

| | web modal presented | bridge poisoned (#66) |
|---|---|---|
| elements | 1 | 1 |
| `Application` label | the app's name | **null** |
| `Application` frame | real bounds | **0x0** |

Worth knowing before concluding a device is broken: the modal case recovers as
soon as the modal is dismissed, and SpringBoard reads normally throughout.

**Testing a vendor modal needs no vendor.** Iterable fetches in-app messages
from `GET https://api.iterable.com/api/inApp/getMessages`, so one Quern mock
conjures a modal on demand — no campaign, no console, no real message. Mock
`~d api.iterable.com & ~u inApp/getMessages`, return the SDK's own
`TestInAppPayloadGenerator` schema, relaunch:

```json
{"inAppMessages":[{"messageId":"probe-msg-1","campaignId":424242,
  "trigger":{"type":"immediate"},
  "content":{"html":"<h1>PROBE_HEADING</h1>…<button>PROBE_BUTTON</button>"}}]}
```

That technique is reusable for any SDK that fetches its content over HTTP, and
is how the measurement above was taken.

## 5. Where the code is

- `server/device/webinspector.py` — transport and RPC
- `server/device/web_content.py`, `web_probing.py` — what reads through it
- `server/api/device_ui.py` — the surface it is exposed on
