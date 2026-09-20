# Quern's own logging: the spec

**Status:** spec, 2026-09-20. Makes #238 followable.

#238 surveyed quern's logging and proposed five "shapes worth considering".
This turns that into decisions someone can implement against, because the
survey left four things undecided and one thing impossible.

Everything measured below was re-measured on `main` at `f3b7414`.

## What #238 got right, and what it left open

The survey holds up. Re-measured:

| claim | #238 | re-measured |
|---|---|---|
| modules with no logger | 28 of 92 | 28 of 92 ✓ |
| `print()` in `server/` | 511 | 511 ✓ |
| distinct logger names | 30 | 30 ✓ |
| `[PERF]` lines | — | 39, across 6 files |

Every number holds. (Counting `__init__.py` too gives 36 of 100, which is the
same tree measured less usefully — package inits have nothing to log.)

What it left undecided: what the levels *mean*, what the category list
actually is, what an action-log entry contains, and which `print()` calls are
in scope. Each is answered below.

What it got wrong: the correlation id, as proposed, cannot be built. See
"Correlation".

## The constraint that reshapes this

**The proxy is a separate process.** `server/sources/proxy.py` spawns
`mitmdump` as a subprocess with the addon as a script. It does not share
memory, a logger, or a `contextvars` context with the server.

So #238's item 3 — "a correlation id per action, attached to the flows" —
cannot be implemented by passing an id into the flow. Nothing in quern
touches the app's outbound requests; the app makes them, and quern observes
them from another process. There is no header to stamp and no context to
inherit.

**But the flows already identify themselves,** which is better than an id we
would have had to invent. `server/proxy/addon.py` monkey-patches
`LiveConnectionHandler` to read `pid` and `process_name` off the connection,
then walks parent PIDs until it finds a `launchd_sim` whose command line
carries a UDID. Every flow can therefore carry:

```python
source_process: str | None   # the app: "Geocaching"
source_pid: int | None
simulator_udid: str | None   # resolved, not guessed
```

All three are already fields on the flow models. This is the join key, and it
is a real one.

## 1. Level policy

Levels are currently assigned by taste. This is the policy; it is the part
that makes a filterable trace possible, because a level that does not mean
anything cannot be filtered on.

| level | means | example |
|---|---|---|
| `DEBUG` | Only useful when reproducing a specific bug. Off by default. | raw simctl argv, per-poll ticks |
| `INFO` | A thing quern did, that the user asked for. One per action. | `tap_element → Map (196.5, 794) 412ms` |
| `WARNING` | Quern did what was asked, and the result is probably not what the caller wanted. | Device Hub suppression advisory |
| `ERROR` | The operation failed. The caller did not get what they asked for. | launch refused, device unreachable |
| `FAULT` | Quern itself is broken or unusable. Not the operation — the server. | ring buffer wedged, port reclaim failed |

Three rules that follow, each from a bug this project has actually shipped:

- **`INFO` is one line per action, not per step.** The current `[PERF] START`
  / `[PERF] SUCCESS` pair means a single tap writes two entries and a trace is
  twice as long as the thing it describes. START lines become `DEBUG`.
- **A successful operation whose result is suspect is `WARNING`, not `INFO`.**
  This is the Device Hub case: a tap that returns `ok` into a device that
  discards it. If the level does not carry that, nothing downstream can.
- **Never `ERROR` for something the caller asked for and got.** A 404 from
  `tap_element` with `scroll_to_find: false` is an answer, not a fault.

### `NOTICE` is unreachable — decide it

`LogLevel.NOTICE` exists in the enum, and `_map_level` in
`server/sources/server_log.py` cannot produce it: Python's `logging` has no
NOTICE level, so the mapping goes DEBUG → INFO → WARNING → ERROR → FAULT and
skips it. Device sources (OSLog) *can* produce NOTICE, so the enum value is
not dead — but no server-side log will ever carry it.

**Decision: leave `NOTICE` for device sources only, and say so in the enum's
docstring.** Adding a Python level 25 to reach it would buy nothing; the
policy above has no gap where NOTICE belongs.

### While here: `_LEVEL_MAP` is dead

`server/sources/server_log.py:18` defines `_LEVEL_MAP`, and nothing reads it —
`_map_level` does the work with comparisons. Delete it. It is a trap: it maps
`logging.CRITICAL` to `FAULT` and reads like the authority while being inert.

## 2. The category vocabulary

`LogEntry.category` exists, is already filterable
(`server/api/logs.py:114`), and the server never populates it — every
server-side entry has `category=""`.

**This is the closed list.** Not illustrative — if a call site does not fit,
the list changes in a PR, rather than a new string being invented.

| category | covers |
|---|---|
| `device.action` | anything that writes to a device: tap, swipe, type, press, launch, install |
| `device.read` | anything that reads: UI tree, screenshot, element state, logs |
| `device.lifecycle` | boot, shutdown, erase, resolve, claim, input repair |
| `proxy` | capture, mocks, intercepts, certificates |
| `build` | xcodebuild, gradle, parsing build output |
| `server.lifecycle` | startup, shutdown, port reclaim, updates, daemonization |
| `knowledge` | landmarks, screen identification, app knowledge base |

Seven. `perf` is deliberately **not** a category — duration is a field on an
action entry, not a class of event. That is the mistake `[PERF]` made.

### Why the logger name cannot be the category

`quern-debug-server.device` is shared by six modules and `.api` by six more.
Renaming 30 loggers to match the seven categories would be a large diff that
breaks anyone filtering on `process`, and it would still conflate
`device.action` with `device.read` — both live in `controller_ui.py`.

So category is set **per call**, not per module.

### How it is set

Standard `logging` `extra=`, read by the existing handler. No new
infrastructure:

```python
# server/logging_ext.py  (new, small)
def action(logger, msg, *args, category, udid=None, duration_ms=None, **kw):
    """Log one completed action. See docs/proposals/logging-spec.md."""
    extra = {"quern_category": category}
    if udid: extra["quern_udid"] = udid
    if duration_ms is not None: extra["quern_duration_ms"] = duration_ms
    logger.info(msg, *args, extra=extra, **kw)
```

and in `_BufferHandler.emit`:

```python
category=getattr(record, "quern_category", ""),
```

`quern_`-prefixed as a convention, not a necessity. Measured: `category`,
`udid`, `action`, `outcome` and `duration_ms` do **not** collide, while
`message`, `module`, `asctime`, `levelname` and — relevantly — `process` do,
raising `KeyError` from `Logger.makeRecord`. The prefix costs nothing, keeps
us clear of that list as it grows, and makes the call sites greppable.

## 3. The action log

One entry per completed action, at `INFO`, carrying the four things a trace
needs. This subsumes the `[PERF] ... SUCCESS` lines.

**Schema.** Four new optional fields on `LogEntry`, all defaulted so nothing
that builds a `LogEntry` today breaks:

```python
action: str = ""             # "tap_element", "launch_app"
udid: str = ""               # the resolved target, not the requested one
duration_ms: int | None = None
outcome: str = ""            # "ok" | "failed" | "not_found" | "ambiguous"
```

`udid` is separate from the existing `device_id` on purpose: `device_id` is
`"server"` for every server-side entry today, and repurposing it would break
the source routing that depends on it.

**Where it is emitted.** In the API handlers in `server/api/`, not the
controller. The handler is the boundary that knows the outcome, already
measures duration for `[PERF]`, and is one layer — the controller methods are
called from each other and would double-count.

**Resolved, not requested.** `udid` must be what `resolve_udid` returned. A
trace keyed on `None` because the caller omitted a udid is not joinable, and
"which device did this actually go to" is a question we have had to answer by
hand more than once.

## 4. Correlation

**What is not achievable:** stamping an id of our own onto proxy flows. The
proxy is a separate process and the requests are the app's.

**What is achievable:** join on what the flow already knows —
`(simulator_udid, source_process, time interval)`.

The action entry contributes the resolved udid and an interval; the flow
contributes its own udid and the originating app; device logs contribute a
process name. That is a three-way join on fields that already exist, and
`source_process` disambiguates two actions running against *different apps*
on one device, which a time window alone could not.

### The limit, stated precisely

`pid` comes from `writer.get_extra_info("pid")`, which is provided by the
**mitmproxy_rs local redirector**. The addon says so itself: the patch is
wrapped in `try/except` with the comment "Not available — non-local mode or
import failure".

So there are two regimes, and the export must not pretend they are one:

| | `source_pid` / `simulator_udid` | join |
|---|---|---|
| **local capture** (`set_local_capture`) | present | udid + process + interval |
| **HTTP proxy** (physical devices, proxied sims) | absent | interval only, per device |

In proxy mode the trace degrades to the time window, with the ambiguity that
implies: two concurrent actions on one device cannot be told apart. Quern
largely serializes per device, so this is rare rather than absent — but the
export should **mark** an interval it cannot attribute cleanly rather than
picking one. Marking ambiguity is the point; inventing a join is how a trace
becomes confidently wrong.

That asymmetry is also an argument for `set_local_capture` being the
recommended mode when anyone wants a trace, which is worth saying in the tool
description rather than leaving to be discovered.

### The in-process id is still worth having

A `contextvars` correlation id ties the nested reads inside one `tap_element`
to that action, for *server* entries, where it does work. Build it in step 2.
Just do not name it in a way that implies it reaches the proxy, because it
cannot.

## 5. The `print()` rule

511 calls. The split is not judgment — it is a location rule:

- **`print()` is correct** where the caller is a terminal: `server/main.py`
  (122), `server/__main__.py` (62), `server/lifecycle/setup.py` (134),
  `updater.py` (53), `menubar.py` (24), `daemon.py` (18). A user running
  `quern setup` wants stdout, not a ring buffer entry. That is **413 of the
  511**, and none of it is in scope.
- **`print()` is a bug** anywhere reachable from a request:
  `server/device/` (81 — `tunneld.py` alone has 79), `server/proxy/` (7),
  `server/api/` (2), `server/sources/` (1). In daemon mode these land in the
  log file with no level, no timestamp and no category, and `query_logs`
  cannot see them at all.

So the real job is **~91 calls**, not 511, and `device/tunneld.py` is most of
it. That is a morning, not a project — which is the main thing #238's headline
number obscured.

A lint check enforcing exactly this is in scope for step 3, and is the only
thing that will stop the count drifting back.

The one legitimate exception, already in the tree: `_task_done` in
`server/sources/server_log.py` prints to stderr deliberately, because logging
from the log handler recurses. Keep it, comment it.

## 6. Order, with acceptance criteria

Each step is independently useful and independently mergeable.

**Step 1 — category plumbing.** Add `quern_category` handling to
`_BufferHandler.emit`, add `server/logging_ext.py`, delete `_LEVEL_MAP`, fix
the `NOTICE` docstring. Convert one subsystem — `device.lifecycle`, because
`sim_input.py` is small and already well-logged.
*Done when:* `GET /api/v1/logs/query?source=server&category=device.lifecycle`
returns the boot and input-repair entries and nothing else. A test asserts a
non-empty category on an entry that went through `logging`.

**Step 2 — the action log.** Add the four `LogEntry` fields; emit from the
`device/ui` handlers; convert the 39 `[PERF]` lines (START → `DEBUG`,
SUCCESS → the action entry).
*Done when:* one `tap_element` produces exactly one `INFO` entry carrying
action, resolved udid, outcome and duration, and a test fails if the udid is
the requested one rather than the resolved one.

**Step 3 — close the `print` gap.** Convert server-path prints; add the lint
check.
*Done when:* the check fails on a `print()` added to `server/device/`, and
passes for `server/cli/`.

**Step 4 — the trace export.** Join action entries with flows and device logs
on `(udid, interval)`, marking overlaps.
*Done when:* a scripted API test yields one timeline, and an export with two
overlapping actions on one device marks them rather than guessing.

Steps 1–3 are worth doing regardless of whether step 4 ever happens.

## What this does not propose

Renaming the 30 loggers. Category is per call, which makes the logger name a
cosmetic issue rather than a blocking one — and a rename would break
`process`-based filtering for anyone using it today.
