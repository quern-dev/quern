# Screen Identification in Action Responses

## Problem

The `feature/screen-landmarks` branch ships landmarks (declarative, structured screen identifiers) and exposes identification through `GET /ui/summary?identify=true`. That's useful, but it requires a follow-up call: an agent has to act, then call `get_screen_summary` separately to confirm where they landed.

The high-value pattern is **single round-trip**: agent acts, response tells them where they landed (and whether it matched expectations). This is the assertion side of landmarks — the part that's strictly safer than imperative recipes — and it's a small lift now that the screen-landmarks plumbing exists.

## Goal

When an agent calls an action endpoint that returns screen context, that screen context should include landmark-based identification automatically. Optionally, the agent can declare what screen they expect to land on, and the response tells them whether reality matched.

## Non-goals

- Navigation recipes, retry loops, or any procedural execution. The line drawn during screen-landmarks review stands: **landmarks are assertions, recipes are actions.** This spec is strictly on the assertion side.
- Auto-recovery on mismatch. The server reports; the agent decides what to do.
- Probabilistic / fuzzy screen matching beyond what landmarks already do.

## Existing plumbing this builds on

Already in main (predates `feature/screen-landmarks`):
- `_capture_screen_context()` in `server/api/device_ui.py` — single helper that builds the screen-context dict
- `include_screen_context: bool` on `tap_element` and `type_text` — opt-in screen capture after a successful action
- `tap_element` on `not_found` already raises 404 with screen context in the detail (line 357)

Added in `feature/screen-landmarks`:
- `LandmarkRegistry` on `request.app.state.landmark_registry`
- `registry.identify(elements) -> {"matched": str|None, "confidence": float, ...}`

The follow-up is mostly wiring these together at one chokepoint.

## Design

### Behavior

1. **Auto-identify when landmarks are loaded.** Inside `_capture_screen_context()`, after fetching elements, if the registry has any landmarks loaded, run `registry.identify(elements)` and add `identified_as` + `confidence` to the screen-context payload. Zero cost when no landmarks are loaded (no-op).

2. **Optional `expected_screen` parameter on action endpoints.** When provided, the response includes `matched_expected: bool` based on whether `identified_as == expected_screen`. `expected_screen` implies `include_screen_context=true` — no point asking what screen you landed on without capturing the screen.

3. **Surface ambiguity honestly.** If `registry.identify()` returns multiple candidate matches (collision), include them as `candidates: [...]` and set `confidence` accordingly. Don't silently pick one.

4. **No new behavior when landmarks aren't loaded.** Endpoints continue to behave exactly as today. This is purely additive.

### API surface

**Affected endpoints** (all existing, all in `server/api/device_ui.py`):
- `POST /ui/tap-element` — primary use case
- `POST /ui/type` — for "did I successfully submit?"
- `POST /ui/swipe` — for paged/carousel navigation
- `POST /ui/press` — for hardware button transitions (home, volume, etc.)
- (`POST /ui/clear` — probably skip; clearing text doesn't change screens)

**New request fields** (all optional, all on the existing request models):
```python
expected_screen: str | None = None  # screen name to assert against
# include_screen_context already exists; expected_screen implies True
```

**New response fields inside `screen_context`:**
```json
{
  "screen_context": {
    "summary": { ... existing ... },
    "identified_as": "Login",        // null if no match
    "confidence": 1.0,                // 0.0-1.0
    "candidates": ["Login", "Signup"],// only when ambiguous
    "matched_expected": false,        // only when expected_screen was provided
    "expected": "Map"                 // echo, only when expected_screen was provided
  }
}
```

### Example exchanges

**Happy path — agent expectation matches reality:**
```
POST /ui/tap-element
{ "label": "Sign In", "expected_screen": "Map" }

200 OK
{
  "status": "ok",
  "tapped": {...},
  "screen_context": {
    "identified_as": "Map",
    "confidence": 1.0,
    "matched_expected": true,
    "expected": "Map"
  }
}
```

**Mismatch — agent thought they'd land on Map, but Login still showing:**
```
200 OK
{
  "status": "ok",
  "tapped": {...},
  "screen_context": {
    "summary": { ... full screen for recovery ... },
    "identified_as": "Login",
    "confidence": 1.0,
    "matched_expected": false,
    "expected": "Map"
  }
}
```

**Element not found — 404 already returns screen context; identification gets folded in for free:**
```
404
{
  "status": "not_found",
  "screen_context": {
    "identified_as": "Environment Picker",
    "confidence": 1.0
  }
}
```

## Implementation plan

Estimated total: ~50-80 lines of source change + ~150 lines of tests.

### Files to touch

| File | Change |
|---|---|
| `server/api/device_ui.py` | Update `_capture_screen_context()` to call `registry.identify()` when landmarks loaded; add `expected_screen` to relevant request models; thread through to screen_context; raise 400 when `expected_screen` set but registry empty |
| `server/models.py` | Add optional `expected_screen: str \| None` to `TapElementRequest`, `TypeTextRequest`, `SwipeRequest`, `PressButtonRequest`; add `include_screen_context` to `SwipeRequest` and `PressButtonRequest` |
| `server/device/landmarks.py` | Update `registry.identify()` to return `1/N` confidence + `candidates` list on ambiguous matches |
| `mcp/src/tools/device-ui.ts` | Add `expected_screen` to MCP tool input schemas for the matching tools; add `include_screen_context` to swipe/press wrappers |
| `tests/test_device_api.py` | New cases for: identify when landmarks loaded, no-op when not loaded, `expected_screen` match, `expected_screen` mismatch, `expected_screen` with no landmarks loaded (400), ambiguous identification |
| `tests/test_landmarks.py` | New cases for ambiguous-match confidence scoring |
| `docs/screen-landmarks.md` | Add a section on the action-bundled assertion pattern |

### MCP tools updated

- `tap_element` — add `expected_screen`
- `type_text` — add `expected_screen`
- `swipe` — add `expected_screen` and `include_screen_context`
- `press_button` — add `expected_screen` and `include_screen_context`

The MCP wrapper stays thin: parameters pass straight through.

### Order of work

1. Land identification in `_capture_screen_context()` (smallest diff, highest reuse)
2. Add `expected_screen` to one endpoint (`tap_element`) end-to-end with tests
3. Replicate to other endpoints
4. Update MCP wrappers
5. Doc update

## Test plan

- **Unit:** `_capture_screen_context()` with/without registry populated, with/without `expected_screen`, with ambiguous landmarks; `registry.identify()` ambiguous-match confidence scoring
- **Integration:** API-level tests posting to each affected endpoint, asserting screen_context shape; 400 when `expected_screen` set but no landmarks loaded
- **MCP:** schema validation that `expected_screen` is accepted as optional string

## Decisions

1. **`expected_screen` implies `include_screen_context=true`.** Asking what screen you landed on without capturing the screen context is meaningless; promote it automatically rather than erroring on a contradictory combination.
2. **Error 400 when `expected_screen` is given but no landmarks loaded.** Returning `matched_expected: null` would silently degrade; the agent has stated an expectation that the server cannot evaluate. Response should hint at the fix, e.g.:
   ```json
   { "detail": "Cannot evaluate expected_screen=Map: no landmarks loaded. POST /landmarks/load first." }
   ```
3. **Add `include_screen_context` to `swipe` and `press` as part of this work.** Keeps the surface coherent — every action that can change the screen should be able to report what it changed to. Trivial addition since `_capture_screen_context()` already exists.
4. **Parameter name: `expected_screen`.** Matches the house convention of descriptive parameter names (`max_elements`, `include_screen_context`, `skip_stability_check`); `expect` would be jarringly terse next to its neighbors. Pairs naturally with the response field `matched_expected`.
5. **Confidence scoring for ambiguous matches.** Update `registry.identify()` so that when N landmarks match, confidence becomes `1/N` and `candidates` lists all matches. Single match stays `confidence: 1.0`. No match stays `confidence: 0.0`. This is a small change inside `server/device/landmarks.py` and warrants its own test cases.

## Naming convention check

Response fields use the past-tense / observed framing:
- `identified_as` — what the server determined (matches existing pattern in screen-landmarks branch)
- `matched_expected` — boolean, did `identified_as` equal `expected_screen`
- `expected` — echo of the input (helpful for log inspection)
- `candidates` — present only on ambiguous match
- `confidence` — float 0.0-1.0

Request fields use imperative / declarative framing:
- `expected_screen` — what the agent declares it expects
- `include_screen_context` — what the agent asks the server to do (existing)

## Out of scope (do not pursue here)

- Navigation recipes (`docs/navigation-recipes.md`) — separate decision, currently in proposal status
- Server-side retry loops or "wait until expected screen appears" — that's the slippery slope into recipe territory
- Mutating actions based on identification (e.g., "if landed on unexpected screen, auto-back-out") — same slope
