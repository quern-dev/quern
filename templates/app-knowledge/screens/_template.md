---
screen: ""
status: documented

# How an agent can confirm it's on this screen.
# List the most reliable signals first — a unique nav title or element.
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

| Element Type | Label | Notes |
|---|---|---|
| | | |

## States

<!-- Describe the distinct visual/functional states this screen can be in.
     For each state: what triggers it, what the agent will see, and how to proceed. -->

## Dynamic Content

<!-- If this screen has lists, feeds, or other content that varies:
     describe what the items are, what they represent in the app's domain,
     and what tapping them leads to. -->

## Quirks

<!-- Link to any quirk documents, or note minor quirks inline. -->
<!-- - See [[quirks/...]] -->
