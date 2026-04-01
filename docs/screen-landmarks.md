# Screen Landmarks

## Problem

Quern's app knowledge base documents screens with an `identify_by` field, but it's a freeform hint for agents — not something Quern can evaluate programmatically. Every consumer that needs to answer "what screen am I on?" reimplements the matching: agents parse `identify_by` hints and compare against `get_screen_summary` output, recipes will need it, screen diffs need it, and the knowledge base itself can't validate whether two screens have ambiguous identities.

There's no shared, machine-evaluable definition of screen identity.

## Proposal

Formalize screen identity as **landmarks** — a small set of elements that uniquely identify a screen. Landmarks are:

- Stored in the knowledge base alongside existing screen documents
- Evaluated server-side by Quern against the live UI tree
- The foundation for `screen.matches()` in navigation recipes, screen diffs, and any future feature that needs to know "where am I?"

## What landmarks are

A landmark is an element selector that must be present (or absent) on a specific screen. A screen's identity is the conjunction of its landmarks — all must match for the screen to be recognized.

```yaml
# In a screen document's frontmatter
landmarks:
  - { element: "navigationBar", label: "Settings" }
  - { element: "staticText", label: "Account" }
```

This says: "If the screen has a navigation bar titled 'Settings' and a static text element labeled 'Account', this is the Settings screen."

### Landmark selection priorities

Not all elements make good landmarks. In order of reliability:

1. **Navigation bar title** — Most unique, most stable. One per screen.
2. **Tab bar selection state** — Which tab is active. Stable across app versions.
3. **Unique static text** — Section headers, screen titles outside nav bars.
4. **Unique interactive elements** — A button or field that only exists on this screen.
5. **Element combinations** — When no single element is unique, two ordinary elements together may be.

Avoid as landmarks:
- Dynamic content (user names, counts, dates)
- Elements that appear on many screens (generic "Back" buttons, tab bar items that aren't selected)
- Identifiers over labels (identifiers are less stable and less human-readable — see identifier reliability notes in the knowledge base guide)

### Landmark structure

Each landmark is a selector with optional fields:

```yaml
landmarks:
  - element: "navigationBar"    # element type (required)
    label: "Settings"           # label to match (optional, but almost always used)
    identifier: "settings_nav"  # accessibility identifier (optional, use when label is ambiguous)
    absent: true                # if true, this element must NOT be present (optional, rare)
```

Matching rules:
- `element` matches against the UI element's type
- `label` matches as a substring (case-insensitive) against the element's label — exact match is preferred but partial accommodates minor label changes
- `identifier` matches exactly against the accessibility identifier
- If multiple fields are specified, all must match (AND)
- All landmarks for a screen must match (AND across the list)

## Integration with the knowledge base

### Template change

The screen template's `identify_by` field evolves into `landmarks`:

```yaml
---
screen: "Settings"
status: documented

# Machine-evaluable screen identity.
# All landmarks must match for this screen to be recognized.
landmarks:
  - { element: "navigationBar", label: "Settings" }

# Legacy field — kept for human-readable hints during transition.
# Agents should prefer landmarks for programmatic matching.
identify_by:
  - { element: "navigationBar", label: "Settings" }
```

During a transition period, both fields coexist. `identify_by` remains as the human-readable hint. `landmarks` is the machine-evaluable version. For most screens they'll be identical. Eventually `identify_by` can be retired.

### Authoring during the guided tour

The guided tour workflow (documented in `app-knowledge-guide.md`) already captures elements per screen. The landmark selection step slots in naturally:

1. Agent visits screen, runs `get_screen_summary`
2. Agent documents key elements (existing step)
3. **Agent selects landmarks** — picks 1-3 elements that best identify this screen
4. Agent writes the screen document with `landmarks` populated

### Naive first pass, then collision check

Landmark selection happens in two phases:

**Phase 1 — Naive selection during tour:**
For each screen, the agent picks the most obvious landmarks — typically the nav bar title. This is fast and correct for most screens.

**Phase 2 — Collision detection after first pass:**
After all screens are documented, run a validation pass:

1. Load all screen documents and their landmarks
2. For each pair of screens, check if their landmark sets overlap — could one screen's landmarks also match another screen?
3. Report collisions: "Settings and Account Settings both match on `navigationBar: Settings` — need a distinguishing landmark"
4. Agent (or human) refines colliding screens by adding a distinguishing landmark

This two-phase approach avoids over-engineering landmarks upfront. Most screens are trivially distinct. Only the ambiguous pairs need refinement.

### Collision detection

Two screens collide when every landmark of screen A could also be present on screen B (or vice versa). This happens when:

- Two screens share the same nav bar title (e.g., a modal and a pushed screen both titled "Settings")
- A screen has only generic landmarks (just a tab bar state)
- A screen has no landmarks at all (stub or lazy documentation)

Collision detection can be:
- **Static** — compare landmark definitions across screen documents (fast, catches obvious cases)
- **Dynamic** — actually navigate to both screens, capture the UI tree, and check if screen A's landmarks match on screen B (thorough, catches subtle cases)

Static is the default. Dynamic is a validation tool for high-confidence knowledge bases.

## Server-side matching API

### New endpoint

```
POST /api/v1/device/screen/identify
{
  "landmarks_set": {
    "Login": {
      "landmarks": [
        {"element": "TextField", "label": "Email"},
        {"element": "Button", "label": "Sign In"}
      ]
    },
    "Home": {
      "landmarks": [
        {"element": "navigationBar", "label": "Home"}
      ]
    },
    "Settings": {
      "landmarks": [
        {"element": "navigationBar", "label": "Settings"}
      ]
    }
  },
  "udid": null
}
```

Response:

```json
{
  "matched": "Login",
  "confidence": "exact",
  "matched_landmarks": [
    {"element": "TextField", "label": "Email", "found": true},
    {"element": "Button", "label": "Sign In", "found": true}
  ],
  "partial_matches": [
    {"screen": "Home", "matched": 0, "total": 1}
  ]
}
```

- `matched` — the screen whose landmarks all matched, or `null` if no screen matched
- `confidence` — `"exact"` (one screen matched), `"ambiguous"` (multiple screens matched), or `"none"`
- `partial_matches` — screens with some but not all landmarks matched, useful for debugging

### MCP tool

```
identify_screen(landmarks_set=<from knowledge base>)
→ matched: "Login" (exact)
```

Or integrated into existing tools:

```
get_screen_summary(identify=true)
→ { ...normal summary..., "identified_as": "Login", "confidence": "exact" }
```

The second form is more practical — agents already call `get_screen_summary` regularly. Adding identification to it avoids an extra round-trip.

### How recipes use it

The `screen.matches()` API in navigation recipes becomes a thin wrapper:

```python
@recipe("navigate_to_map")
async def navigate_to_map(quern, credentials=None):
    for _ in range(10):
        screen = await quern.get_screen_summary(identify=True)

        if screen.identified_as == "Map":
            return {"screen": "Map"}

        if screen.identified_as == "Login":
            # handle login...
```

The recipe doesn't carry landmark definitions — they come from the knowledge base, loaded into Quern when the recipes are activated. The recipe just uses screen names.

## Landmark loading

Landmarks need to get from knowledge base files (in the app repo) into Quern's runtime. Two mechanisms:

### 1. Load with recipes

When recipes are activated (`POST /api/v1/recipes/load`), the loader also scans for a landmarks file in the same directory or a sibling `knowledge/` directory. This keeps landmarks and recipes co-located and co-deployed.

### 2. Explicit load

```
POST /api/v1/landmarks/load
{
  "source": "/Users/dev/myapp/.quern/knowledge/"
}
```

Or via MCP:
```
load_landmarks(path="/Users/dev/myapp/.quern/knowledge/")
```

Quern scans screen documents, extracts `landmarks` from frontmatter, and holds them in memory for identification queries.

## Validation tools

### Collision check command

```bash
quern validate-landmarks ~/Dev/myapp/.quern/knowledge/
```

Or via MCP:
```
validate_landmarks(path="/Users/dev/myapp/.quern/knowledge/")
→ 2 collisions found:
  - "Settings" and "Account Settings" share landmarks: navigationBar="Settings"
  - "Home" and "Explore" share landmarks: tabBar selected="Home"
  3 screens have no landmarks: stub-profile, stub-help, stub-about
```

### Live validation

```
POST /api/v1/landmarks/validate
{
  "screen": "Login",
  "udid": null
}
```

Navigate to the Login screen manually, then call this. Quern checks the Login screen's landmarks against the live UI and reports which matched and which didn't. Useful for verifying landmarks after app updates.

## Implementation phases

### Phase 1: Schema and knowledge base integration
- Add `landmarks` field to screen template frontmatter
- Add landmark parsing to the knowledge base loader (extract from YAML frontmatter)
- Update the guided tour workflow in `app-knowledge-guide.md` to include landmark selection
- Static collision detection across screen documents

### Phase 2: Server-side matching
- Landmark matching logic against live UI tree
- `POST /api/v1/device/screen/identify` endpoint
- `identify_screen` MCP tool
- Optional `identify=true` parameter on `get_screen_summary`

### Phase 3: Recipe integration
- `screen.identified_as` in RecipeContext (built on phase 2 endpoint)
- Automatic landmark loading when recipes are activated
- `validate_landmarks` CLI and MCP tool

## Open questions

### Substring vs exact label matching
Substring matching is more resilient to minor label changes ("Settings" matches "Settings (Beta)") but risks false positives ("Log" matching "Login" and "Log Out"). The default should probably be exact match with an explicit `contains: true` option for cases where partial matching is needed.

### How many landmarks are enough?
For most screens, 1-2 landmarks suffice. The collision check tells you when you need more. There's no hard rule — the minimum set that uniquely identifies the screen is the right number.

### Landmark stability across app versions
Landmarks should be the most stable elements on screen — nav bar titles, primary headings. But apps change. The validation tooling (live checks, collision detection) is the safety net. When a landmark stops matching, the failure is fast and obvious — not silent misbehavior.
