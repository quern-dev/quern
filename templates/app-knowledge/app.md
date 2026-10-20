---
# Fill in these fields for your app.
app_name: ""
bundle_id: ""
url_scheme: ""
universal_link_domain: ""

# Navigation available from every screen -- a tab bar, a persistent menu --
# declared once instead of in every screen's leads_to (#503). Same format as
# leads_to; `except` lists the screens it does not apply from (a login screen,
# a full-screen sheet: anything that hides the bar). A screen's own leads_to
# entry for the same element wins on that screen. Checked by tap_element like
# any edge: see "Writing Navigation Edges" in the knowledge-base guide.
global_leads_to: []
#  - screen: "[[screens/home]]"
#    action: 'tap_element label="Home" element_type="Button"'
#    except: ["[[screens/login]]"]
---

# App Name

## Entry Points

| Method | Command | Lands On |
|---|---|---|
| Cold launch (logged out) | `launch_app bundle_id="..."` | [[screens/...]] |
| Cold launch (logged in) | `launch_app bundle_id="..."` | [[screens/...]] |
| Deep link index | See `deep-links/` directory |

## Global Navigation

<!-- Describe the primary navigation structure: tab bar, sidebar, hamburger menu, etc. -->
<!-- Example for a tab bar app: -->

<!--
| Tab | Label | Screen |
|---|---|---|
| 1 | Home | [[screens/home]] |
| 2 | Search | [[screens/search]] |
| 3 | Profile | [[screens/profile]] |

To switch tabs: `tap_element label="{Tab Label}"`. Declare the tabs in
`global_leads_to` above so quern can check tab taps.
-->

## Global Elements

<!-- Elements that appear on many/all screens: back buttons, error banners, loading overlays, etc. -->

## Test Accounts

<!-- How to obtain test credentials. Reference a skill, API, or fixture file. -->
