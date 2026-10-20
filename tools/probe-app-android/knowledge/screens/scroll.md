---
screen: "scroll"
status: documented
landmark_conventions: 2
scrollable: true
landmarks:
  - { element: "StaticText", label: "Scroll", selected: true }
  - { element: "ScrollView", identifier: "scroll_list" }
leads_to:
  - screen: "[[screens/text]]"
    action: 'tap_element label="Text"'
  - screen: "[[screens/controls]]"
    action: 'tap_element label="Controls"'
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

# Scroll

The Scroll tab. Every page is in the tree at once, so the selected tab is what tells them apart.
