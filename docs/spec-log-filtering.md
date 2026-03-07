# Spec: Log Filtering & Retention

**Status:** Draft
**Origin:** [Lodestone team feature request](/.claude/projects/-Volumes-sourcecode-lodestone/memory/quern_feature_request_log_filtering.md)

---

## Problem

Physical device logs are unusable without targeted `query_logs` searches. The ring buffer fills with system noise at ~100+ entries/second, evicting app logs within seconds. `tail_logs(count=50)` returns 50 entries of CoreBrightness lux readings; zero app entries. The `set_log_filter` MCP tool and `/api/v1/logs/filter` endpoint exist but return "not yet implemented."

Simulator logs have the same problem at lower intensity — framework noise (HangTracer, CoreFoundation accessibility lookups) dilutes the signal even when process-filtered.

---

## Current Architecture

```
Subprocess (pymobiledevice3 / simctl / log / idevicesyslog)
    | (tool-level filters: -pn, --predicate, -p)
    v
Adapter (parses lines -> LogEntry)
    |
    v
Deduplicator (suppresses repeated messages, no filtering)
    |
    v
RingBuffer (stores all entries, 10k default)
    |
    v
Query-time filtering (process, level, source, search, device_id)
```

**Where filtering exists today:**

| Layer | Filters | Reconfigurable? |
|-------|---------|-----------------|
| Subprocess args | process, subsystem, match, level | No — baked into spawn command |
| Adapter parsing | None | N/A |
| Deduplicator | None (dedup only) | N/A |
| RingBuffer ingestion | None — accepts everything | N/A |
| Query-time (`_filter`) | process, level, source, search, device_id, time range | Yes — every query |

**Key constraint:** All ingestion-level filters are passed at adapter construction and encoded into the subprocess command line. Changing them requires stopping the subprocess, destroying the adapter, creating a new one, and restarting.

---

## Design

### Principle: Filter at ingestion, not just query time

The core problem is that unfiltered noise enters the ring buffer and evicts app logs before anyone queries them. Query-time filtering can't help if the entries are already gone. The fix must happen before entries reach the buffer.

### Layer 1: Ingestion filter (in the adapter pipeline)

Add a configurable filter stage between the deduplicator and the ring buffer. This is a lightweight predicate check that runs on every entry before it's stored:

```
Adapter -> Deduplicator -> IngestionFilter -> RingBuffer
```

The ingestion filter is a set of rules applied in Python, independent of the subprocess. This means:

- **Reconfigurable at runtime** without restarting subprocesses
- **Applies to all sources** uniformly (device, simulator, syslog, oslog)
- **Complements tool-level filters** rather than replacing them

Filter rules:

| Rule | Type | Effect |
|------|------|--------|
| `process` | include | Only admit entries from this process (exact match) |
| `processes` | include | Only admit entries from these processes (list) |
| `subsystems` | include | Only admit entries from these subsystems |
| `exclude_processes` | exclude | Drop entries from these processes |
| `exclude_subsystems` | exclude | Drop entries from these subsystems |
| `exclude_messages` | exclude | Drop entries whose message contains any of these substrings |
| `min_level` | threshold | Drop entries below this level |

Include rules are AND'd (entry must match all specified includes). Exclude rules are OR'd (entry is dropped if it matches any exclude).

When no include rules are set, all entries pass (only excludes apply). This preserves current behavior by default.

### Layer 2: Subprocess-level filter passthrough

When `set_log_filter` sets a `process` include filter, also restart the underlying subprocess with the corresponding tool-level filter (`-pn`, `--predicate`, `-p`). This reduces the volume of data the adapter has to parse in the first place.

This is an optimization, not a requirement. The ingestion filter handles correctness; the subprocess filter reduces CPU/IO.

### Layer 3: Default noise excludes

Ship a built-in set of exclude patterns for known iOS noise sources that agents can opt into:

```python
DEVICE_NOISE_DEFAULTS = {
    "exclude_subsystems": [
        "CoreBrightness",
        "ColourSensorFilterPlugin",
    ],
    "exclude_processes": [
        "remotepairingdeviced",
    ],
}
```

These are **not applied automatically** — agents opt in via `set_log_filter(preset="device-quiet")` or by setting excludes explicitly. We don't want to silently hide logs that might matter in edge cases.

---

## API Changes

### `POST /api/v1/logs/filter` (implement existing stub)

**Request body:**

```json
{
  "source": "device",
  "device_id": "48CF8DD9-...",
  "process": "MyApp",
  "processes": ["MyApp", "MyAppExtension"],
  "subsystems": ["com.example.myapp"],
  "exclude_processes": ["remotepairingdeviced"],
  "exclude_subsystems": ["CoreBrightness", "ColourSensorFilterPlugin"],
  "exclude_messages": ["HangTracer"],
  "min_level": "notice",
  "preset": "device-quiet"
}
```

All fields optional. `source` + `device_id` scope which adapter(s) the filter applies to. If `source` is omitted, the filter applies globally (all sources). If `device_id` is omitted, applies to all devices for that source type.

`preset` loads a named set of defaults, then any explicit fields override them.

**Response:**

```json
{
  "status": "applied",
  "filter": { ... },
  "adapter_restarted": true
}
```

`adapter_restarted` indicates whether the subprocess was restarted to apply tool-level filters (optimization).

### `GET /api/v1/logs/filter`

Returns the current active filter configuration. Useful for agents to check what's active before modifying.

### `start_device_logging` / `start_simulator_logging` — add filter params

Add optional filter parameters to the start endpoints so agents can set up filtering at session start:

```json
{
  "udid": "...",
  "process": "MyApp",
  "exclude_subsystems": ["CoreBrightness"],
  "min_level": "notice",
  "preset": "device-quiet"
}
```

These are syntactic sugar — equivalent to calling `start_device_logging` then `set_log_filter`, but saves a round trip and ensures the filter is active before the first log entry arrives.

### MCP tool changes

**`set_log_filter`** — already registered, just needs the backend. Add `preset`, `min_level`, `processes`, `subsystems`, `exclude_processes`, `exclude_subsystems`, `exclude_messages`, and `device_id` params.

**`start_device_logging`** — add `exclude_subsystems`, `exclude_messages`, `min_level`, `preset` params.

**`start_simulator_logging`** — add `exclude_subsystems`, `exclude_messages`, `min_level`, `preset` params.

**`tail_logs`** / **`query_logs`** — no changes needed. They already filter at query time. The ingestion filter just ensures the entries they need are actually in the buffer.

---

## Implementation Plan

### Phase 1: Ingestion filter (P0)

1. **Create `IngestionFilter` class** (`server/processing/ingestion_filter.py`)
   - Holds the current filter config (dataclass)
   - `should_admit(entry: LogEntry) -> bool` method
   - Thread-safe config swap via `update_filter(new_config)`
   - Scoped filters: global + per-source + per-device-id

2. **Wire into pipeline** (`server/main.py`)
   - Create `IngestionFilter` at startup
   - Insert between deduplicator and ring buffer:
     ```python
     def filtered_append(entry):
         if ingestion_filter.should_admit(entry):
             buffer.append(entry)
     dedup = Deduplicator(on_entry=filtered_append)
     ```
   - Store on `app.state.ingestion_filter`

3. **Implement `POST /api/v1/logs/filter`** (`server/api/logs.py`)
   - Parse request body into filter config
   - Call `ingestion_filter.update_filter(config)`
   - Return applied config

4. **Implement `GET /api/v1/logs/filter`**
   - Return current filter config

5. **Update MCP `set_log_filter` tool**
   - Add new params, pass through to API

### Phase 2: Subprocess restart optimization (P0)

6. **Add `reconfigure()` to on-demand adapters**
   - `PhysicalDeviceLogAdapter.reconfigure(process_filter, match_filter)`
   - `SimulatorLogAdapter.reconfigure(process_filter, subsystem_filter, level)`
   - Implementation: stop subprocess, update args, start subprocess
   - Preserve `on_entry` callback, reset `entries_captured`

7. **Wire `set_log_filter` to adapter restart**
   - When `process` include filter changes and a matching adapter is running, call `reconfigure()` to apply at subprocess level too

### Phase 3: Start-time filters & presets (P1)

8. **Add filter params to `start_device_logging` / `start_simulator_logging`**
   - Parse new params from request body
   - Apply to both adapter construction and ingestion filter

9. **Implement presets**
   - `device-quiet`: excludes CoreBrightness, ColourSensorFilterPlugin, remotepairingdeviced
   - `simulator-quiet`: excludes HangTracer, CoreFoundation accessibility noise
   - Stored as constants, loaded when `preset` param is passed

10. **Update MCP tools** with new start params

### Phase 4: Documentation (P1)

11. **Update agent guide** (`docs/agent-guide.md`)
    - Physical device noise warning
    - Recommend `process` filter or `device-quiet` preset for physical devices
    - Best practices for iOS logging (os.Logger, .notice level, .public privacy)

---

## What this does NOT include

- **Per-process ring buffer partitioning** (P1 in feature request) — Deferred. The ingestion filter solves the eviction problem more simply. If agents need both app logs and system context simultaneously, they can use a broader filter with excludes rather than partitioned buffers. Revisit if the ingestion filter proves insufficient.

- **Automatic filter detection** — No attempt to guess the app process name. Agents must specify it. This keeps behavior predictable.

- **Persistent filter config** — Filters reset on server restart. This is intentional — filters are session-scoped, tied to what the agent is currently debugging. Agents set them at the start of each session via `start_device_logging` or `set_log_filter`.

---

## Testing Plan

- Unit tests for `IngestionFilter.should_admit()` with various rule combinations
- Unit tests for preset loading and override behavior
- Integration test: start device logging with process filter, verify only matching entries reach buffer
- Integration test: `set_log_filter` changes filter mid-stream, verify new entries are filtered
- Integration test: adapter `reconfigure()` stops and restarts subprocess correctly
- Test that empty/default filter admits everything (backward compat)
- Test that `GET /api/v1/logs/filter` returns current state accurately
