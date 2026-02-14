# Quern API Additions: Field Trial Follow-ups

**Date:** February 13, 2026
**Status:** Spec — ready for implementation
**Context:** These three additions address friction points discovered during Quern's first field trial (automated login/logout testing of the Geocaching app). Each one eliminates a pattern that every test script currently reimplements by hand.

---

## 1. `GET /api/v1/device/ui/element` — Get Element State

### Problem

Every test script that needs to check whether a button is enabled, a field has a value, or an element is visible currently has to:

1. Fetch the entire UI tree (`GET /api/v1/device/ui`)
2. Iterate through all elements client-side
3. Find the matching element
4. Extract the property

This is the single most repeated pattern in the field trial test code:

```python
# This pattern appears 10+ times in the robust test script
ui_tree = self.get_ui_tree()
for elem in ui_tree.get("elements", []):
    if elem.get("identifier") == "_Log in button":
        return elem.get("enabled", False)
```

### Endpoint

```
GET /api/v1/device/ui/element?identifier={id}
GET /api/v1/device/ui/element?label={label}
GET /api/v1/device/ui/element?type={type}&label={label}
```

### Query Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `identifier` | string | No* | Accessibility identifier to match |
| `label` | string | No* | Accessibility label to match |
| `type` | string | No | Element type filter (e.g., `button`, `textField`). Combines with other params. |

\* At least one of `identifier` or `label` is required. If both are provided, both must match (AND logic).

### Response — 200 OK

```json
{
  "element": {
    "type": "button",
    "identifier": "_Log in button",
    "label": "Log in",
    "value": null,
    "enabled": false,
    "visible": true,
    "frame": {
      "x": 120,
      "y": 680,
      "width": 280,
      "height": 48
    }
  }
}
```

### Response — 404 Not Found

```json
{
  "error": "element_not_found",
  "message": "No element matching identifier '_Log in button' found on current screen",
  "query": {
    "identifier": "_Log in button"
  }
}
```

### Design Notes

- Returns the **first** matching element. If multiple elements match, the response includes a `"match_count": N` field so callers know they're getting an ambiguous result.
- The `frame` field uses the same coordinate system as `tap` coordinates — callers can use it to tap the element's center without a separate lookup.
- This is a read-only query against the current UI tree. It does not wait for the element to appear — that's what `wait-for-element` is for.

---

## 2. `POST /api/v1/device/ui/wait-for-element` — Wait for Element Condition

### Problem

Every test script reinvents polling:

```python
def wait_for_condition(self, condition_fn, description, timeout=10, interval=0.5):
    start = time.time()
    while time.time() - start < timeout:
        if condition_fn():
            return True
        time.sleep(interval)
    return False

# Usage: wait for login button to become enabled
def login_button_enabled():
    ui_tree = self.get_ui_tree()
    for elem in ui_tree.get("elements", []):
        if elem.get("identifier") == "_Log in button":
            return elem.get("enabled", False)
    return False

self.wait_for_condition(login_button_enabled, "login button enabled", timeout=10)
```

This generates many redundant UI tree fetches and pushes retry logic into every client. The server should handle it — it already owns the UI tree and can poll more efficiently with less overhead than HTTP round-trips.

### Endpoint

```
POST /api/v1/device/ui/wait-for-element
```

### Request Body

```json
{
  "identifier": "_Log in button",
  "condition": "enabled",
  "timeout": 10,
  "interval": 0.5
}
```

### Request Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `identifier` | string | No* | — | Accessibility identifier to match |
| `label` | string | No* | — | Accessibility label to match |
| `type` | string | No | — | Element type filter (combines with other params) |
| `condition` | string | Yes | — | Condition to wait for (see below) |
| `value` | string | No | — | Expected value (required for `value_equals` condition) |
| `timeout` | number | No | `10` | Max seconds to wait |
| `interval` | number | No | `0.5` | Seconds between polls |

\* At least one of `identifier` or `label` is required.

### Conditions

| Condition | Satisfied when... |
|-----------|-------------------|
| `exists` | Element is present in the UI tree |
| `not_exists` | Element is NOT present in the UI tree |
| `visible` | Element exists and `visible == true` |
| `enabled` | Element exists and `enabled == true` |
| `disabled` | Element exists and `enabled == false` |
| `value_equals` | Element exists and `value == {value}` parameter |
| `value_contains` | Element exists and `value` contains `{value}` parameter |

### Response — 200 OK (condition met)

```json
{
  "matched": true,
  "elapsed_seconds": 2.3,
  "polls": 5,
  "element": {
    "type": "button",
    "identifier": "_Log in button",
    "label": "Log in",
    "value": null,
    "enabled": true,
    "visible": true,
    "frame": {
      "x": 120,
      "y": 680,
      "width": 280,
      "height": 48
    }
  }
}
```

### Response — 200 OK (timeout, condition not met)

```json
{
  "matched": false,
  "elapsed_seconds": 10.0,
  "polls": 20,
  "element": null,
  "last_state": {
    "type": "button",
    "identifier": "_Log in button",
    "label": "Log in",
    "enabled": false,
    "visible": true
  }
}
```

Note: timeout returns 200, not an error status. The caller asked "wait for this condition" and the answer is "it didn't happen within your timeout." The `matched` field is the discriminator. This avoids conflating "the server broke" (5xx) with "the condition wasn't met" (expected outcome).

When the condition is `not_exists` and the timeout is reached with the element still present, `last_state` contains the element that was found. When `not_exists` succeeds, `element` is null (since the element isn't there).

### Design Notes

- **Timeout cap:** Server should enforce a maximum timeout (e.g., 60 seconds) to prevent runaway requests. Requests exceeding the cap get a 400 with a clear message.
- **Polling happens server-side.** The server polls idb's accessibility tree at the specified interval. This is far more efficient than the client making N separate HTTP requests.
- **The `last_state` field on timeout** tells the caller *why* the wait failed without requiring a follow-up query. Was the element there but disabled? Was it missing entirely? This eliminates a whole category of debugging guesswork.
- **This pairs with `get_element`:** Use `get_element` for instant checks, `wait-for-element` when you need to wait for a state transition (screen load, button enable after form fill, element disappearing after dismiss).

---

## 3. `GET /api/v1/device/screen-summary` — Improved Summary

### Problem

The current `screen-summary` endpoint truncates its element list when there are many elements on screen, returning a note like "...and 415 more." This caused real issues during the field trial — tab bar buttons were excluded from the truncated list, leading to false negatives when checking for navigation state.

The field trial workaround was to fall back to the full UI tree (`GET /api/v1/device/ui`) for any critical check, which defeats the purpose of having a summary endpoint.

### Changes

This isn't a new endpoint — it's improvements to the existing `GET /api/v1/device/screen-summary`.

#### 3a. Add `max_elements` parameter

```
GET /api/v1/device/screen-summary?max_elements=50
```

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `max_elements` | integer | No | `20` (current behavior) | Maximum interactive elements to include. Set to `0` for no limit. |

This gives callers control over the trade-off between response size and completeness. Agents that are token-conscious can use a low limit; scripts that need completeness can request more or uncap it.

#### 3b. Smarter element prioritization when truncating

When the element count exceeds `max_elements`, the current implementation appears to truncate by DOM order (or similar), which means elements lower in the tree (like tab bars, toolbars, navigation bars) get dropped first — exactly the elements that are most useful for determining *which screen* you're on.

The truncation should prioritize by navigational value:

1. **Navigation elements** — tab bar buttons, navigation bar buttons, back buttons
2. **Primary actions** — buttons with accessibility identifiers
3. **Form inputs** — text fields, toggles, pickers
4. **Labels and static text** — lowest priority for truncation

The response should include a `truncated` boolean and `total_interactive_elements` count so callers know whether they're seeing everything:

```json
{
  "summary": "Login screen with email and password fields",
  "interactive_elements": [ ... ],
  "truncated": true,
  "total_interactive_elements": 435,
  "max_elements": 20
}
```

#### 3c. Always include navigation chrome

Regardless of `max_elements`, the summary should **always** include:

- Tab bar buttons
- Navigation bar items (back button, title, right bar button items)
- Alert/dialog buttons (system or app-level)

These are carved out of the element budget — they're included in addition to `max_elements`, not counted against it. This guarantees that a caller can always determine the app's navigation state from the summary, which is its primary use case.

### Design Notes

- The `truncated` / `total_interactive_elements` fields are cheap to compute and immediately tell the caller whether they need the full tree. This eliminates the guessing that happened during the field trial.
- The "always include navigation chrome" rule is the key behavioral change. The summary exists to answer "where am I and what can I do?" — navigation elements are essential to both questions.
- The prioritization logic doesn't need to be perfect. A reasonable heuristic (tab bar items have type `tabBarButton`, nav bar items are children of a `navigationBar` container) covers the common cases. Edge cases can be refined based on usage.

---

## MCP Tool Implications

Each new API endpoint should have a corresponding MCP tool so agents get the same benefits:

| API Endpoint | MCP Tool | Primary Use |
|---|---|---|
| `GET /api/v1/device/ui/element` | `get_element_state` | Quick check: is this button enabled? What's the field value? |
| `POST /api/v1/device/ui/wait-for-element` | `wait_for_element` | Wait for screen load, button enable, element disappear |
| `GET /api/v1/device/screen-summary` (improved) | `get_screen_summary` (existing, improved) | Where am I? What can I do? |

The `get_element_state` tool reduces agent token usage significantly — instead of receiving the full UI tree and searching it, the agent gets back a single element's properties.

The `wait_for_element` tool changes the interaction pattern for agents. Currently an agent has to implement its own retry loop ("take screenshot, check state, wait, repeat"). With this tool, the agent says "wait until the login button is enabled, up to 10 seconds" and gets a single response. This is both cheaper (fewer tool calls) and more reliable (server-side polling is tighter than agent retry loops).

---

## Implementation Priority

These are ordered by impact-to-effort ratio:

1. **`get_element`** — Smallest change, biggest quality-of-life improvement. Single element lookup against the existing UI tree. Could ship in an hour.
2. **Screen summary improvements** — The `max_elements` parameter and `truncated` metadata are straightforward. The prioritization heuristic takes a bit more thought but doesn't need to be perfect on day one.
3. **`wait-for-element`** — Most impactful for test authors and agents, but slightly more complex (server-side polling loop, timeout management, max timeout enforcement).

All three can be built incrementally and don't depend on each other.
