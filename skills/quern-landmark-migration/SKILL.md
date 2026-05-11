---
name: quern-landmark-migration
description: >
  Migrate a Quern app knowledge base from the legacy `identify_by:` schema to
  the `landmarks:` schema (introduced April 2026 in PR #22). Use this skill
  whenever `load_landmarks` returns a non-empty `skipped[]` array with
  `legacy_format` entries, or when the user explicitly asks to migrate /
  upgrade a knowledge base to landmarks. Categorizes files by translation
  difficulty, batches the mechanical ones with a single consolidated diff,
  and defers files that need the live screen for re-authoring.
  Trigger on: load_landmarks response showing skipped legacy_format files,
  user mentions "migrate landmarks" / "migrate knowledge base" / "update KB
  to landmarks" / "upgrade screens to landmarks", or working in a `.quern/
  knowledge/` directory whose screens use `identify_by:` not `landmarks:`.
---

# Quern Landmark Migration

The Quern app knowledge base format gained a structured `landmarks:` field in PR #22 (April 2026) for machine-evaluable screen identification. Knowledge bases authored before that use the older `identify_by:` field, which the loader now ignores. `load_landmarks` reports each pre-landmarks file in its `skipped[]` array with `reason: "legacy_format"` and the original `identify_by:` entries echoed back — that's your migration source.

Most legacy entries can be translated mechanically (rename the field, swap a couple of values). A few need judgment. The skill batches the safe stuff, surfaces the uncertain stuff, and defers the parts that genuinely need a live screen.

## When to use this skill

- A user wants to migrate an entire pre-landmarks knowledge base. **This is the primary case.**
- `load_landmarks` against a KB returns ≥5 `legacy_format` entries — enough that a per-file walk would be tedious.

**When NOT to use it:** If only one or two files need migration, just translate them by hand using the rules below. The skill's batch flow is overhead for tiny migrations.

## Workflow

### 1. Categorize first, plan once

Call `load_landmarks` against the KB path. Walk the `skipped[]` array, group each `legacy_format` entry into one of three tiers based on its content:

- **Tier A — Pure-mechanical.** Entries are dicts using only `element`, `identifier`, `label`, `label_contains`, and/or `absent`. Translation is a 1:1 field rename + drop the `identify_by:` key in favor of `landmarks:`. **Drift risk: low** — these are the same selectors, just under a new field name.
- **Tier B — Schema-translation needed.** Entries that use one or more of: `value: "1"` (→ `selected: true`), `value: "0"` (drop the entry), `role_description` (drop the field), `label_prefix` (translate to `label_contains` with a note about the loss of prefix anchoring), or any `value: <other>` (drop the entry). Mechanical with rules; the agent applies them, the user reviews the consolidated diff.
- **Tier C — Prose-only.** `identify_by:` is a list of plain strings rather than dicts (e.g. `"SFSafariViewController showing server settings page"`). Cannot be translated mechanically. **Needs the live screen** for re-authoring landmarks from scratch.

Report the categorization to the user — counts per tier plus the per-file list — and propose:

- **Tier A + B in one batch.** Single consolidated diff, one approval. The translation rules are deterministic; reviewing 30 micro-edits one-by-one doesn't add safety, just toil.
- **Tier C deferred.** TODO list. Each entry needs a screen visit; do it when the user is on that screen, or as a separate session.

If the user just wants Tier A migrated and not Tier B, that's fine — split the batch.

### 2. Watch for drift signals before batching

The translation is mechanical, but the *underlying app* may have moved on since the KB was authored. Before applying the batch, check for drift:

- If you have a recent `get_screen_summary` from one of the screens in the batch, eyeball whether the landmark identifiers actually exist on screen. If a specific identifier from the legacy YAML (e.g. `feature.somefield`) doesn't appear in the current UI tree at all, the data is stale — Tier A migration would silently produce landmarks that never match.
- If the KB hasn't been touched in 6+ months, treat the entire batch as drift-suspect.

When drift is suspected, switch to **per-file mode** for the affected files: open the screen, run `get_screen_summary`, re-author landmarks from what's actually there. Don't mechanically translate stale data.

When drift looks unlikely (recent KB, identifiers spot-checked clean), batch confidently.

### 3. Translation rules

For each entry within `identify_by:`:

| Legacy field | Action |
|---|---|
| `element: "..."` | Keep verbatim. Required field on the new schema too. |
| `identifier: "..."` | Keep verbatim. Same semantics. |
| `label: "..."` | Keep verbatim. Same semantics. |
| `label_contains: "..."` | Keep verbatim. Same semantics. |
| `label_prefix: "..."` | The new schema doesn't have `label_prefix`. Translate to `label_contains:` and note the loss of prefix-anchored matching in the diff. |
| `value: "1"` | Translate to `selected: true`. The legacy way to express "this radio button / tab / switch is in the on state." |
| `value: "0"` | Drop the entry entirely. "This thing is unselected" is rarely useful — almost always the actual landmark is something else about the screen and the unselected state was overspecified. Surface the drop in the diff so the user can override if they specifically want a `selected: false` landmark. |
| `value: "<other string>"` | Drop the entry; flag in the diff. The new schema has no general value-matching surface. |
| `role_description: "..."` | Drop. This was a freeform hint that the loader has always ignored. |
| `absent: true` | Keep verbatim. Same semantics. |
| Any other key | Drop; flag in the diff. |

Bare-string entries (Tier C) cannot be translated. Either:

- Mark the file with a TODO comment and an empty `landmarks: []` list, leaving `identify_by:` intact as the human-readable note.
- Visit the screen with `get_screen_summary` and propose new landmarks based on what's actually there.

The second option is better when the screen is reachable. Surface the choice to the user.

### 4. Apply the batch

For each file in the batch (after the user approves the consolidated diff):

1. Read the file with the Read tool.
2. Build the replacement YAML block per the rules above.
3. Apply Edit: replace the `identify_by:` block with the new `landmarks:` block. By default, leave `identify_by:` intact below the `landmarks:` block — the loader ignores it, but it serves as a human-readable hint that other tools and humans may still read. Drop `identify_by:` only if the user explicitly wants a clean rewrite.
4. Move on without per-file reconfirmation. The batch was approved as a unit.

If a single file's edit fails (frontmatter shape unusual, YAML parse error in the file), stop and report the file. Don't silently skip.

### 5. Tier C: per-file with the live screen

For each prose-only file the user wants to migrate now (vs. defer):

1. Identify the screen (use `reachable_from` in the existing file, or ask the user).
2. Navigate there: `launch_app` + `tap_element` chain, or restore an app state.
3. Run `get_screen_summary` (and optionally `get_ui_tree`) to see actual elements.
4. Pick 1–3 landmarks per the same priorities as a fresh authoring (nav title, unique identifier, tab selection state).
5. Apply Edit with the new landmarks block. Drop the prose `identify_by:` or keep it as a comment, depending on whether it was a useful note or a placeholder.

### 6. Verify

After the batch (and any per-file Tier C work):

1. Re-run `load_landmarks` against the KB path. Confirm `skipped[]` no longer contains `legacy_format` entries (only `no_landmarks` stubs and any deferred Tier C files should remain).
2. Run `validate_landmarks(app="...")` to detect collisions — overlapping landmark sets where two screens could be mistaken for each other. Resolve by adding a distinguishing element.
3. **Walk the live app screen-by-screen** and re-verify each migrated landmark against reality. This is the step that actually catches drift; mechanical correctness in YAML is not a guarantee that the underlying app still exposes those elements.

**Per-screen verification pattern** (works on every reachable screen):

```
1. Navigate to the screen (use `reachable_from` from the screen file as the recipe;
   for the home/main screen, just `launch_app`)
2. wait_for_element on a landmark (handles mid-transition empty UI trees)
3. get_screen_summary(identify=true)
4. If identified_as == screen_name and confidence == "exact" → ✅, move on
5. If confidence == "none" or "ambiguous" → drift; call get_ui_tree, find what the
   actual screen exposes, edit landmarks, re-load, re-verify
```

The `wait_for_element` step is essential — tapping into a new screen often returns
a transient near-empty UI tree (just the Application element) for ~1–2 seconds
before content settles. Without it you'll see false `confidence: none` results.

**Common drift patterns to watch for.** These are general; examples in the table are illustrative, not specific to any one app.

| Pattern | Symptom | Fix |
|---|---|---|
| Identifier namespace renamed | Multiple `identifier:` landmarks all miss; the screen's elements use a different prefix (e.g. a refactor moved `feature.foo` → `screen.foo`) | Re-author with the current identifier namespace from `get_ui_tree`. |
| Documentation placeholder used as a literal | A `label:` landmark with text like `"@username"`, `"<user>"`, `"{name}"`, `"[email protected]"`, or any value that was a doc convention rather than what the app actually displays | Drop that landmark; rely on the structural ones (heading text, unique button identifier). |
| Empty-state UI hides a chrome element | `Group identifier:"X"` lookup returns nothing on a "no data yet" screen, even though it exists when the screen has populated content | Pick a landmark that's stable across states (usually the screen Heading). Required-AND landmarks must use elements present in *every* state of the screen. |
| Renamed/restyled labels | A `label: "Old Label"` landmark misses because the product copy was rewritten | Re-author with the current label, or switch to an identifier-based selector. |
| Element type changed | Landmark with `element: "Heading"` misses; the same text is now under `StaticText` (or vice versa) | Inspect `get_ui_tree`, update the `element:` value to the current type. |

**What about Tier C screens?** The verify-by-navigation pattern often won't work for:
- Embedded web views (iOS: `SFSafariViewController`, `ASWebAuthenticationSession`; Android: `WebView`, Chrome Custom Tabs). Accessibility is shallow inside; identifiers from the website don't carry through to the platform UI tree.
- Empty-state secondary screens whose entry points require app data the test environment doesn't have (e.g. screens that only appear when a user has X items, when subscribed to a paid tier, or under specific permissions).

For these, document what's *known* about the screen state in the file's prose section and leave `landmarks: []` with a comment explaining why. Don't fabricate landmarks you can't verify.

**When verification is too expensive to run end-to-end**: spot-check a representative subset (5–10 screens spanning different navigation depths), and surface the rest as a TODO for the next time the user is using the app. Drift discovered later is better than mechanical migration trusted blindly.

## Output

Summarize at the end:

- Files migrated (Tier A + B): N
- Files deferred (Tier C, prose-only): list them with their original prose entries
- Entries dropped during translation (with reasons): summarize
- Collisions found by `validate_landmarks`: list with proposed fixes
- Drift detected during spot-checks: list files where live identification failed and propose Tier C re-authoring

## Common Pitfalls

- **Don't trust mechanical migration on a stale KB.** Drift is silent — landmarks look correct in YAML but never match the live UI. Spot-check post-migration; if drift is widespread, fall back to Tier C re-authoring for affected files.
- **Don't drop prose `identify_by:` entries without showing them to the user.** Those represent intentional notes that the structured schema can't preserve. Keep them as a TODO marker, or have the user decide.
- **Don't confuse `value: "1"` with the new `value:` field.** The new schema has no `value:` field at all. `value: "1"` always becomes `selected: true`; everything else gets dropped.
- **Don't over-engineer per-file approval.** The original version of this skill required per-file confirmation, which doesn't scale to a real KB with 30+ files. The batch approach with a consolidated diff is faster, equally safe (the rules are deterministic), and respects the user's time. Per-file mode exists for genuine drift cases, not as the default.

## Reference

- Schema: `templates/app-knowledge/screens/_template.md` for current frontmatter shape.
- Authoring guide: `docs/app-knowledge-guide.md` (Choosing Landmarks; Migrating a Legacy Knowledge Base).
- Design rationale: `docs/screen-landmarks.md` for the original PR #22 spec.
