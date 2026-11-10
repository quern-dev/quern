# Writing Navigation Edges

*`quern://app-knowledge-guide/navigation-edges` -- part of `quern://app-knowledge-guide`, the app knowledge base guide. Start from `quern://app-knowledge-guide`.*

`leads_to` is not only a map for the next agent. `tap_element` reads it (#491). Suppose a tap documented by a `leads_to` edge leaves the agent still on the edge's source screen. Then a later `tap_element` miss, or `wait_for_element` timeout, says the tap probably did not register (`previous_tap`). With `verify_navigation: true`, the tap is checked as it is made instead. So write each edge so quern can match it:

```yaml
leads_to:
  - screen: "[[screens/order-detail]]"
    action: 'tap_element label_prefix="Order #" element_type="Button"'
  - screen: "[[screens/settings]]"
    action: 'tap_element identifier="settings_button"'
  - screen: "[[screens/home]]"
    action: 'tap_element label="Sign In" element_type="Button"'
    timeout: 10          # waits on the network
  - screen: "[[screens/paywall]]"
    action: 'tap_element label="Export" element_type="Button"'
    condition: "free accounts only"
```

- **The `action` is a `tap_element` call**, written the way an agent will make it, naming a `label` (or `label_contains`, `label_prefix`) or an `identifier`. Prose ("tap the gear"), other tools (`swipe`, `open_url`) and a type with nothing else are kept as documentation but not checked.
- **Name the element the way agents call it.** An edge is matched against the element that was tapped, so any selector that picks the right element works for `previous_tap`. The check at the tap (`verify_navigation`) decides before tapping, so there the call and the edge have to share a label or an identifier.
- **`screen` links the target's file**, `[[screens/<file name>]]`, and that file needs landmarks: an edge to a stub cannot confirm an arrival, so it is not checked until the stub is documented.
- **Navigation from every screen goes in `app.md`.** A tab bar or a persistent menu is reachable from almost everywhere, so declare it once in `app.md`'s frontmatter as `global_leads_to` -- the same entry format, plus `except:` for the screens it does not apply from (a login screen, a full-screen sheet). quern copies it onto every other screen. Three rules decide where:
  - **A tab with a stack:** a tab that shows whatever its stack was left on gets one global edge per stack screen, all naming the same element. The group is not applied on any of those screens, so re-tapping the tab there -- which pops to its root -- is not checked against the stack.
  - **A screen's own edge wins:** a screen whose own `leads_to` names the same element (the same label or identifier, whatever the `element_type`, conditional or not) keeps its own edges and gets no global copy.
  - **List every screen a global edge can reach.** A tab whose stack screens are not listed will be checked as if it always lands on its root, and an arrival on a pushed screen can then be taken for a dropped tap.
- **One tap, several possible screens: one edge each.** A tab that shows whatever its own stack was left on leads to each screen of that stack; arriving at any of them counts.
- **`condition:`** marks an edge that exists only in some states. It confirms an arrival but is never blamed for a missed one.
- **`timeout:`** (seconds, up to 60) is for a screen slow to arrive -- one that waits on the network -- so a slow arrival is not taken for a dropped tap. The default is 3s for a check at the tap, and 1s before a later miss can blame it.

After loading, read the `navigation` block `load_landmarks` returns: `edges` is how many edges quern checks (a global edge counted once per screen it applies from, and `global` the number declared in `app.md`), `unusable` lists each `leads_to` entry it does not check and why, `timeouts_ignored` and `except_ignored` list edges still checked with a `timeout:` or `except:` it could not read, and `app_md` reports a problem with `app.md` itself (unreadable, or `global_leads_to` outside valid frontmatter). An edge you meant to be checked that appears under `unusable` usually needs its action rewritten as a `tap_element` call, or its target documented.
