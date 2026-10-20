---
app_name: "QuernProbe"
bundle_id: "com.quern.probe"

# The tab bar's own tabs, from every screen (#503). The More tab is not here:
# where it leads depends on the screen it is tapped from, so its edges stay on
# the screens.
global_leads_to:
  - screen: "[[screens/text]]"
    action: 'tap_element identifier="tab_text"'
  - screen: "[[screens/controls]]"
    action: 'tap_element identifier="tab_controls"'
  - screen: "[[screens/scroll]]"
    action: 'tap_element identifier="tab_scroll"'
  - screen: "[[screens/links]]"
    action: 'tap_element identifier="tab_links"'
---

# QuernProbe (iOS)

The fixture app for quern's own tests. Four tabs on the bar -- Text, Controls,
Scroll, Links -- and the rest behind the system More list.
