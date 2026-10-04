# Quern Event Model & Helm Vision

## The core idea

Quern already captures three streams of data: device logs, network traffic, and UI state. Right now these are separate — you query logs in one tool, flows in another, screenshots in a third. The human (or agent) does the correlation mentally.

The event model unifies them into a single timeline where actions, observations, and state changes are linked. Helm visualizes that timeline instead of showing raw streams.

## What we have today

These aren't hypothetical — they're shipping:

- **UI automation** — tap, swipe, type, read accessibility tree
- **Network capture** — full request/response interception, mocking, replay
- **Log pipeline** — structured, filterable, multi-source (syslog, logcat, crash, build, plist)
- **Plist watcher** — real-time state observation with diffs
- **State checkpoints** — save/restore app state for reproducible starting points
- **App knowledge base** — persistent graph of screens, navigation, alerts, quirks
- **`set_location`, `open_url`** — device environment control

The gap: these are independent tools. Nothing ties "agent tapped Login" to "POST /auth fired" to "authToken appeared in plist" to "home screen loaded."

## Phase 1: Unified event stream

**Goal:** Every tool call and every captured signal becomes an event in a shared timeline.

### The event primitive

```python
class Event:
    id: str
    timestamp: datetime
    type: Literal["action", "observation", "state_change", "anomaly"]
    source: Literal["agent", "device", "network", "log", "plist"]
    summary: str              # human-readable one-liner
    data: dict                # type-specific payload
    correlation_id: str | None  # groups related events
    device_id: str | None     # which device
```

That's it. No inference events, no confidence scores, no hypothesis fields. Just facts with an optional correlation ID to group them.

### What becomes an event

| Source | Event type | Example |
|--------|-----------|---------|
| MCP tool call | action | "tap_element label='Login'" |
| Proxy flow | observation | "POST /api/auth → 500" |
| Log entry (error+) | observation | "AuthError: invalid token" |
| Plist change | state_change | "authToken: null → 'abc123'" |
| Crash report | observation | "EXC_BAD_ACCESS in LoginVC" |
| Screenshot | observation | "screenshot captured" |
| Anomaly detector | anomaly | "30 identical requests in 2s" |

### Correlation

When the agent calls a tool, the server assigns a `correlation_id`. Any events that occur within a short time window (1-2s) on the same device get the same ID. This is imperfect but useful — it ties "tap Login" to the network request and state change that follow.

The correlation is best-effort. No causal graphs, no weighted signals. Just temporal proximity on the same device.

### Where this lives

A new event store in the server, similar to the existing ring buffer for logs and FlowStore for proxy. Events are append-only, queryable by time range, device, type, and correlation_id.

### API surface

- `GET /api/v1/events` — query with filters
- `GET /api/v1/events/summary` — cursor-based summary (same pattern as log/flow summaries)
- SSE stream for real-time consumption by Helm

## Phase 2: Event bundles

**Goal:** Group related events into human-readable chunks.

### The bundle

```python
class EventBundle:
    id: str
    title: str               # "Login Attempt #3"
    events: list[str]         # event IDs
    start_time: datetime
    end_time: datetime
    outcome: Literal["success", "failure", "unknown"] | None
```

### How bundles form

Start simple with two strategies:

1. **Correlation-based** — events sharing a `correlation_id` form a bundle automatically
2. **Flow-based** — when executing a knowledge base flow, the entire flow execution is a bundle

Don't try to auto-detect bundle boundaries from raw event streams. That's an ML problem disguised as a heuristic problem. Use the correlation IDs and explicit flow execution as natural boundaries.

### What this enables

A bundle like "Login Attempt #3" contains: the tap action, the network request, the response, the plist change (or lack thereof), and the resulting screen. Click it in Helm and you see the whole story instead of hunting through three separate streams.

## Phase 3: Anomaly detection

**Goal:** Automatically surface things that look wrong.

Start with three simple detectors. No ML, no scoring models. Just rules:

### Request bursts
If the same endpoint is hit N+ times within T seconds, emit an anomaly event.
- Default: 5+ identical requests in 3 seconds
- This catches reactive loops, missing debounce, retry storms

### Repeated errors
If the same error message appears N+ times in T seconds in the log stream, emit an anomaly.
- Default: 3+ identical errors in 10 seconds
- This catches error cascades, crash loops

### State contradictions
If the plist says one thing and the UI says another, flag it.
- Example: `authToken` exists in plist but login screen is visible
- This requires checking plist state against screen identity from the knowledge base
- Only works when the knowledge base is populated — that's fine, it's a progressive enhancement

Each detector runs as a lightweight background task in the server, watching the event stream.

## Phase 4: Helm timeline

**Goal:** Helm's primary view is a timeline, not a device grid.

### Layout

```
┌─────────────────────────────────────────────────────┐
│  [Device selector]              [Time range] [Live] │
├──────────────────────┬──────────────────────────────┤
│                      │                              │
│  Timeline            │  Detail panel                │
│                      │                              │
│  ▶ Login Attempt #3  │  [Screenshot]                │
│    tap Login         │                              │
│    POST /auth → 500  │  Network: POST /auth         │
│    ⚠ AuthError       │  Status: 500                 │
│    UI: error banner  │  Body: {"error": "invalid"}  │
│                      │                              │
│  ⚠ 30 requests/2s   │  Logs:                       │
│                      │  AuthError: invalid token    │
│  ▶ Login Attempt #4  │                              │
│    ...               │  State:                      │
│                      │  authToken: (unchanged)      │
│                      │                              │
├──────────────────────┴──────────────────────────────┤
│  [Live device preview]                    (optional) │
└─────────────────────────────────────────────────────┘
```

- Left panel: timeline of bundles and standalone events, newest at top
- Anomalies float to the top with visual emphasis
- Click a bundle to expand events; click an event to see detail
- Right panel: context for selected event — network detail, logs, screenshot, state diff
- Device grid becomes a selector, not the primary view

### What makes this different from DevTools

DevTools shows you raw streams and you correlate mentally. Helm shows you stories (bundles) with correlated data pre-linked. Click a network request and the related log entries, UI change, and state diff are right there.

## What to defer

These are interesting ideas from the conversation that aren't worth building yet:

- **Inference events / agent reasoning display** — Requires hooking into the agent's thought process, which we don't control. The agent's reasoning lives in Claude's context, not in Quern. Revisit if/when MCP adds a way for tools to receive agent reasoning.
- **Confidence scores on state** — Binary is fine. Either the plist says the user is logged in or it doesn't.
- **Auto-discovery of state channels** (file system scanning, sqlite watching) — The plist watcher works. Generalize when there's a second concrete use case, not before.
- **State recipes as a formal system** — We already have checkpoints. They work. Don't abstract them into a recipe framework until the pattern proves itself.
- **Replay / scrubbing** — Requires recording enough state to recreate the app at any point. That's a massive engineering effort for uncertain value. Capture everything; build replay later if the data proves useful.
- **Editable history** — Cool concept, no clear use case yet.

## Implementation order

1. **Event primitive + store** — Add to the server. Emit events from existing tool calls and capture pipelines. No new capabilities, just unification.
2. **Event query API** — `/api/v1/events` with filtering. Expose via MCP tool.
3. **Correlation** — Assign correlation IDs in tool calls, match nearby events.
4. **Anomaly detectors** — Request burst and repeated error detectors. Run as background tasks.
5. **Bundles** — Group by correlation ID. Add flow-execution bundles.
6. **Helm timeline** — Primary view showing bundles + anomalies. Detail panel with cross-referenced data.

Steps 1-4 are server-side and benefit both CLI agents and Helm. Step 5 is the bridge. Step 6 is Helm-specific.

## The framing

Quern is not a test framework. It's not a debug dashboard. It's an interface layer between AI and mobile apps — execution, observation, and understanding in a closed loop.

Helm is how humans see that loop working.
