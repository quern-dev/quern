---
screen: "logs"
status: documented
landmark_conventions: 2
landmarks:
  - { element: "StaticText", identifier: "log_heading" }
  - { element: "StaticText", label: "Logs", selected: true }
leads_to:
  - screen: "[[screens/text]]"
    action: 'tap_element label="Text"'
  - screen: "[[screens/controls]]"
    action: 'tap_element label="Controls"'
  - screen: "[[screens/scroll]]"
    action: 'tap_element label="Scroll"'
  - screen: "[[screens/links]]"
    action: 'tap_element label="Links"'
  - screen: "[[screens/web]]"
    action: 'tap_element label="Web"'
  - screen: "[[screens/diag]]"
    action: 'tap_element label="Diag"'
  - screen: "[[screens/location]]"
    action: 'tap_element label="Location"'
---

# Logs

The Logs tab. Every page is in the tree at once, so the selected tab is what tells them apart.
