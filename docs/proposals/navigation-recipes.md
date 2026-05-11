# Navigation Recipes

## Problem

AI agents and automation scripts spend most of their tokens and time on the same navigation problems repeatedly. Getting from "app just launched in unknown state" to "ready to test feature X" involves handling login screens, onboarding flows, permission dialogs, environment pickers, and other intermediate states. This logic is:

- **Rediscovered every session** by agents, at significant token cost
- **Duplicated across test files** in traditional automation, scattered through setUp methods and helper functions
- **Framework-locked** — written in Swift for XCUITest, Java for Espresso, JS for Detox, never portable

Every mature test suite has this code. Nobody treats it as a first-class concept.

## Proposal

Add **navigation recipes** to Quern — Python functions that encode procedural navigation logic, loaded at runtime and exposed as API endpoints.

A recipe takes a device from any starting state to a named destination through whatever intermediate screens it encounters. It navigates and confirms arrival. It does not assert business logic.

### Example

```python
# recipes/geocaching.py

from quern.recipes import recipe

@recipe("navigate_to_map")
async def navigate_to_map(quern, credentials: dict = None):
    """Get to the main map screen from any starting state."""
    for _ in range(10):
        screen = await quern.get_screen_summary()

        if screen.matches("Map"):
            return {"screen": "Map", "status": "arrived"}

        if screen.matches("Environment Picker"):
            await quern.tap_element(label="Production")

        elif screen.matches("Login"):
            await quern.tap_element(label="Email")
            await quern.type_text(credentials["email"])
            await quern.tap_element(label="Password")
            await quern.type_text(credentials["password"])
            await quern.tap_element(label="Sign In")
            await quern.wait_for_element(label="Sign In", absent=True, timeout=10)

        elif screen.matches("Terms of Use"):
            await quern.tap_element(label="Agree")

        elif screen.matches("Location Permission"):
            await quern.tap_element(label="Allow While Using App")

        elif screen.matches("Onboarding"):
            await quern.tap_element(label="Skip")

        else:
            # Unknown intermediate screen — try advancing
            await quern.swipe("left")

    raise RecipeError("Could not reach Map screen after 10 attempts")
```

### Consumption

The same recipe is callable from three contexts:

**MCP tool (agent)**:
```
execute_recipe(name="navigate_to_map", params={"credentials": {"email": "...", "password": "..."}})
→ handled Environment Picker, Login, Location Permission — arrived at Map
```

**HTTP API (automation script)**:
```
POST /api/v1/recipes/execute
{
  "name": "navigate_to_map",
  "params": {"credentials": {"email": "...", "password": "..."}}
}
```

**Python direct (in-process)**:
```python
from recipes.geocaching import navigate_to_map
await navigate_to_map(quern, credentials={...})
```

## What recipes are and are not

### Are

- **Navigation presets.** They move the device from state A to state B.
- **Procedural.** They contain real logic — conditionals, loops, state machines.
- **Declarative in intent.** The caller says "get me to the map." The recipe handles the how.
- **Confirming.** A recipe verifies it reached the destination before returning success.
- **Reusable.** Written once, called from agents, scripts, CI, or humans equally.

### Are not

- **Tests.** Recipes do not assert business logic. "Navigate to map" confirms the map screen is showing. It does not check that 47 pins loaded in under 2 seconds.
- **Page objects.** Recipes are not a model of what's on screen. They're the procedural knowledge of how to traverse screens. They use screen identity internally but don't expose it as a data model.
- **Exploratory.** Recipes encode known paths. They are not a framework for discovering new paths — that's the agent's job during the exploration/knowledge-building phase.

The boundary: **if it ends with "and I'm on the right screen," it's a recipe. If it ends with "and the data is correct," it's a test.** This line must be clear to both humans reading recipe files and agents deciding whether to put logic in a recipe or in their own reasoning.

## Architecture

### Recipe files

Recipes are Python files that live in the app project repo, version-controlled alongside app code:

```
myapp/
  src/
  tests/
  quern-recipes/
    __init__.py
    geocaching.py
    settings.py
```

Each file contains one or more `@recipe`-decorated async functions. The decorator registers metadata (name, description, parameter schema) without altering the function's behavior.

### Activation

Recipe files are inert until explicitly sent to a running Quern server. This is analogous to serverless function deployment — the file is authored and reviewed locally, then injected into Quern's API runtime.

```
POST /api/v1/recipes/load
{
  "source": "/Users/dev/myapp/quern-recipes/geocaching.py"
}
```

Or via CLI:
```bash
quern load-recipes ~/Dev/myapp/quern-recipes/
```

Or via MCP tool:
```
load_recipes(path="/Users/dev/myapp/quern-recipes/")
```

On load, Quern:
1. Reads the Python file
2. Validates syntax and decorator usage
3. Registers each `@recipe` function as a callable endpoint
4. Returns the list of registered recipes with their parameter schemas

Recipes can be reloaded (updated) or unloaded at any time. They persist in memory for the server's lifetime. They are not persisted across server restarts unless re-loaded — the source of truth is always the file in the app repo.

### Execution runtime

When a recipe is executed, Quern:
1. Creates a `RecipeContext` — a scoped client that provides `tap_element`, `type_text`, `get_screen_summary`, etc., bound to the target device
2. Calls the recipe function with the context and any caller-supplied parameters
3. Captures a structured execution log (each step taken, screen states observed, time elapsed)
4. Returns the recipe's return value plus the execution log

The `RecipeContext` exposes the same primitives as the existing MCP tools, but as an async Python API. No new capabilities — just a different calling convention.

### Credential handling

Recipes must not contain hardcoded credentials. If a recipe needs credentials (login flows, API keys, etc.), they are supplied by the caller at execution time via the `params` dict:

```
POST /api/v1/recipes/execute
{
  "name": "navigate_to_map",
  "params": {
    "credentials": {"email": "test@example.com", "password": "..."}
  }
}
```

Quern does not store, cache, or log credential values. They exist only for the duration of the recipe execution. The recipe file documents what parameters it expects; the caller is responsible for sourcing them (environment variables, vault, CI secrets, etc.).

### Execution log

Every recipe execution returns a structured log alongside the result:

```json
{
  "status": "success",
  "result": {"screen": "Map", "status": "arrived"},
  "duration_ms": 4200,
  "steps": [
    {"action": "get_screen_summary", "observed": "Environment Picker", "ms": 120},
    {"action": "tap_element", "label": "Production", "ms": 85},
    {"action": "get_screen_summary", "observed": "Login", "ms": 130},
    {"action": "tap_element", "label": "Email", "ms": 60},
    {"action": "type_text", "ms": 45},
    {"action": "tap_element", "label": "Password", "ms": 55},
    {"action": "type_text", "ms": 40},
    {"action": "tap_element", "label": "Sign In", "ms": 70},
    {"action": "wait_for_element", "label": "Sign In", "absent": true, "ms": 2100},
    {"action": "get_screen_summary", "observed": "Location Permission", "ms": 125},
    {"action": "tap_element", "label": "Allow While Using App", "ms": 90},
    {"action": "get_screen_summary", "observed": "Map", "ms": 110}
  ]
}
```

This log serves multiple purposes:
- **Agent context**: the agent knows what happened without re-inspecting the screen
- **Debugging**: when a recipe fails, the log shows exactly where and why
- **Performance tracking**: identify slow transitions over time

### Error reporting

When a recipe fails, the response includes:
- The error message from the recipe
- The execution log up to the point of failure
- The current screen summary at time of failure

```json
{
  "status": "error",
  "error": "Could not reach Map screen after 10 attempts",
  "duration_ms": 15000,
  "steps": [...],
  "screen_at_failure": { "summary": "..." }
}
```

Recipes should raise `RecipeError` with a human-readable message. Unhandled exceptions are caught and reported as recipe failures, not server errors.

## Recipe versioning and staleness

Recipe files are version-controlled in the app repo. Keeping them in sync with app changes is the responsibility of the development workflow, not Quern.

Approaches that can work alongside recipes:

- **CI validation**: run recipes against a fresh app install as a smoke test. Recipe failures surface broken navigation paths.
- **Agent repair**: when an agent encounters a recipe failure, it can explore the new UI, identify what changed, and propose an update to the recipe file.
- **Screen identity drift**: if `screen.matches("Login")` stops matching because the login screen was redesigned, the recipe fails fast with a clear log showing "unknown screen" — not silently doing the wrong thing.

Quern does not track recipe versions or detect staleness automatically. The recipe either works or it fails. The file diff in version control shows what changed.

## Relationship to existing concepts

### App knowledge base (`init_app_knowledge`)

Navigation recipes are a **separate concept** from the app knowledge base. The knowledge base is a discovery tool — an agent explores the app and builds a map of screens and elements. Recipes are authored artifacts — a human or agent writes code that encodes specific navigation paths.

An agent might use the knowledge base to inform writing a recipe, but the two are not coupled. You can have recipes without a knowledge base (hand-written by someone who knows the app) or a knowledge base without recipes (agent navigates ad-hoc every time).

### Screen context on errors

The existing screen-context-on-error feature returns UI state when an action fails. Recipes build on this — the `RecipeContext` captures screen state at each step, giving richer context than a single action's error response. The two features are complementary.

## Open questions

### Agent authoring capability

Can agents reliably author recipes today? Experience so far suggests they need significant human guidance to write the conditional logic and screen-matching patterns. This may improve as:
- Agents get better at the explore → codify workflow
- The recipe API provides good primitives (`screen.matches()`) that reduce the authoring burden
- Example recipes establish patterns the agent can follow

For now, assume recipes are human-authored or human-guided. Design the system so agent authoring is possible but not required.

### Screen matching API

`screen.matches("Login")` needs a definition. Options:
- **Name-based**: match against screen names from the app knowledge base
- **Element-based**: match against presence of specific landmark elements (e.g., "has a text field labeled Email and a button labeled Sign In")
- **Hybrid**: try name first, fall back to element fingerprint

The matching API is the most important design detail to get right. It determines how robust recipes are to minor UI changes.

### Recipe composition

Can recipes call other recipes? For example:

```python
@recipe("navigate_to_account_settings")
async def navigate_to_account_settings(quern, credentials=None):
    await quern.execute_recipe("navigate_to_map", credentials=credentials)
    await quern.tap_element(label="Profile")
    await quern.tap_element(label="Settings")
```

This is natural and probably necessary, but adds execution depth and error-attribution complexity. A recipe that fails three levels deep needs clear reporting of which recipe at which level failed.

### Parameterized destinations

Some recipes might want a destination parameter:

```python
@recipe("navigate_to_tab")
async def navigate_to_tab(quern, tab_name: str):
    await quern.execute_recipe("navigate_to_map")
    await quern.tap_element(label=tab_name, type="Tab")
    screen = await quern.get_screen_summary()
    if not screen.matches(tab_name):
        raise RecipeError(f"Expected {tab_name} screen after tapping tab")
```

This keeps the recipe count manageable for apps with many similar navigation paths.

## Implementation phases

### Phase 1: Runtime and execution
- `@recipe` decorator and `RecipeContext` class
- Recipe loading from file (Python import + validation)
- `POST /api/v1/recipes/load` and `POST /api/v1/recipes/execute` endpoints
- `execute_recipe` and `load_recipes` MCP tools
- Execution logging and error reporting
- `quern load-recipes <path>` CLI command

### Phase 2: Authoring support
- Recipe file template/scaffold generator
- `screen.matches()` implementation with element-based fingerprinting
- Example recipes for common patterns (login, onboarding skip, tab navigation)
- Documentation for writing recipes

### Phase 3: Ecosystem
- Recipe composition (recipes calling recipes)
- CI integration examples (run recipes as smoke tests)
- Agent-assisted recipe authoring workflow
- Recipe validation command (`quern validate-recipes <path>`)
