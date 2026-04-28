---
name: quern-landmark-migration
description: >
  Migrate a Quern app knowledge base from the legacy `identify_by:` schema to
  the `landmarks:` schema (introduced April 2026 in PR #22). Use this skill
  whenever `load_landmarks` returns a non-empty `skipped[]` array with
  `legacy_format` entries, or when the user explicitly asks to migrate /
  upgrade a knowledge base to landmarks. The skill walks through per-file
  review with user confirmation — never rewrite files in bulk without review.
  Trigger on: load_landmarks response showing skipped legacy_format files,
  user mentions "migrate landmarks" / "migrate knowledge base" / "update KB
  to landmarks" / "upgrade screens to landmarks", or working in a `.quern/
  knowledge/` directory whose screens use `identify_by:` not `landmarks:`.
---

# Quern Landmark Migration

The Quern app knowledge base format gained a structured `landmarks:` field in PR #22 (April 2026) for machine-evaluable screen identification. Knowledge bases authored before that use the older `identify_by:` field, which the loader now ignores. `load_landmarks` reports each pre-landmarks file in its `skipped[]` array with a `reason` of `legacy_format` and the original `identify_by:` entries echoed back — that's your migration source.

This skill is the agent-side workflow for migrating a knowledge base. The translation is mostly mechanical, but each file needs human review before rewrite, especially when the underlying app has changed since the KB was authored.

## When to Run

- Right after `load_landmarks` returns `skipped[]` with one or more `legacy_format` entries.
- When the user says "migrate the knowledge base" or "upgrade the screens to landmarks."
- Before relying on `identify_screen` for an app whose KB was authored before April 2026.

If the response only has `no_landmarks` or `yaml_error` skips and no `legacy_format` entries, this skill doesn't apply — those files weren't using the legacy schema, just unannotated.

## Workflow

### 1. Get the skip list

Call `load_landmarks` against the knowledge base path with the app bundle ID. Extract entries where `reason == "legacy_format"`. Group them:

- **Structured entries** (`identify_by[].element` is a string-keyed object) — mechanical migration possible.
- **Prose entries** (`identify_by[]` contains plain strings like `"SFSafariViewController showing server settings page"`) — flag for re-visit; don't auto-translate.
- **Mixed** — some entries structured, some prose. Migrate the structured ones; flag the rest.

### 2. Per-file migration

For each file in the `legacy_format` skip list:

1. **Read the file** with the Read tool. The skip's `file` field is a path relative to the knowledge base root (e.g., `screens/timelines.md`).
2. **Locate the frontmatter** between the leading `---` delimiters.
3. **Build the replacement `landmarks:` block** by translating each `identify_by:` entry per the rules below.
4. **Show the diff to the user** before writing — proposed `landmarks:` block + any flags (prose entries to re-visit, fields dropped, tab-selection guesses). Wait for confirmation.
5. **Apply the edit** with the Edit tool: replace the `identify_by:` block with the new `landmarks:` block. Leave `identify_by:` in the file *only if* the user wants the freeform-prose entries kept as a human hint — otherwise remove it.

Do not batch-rewrite. Per-file confirmation is the safety net.

### 3. Per-entry translation rules

For each entry within `identify_by:`:

| Legacy field | Action |
|---|---|
| `element: "..."` | Keep verbatim. Required field on the new schema too. |
| `identifier: "..."` | Keep verbatim. Same semantics. |
| `label: "..."` | Keep verbatim. Same semantics. |
| `label_contains: "..."` | Keep verbatim. Same semantics. |
| `label_prefix: "..."` | The new schema doesn't have `label_prefix`. Translate to `label_contains:` and note the loss of prefix-anchored matching, OR keep the prefix string under `label_contains:` and accept that it'll match middle-substring positions too. |
| `value: "1"` | Translate to `selected: true`. This was the legacy way to express "this radio button / tab / switch is in the on state." |
| `value: "0"` | Drop the entry entirely. "This thing is unselected" is rarely a useful screen-identifying signal — almost always the actual landmark is something *else* about the screen, and the unselected state was overspecified. Surface the drop in the diff so the user can override if they specifically want a `selected: false` landmark. |
| `value: "<other string>"` | Drop the entry; flag in the diff. The new schema has no general value-matching surface. |
| `role_description: "..."` | Drop. This was a freeform hint that the loader has always ignored. |
| `absent: true` | Keep verbatim. Same semantics. |
| Any other key | Drop; flag in the diff. |

Entries that are bare strings (`"SFSafariViewController showing..."`) cannot be translated mechanically. Either:
- Delete the file's `identify_by:` block entirely and add a `landmarks:` block with `[]` (empty list) plus a comment that the screen needs re-visiting, or
- Visit the screen with `get_screen_summary` and propose new landmarks based on what's actually there.

Prefer the second option when the simulator/device with the app is available. Surface the choice to the user.

### 4. Tab-selection landmarks specifically

If the legacy file has a tab-selection pattern like:

```yaml
identify_by:
  - { element: "RadioButton", identifier: "tab.timelines", value: "1" }
```

Translate to:

```yaml
landmarks:
  - { element: "RadioButton", identifier: "tab.timelines", selected: true }
```

Tab landmarks are usually the most distinguishing element on tabbed screens — keep them.

### 5. Verify after migration

After all files are rewritten:

1. Re-run `load_landmarks` against the knowledge base path. Confirm `skipped[]` no longer contains `legacy_format` entries (any remaining skips should be `no_landmarks` stubs or files the user chose to defer).
2. Run `validate_landmarks` to detect collisions — overlapping landmark sets where two screens could be mistaken for each other. Resolve by adding a distinguishing element.
3. (Optional but recommended) For each migrated screen, when the simulator is on that screen, call `identify_screen` and confirm `confidence == "exact"`. This catches landmarks that *look* right in YAML but don't match the live UI tree because the app changed since the KB was authored.

## Output

End with a summary:

- Files migrated: N
- Files flagged for re-visit (prose entries): list them
- Entries dropped (with reasons): summarize
- Collisions found by validator: list them with proposed fixes

If the user wants, suggest creating a follow-up task list for the flagged-for-re-visit files.

## Common Pitfalls

- **Don't migrate without reading the live screen first** if the app has been actively developed since the KB was authored. The legacy KB's identifiers may no longer exist on the running app — a mechanically-correct YAML rewrite gives a false sense of done while landmarks silently fail to match.
- **Don't drop `identify_by:` entries that contain prose** without showing them to the user. Those represent intentional notes that the structured schema can't preserve. Keep them in a comment block, or have the user decide.
- **Don't trust `value: "1"` blindly** — sometimes the legacy entry was an incorrect hand-edit. Verify against the live screen if you have any doubt.
- **Don't over-migrate.** If the user only wants a single file fixed, don't tour the whole KB. The skill's batch flow is opt-in.

## Reference

- Schema: `templates/app-knowledge/screens/_template.md` for current frontmatter shape.
- Authoring guide: `docs/app-knowledge-guide.md` (Choosing Landmarks; Migrating a Legacy Knowledge Base).
- Design rationale: `docs/screen-landmarks.md` for the original PR #22 spec.
