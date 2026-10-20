---
screen: "scroll"
status: documented
landmark_conventions: 2
scrollable: true
landmarks:
  - { element: "RadioButton", identifier: "tab_scroll", selected: true }
  - { element: "Group", identifier: "scroll_table" }
leads_to:
  - screen: "[[screens/text]]"
    action: 'tap_element identifier="tab_text"'
  - screen: "[[screens/controls]]"
    action: 'tap_element identifier="tab_controls"'
  - screen: "[[screens/links]]"
    action: 'tap_element identifier="tab_links"'
  - screen: "[[screens/more]]"
    action: 'tap_element label="More" element_type="RadioButton"'
  - screen: "[[screens/logs]]"
    action: 'tap_element label="More" element_type="RadioButton"'
  - screen: "[[screens/location]]"
    action: 'tap_element label="More" element_type="RadioButton"'
  - screen: "[[screens/web]]"
    action: 'tap_element label="More" element_type="RadioButton"'
  - screen: "[[screens/diag]]"
    action: 'tap_element label="More" element_type="RadioButton"'
  - screen: "[[screens/state]]"
    action: 'tap_element label="More" element_type="RadioButton"'
  - screen: "[[screens/widgets]]"
    action: 'tap_element label="More" element_type="RadioButton"'
  - screen: "[[screens/lists]]"
    action: 'tap_element label="More" element_type="RadioButton"'
  - screen: "[[screens/lazy]]"
    action: 'tap_element label="More" element_type="RadioButton"'
---

# Scroll

The Scroll tab, on the tab bar.
