# Identifying Screens with Landmarks

*`quern://guide/ui/landmarks` -- part of `quern://guide/ui` in the Quern agent guide. Start from `quern://guide`.*

When the question is "what screen am I on right now?" — for verifying navigation, gating actions, or driving recipe-style workflows — landmarks give you a deterministic answer that doesn't depend on label parsing or model swaps.

**Why use landmarks instead of just reading `get_screen_summary`?**

- **Deterministic.** Quern matches the live UI tree against authored selectors and returns `matched: <screen_name>` with `confidence: exact | ambiguous | none`. No prose interpretation.
- **Cross-platform.** The same landmarks work on iOS (sim-bridge / idb / WDA depending on device) and Android (uiautomator2) — selection state, identifiers, and labels are normalized to a single schema.
- **Stable across LLMs.** A workflow that asks "is this the cart screen?" gets the same answer regardless of which model is driving.
- **Authored once, reused everywhere.** The screen knowledge base is the source of truth; agents don't re-discover screen identity on every call.

**Workflow:**

1. Load landmarks from the app's knowledge base. In the app's own project you usually need not: the MCP server loads the project's `.quern/knowledge` at session start, and the session's instructions say whether it did. Set `QUERN_PROJECT_DIR` in the MCP server's environment to point it at another project. Elsewhere -- or to keep it loaded across restarts:
   ```
   load_landmarks(path="/Users/dev/myapp", remember=true)
   ```
   **From a remote quern or a client key** the MCP can't send a path the server can read, so it uploads the project's knowledge base at session start and loads it for your session only; `load_landmarks(path=...)` uploads the same way, and `remember=true` is refused there. The set ends with your session -- not when you release a device -- and follows you through a `use_remote` switch.
   In an app project with no knowledge base, the instructions say that too, and `identify_screen` answers `no_landmarks_loaded` with a `hint`. Suggest building one to the user -- `init_app_knowledge` and `quern://app-knowledge-guide` -- rather than starting it unasked: it is a guided tour with them, and it writes into their repo.
   Or pass landmarks inline (useful for ad-hoc identification):
   ```
   load_landmarks(app="...", landmarks={"home-tab": [{"element": "RadioButton", "identifier": "tab.home", "selected": true}]})
   ```
2. Call `identify_screen(app="...")` or — more often — pass `identify=true` to `get_screen_summary` to fold identification into a call you were already making.
3. Read the response. On a match, you get `matched`, `confidence: "exact"`, and `matched_landmarks` with per-landmark hit/miss detail. On no match, `partial_matches` lists every evaluated screen sorted by descending match count, *each with its full per-landmark results* — you can see exactly which selectors failed without re-running.

**Landmark schema** (in screen frontmatter or inline JSON):

| Field | Purpose |
|---|---|
| `element` | Element type, required (e.g. `Button`, `RadioButton`, `Heading`). Matched against `type`, which follows the backend that read the screen — a simulator's tab is `RadioButton` from the accessibility tree and `Button` through WDA. With an `identifier` or a `label` as well, quern matches the same element under either name, and says so with `matched_via: "RadioButton≈Button"`; a type-only landmark is matched exactly. Avoid `navigationBar`: the accessibility tree does not expose one, so anchor on the screen title as a `Heading` |
| `identifier` | Accessibility identifier, exact match (preferred — locale-independent) |
| `label` | Label text, case-insensitive exact match |
| `label_contains` | Substring match for elements with dynamic content in their label |
| `absent: true` | Element must NOT be present (use sparingly — rare) |
| `selected: true` | For tabs/switches/radios/checkboxes, element must be in the on/active state. Distinguishes "the Home tab is the selected one" from "a Home tab exists." |

**A knowledge base with no landmarks:** If `load_landmarks` returns `screens: 0` and a populated `skipped[]`, each entry says why. `legacy_format` means the file uses `identify_by:`, the field that preceded `landmarks:` and that the loader has never evaluated; the original entries come back in the response, so the rename can be done from it directly (keep `element`/`identifier`/`label`/`label_contains`/`absent`, turn `value: "1"` into `selected: true`, drop the rest). Check the result against the running app rather than translating blind — a knowledge base old enough to use that field is old enough to have drifted.

**Validating before relying on landmarks**: Run `validate_landmarks(app="...")` after loading. Reports collisions (two screens whose landmark sets overlap — one could be mistaken for the other) and screens with no landmarks. Fix collisions by adding a distinguishing element to one of the screens.

**Checking a knowledge base against the current conventions**: `load_landmarks` and `validate_landmarks` both return a `conventions` block — counts over every screen file, and each file not `current` with the landmarks at fault and why (for example a type-only landmark that matches on one backend only). A file's `landmark_conventions: 2` is what it is written for, not a claim it complies; the block is the computed answer. "Auditing a knowledge base" in `docs/screen-landmarks.md` is the procedure.

**When `confidence: "none"` on a known-good screen**: the landmarks are likely stale — the app has shipped UI changes since the knowledge base was authored. Surface this to the user before continuing; downstream automation built on top of stale landmarks will silently produce wrong results (you'll act on the wrong screen and misreport state). The fix is to navigate to the screen, run `get_ui_tree`, and re-author the landmarks block from what's actually there. See "Keeping Landmarks in Sync" in the knowledge-base authoring guide.
