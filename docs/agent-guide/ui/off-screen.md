# Reaching elements off screen

*`quern://guide/ui/off-screen` -- part of `quern://guide/ui` in the Quern agent guide. Start from `quern://guide`.*

**"The element I want is scrolled off-screen"**
- `tap_element` handles this: on a miss it scrolls the target into view and retries. `scroll_to_find` is tri-state — `true` always sweeps, `false` never, and **unset (the default)** sweeps on Android, while on iOS it asks the knowledge base and sweeps only on a screen recorded as `scrollable: true`.
- On iOS with no knowledge base loaded nothing is recorded, so a miss does *not* sweep and the response says `scrollability_unknown`. Pass `scroll_to_find: true` if you know the screen scrolls, or record it once (see `quern://app-knowledge-guide`) and every later tap gets it right for free.
- A sweep that runs is reported: the response carries the swipes and whether anything moved, so a gesture you did not ask for is never invisible. On Android it also stops early and says why: `ends` lists the directions found at their end, and `no_progress: true` means the screen did not move when swiped either way -- it does not scroll there, so the element is not off screen.
- Need it visible but *not* tapped (asserting on it, screenshotting it): `scroll_to_element`
- **Finding the target's identifier:** `get_screen_summary` lists only interactive elements (buttons, cells, fields and the like), so a row drawn as StaticText, or anything else not interactive, is not in it at all. Look in `get_ui_tree` (narrow it with `children_of`) for the row's `identifier` and `label`, which are often different: a row may be labelled "Row 150" and identified as `row_150`
- Both work on Android and iOS (simulator and physical), and both drive a bounded swipe loop that re-checks the target by selector rather than dumping the UI tree — a tree dump can itself scroll the target away, which is the bug this design exists to avoid
- Neither is a substitute for `swipe` when you want to scroll a *screen* rather than reach a known element
