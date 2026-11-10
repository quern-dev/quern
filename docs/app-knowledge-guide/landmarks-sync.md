# Keeping Landmarks in Sync

*`quern://app-knowledge-guide/landmarks-sync` -- part of `quern://app-knowledge-guide`, the app knowledge base guide. Start from `quern://app-knowledge-guide`.*

The knowledge base is a living artifact, not a one-time setup. App teams ship UI changes — accessibility identifiers get renamed during refactors, copy gets rewritten, screens get redesigned — and landmarks that were correct last quarter can silently fail to match today. Mechanical correctness in YAML is no guarantee that the underlying app still exposes those elements.

**Signs that drift has occurred:**

- `identify_screen` (or `get_screen_summary?identify=true`) returns `confidence: "none"` for a screen you expect to match. Inspect `partial_matches` — if the screen you're on is sitting at 0/N or 1/N matched, the landmarks for it are stale.
- An agent's automation suddenly starts failing on screens that used to work — often surfacing as `tap_element` not_found errors with the legacy identifier.
- `validate_landmarks` reports collisions where there were none before — a sibling screen got redesigned and now overlaps with this one.

**How to fix drift:**

1. Navigate to the affected screen (use `reachable_from` from the screen file as the recipe).
2. `wait_for_element` on a likely-stable element to handle mid-transition states.
3. Call `get_ui_tree` (with `include_raw=true` if you need to debug the platform normalizer) to see what the screen actually exposes now.
4. Re-author the `landmarks:` block — drop selectors that no longer exist, swap in current identifiers, prefer structural elements (nav title, unique button identifier) over copy-dependent labels.
5. Re-run `load_landmarks` and `identify_screen` to confirm `confidence: "exact"`.

If the file still carries an `identify_by:` block, delete it while you are there. Nothing reads it, and one left pointing at renamed identifiers misleads the next person who opens the file.

**Cadence and triggers:**

- After every major app release (a refactor, a redesign, a copy pass).
- Whenever an agent reports a sudden batch of `tap_element not_found` failures across screens that used to work.
- Whenever you're already touching a screen for another reason — opportunistic re-verification keeps drift small and easy to fix.

There's no scheduled audit or calendar-based heuristic; drift surfaces through the agent's normal use of `identify_screen`. When `confidence: "none"` appears, treat it as a maintenance signal, not a transient bug to ignore.
