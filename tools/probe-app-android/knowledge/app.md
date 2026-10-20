---
app_name: "QuernProbe"
bundle_id: "com.quern.probe"

# Every tab this knowledge base documents, from every screen (#503). The
# Gestures tab is not in the conformance contract, so it has no screen here.
global_leads_to:
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
  - screen: "[[screens/diag]]"
    action: 'tap_element label="Diag"'
  - screen: "[[screens/location]]"
    action: 'tap_element label="Location"'
---

# QuernProbe (Android)

The fixture app for quern's own tests: a strip of tabs, every page in the tree at once.
