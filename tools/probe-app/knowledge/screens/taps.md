---
screen: "taps"
status: documented
landmark_conventions: 2
scrollable: false
landmarks:
  - { element: "Heading", label: "Taps" }
  - { element: "StaticText", identifier: "taps_heading" }
leads_to:
  - screen: "[[screens/more]]"
    action: 'tap_element label="More" element_type="Button"'
  - screen: "[[screens/more]]"
    action: 'tap_element label="More" element_type="RadioButton"'
---

# Taps

The Taps tab, pushed from the More list (#511): every button counts its own taps into `<id>_count`, so a test reads which element a tap reached. Its back button is `Button "More"`, as on the other More screens.
