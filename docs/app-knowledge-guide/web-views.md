# Documenting Web Views

*`quern://app-knowledge-guide/web-views` -- part of `quern://app-knowledge-guide`, the app knowledge base guide. Start from `quern://app-knowledge-guide`.*

A screen built on web content looks almost empty to `get_ui_tree` on an iOS simulator (on Android and physical iPhones, `get_ui_tree` already sees web content; this page is about simulators): the
accessibility tree does not descend into a `WKWebView`, and out-of-process hosts
are not in the app's hierarchy at all — a settings page presented in
`SFSafariViewController` reports **one** element, the Application. The failure
mode is a misdiagnosis, not an error: a near-empty tree reads as "the screen
failed to load" or "every landmark drifted", and the agent goes looking for a
problem that isn't there.

`get_web_content` reads the page and returns its elements with real screen
frames. What it cannot work out for itself is which process hosts the view and
whether it can be inspected at all — so record that.

```yaml
web_content:
  - host: "SFSafariViewController"
    process: "com.apple.SafariViewService"
    reachable_by: [inspector, hit_test]
    url: "https://social.arctian.org/settings"
    anchor:
      origin: [0, 106]
      viewport: [402, 685]
      measured_on: "iPhone 16 Pro - iOS 18.6"
```

**Identifying a screen that has no native identity.** A screen built entirely on
a web view cannot be named by its elements, because there are none to name — the
accessibility tree reports the Application and nothing else. Match the page:

```yaml
landmarks:
  - { web_url_contains: "/settings", web_process: "com.apple.SafariViewService" }
```

`web_process` is the bundle id hosting the page — the same value
`web_content.process` records. Several applications are connected at once, the
app under test and `com.apple.SafariViewService`, so a URL alone cannot say
which of them is showing it. Scoping is optional but worth doing whenever the
same URL could plausibly load in either.

The URL comes from the Web Inspector's page listing — one round trip, no probes
— and is only requested when some loaded landmark asks for one, so a knowledge
base that uses none pays nothing.

**Know what it actually asserts.** The listing is scoped to the device and to
real applications (WebKit's helper processes are excluded), but it is *not*
scoped to what is on screen. It says **a page with this URL is loaded**, not
that you are looking at it. An app can hold an inspectable page in the
background — Metatext keeps a YouTube embed alive in the timeline — and a URL
landmark cannot tell that apart from the same page in the foreground.
Establishing that would need probes, which is the cost this avoids.

So pick a URL specific enough that its mere presence identifies the screen, and
add an element landmark whenever the screen has anything native to name: the two
are ANDed, and the element is what pins it to the foreground. A screen with *no*
native elements — the case this exists for — is relying on the URL alone, so
choose one no background page could share.

It does not work for an `ASWebAuthenticationSession`, which publishes no pages
at all. A landmark that cannot be evaluated fails rather than passes.

**`reachable_by` is the field worth having.** There are three cases and they do
not follow the rule you would guess:

| view | Web Inspector | hit-test |
|---|---|---|
| in-process `WKWebView` | yes — but only if the app sets `isInspectable` | yes |
| `SFSafariViewController` | **yes, with no opt-in from the app** | yes |
| `ASWebAuthenticationSession` | **no — reports no connected application at all** | yes |

`get_web_content` always asks the Inspector first and falls back to probing only
when it returns nothing, reporting which answered in `route`. `reachable_by`
does not change that order — it tells a reader what to expect, and what a result
costs: the probe route needs a screenshot, text recognition and a dozen or so
hit-tests, around 2s, against ~0.2s for an Inspector page with a recorded
origin. A screen recorded as `hit_test` only will always pay the failed
Inspector attempt first.

The middle row is the surprising one. The app has no handle on a
`SFSafariViewController`, yet WebKit still publishes it for inspection under
`com.apple.SafariViewService`. The bottom row is the one to record loudest:
while an auth session is presented the inspector reports *zero* connected
applications, so an agent that assumes "system-presented means
SafariViewService" wastes a call. Hit-testing reaches all three.

**`process` is what to pass as `bundle_id`.** An out-of-process view is hosted
by `com.apple.SafariViewService`, not by the app. Without it `get_web_content`
sees two connected applications and asks which one you meant.

**`isInspectable` is per view, not per app.** Since iOS 16.4 an app's own
`WKWebView` is inspectable only if the app sets it on *that instance*, so one
view opting in says nothing about another in the same app. Put it behind
`#if DEBUG`, which is where it belongs.

**`anchor` is a hint, never a fact.** DOM geometry is viewport-relative, and
nothing in the protocol says where the view sits on screen. The offset depends
on device size, iOS version and text size, so a recorded one is offered as the
first candidate and confirmed by a single probe — if it no longer holds it is
discarded and the ordinary search runs. Recording it is still worth it: an
out-of-process page took a **17-probe sweep and ~3.5s** to locate cold, against
**1 probe and ~0.2s** once written down. Out-of-process views need this most,
because they carry their own chrome at both ends and none of it is in the native
tree, so geometry cannot even guess.

**Where to get the values.** `get_web_content` returns them under `anchors`,
ready to paste:

```json
{"page_id": 3, "url": "https://social.arctian.org/settings",
 "origin": [0, 106], "viewport": [402, 685], "strategy": "sweep"}
```

A `strategy` of `hint` means the recorded value was used and confirmed;
`geometry` or `sweep` means it was rediscovered, which is the signal to write
the new value down.

**Note when the URL is third-party.** It can change without an app release —
the Mastodon instance picker moved from `/communities` to `/servers` between
knowledge-base revisions. Prefer structural elements -- a heading, a button named by its
`aria-label` -- over text the site can reword. Quern takes no CSS selectors: `get_web_content` maps
these to the `Heading` or `Button` type and the label that its tools and landmarks match. Landmark matching sees web elements only for 60 seconds after `get_web_content`, so on a web screen call it
right before `identify_screen`.

None of this applies on Android, where the accessibility tree does descend into
a `WebView` and web content appears as ordinary elements.
