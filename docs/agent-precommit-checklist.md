# Pre-Commit Checklist

This checklist is surfaced as a system reminder before any `git commit` in
this repo (configured in `.claude/settings.json` → `scripts/agent-precommit-checklist.sh`).
Walk it before finalizing the commit. Items are conditional — only the
ones relevant to the change need attention.

## Always

- **Tests pass.** The pre-commit hook (ruff + pytest) runs automatically,
  but if you edited code without re-running locally first, expect a hook
  rejection. Better to run `.venv/bin/python -m pytest tests/` yourself
  before invoking `git commit`.
- **New behavior has new tests.** Existing tests passing isn't enough if
  the new path isn't covered.
- **Commit message focuses on the *why*.** A future reader (you, an
  agent, a code reviewer) needs to understand what motivated the change,
  not just what got typed where.

## If the change touches MCP tool behavior

- **Tool descriptions in `mcp/src/tools/*.ts` reflect the change.** New
  params, response shape, semantics. Agents read these descriptions cold;
  stale ones silently mislead the next session.
- **`docs/agent-guide.md` updated** when the change introduces a new
  workflow or significantly changes an existing one. The agent guide is
  the practical workflow surface; the MCP tool descriptions are reference.

## If the change touches API endpoints, request/response shapes, or persisted state

- **README's API endpoints table updated.**
- **README's `~/.quern/` files table updated** when a new sidecar file
  or persisted state is introduced.
- **MCP tool descriptions match.** API and MCP descriptions diverging is
  a common silent drift.

## If the change touches landmark schema, knowledge base loader, or screen-summary output

- **`docs/screen-landmarks.md`** (the spec doc) reflects the change.
- **`docs/app-knowledge-guide.md`** (the authoring guide) reflects new
  schema fields, validation behavior, or maintenance considerations.
- **`templates/app-knowledge/screens/_template.md`** updated if new
  fields belong in fresh screen documents.
- **Migration impact**: if downstream knowledge bases need to change, add
  a CHANGELOG note about the migration path. Consider whether the
  `quern-landmark-migration` skill needs updates.

## If the change adds, removes, or changes any user-facing behavior

- **`CHANGELOG.md` `[Unreleased]` section** has an entry. Group under
  `Added` / `Changed` / `Fixed` / `Documentation` per Keep a Changelog
  conventions.

## Before pushing

- **Branch in shape for PR.** If this is the last commit before a PR, do
  one final pass: tests pass on the whole branch, docs match shipped
  behavior, no stray debug prints / placeholder TODOs that were meant to
  be addressed.

---

*This checklist is project-shipped. Personal additions to your
pre-commit reminder belong in `.claude/settings.local.json` rather than
modifications to this file.*
