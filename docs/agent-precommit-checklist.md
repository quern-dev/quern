# Pre-Commit Checklist (Quern-using project)

This checklist is surfaced as a system reminder before any `git commit`
in a project that uses Quern (signaled by a `.quern/` directory at the
repo root). Installed globally by `quern setup`. Walk it before
finalizing the commit — items are conditional, only the relevant ones
need attention.

## If you changed app code that affects the UI

- **Accessibility identifiers**: did you rename, remove, or add any
  `accessibilityIdentifier` values? If yes, the corresponding
  `.quern/knowledge/screens/*.md` files need a matching update — call
  `identify_screen` (or `get_screen_summary?identify=true`) on the
  affected screens and re-author the landmarks block to match the new
  identifiers. Stale landmarks silently fail to match, breaking
  downstream automation.
- **Visible labels**: did you change a button's title, a heading, an
  accessibility label? Same drill — landmarks that pin on those labels
  may now miss. Especially common after copy reviews and localization
  passes.
- **New screens**: does the screen have a `.quern/knowledge/screens/`
  document? If you added a new screen to the app, add a corresponding
  KB file (use `templates/_template.md` from the Quern repo as a
  starting point, or copy an existing screen file as a template).
- **Removed screens**: drop the corresponding KB file *and* update
  `reachable_from` / `leads_to` references in neighboring screen
  files that pointed at it.

## If you changed knowledge base files (`.quern/knowledge/`)

- **Verify against the live app**: run `load_landmarks` and
  `identify_screen` (or `get_screen_summary?identify=true`) on the
  screens you touched. `confidence: "exact"` confirms the landmarks
  match reality. `confidence: "none"` means you're committing
  landmarks that don't match the actual UI.
- **Validator clean**: `validate_landmarks` should report no
  collisions. Two screens with overlapping landmark sets will fight
  for identification — fix by adding a distinguishing element to one.
- **No legacy `identify_by:` left over**: if you migrated from the
  pre-landmarks schema, the `identify_by:` field is now optional
  human-readable hint material. Ensure it doesn't lie about the
  current schema (e.g., reference fields the loader doesn't read).

## If you added or modified tests

- **Tests pass locally** before the hook runs — your existing
  pre-commit hook (or your test runner of choice) will catch regressions
  but only after the commit attempt. Better to know now than after a
  hook rejection.
- **New behavior has new tests** — existing tests passing isn't
  enough if the new path isn't covered.

## Always

- **Commit message focuses on the *why*** — a future reader (you,
  an agent, a reviewer) needs to understand what motivated the change,
  not just what got typed where.
- **No stray debug code** — `print` / `console.log` / `NSLog` left in
  code that wasn't there before, hard-coded credentials in test
  scaffolding, etc. Quick scan of the diff.

## When in doubt

If a Quern automated workflow you've built (a recipe, an agent task,
a script that drives `tap_element` and friends) suddenly stops working
after a commit, the most likely cause is landmark drift. Re-run the
verification step above on the affected screens. The `quern-landmark-
migration` agent skill walks the per-file workflow for catching
multiple drifted screens at once.

---

*This checklist is project-default, installed by `quern setup`. To
add personal items, edit `~/.claude/settings.local.json` (Claude Code's
per-user override location). To remove this hook entirely, delete the
matching entry from `~/.claude/settings.json`.*
