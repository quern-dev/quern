---
screen: "controls"
status: documented
landmark_conventions: 2
scrollable: false
landmarks:
  - { element: "StaticText", identifier: "control_heading" }
  - { element: "StaticText", label: "Controls", selected: true }
leads_to:
  - screen: "[[screens/text]]"
    action: 'tap_element label="Text"'
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

# Controls

The Controls tab. Every page is in the tree at once, so the selected tab is what tells them apart.
