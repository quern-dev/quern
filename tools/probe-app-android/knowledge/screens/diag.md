---
screen: "diag"
status: documented
landmark_conventions: 2
landmarks:
  - { element: "StaticText", identifier: "diag_heading" }
  - { element: "StaticText", label: "Diag", selected: true }
leads_to:
  - screen: "[[screens/text]]"
    action: 'tap_element label="Text"'
  - screen: "[[screens/controls]]"
    action: 'tap_element label="Controls"'
  - screen: "[[screens/scroll]]"
    action: 'tap_element label="Scroll"'
  - screen: "[[screens/logs]]"
    action: 'tap_element label="Logs"'
  - screen: "[[screens/links]]"
    action: 'tap_element label="Links"'
  - screen: "[[screens/web]]"
    action: 'tap_element label="Web"'
  - screen: "[[screens/location]]"
    action: 'tap_element label="Location"'
---

# Diag

The Diag tab. Every page is in the tree at once, so the selected tab is what tells them apart.
