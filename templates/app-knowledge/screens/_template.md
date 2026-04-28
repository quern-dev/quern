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

# Human-readable identification hints for agents reading the doc.
# Optional. Leave it out if landmarks above already capture the screen
# identity clearly.
identify_by:
  - { element: "", label: "" }

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
     Not alerts (those are transient). For each panel: trigger, identify_by, key elements,
     navigation edges, and how to dismiss.
     Delete this section if the screen has no overlays. -->

## Quirks

<!-- Link to any quirk documents, or note minor quirks inline. -->
<!-- - See [[quirks/...]] -->
