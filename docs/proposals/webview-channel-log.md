# Webview Channel — Working Log

A shared, append-only log for the webview-automation investigation, written by
more than one agent working on different machines against different apps.

Companion to [`hybrid-automation-design-notes.md`](./hybrid-automation-design-notes.md)
(the design) and [`webview-a11y-spike-findings.md`](./webview-a11y-spike-findings.md)
(the spike results). Those two hold conclusions. **This holds the conversation,
including the parts that turned out to be wrong.**

---

## Why this file exists

On 2026-09-01 a conclusion in the spike findings — *"a runtime hybrid that
switches between sim-bridge and WDA per screen is probably unworkable"* — was
invalidated **51 minutes after it was written**, by the root cause landing in
issue #66. Nothing was done wrong. The two findings simply lived in different
places and nobody was watching both.

That is the failure mode this file is meant to catch: not error, but *staleness
across sources*. Two agents on two machines will keep producing findings faster
than either can reconcile them.

## Protocol

1. **Append, don't rewrite.** Add entries at the bottom of the log. Do not edit
   someone else's entry, even to correct it — post a superseding entry and link
   back. The wrong turns are worth keeping; see the Automation Mode dead end in
   #66, which saved the next reader a day.
2. **Sign and timestamp every entry.** `machine · UTC`. Timestamps are load
   bearing here — that is the whole lesson above. Use UTC; the machines are not
   guaranteed to share a timezone.
3. **State what kind of claim you are making.** `MEASURED` (I ran it and here is
   the output) · `INFERRED` (follows from evidence, not directly observed) ·
   `ASSUMED` (working belief, unverified). Most disagreements resolve instantly
   once these are separated.
4. **Say what would falsify it.** Especially for `INFERRED` and `ASSUMED`.
5. **When you supersede something, update "Current answers" below** and note
   what it replaced. That section is the only part of this file that gets
   rewritten, and it is the only part anyone should read for the current state.

## Participants

| Machine | Working against | Repos |
|---|---|---|
| `scorpius` | Metatext (third-party webviews) | quern, quern.dev, metatext |
| *(work machine)* | Geocaching iOS (first-party webviews) | quern, Geocaching iOS |

Same model, different sessions and machines — neither has the other's context, so
do not assume shared knowledge of anything not written down here.

---

## Current answers

Rewrite this section as things settle. Everything else in the file is append-only.

| Question | Answer | Confidence | Source |
|---|---|---|---|
| Does the a11y tree contain webview DOM? | Yes. WebKit bridges it. | MEASURED | spike findings |
| Can Quern see it on simulators? | **No** — sim-bridge/idb do not descend into `WKWebView`. Not a Quern-Python bug; below Quern in the native walk. | MEASURED | spike findings |
| Does XCUITest/WDA see it? | Yes — 47 vs 5, and 63 vs 5 by two separate measurements. | MEASURED | spike findings |
| Does Web Inspector (iwdp) work on simulators? | Yes, with `-s` pointed at the `webinspectord_sim` socket. | MEASURED | spike findings; reproduced on `scorpius` |
| Does it work on **third-party** content? | **Yes.** Inspectability is a property of the hosting app, not the content. | MEASURED | entry 3 below |
| Does running WDA poison the a11y bridge? | Yes — but recoverable in ~10s, **not** "until reboot". | MEASURED | #66 root cause |
| Does Web Inspector poison it? | No. It composes with sim-bridge. | MEASURED | spike findings; reproduced on `scorpius` |
| Do our pages ship testids? | No — 0 on Geocaching Shareables, 0 on joinmastodon.org. | MEASURED | spike findings; entry 3 |

---

## Log

### Entry 1 — cost ladder is stale · `scorpius` · 2026-09-01T05:03Z

**MEASURED.** Timing, from git and the GitHub API:

- last spike commit `9acfa9a` — `2026-09-01T03:51Z`
- #66 root-cause comment — `2026-09-01T04:42Z`

So the spike's ladder row *"Tier 1 … Disturbs a11y bridge: **Yes — poisons it
until reboot**"*, and the conclusion that a per-screen runtime hybrid is
*"probably unworkable"*, both predate the fix by 51 minutes. The recovery is
`kill -9 $(pgrep -x CoreSimulatorBridge)` — ~10s, launchd respawns it, app state
survives.

This does **not** flip the decision. Tier 2 still wins on JS evaluation,
`readyState`, and testids, none of which WDA provides. But "unworkable"
overstates it, and that column should read *"briefly; recoverable in ~10s without
losing state."*

It also answers the spike's own open question — *"is the corruption caused by WDA
specifically, or by any XCTest process?"* #66 says any XCUITest run, and the
mechanism (a host-side stale mach-port cache in `CoreSimulatorBridge`) explains
why it could not be WDA-specific.

### Entry 2 — my iwdp failure was invocation, not approach · `scorpius` · 2026-09-01T05:03Z

**MEASURED.** Recording it because the failure is silent and looks like a
negative result.

Two wrong invocations, both of which return `[]` with **no log output at all**:

```bash
ios_webkit_debug_proxy -s "unix:/var/run/usbmuxd" -c null:9222   # usbmux, not the sim socket
ios_webkit_debug_proxy -c null:9222                              # no -s: enumerates usbmux, finds nothing
```

The working form needs the socket discovered first:

```bash
SOCK=$(lsof -U | grep -o "/private/tmp/com.apple.launchd.[^ ]*/com.apple.webinspectord_sim.socket" | head -1)
ios_webkit_debug_proxy -s "unix:$SOCK" -c null:9221,:9222-9250
```

An empty `[]` is indistinguishable between "wrong socket" and "nothing is
inspectable", which is a good way to wrongly conclude `isInspectable` did not
take. Worth a note wherever this gets automated.

Separately: the spike warns that `#if DEBUG` silently compiles away in the
Geocaching codebase, which defines `INTERNAL` and not `DEBUG`. Metatext **does**
define `DEBUG` (`SWIFT_ACTIVE_COMPILATION_CONDITIONS = DEBUG`), so `#if DEBUG` is
correct there. The general lesson holds: verify the symbol landed rather than
trusting the guard.

### Entry 3 — the channel works on third-party content · `scorpius` · 2026-09-01T05:03Z

**MEASURED.** This is the entry that changes something.

The spike ran against `staging.geocaching.com` — web content we control. I ran
the same technique against Metatext, whose webviews are **entirely third-party**.
Both appeared as inspectable targets:

```
YouTube             https://www.youtube.com/embed/IPSbNdBmWKE?playsinline=1
Servers - Mastodon  https://joinmastodon.org/servers
```

`Runtime.evaluate` against joinmastodon.org, live:

```json
{"title":"Servers - Mastodon","readyState":"complete",
 "href":"https://joinmastodon.org/servers","ids":8,"testids":0,
 "buttons":["Resources","文A","Language: English","Sign-up process: All"],
 "h1":["Servers","Getting started with Mastodon is easy"]}
```

**INFERRED, and the important part:** `isInspectable` is set by the *hosting app*
— Metatext — and joinmastodon.org has no say in it. So inspectability is a
property of the host, not the content.

That narrows §4's *"Not covered: third-party webviews (Iterable, payment,
OAuth)"* considerably. If the third-party content is in a `WKWebView` **your app
hosts**, the full channel is available. What genuinely remains uncovered is
out-of-process surfaces you do not host: `SFSafariViewController` and
`ASWebAuthenticationSession`. For §7's Iterable modals specifically, the
distinction to check is which of those two they are.

*Falsifiable by:* finding a hosted `WKWebView` that refuses inspection despite
the app setting `isInspectable` — e.g. content served with headers that suppress
it, or a webview created by an SDK that sets its own configuration.

Two corroborations of existing findings, from an unrelated app and page:

- **0 testids**, independently matching Geocaching Shareables. §6 stands.
- **Web Inspector does not disturb the a11y bridge** — `get_ui_tree` returned the
  same 6 native elements before and after a full proxy + WebSocket + JS-eval
  session. (Not the poisoned signature, which is 1 bare `Application` at `0x0`.)

Client was ~50 lines of Python against `websockets`, already in Quern's venv. The
`Target.sendMessageToTarget` wrapping is required exactly as described. **No Node
needed** — relevant to the dependency argument in the client comparison.

### Entry 4 — a cheaper answer to the frame problem? · `scorpius` · 2026-09-01T05:03Z

**ASSUMED — not tested.** Flagging rather than claiming.

The spike's closing wrinkle: DOM→screen coordinate conversion needs the
`WKWebView`'s frame in screen space, which sim-bridge never exposes, and WDA
does. Adding a WDA dependency purely to obtain one rectangle seems expensive
given WDA poisons the bridge (entry 1).

Possible cheaper route: derive the frame from geometry Quern already has — the
window bounds, minus nav-bar and safe-area insets, both of which appear in the
native tree. On the Metatext instance picker the native chrome sits at `y=72
h=56`, so the webview plausibly occupies `y=128` to the bottom inset.

*Falsifiable by:* comparing a derived rectangle against WDA's reported webview
frame on the same screen. If they agree within a point or two across a few
screens, the frame is free. If not — insets, split views, keyboard avoidance —
the derivation is not worth defending and WDA (or the agent) should supply it.

Worth someone measuring before the frame requirement is used to justify a
backend choice.

---

## Open questions

| # | Question | Raised by | Status |
|---|---|---|---|
| 1 | Can the webview frame be derived from native geometry instead of WDA? | `scorpius`, entry 4 | untested |
| 2 | Are §7's Iterable modals hosted `WKWebView`s or out-of-process? Determines whether entry 3 covers them. | `scorpius`, entry 3 | untested |
| 3 | Does any hosted `WKWebView` refuse inspection despite the app opting in? | `scorpius`, entry 3 | untested |
| 4 | Should Quern detect the poisoned-bridge signature and auto-recover? | #66 | proposed in #66 |
| 5 | Should `WdaBackend` be allowed on simulators at all, given entry 1 softens but does not remove the cost? | spike findings | open |
