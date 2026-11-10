# When a Knowledge Base Has No Landmarks

*`quern://app-knowledge-guide/no-landmarks` -- part of `quern://app-knowledge-guide`, the app knowledge base guide. Start from `quern://app-knowledge-guide`.*

If `load_landmarks` returns `screens: 0` with a populated `skipped[]`, each entry
says why:

- `legacy_format` — the file uses `identify_by:`, the field that preceded After renaming, bring the files to the current conventions: `quern://app-knowledge-guide/auditing` and `quern://app-knowledge-guide/choosing-landmarks`.
  `landmarks:` (April 2026). The loader has never evaluated it. The original
  entries are echoed back in `skipped[].identify_by`, so the rename can be done
  from the response alone: keep `element`, `identifier`, `label`,
  `label_contains` and `absent` as they are, turn `value: "1"` into
  `selected: true`, and drop anything else — those were freeform hints nothing
  read. Entries that are prose rather than mappings describe a state the schema
  cannot express; re-visit the screen and author landmarks from what it actually
  exposes.
- `no_landmarks` — a stub, or never annotated. Add landmarks on the next visit.
- `yaml_error` / `no_frontmatter` / `invalid_entries` — malformed. Inspect it.

Do not translate a stale knowledge base mechanically. Landmarks may need
adjustment for app changes made since it was written, and YAML that parses is no
evidence the app still exposes those elements — see `quern://app-knowledge-guide/landmarks-sync`.
