---
screen: ""
status: documented

# Machine-evaluable screen identity (identifier-first for locale independence).
# All landmarks must match for this screen to be recognized.
# Priority: identifier > element type alone > label (locale-dependent, last resort).
#
# Re-verify when the UI changes. If `identify_screen` returns
# `confidence: "none"` for this screen — or `tap_element` calls start failing
# with `not_found` on identifiers that used to work — the landmarks below are
# probably stale. Navigate to the screen, call `get_ui_tree`, and re-author
# from what's actually present. See "Keeping Landmarks in Sync" in the
# knowledge-base authoring guide.
#
# Available fields per landmark:
#   element          - element type (required, e.g. "navigationBar", "Button")
#   identifier       - exact-match accessibility identifier (preferred)
#   label            - exact-match (case-insensitive) label text
#   label_contains   - substring match for dynamic labels
#   absent: true     - landmark matches only when the element is NOT present
#   selected: true   - for tabs/switches/radios/checkboxes — the element must
#                      be in the selected/on state. Use this to distinguish
#                      "Timelines tab is the active one" from "a Timelines
#                      tab exists" when several tabs share the same id pattern.
landmarks:
  - { element: "", identifier: "" }
  # For a screen whose identity is entirely web -- an SFSafariViewController
  # reports one element to the accessibility tree, the Application -- match the
  # page instead. Read from the Web Inspector's page listing, so it costs one
  # round trip and no probes. Not available for an ASWebAuthenticationSession,
  # which no process publishes; those screens have no identity to match.
  # - { web_url_contains: "/settings" }

# Web-backed content on this screen. Omit the key entirely when there is none.
#
# The accessibility tree does not descend into a WKWebView on iOS, so a screen
# built on web content looks almost empty to get_ui_tree — which reads as "the
# screen failed to load" rather than "the content is behind another door".
# Recording it here stops that misdiagnosis and names the route that works.
#
# Which route applies is decided by facts only the source knows, so this is
# exactly the kind of thing worth writing down once:
#
#   host          WKWebView | SFSafariViewController | ASWebAuthenticationSession
#                 | WebView (Android)
#   in_process    true when the app constructs the view itself. The system-hosted
#                 ones (SFSafariViewController, ASWebAuthenticationSession) run in
#                 another process and are not in the app's hierarchy at all.
#   inspectable   true | false | "debug". Since iOS 16.4 a WKWebView is only
#                 inspectable if the app sets isInspectable on it. That is a
#                 property of the view the app creates, NOT of the content it
#                 loads — so this can be true even for a third-party page, and is
#                 always false for the out-of-process hosts.
#   url           what it loads. Note when it is third-party: it can change
#                 without an app release, so prefer structural selectors over
#                 text the site can reword.
#   page_offset   [x, y] of page (0,0) in screen points, when known. DOM
#                 geometry is viewport-relative; this is what makes it tappable.
#                 One accessibility hit-test on any element recovers it.
#
# On Android none of this applies: the accessibility tree does descend into a
# WebView, so web content appears as ordinary elements.
# What is already known about each web view on this screen, so an agent does
# not have to rediscover it. Emitted ready to paste by get_web_content, under
# "anchors".
web_content:
  # - host: "SFSafariViewController"   # the class presenting it
  #   process: "com.apple.SafariViewService"
  #   # Which bundle_id to pass to get_web_content. An out-of-process view
  #   # (SFSafariViewController, ASWebAuthenticationSession) is hosted by
  #   # SafariViewService, NOT by the app -- and needs no isInspectable, since
  #   # that property only governs the app's own WKWebViews. An in-process
  #   # WKWebView uses the app's own bundle id and does need it, per instance.
  #   reachable_by: [inspector, hit_test]
  #   # Measured, and the most valuable field here. The three cases differ:
  #   #   in-process WKWebView        -> inspector (with isInspectable), hit_test
  #   #   SFSafariViewController      -> inspector, hit_test
  #   #   ASWebAuthenticationSession  -> hit_test ONLY; while one is presented
  #   #                                 the inspector reports no connected app
  #   #                                 at all, so do not spend a call on it.
  #   url: "https://example.com/page"
  #   # How a live page is matched to this entry. No screen identification is
  #   # needed first, which matters because a screen with a web view often has
  #   # no native identity at all.
  #   anchor:
  #     origin: [0, 106]               # page top-left, in points
  #     viewport: [402, 685]
  #     measured_on: "iPhone 16 Pro - iOS 18.6"
  #   # A HINT, never a fact. It depends on device size, iOS version and text
  #   # size, so it is offered as the first candidate and confirmed by one
  #   # probe; if it no longer holds it is discarded and the usual search runs.
  #   # Worth recording because the alternative is expensive: locating an
  #   # out-of-process page cost a 17-probe sweep and ~3.5s, against 1 probe
  #   # and ~0.2s once written down.

# Where this screen can be reached from.
reachable_from:
  - screen: "[[screens/...]]"
    action: 'tap_element label="..." element_type="..."'
  # - deep_link: "appscheme://..."

# Where this screen leads to.
leads_to:
  - screen: "[[screens/...]]"
    action: 'tap_element label="..." element_type="..."'
    # condition: "optional — when this edge only exists in certain states"

# What must be true before the agent can reach this screen.
preconditions: []

tags: []
---

# Screen Name

<!-- One-line description of the screen's purpose. -->

## Key Elements

<!-- Record both the visible label and the accessibility identifier.
     Flag mismatches or shared identifiers in Notes — see "Identifier Reliability" in the guide. -->

| Element Type | Label | Identifier | Notes |
|---|---|---|---|
| | | | |

## States

<!-- Describe the distinct visual/functional states this screen can be in.
     For each state: what triggers it, what the agent will see, and how to proceed. -->

## Dynamic Content

<!-- If this screen has lists, feeds, or other content that varies:
     describe what the items are, what they represent in the app's domain,
     and what tapping them leads to. -->

## Overlay Panels

<!-- Persistent overlays on this screen (map pin cards, bottom sheets, floating panels).
     Not alerts (those are transient). For each panel: trigger, how to recognize it, key elements,
     navigation edges, and how to dismiss.
     Delete this section if the screen has no overlays. -->

## Quirks

<!-- Link to any quirk documents, or note minor quirks inline. -->
<!-- - See [[quirks/...]] -->
