# Screen Landmarks

## Problem

Quern's app knowledge base originally documented screens with an `identify_by` field, but it was a freeform hint for agents — not something Quern could evaluate programmatically. Every consumer that needs to answer "what screen am I on?" reimplements the matching: agents parse `identify_by` hints and compare against `get_screen_summary` output, recipes will need it, screen diffs need it, and the knowledge base itself can't validate whether two screens have ambiguous identities.

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
  - { element: "Heading", label: "Settings" }
  - { element: "StaticText", label: "Account" }
```

This says: "If the screen has a title 'Settings' and a static text element labeled 'Account', this is the Settings screen." The title is a `Heading` on a simulator's accessibility tree and a `StaticText` through WDA; with a label, the landmark matches either (see [Across backends](#across-backends)).

### Landmark selection priorities

Not all elements make good landmarks. In order of reliability:

1. **Screen title** — Most unique, most stable. One per screen. Name it as a `Heading` with its label, not as a `navigationBar`: the accessibility tree exposes no navigation bar a landmark can name, so a `navigationBar` landmark matches through WDA only.
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
  - element: "Heading"          # element type (required)
    label: "Settings"           # label to match (optional, but almost always used)
    label_contains: "Set"       # substring match for dynamic labels (optional)
    identifier: "settings_nav"  # accessibility identifier (optional, use when label is ambiguous)
    absent: true                # if true, this element must NOT be present (optional, rare)
    selected: true              # for tabs/switches/radios/checkboxes — element must be in the on/active state (optional)
```

Matching rules:
- `element` matches against the UI element's type
- `label` matches case-insensitively against the element's label
- `label_contains` matches as a case-insensitive substring (use for elements with dynamic content in their label)
- `identifier` matches exactly against the accessibility identifier
- `selected: true` requires the element's UI selection state to be on. Both iOS (`AXValue == "1"` from idb) and Android (uiautomator `selected="true"` for tabs, `checked="true"` for checkable widgets) are normalized to `AXValue == "1"` so the same landmark works cross-platform.
- If multiple fields are specified, all must match (AND)
- All landmarks for a screen must match (AND across the list)

### Across backends

The element type depends on which backend read the screen. A simulator is read through the accessibility tree by default; a physical device, and a simulator after `start_driver`, through WebDriverAgent, which reports XCUITest's types. The same tab-bar item is a `RadioButton` in one and a `Button` in the other.

So `element` is matched across the two, as far as the landmark's other fields make safe:

| Landmark has | Type matches |
|---|---|
| an `identifier` | the same type, its safe pair, or its family |
| a `label` or `label_contains`, no identifier | the same type or its safe pair |
| neither | the same type only |

**Safe pairs** — `RadioButton`↔`Button`, `Heading`↔`StaticText`, `TextField`↔`SecureTextField`, `CheckBox`↔`Switch`, `TabGroup`↔`SegmentedControl`, `TextArea`↔`TextView`, `Slider`↔`PageIndicator`.

**Families** — containers (`Group`, `Other`, `TabBar`, `NavigationBar`, `Toolbar`, `Table`, `CollectionView`); rows (`Button`, `StaticText`, `Cell`); indicators (`GenericElement`, `ProgressIndicator`, `ActivityIndicator`, `ColorWell`, `StaticText`); headers (`Heading`, `Other`, `StaticText`).

Families need an identifier because the accessibility tree's generic types stand for many specific ones — `Group` was the tab bar, the navigation bar, the toolbar, the table and the collection view on one probe app. And `Button`↔`StaticText` is never equated by label: a list row is one `Button` to the accessibility tree and a `Cell` holding a `StaticText` to XCUITest, so the label lands on the text *inside* the row.

**An identifier that only repeats the element's label counts as a label.** WebDriverAgent reports an element's identifier, or its label when it has none, and nothing distinguishes the two — so on WDA a screen title with no identifier reads as identifier "Settings", label "Settings". An identifier pins more than a label only when it says something the label does not, so such an element gets the label rule, not the family.

A landmark that matched only through an equivalence says so in its per-landmark result, as `matched_via: "RadioButton≈Button"`. An exact match carries nothing, and wins when both are on screen. The same rule applies to `element_type` on `tap_element`, `get_element` and `wait_for_element`, whose responses carry `matched_via` the same way.

The tables are measured, not inferred, and live in `server/device/element_types.py`; the measurement is in [`proposals/landmark-conventions.md`](proposals/landmark-conventions.md).

### Screen properties: `scrollable`

Alongside its landmarks, a screen may record whether it scrolls:

```yaml
---
screen: OrderHistory
scrollable: true      # false = known not to; omit = nobody has said
landmarks:
  - element: "Heading"
    label: "Orders"
---
```

**Why it is recorded rather than detected.** It cannot be detected. The
accessibility tree quern reads exposes interactive leaves, not containers:
measured on a simulator, Settings and Safari both scroll and both report zero
scroll containers in `type` and in `role`. So `tap_element` had to *swipe* to
find out, which on a screen that cannot scroll is two real gestures — and the
second is the pull-to-refresh and sheet-dismiss drag.

**What each value does.** `tap_element`'s `scroll_to_find` is tri-state:

| `scroll_to_find` | behaviour |
|---|---|
| `true` | always sweeps, knowledge base not consulted |
| `false` | never sweeps |
| unset (default) | identifies the screen and sweeps only on `scrollable: true` |

`false` and "nobody has said" both skip the sweep and **read differently**:
only `false` lets a miss say *"this screen does not scroll, the element is not
here"* and save a pointless retry. That is why the field is tri-state rather
than a boolean.

It is a hint, never a gate — an explicit `scroll_to_find=true` overrides it, so
a wrong entry costs a slowdown rather than making an element unreachable.

**iOS only.** Android does not read `scrollable`. A `tap_element` by exact
`label` or `identifier` alone goes through the native selector, and with
`scroll_to_find` unset it sweeps whenever the element is not in view, as it
did before the field existed; `false` still stops it. Asking the knowledge
base there would cost the full tree read that path exists to avoid. Android
taps that add a type, a value or a substring match take the tree path, which
does not sweep at all.

**Write an unquoted boolean.** `scrollable: "true"` is a string, and anything
that is not a literal boolean is read as "nobody has said" — a typo must never
be read as consent to swipe someone's screen. Note that the coercion is
currently **silent**: nothing reports it, so a quoted boolean simply does
nothing. Reporting it is proposed in
[`proposals/knowledge-base-health.md`](proposals/knowledge-base-health.md).

## Integration with the knowledge base

### Template change

`identify_by` was replaced by `landmarks`:

```yaml
---
screen: "Settings"
status: documented

# Machine-evaluable screen identity.
# All landmarks must match for this screen to be recognized.
landmarks:
  - { element: "Heading", label: "Settings" }
```

The two coexisted in the template for a transition period after April 2026 and
no longer do. `landmarks` is the only field used to load or match a screen. `identify_by`
is read solely to report it back as a diagnostic, and has never been evaluated
against a live UI. Prose that does not fit the structured schema belongs in the
body of the document, where a reader will actually find it.

**A knowledge base written before April 2026** has only `identify_by:`. The
loader reports those files in `skipped[]` with `reason: "legacy_format"` and
echoes the original entries back, which is enough to do the rename from the
response alone — see "When a Knowledge Base Has No Landmarks" in the app
knowledge guide. Do not translate one mechanically without checking it against
the running app; YAML that parses is no evidence the elements still exist.

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
3. Report collisions: "Settings and Account Settings both match on `Heading: Settings` — need a distinguishing landmark"
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
        {"element": "Heading", "label": "Home"}
      ]
    },
    "Settings": {
      "landmarks": [
        {"element": "Heading", "label": "Settings"}
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
    {"landmark": {"element": "TextField", "label": "Email"}, "matched": true},
    {"landmark": {"element": "Button", "label": "Sign In"}, "matched": true}
  ],
  "partial_matches": [
    {
      "screen": "Home",
      "matched": 0,
      "total": 1,
      "landmarks": [
        {"landmark": {"element": "Heading", "label": "Home"}, "matched": false}
      ]
    },
    {
      "screen": "Settings",
      "matched": 0,
      "total": 1,
      "landmarks": [
        {"landmark": {"element": "Heading", "label": "Settings"}, "matched": false}
      ]
    }
  ]
}
```

- `matched` — the screen whose landmarks all matched, or `null` if no screen matched
- `confidence` — `"exact"` (one screen matched), `"ambiguous"` (multiple screens matched), or `"none"`
- `matched_landmarks` — per-landmark results for the matched screen
- `partial_matches` — every evaluated non-fully-matched screen (including zero-match), sorted by descending match count so the best candidate is first. Each entry includes a `landmarks` array with per-landmark match results so an agent can debug "why didn't my landmarks match?" without re-running identification. The previous behavior — silently dropping zero-match screens — hid the most useful debugging signal.

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

Landmarks can also be loaded inline, keyed by screen name. Each value is either a list of landmarks, or an object that says more about the screen — the same fields a screen file's frontmatter carries:

```
load_landmarks(app="com.example.app", landmarks={
  "Home": [{"element": "Heading", "label": "Home"}],
  "Settings": {
    "landmark_conventions": 2,
    "scrollable": true,
    "landmarks": [{"element": "RadioButton", "identifier": "tab_settings", "selected": true}]
  },
  "Help": [{"web_url_contains": "/help", "web_process": "com.apple.SafariViewService"}]
})
```

An invalid landmark refuses the whole load with a 400 that names the screen and the landmark, and loads nothing.

The response includes:
- `loaded`: the app identifier
- `source`: the path that was scanned (or `"inline"` for inline-loaded landmarks)
- `screens`: the count of successfully loaded screens
- `skipped`: an array of screen files that couldn't be turned into landmarks, each with a `reason` and (where applicable) the original frontmatter data so the caller can act on it. Reason codes:
  - `legacy_format` — file has `identify_by:` but no usable `landmarks:`. The original entries are echoed back in `skipped[].identify_by` (verbatim — mappings can be renamed mechanically; freeform-prose strings need the screen re-visited).
  - `no_landmarks` — file has neither field, likely a stub.
  - `no_frontmatter` / `yaml_error` / `invalid_entries` — file is malformed; `error` is populated where applicable.
  - `read_error` — couldn't read the file.

## Auditing a knowledge base

Each screen file may declare the landmark conventions it is written for:

```yaml
---
screen: home
landmark_conventions: 2
landmarks:
  - { element: RadioButton, identifier: tab.home, selected: true }
---
```

This is a **target, not a claim of compliance**. Quern never writes it. It checks every file against the current conventions on `load_landmarks` and `validate_landmarks`, whatever the file declares, and reports the result in a `conventions` block:

```json
"conventions": {
  "current_version": 2,
  "counts": {"current": 40, "failing": 1, "behind": 0, "undeclared": 12},
  "files": [
    {"file": "screens/list.md", "screen": "list", "declared": null, "state": "undeclared",
     "findings": [{"landmark": {"element": "Group", "label": "Toolbar"},
                   "code": "needs_identifier", "message": "..."}]}
  ],
  "how_to_migrate": "..."
}
```

`counts` covers every file; `files` lists only those not reported `current`. The states:

| Declared | Findings | State |
|---|---|---|
| nothing | — | `undeclared` — read as v1 |
| older than current | — | `behind` |
| current | some | `failing` |
| current | none | `current` |
| newer than this quern | — | `newer` — update quern |

The findings are the same whatever the file declares, so for an undeclared or older file they are exactly what migrating would change. The v2 checks:

- `needs_identifier_or_label` — a type-only landmark on a type with a safe pair. It matches on one backend only.
- `needs_identifier` — a landmark on a generic type (`Group`, `Other`, `GenericElement`, `Cell`, `TabBar`, …) with no identifier. A label does not make it portable.
- `no_portable_counterpart` — a landmark on `NavigationBar`, `SearchField`, `DatePicker` or `Picker`, which the accessibility tree exposes with no label or identifier. Anchor the screen on its title instead.
- `legacy_format` — `identify_by:` without `landmarks:`.
- `invalid_declaration` — `landmark_conventions` is not a positive integer. Read as undeclared.

A labelled `Button` or `StaticText` is not flagged even though each is in a family: a labelled `Button` is portable to a tab item and not to a table row, quern cannot tell which a landmark means, and flagging the two commonest landmarks there are would bury the findings that are certain.

To audit:

1. **Load the knowledge base and read the `conventions` block.**
2. **Fix what it reports.** Typically: add an `identifier` to a type-only landmark, or move a landmark off a non-portable type onto the screen title.
3. **Set `landmark_conventions: 2`** on each file once it is fixed, and reload to confirm it reports `current`.
4. **Optionally, confirm behaviour as well as form.** Identify the live screen on the default backend, then again after `start_driver`. A landmark that identifies the screen on only one of them is the thing this exists to catch.

Steps 1–3 check the file against the conventions; step 4 checks it against the app. A file can pass the first three and still be wrong about the app — see [Keeping Landmarks in Sync](app-knowledge-guide.md#keeping-landmarks-in-sync).

Two views, two questions: `grep -L "landmark_conventions: 2" screens/*.md` lists the files nobody has migrated, with nothing loaded; the `conventions` block says which files actually comply.

## Validation tools

### Collision check command

```bash
quern validate-landmarks ~/Dev/myapp/.quern/knowledge/
```

Or via MCP:
```
validate_landmarks(path="/Users/dev/myapp/.quern/knowledge/")
→ 2 collisions found:
  - "Settings" and "Account Settings" share landmarks: Heading="Settings"
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
