# Quern: App Graph & Test DSL Design

**Date:** February 12, 2026
**Status:** Early design. Captures conversations and directional decisions, not final spec.

---

## 1. Overview

Quern's test automation strategy rests on three pillars:

1. **The App Graph** — a structural model of the app's screens, controls, and navigation paths, stored as a versioned artifact in the project repo.
2. **The Test DSL** — a human-readable, AI-writable language for defining test flows at multiple levels of abstraction, from granular button presses to high-level outcome verbs.
3. **Agent Reasoning** — the graph serves not just as a test execution scaffold but as a resource that AI agents can use to *understand* the app when performing coding, debugging, and test tasks.

These three reinforce each other. The graph provides structure. The DSL provides expression. Agent reasoning provides intelligence.

---

## 2. The App Graph

### 2.1 What It Is

A directed graph where:

- **Nodes** are screens or distinct UI states (e.g., Login, Home, Settings, Map with Location Permission Dialog).
- **Edges** are user actions that transition between states (e.g., `tap "Sign In"` transitions from Login → Home).
- **Nodes carry metadata** about what's on each screen — controls, labels, data displays, accessibility identifiers.
- **Edges carry metadata** about what triggers the transition and any preconditions (e.g., "valid credentials required" for login).

### 2.2 Schema (Sketch)

```json
{
  "screens": {
    "login": {
      "name": "Login",
      "identity": {
        "markers": ["Email field", "Password field", "Sign In button"],
        "accessibility_id": "LoginViewController"
      },
      "source_files": [
        { "file": "LoginViewController.swift", "discovered_via": "accessibility_tree" },
        { "file": "AuthService.swift", "discovered_via": "runtime_observation", "functions": ["authenticate(email:password:)"] }
      ],
      "controls": [
        { "type": "text_field", "label": "Email", "accessibility_id": "email_input" },
        { "type": "text_field", "label": "Password", "accessibility_id": "password_input", "secure": true },
        { "type": "button", "label": "Sign In", "accessibility_id": "sign_in_button" },
        { "type": "button", "label": "Forgot Password?", "accessibility_id": "forgot_password_link" }
      ],
      "transient_states": [
        {
          "name": "invalid_email",
          "trigger": "enter non-email string in Email, tap 'Sign In'",
          "indicators": ["'Please enter a valid email' label visible"],
          "exit": "edit Email field (returns to base state automatically)"
        },
        {
          "name": "bad_credentials",
          "trigger": "enter valid email + wrong password, tap 'Sign In'",
          "indicators": ["'Invalid credentials' label visible", "'Sign In' button re-enabled"],
          "exit": "edit any field (returns to base state automatically)"
        },
        {
          "name": "network_error",
          "trigger": "tap 'Sign In' with no connectivity",
          "indicators": ["'Unable to connect' alert displayed", "'OK' button in alert"],
          "exit": "tap 'OK' (dismisses alert, returns to base state)"
        }
      ],
      "data_displays": [],
      "notes": "Initial screen on fresh launch. Also reached after logout."
    }
  },
  "edges": [
    {
      "from": "login",
      "to": "home",
      "action": "tap 'Sign In'",
      "preconditions": ["valid credentials entered"],
      "data_flow": "auth token stored in keychain"
    },
    {
      "from": "login",
      "to": "forgot_password",
      "action": "tap 'Forgot Password?'",
      "preconditions": []
    }
  ]
}
```

Note how the error states that were previously modeled as separate nodes (`login_error`) are now `transient_states` within the Login node. The edges only represent *navigation* — moving from one screen to another. Transient states are behavioral properties of a single screen. This keeps the graph compact and navigational while still capturing the full range of screen behaviors for testing.

### 2.3 Screen Identity

A key design challenge: how does the system know it's on the Login screen vs. the Registration screen, especially when UI changes between releases?

The identity model uses a **marker-based** approach rather than a single brittle identifier. A screen is recognized by the *combination* of elements present — "has an Email field, a Password field, and a Sign In button" identifies Login even if the layout changes, a subtitle is added, or the background color shifts. Accessibility identifiers provide a stronger signal when available, but the marker set provides resilience when they're missing or renamed.

This is also how the graph detects drift — if the agent visits a screen and the markers don't match any known node, it's either a new screen or an existing screen that changed enough to warrant review.

### 2.4 Graph Construction & Maintenance

**Initial construction:** The AI agent explores the app systematically — launching it, visiting every reachable screen, recording the controls and transitions it discovers. This is the expensive operation (many taps, screenshots, UI tree reads). The output is the initial `app.graph.json`.

**Ongoing maintenance (pre-commit hook):**
- Hook examines which source files changed in the commit (view controllers, storyboards, SwiftUI views, navigation code).
- Cross-references changed files against graph nodes (nodes can carry a `source_files` metadata field linking them to their implementation).
- Flags potentially affected nodes/edges and marks them for re-validation.
- Lightweight and fast — no device needed, just static analysis of the diff.

**Periodic re-exploration (on-demand or scheduled):**
- Agent re-explores areas of the graph flagged by the pre-commit hook, or the entire app on a cadence.
- Diffs against existing graph: new nodes, removed nodes, changed controls, broken edges.
- Proposes updates as a PR — human reviews the graph diff just like a code diff.

**Branch association:** Graph changes live in the feature branch that causes them. A feature that adds a new Settings sub-screen creates a branch where the graph has a new node and edges. The PR shows the graph diff alongside the code diff. Reviewers can see "this feature adds two screens and modifies navigation from Settings."

---

## 3. The Test DSL

### 3.1 Design Principles

- **Readable by humans.** A non-engineer should be able to read a test and understand what it's checking.
- **Writable by AI agents.** Minimal syntax, no boilerplate, deterministic structure.
- **Token-efficient.** An agent should be able to hold dozens of tests in context simultaneously.
- **Two levels of abstraction.** Granular steps when precision matters; named flows when steps are just setup.
- **Diffable and reviewable.** One-line changes for one-concept changes.
- **Parameterizable.** Same flow, different data, without duplicating the definition.

### 3.2 Syntax (Sketch)

#### Granular Steps

```quern
test: login shows error for bad credentials
  screen: Login
  enter "Email" "wrong@example.com"
  enter "Password" "badpassword"
  tap "Sign In"
  expect state: bad_credentials
  expect element: "Invalid credentials" visible
  expect element: "Sign In" enabled
  # Still on Login screen — bad_credentials is a transient state, not a navigation
```

#### Reusable Flows (Presets / Verbs)

```quern
flow: logged_in_as(user)
  navigate_to: Login
  enter "Email" {{user.email}}
  enter "Password" {{user.password}}
  tap "Sign In"
  expect screen: Home

flow: on_map_screen(user)
  do: logged_in_as(user)
  tap "Map"
  expect screen: Map
```

#### Using Flows in Tests

```quern
test: map loads user location
  do: on_map_screen(premium_user)
  expect element: "Current Location" visible within 5s

test: free user sees upgrade prompt on map
  do: on_map_screen(free_user)
  expect element: "Upgrade to Premium" visible
```

#### Parameterized Data

```quern
# users.data.quern
data: premium_user
  email: "premium@test.com"
  password: "testpass123"
  tier: "premium"

data: free_user
  email: "free@test.com"
  password: "testpass456"
  tier: "free"
```

#### Conditional Steps

```quern
test: handle optional permission dialog
  do: logged_in_as(premium_user)
  tap "Map"
  if element: "Allow Location Access" visible
    tap "Allow"
  expect screen: Map
```

#### Graph-Aware Navigation

```quern
# navigate_to uses the graph to find the shortest path
# from the current screen to the target screen.
# It emits the necessary steps automatically.

test: change notification settings
  do: logged_in_as(premium_user)
  navigate_to: Notification Settings
  # The graph knows: Home → Settings → Notification Settings
  # The runtime taps through that path automatically
  toggle "Push Notifications" off
  expect element: "Push Notifications" value "off"
```

The `navigate_to` keyword is where the graph and DSL intersect. The test doesn't need to specify *how* to get to Notification Settings — the graph knows the path. If the navigation changes (Settings gets reorganized), the graph updates and `navigate_to` adapts. Tests that use `navigate_to` are resilient to structural changes; tests that use explicit steps are precise but brittle by design (because the steps *are* the test).

### 3.3 Compilation & Execution

The DSL compiles down to a sequence of Quern MCP tool calls:

```
enter "Email" "test@example.com"
  → tap(accessibility_id: "email_input") + type_text("test@example.com")

expect element: "Sign In" enabled
  → describe_ui() + assert(element matching "Sign In" has enabled=true)

navigate_to: Settings
  → graph.shortest_path(current_screen, "Settings") → [tap "Menu", tap "Settings"]
```

This compilation step is where the graph informs execution. `navigate_to` is graph-resolved at compile/runtime. Explicit steps compile directly to tool calls.

### 3.4 File Organization

```
quern-tests/                        # lives in the project repo
  graph/
    app.graph.json                  # the navigation graph
  flows/
    auth.flows.quern                # reusable flow definitions
    navigation.flows.quern
    onboarding.flows.quern
  tests/
    login.test.quern                # test definitions grouped by feature
    map.test.quern
    settings.test.quern
    nfc.test.quern
  data/
    users.data.quern                # parameterized test data
    environments.data.quern
    tags.data.quern                 # NFC tag test fixtures
```

Everything version-controlled. Everything diffable. Everything reviewable in a PR. The `.quern` extension keeps it distinctive and greppable.

---

## 4. Agent Reasoning with the Graph

### 4.1 The Graph as a Cognitive Resource

The app graph isn't just for test execution — it's a **map of the application** that AI agents can consult when performing *any* task, not just testing. This is potentially the highest-leverage capability in the entire system.

### 4.2 Use Cases

**During coding tasks:**
- Agent is asked to modify the checkout flow. It consults the graph to understand what screens are involved, what controls exist on each, and what other screens link to/from checkout. It has structural context *before reading a single line of code.*
- Agent is adding a new screen. It can propose where the screen fits in the graph, what edges should lead to it, and which existing tests will need updating.
- Agent is fixing a bug. The graph tells it which screens are reachable from the buggy state, helping it reason about reproduction steps and blast radius.

**During debugging:**
- Agent sees a crash log referencing `SettingsViewController`. The graph tells it: Settings is reachable from Home via the menu, it has these controls, and these edges lead out of it. The agent can navigate there to reproduce the issue without trial-and-error.
- Agent sees a failed network request to `/api/v1/nfc/scan`. The graph knows which screen triggers NFC scans and what the expected flow is, giving the agent a theory about what went wrong before it even looks at code.

**During test maintenance:**
- Agent is asked "what's our test coverage for the Settings area?" It can traverse the graph, find all Settings-related nodes, and cross-reference against existing `.test.quern` files to identify untested screens or edges.
- A PR changes the navigation structure. The agent diffs the old and new graph, identifies affected tests, and proposes updates — all before a human asks.

**During code review:**
- Agent can annotate a PR with graph context: "This change modifies the Login screen. 4 test flows depend on this screen. The `Sign In` button accessibility ID changed from `sign_in_button` to `login_button` — 2 flows reference the old ID."

### 4.3 MCP Integration

The graph should be available as an MCP resource that agents can query:

```
# Possible MCP tools for graph reasoning

get_screen(screen_name)           → full node with controls, edges, metadata
find_path(from, to)               → shortest path with steps
screens_affected_by(file_path)    → which screens are implemented in this file
tests_covering(screen_name)       → which .test.quern files exercise this screen
unreachable_screens()             → screens with no inbound edges (dead ends)
untested_edges()                  → transitions with no test coverage
graph_diff(branch_a, branch_b)    → structural changes between versions
```

### 4.5 Proxy-Driven Fault Injection

Because Quern controls the network proxy, the agent doesn't have to wait for error conditions to occur naturally — it can *induce* them. This closes the loop between static code analysis, the app graph, and runtime verification:

1. Agent reads source code — sees `AuthService` handles `NetworkTimeoutError`.
2. Agent checks the graph — the Login node has a `network_error` transient state.
3. Agent uses the proxy to inject a timeout on `POST /api/v1/login` (mock/intercept tools already exist).
4. Agent taps "Sign In" and verifies the `network_error` state appears with the expected indicators.
5. Agent checks logs to confirm the error was handled gracefully (no crash, proper logging).

This is **automated fault injection driven by code analysis, validated against the graph, using existing infrastructure.** The proxy, UI automation, log capture, and graph all compose into a capability none of them could provide alone.

The same pattern applies to any network-dependent behavior: server errors (inject 500s), slow responses (inject latency), malformed responses (inject garbage JSON), auth expiration (inject 401s), and offline states (block all traffic). Every error-handling code path becomes testable on demand, not just when conditions happen to align.

This also feeds back into the code path reconciliation from section 4.4: if the agent finds error-handling code with no corresponding transient state in the graph, it can *attempt* to trigger that state via fault injection. If the state appears, the graph gets updated. If it doesn't, the error handling may be dead code — or there may be a bug preventing the error from surfacing correctly.

### 4.6 Graph Enrichment Over Time

The graph starts as a navigation model but can accumulate richer metadata over time:

- **Performance baselines** — average load time for each screen, captured during test runs.
- **Error hotspots** — screens where crashes or errors are most frequently observed.
- **Change frequency** — which screens change most often in commits (churn analysis).
- **Data flow annotations** — what data enters and leaves each screen (populated from proxy traffic during test runs).
- **Accessibility audit results** — which screens pass/fail accessibility checks.

Each enrichment layer makes the graph more valuable as a reasoning resource, without changing the core structure.

---

## 5. Design Tensions & Open Questions

1. **Graph granularity (RESOLVED):** Dead-end states (errors, validation messages, loading spinners, permission dialogs) are properties of the parent node, not separate nodes. The graph stays navigational — nodes represent screens you can *go to*, not every possible visual state. Each node carries a set of **transient states** with entry conditions, visible indicators, and exit actions. This keeps the graph clean for pathfinding while preserving behavioral detail for testing. See updated schema in section 2.2.

2. **DSL type system:** Should the DSL understand types? E.g., `enter "Email" {{user.email}}` — should it know that `email` is an email address and validate format? Probably not initially, but worth thinking about for future error messages.

3. **Flow composition depth:** Can flows call flows that call flows? Probably yes, but need a depth limit or cycle detection to avoid infinite recursion.

4. **Graph source-file linkage (RESOLVED — automatic):** Linkage is built automatically during agent exploration, not maintained manually. The accessibility tree typically reveals the view controller class name, which maps to a source file. Over time, runtime observation enriches the linkage beyond "this file implements this screen" to "these functions execute when this screen loads." Crucially, discrepancies between code paths and observed behavior are high-value signals: error-handling code that never executes at runtime could be dead code *or* could indicate a missing edge in the graph (a state that should be reachable but isn't due to an upstream bug). The agent should flag these discrepancies rather than ignoring them.

5. **Handling dynamic content:** Some screens look different depending on server state (e.g., a feed with different content each time). The graph needs to distinguish between structural elements (navigation bar, tab bar, pull-to-refresh) and dynamic content (feed items, search results). Markers should reference structural elements only.

6. **Offline graph reasoning:** Can the agent reason about the graph without a running device? Yes — the graph is a JSON file. But validating that the graph matches reality requires device access. This is an important distinction: the graph is always available for reasoning, but its accuracy degrades over time without re-validation.

---

## 6. Evolution Path

**Phase 1: Manual graph + granular DSL**
- Agent explores app, produces `app.graph.json`.
- Tests written in DSL with explicit steps only. No `navigate_to`, no flows yet.
- Graph is a reference document, not yet wired into execution.

**Phase 2: Flows + navigate_to**
- Extract common step sequences into reusable flows.
- Implement `navigate_to` using graph pathfinding.
- DSL compiler produces MCP tool call sequences.

**Phase 3: Pre-commit validation**
- Source-file-to-screen linkage in graph metadata.
- Pre-commit hook flags affected nodes on code changes.
- Agent proposes graph updates on flagged changes.

**Phase 4: Agent reasoning MCP tools**
- Graph query tools available to agents during any task.
- Coverage analysis, impact analysis, path finding.
- Graph becomes the agent's mental model of the app.

**Phase 5: Graph enrichment**
- Performance baselines, error hotspots, data flow annotations.
- Graph becomes a living, multi-dimensional model of the application.
