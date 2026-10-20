---
screen: "web"
status: documented
landmark_conventions: 2
landmarks:
  - { element: "Heading", label: "Web" }
  - { element: "StaticText", identifier: "web_heading_native" }
leads_to:
  - screen: "[[screens/more]]"
    action: 'tap_element label="More" element_type="Button"'
  - screen: "[[screens/more]]"
    action: 'tap_element label="More" element_type="RadioButton"'
---

# Web

The Web tab, pushed from the More list. Its back button is `Button "More"`; through WDA the More tab is a `Button "More"` too, so a tap by label is ambiguous there and is not checked. Tapping the More tab while one of its screens is shown pops back to the list.
