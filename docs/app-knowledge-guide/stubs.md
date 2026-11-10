# Stubs: Tracking Undiscovered Screens

*`quern://app-knowledge-guide/stubs` -- part of `quern://app-knowledge-guide`, the app knowledge base guide. Start from `quern://app-knowledge-guide`.*

As you document a screen, you'll find navigation edges leading to screens you haven't visited yet. Don't lose track of them — create a **stub**.

A stub is a minimal screen file that records "this screen exists and I know how to get there" without requiring a full visit. Use `screens/_stub.md` as the starting point.

**When to create a stub:**

1. You're documenting Screen A and find it leads to Screen B. Add the edge to A's `leads_to` now, while you know the exact tap.
2. Before creating a stub for Screen B, check if a file already exists for it — search every screen document by name first, then read its `landmarks:` (or, on a file written before April 2026, its `identify_by:`). Searching only for `landmarks:` misses a pre-landmarks file entirely, and the duplicate stub splits that screen's `reachable_from` edges across two documents.
3. If no match exists, create a stub: `screens/screen-b.md` with `status: stub`, the `reachable_from` edge you just discovered, and whatever you can infer about the screen name.
4. If a match exists (stub or documented), just add the new `reachable_from` edge to the existing file.

**When to upgrade a stub:**

When you visit the screen, replace the stub content with a full document (use `_template.md`). Change `status: stub` to `status: documented` and fill in all sections.

**Reconciling duplicates:**

It's possible that the same screen gets stubbed twice from different edges before you realize they're the same destination. When you discover duplicates:

1. Keep the file with richer content (or the one with the better filename).
2. Merge `reachable_from` edges from both files.
3. Delete the duplicate.
4. Update any `leads_to` references in other screen docs that pointed to the deleted file.

The `init_app_knowledge` tool reports stub vs. documented counts so you can gauge tour progress.
