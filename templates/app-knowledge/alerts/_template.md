---
alert: ""

# What triggers this alert to appear.
trigger: ""

# Screens where this alert can appear. Use [] for "any screen".
appears_on: []

# How the agent can identify this alert when it appears unexpectedly.
identify_by:
  - { element: "", label: "" }

# Available actions to dismiss or respond to the alert.
actions:
  - label: ""
    command: 'tap_element label="..." element_type="button"'
    effect: ""  # what happens after this action

# App states where this alert is relevant (reference states.md entries).
relevant_states: []

tags: []
---

# Alert Name

<!-- What this alert is, why it appears, and what the agent should do about it. -->

## Recognition

<!-- How to distinguish this alert from others. What makes it unique? -->

## Recommended Response

<!-- What the agent should do when encountering this alert.
     Different responses may be appropriate depending on the current task. -->

## Notes

<!-- Frequency, suppression (does it stop appearing after N dismissals?),
     conditions under which it won't appear, etc. -->
