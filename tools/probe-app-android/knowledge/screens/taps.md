---
screen: "taps"
status: documented
landmark_conventions: 2
landmarks:
  - { element: "StaticText", identifier: "taps_heading" }
  - { element: "StaticText", label: "Taps", selected: true }
leads_to: []
---

# Taps

The Taps tab (#511): every button counts its own taps into `<id>_count`, so a test reads which element a tap reached. Its heading repeats the tab's label, so a tap on "Taps" from this tab is ambiguous.
