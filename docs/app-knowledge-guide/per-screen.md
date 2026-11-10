# What to Capture Per Screen

*`quern://app-knowledge-guide/per-screen` -- part of `quern://app-knowledge-guide`, the app knowledge base guide. Start from `quern://app-knowledge-guide`.*

Follow this workflow for each screen: `get_screen_summary` first for quick orientation, then `get_ui_tree` for the full element list, then write the document, then move on to the next screen. This summary → tree → document → move on rhythm is the most efficient way to maintain momentum without missing details.

For each screen, capture:

1. **Identification** — Find the most unique, stable element(s) that distinguish this screen. A navigation bar title is ideal. Avoid dynamic content. These become the screen's landmarks; what makes a good one, and the element types to record: `quern://app-knowledge-guide/choosing-landmarks`.
2. **Key elements** — List interactive elements with their types and labels. Note which elements are conditional (appear only in certain states).
3. **Navigation edges** — Where does this screen lead, and what action triggers each transition? Record those outgoing edges in this screen's `leads_to` (see `quern://app-knowledge-guide/navigation-edges`): quern checks them. Where can you arrive from? Record those incoming paths in `reachable_from`.
4. **States** — What are the distinct states? (empty, loading, error, populated, etc.) How does the agent recognize each?
5. **Dynamic content** — If there's a list/feed/collection: what are the items? What do they represent? What happens when you tap one?
6. **Quirks** — Anything non-obvious. Ask the user.
