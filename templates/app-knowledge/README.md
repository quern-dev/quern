# App Knowledge Base

Structured knowledge about this app, designed for consumption by AI agents
using quern tools to navigate, test, and debug the app.

## Structure

- `app.md` — App identity, entry points, global navigation, and test accounts.
- `screens/` — One file per distinct screen. Identification, elements, states, navigation edges.
- `flows/` — Multi-step sequences that accomplish a goal. References screens by link.
- `deep-links/` — URL schemes and universal links that jump directly into app sections.
- `alerts/` — Dialogs, popups, permission prompts, and transient UI that can appear across screens.
- `quirks/` — Non-obvious behaviors, timing issues, device-specific workarounds.
- `states.md` — App-wide states (auth, subscription tier, onboarding, connectivity) that affect what the agent sees and can do.
- `environments.md` — Server environments (production, staging), how to switch, and behavioral differences.
- `glossary.md` — App-specific terminology and domain concepts.

## Conventions

- Screen identification uses accessibility labels and element types (what quern tools see).
- Links between documents use `[[wiki-link]]` style for graph traversal.
- Frontmatter is YAML for programmatic filtering.
- Prose is written for an agent — precise and actionable, not conversational.
- Dynamic content (lists, feeds) is described structurally: what the elements are, what they represent, and their navigation edges — not their specific content.
