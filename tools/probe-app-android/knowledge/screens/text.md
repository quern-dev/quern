---
screen: "text"
status: documented
landmark_conventions: 2
scrollable: false
landmarks:
  - { element: "StaticText", identifier: "text_heading" }
  - { element: "StaticText", label: "Text", selected: true }
leads_to:
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
  - screen: "[[screens/diag]]"
    action: 'tap_element label="Diag"'
  - screen: "[[screens/location]]"
    action: 'tap_element label="Location"'
---

# Text

The Text tab. Every page is in the tree at once, so the selected tab is what tells them apart.
