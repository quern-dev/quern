# Overlay Panels

*`quern://app-knowledge-guide/overlay-panels` -- part of `quern://app-knowledge-guide`, the app knowledge base guide. Start from `quern://app-knowledge-guide`.*

Some UI doesn't fit neatly into "screen" or "alert" categories: map pin summary cards, bottom sheets, floating panels, and similar overlays that are persistent (not transient like alerts), interactive (they have navigation edges), but not full screens (no navigation bar, they overlay the parent screen).

**Document these inside the parent screen's doc** under a dedicated `## Overlay Panels` section. For each panel:

- How to trigger it (e.g., "tap a map pin")
- How to identify it (`landmarks` for the panel, evaluated against the live UI tree)
- Key elements within the panel
- Navigation edges (e.g., "tapping the panel title navigates to [[screens/cache-detail]]")
- How to dismiss it (swipe down, tap outside, back button)

This keeps them associated with the screen they overlay, while giving them enough structure that an agent can interact with them reliably. If an overlay is complex enough to warrant its own file (many elements, multiple states), promote it to a screen doc with a note that it's an overlay of the parent.
