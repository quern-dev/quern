---
screen: "more"
status: documented
landmark_conventions: 2
scrollable: false
landmarks:
  - { element: "Heading", label: "More" }
leads_to:
  - screen: "[[screens/logs]]"
    action: 'tap_element label="Logs"'
  - screen: "[[screens/location]]"
    action: 'tap_element label="Location"'
  - screen: "[[screens/web]]"
    action: 'tap_element label="Web"'
  - screen: "[[screens/diag]]"
    action: 'tap_element label="Diag"'
  - screen: "[[screens/state]]"
    action: 'tap_element label="State"'
  - screen: "[[screens/widgets]]"
    action: 'tap_element label="Widgets"'
  - screen: "[[screens/lists]]"
    action: 'tap_element label="Lists"'
  - screen: "[[screens/lazy]]"
    action: 'tap_element label="Lazy"'
  - screen: "[[screens/text]]"
    action: 'tap_element identifier="tab_text"'
  - screen: "[[screens/controls]]"
    action: 'tap_element identifier="tab_controls"'
  - screen: "[[screens/scroll]]"
    action: 'tap_element identifier="tab_scroll"'
  - screen: "[[screens/links]]"
    action: 'tap_element identifier="tab_links"'
---

# More

The system More list: the tabs that do not fit on the bar. Its rows sit one level below quern's default WDA depth (13 on an iPhone 11), so a tap on one looks deeper on a miss (#494).
