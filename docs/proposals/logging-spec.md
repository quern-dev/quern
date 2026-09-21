# Quern's own logging: the spec

**Status:** spec, 2026-09-20. Makes #238 followable.

#238 surveyed quern's logging and proposed five "shapes worth considering".
This turns that into decisions someone can implement against, because the
survey left four things undecided and one thing impossible.

Everything measured below was re-measured on `main` at `f3b7414`.

## What #238 got right, and what it left open

The survey holds up. Re-measured:

| claim | #238 | re-measured, before step 1 |
|---|---|---|
| modules with no logger | 28 of 92 | 28 of 92 ✓ |
| `print()` in `server/` | 511 | 511 by `grep`, **500 real** |
| — of those on a request path | "others are in server paths" | **0** |
| distinct logger names | 30 | 30 ✓ |
| `[PERF]` lines | — | 39, across 6 files |

The counts held; one *characterisation* did not. #238's claim that some of the
prints "are in server paths", with four modules named, is wrong in all four
cases — see section 5. `grep` counting `fingerprint(` as `print(` is how that
happened, and it is worth noting as a method: every number in this document
that could be checked with `ast` now was. Two have since changed by design: logger names are now one
per module via `__name__`, and the "no logger" count is no longer treated as
a defect at all — see below. (Counting `__init__.py` too gives 36 of 100, which is the
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
would have had to invent. Depending on how the device reaches the proxy, a
flow already carries `source_pid`, `source_process`, `simulator_udid` or
`client_ip` — all existing fields on the flow models. Section 4 works through
what each regime gives us, including physical devices.

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

### While here: `_LEVEL_MAP` was dead — removed

`server/sources/server_log.py` defined `_LEVEL_MAP` and nothing read it;
`_map_level` does the work with comparisons. It was a trap: it mapped
`logging.CRITICAL` to `FAULT` and read like the authority while being inert.
Deleted in step 1.

## 2. The category vocabulary

`LogEntry.category` exists and the server never populates it — every
server-side entry has `category=""`.

An earlier draft said it was "already filterable". Half true, and the wrong
half: the predicate existed only on the SSE `/stream` endpoint. `/logs/query`
— which is what the `query_logs` tool calls — had **no `category` parameter
at all**, so FastAPI dropped the query string and returned the whole buffer.
Every category matched everything, which is worse than no filter because it
looks like it worked. Found by running it, not by reading it; fixed as part
of step 1.

**This is the closed list.** Not illustrative — if a call site does not fit,
the list changes in a PR, rather than a new string being invented.

It was validated by classifying all 99 MCP tools against it, which is what
turned a seven-item guess into this nine-item list. A first draft left **25 of
99 tools with no category at all** — a quarter of the surface.

| category | covers | tools |
|---|---|---|
| `device.action` | any write to a device or an app on it | 24 |
| `device.read` | any read of device or app state | 12 |
| `device.lifecycle` | boot, shutdown, erase, resolve, claim, driver/WDA, input repair | 10 |
| `proxy` | proxy control, certificates, bypass, intercepts, mocks, flow queries | 18 |
| `logs` | log-stream control, filters, queries, crashes, plist watch | 19 |
| `media` | screenshot timeline, live preview, and the timestamped video later | 6 |
| `build` | build orchestration and output parsing | 3 |
| `knowledge` | landmarks, screen identification, app knowledge base | 6 |
| `server.lifecycle` | startup, shutdown, port reclaim, updates, daemonization | 1 |

`perf` is deliberately **not** a category — duration is a field on an action
entry, not a class of event. That is the mistake `[PERF]` made.

### What the classification exercise found

**`logs` and `media` were missing entirely.** 25 tools — every
`start_*_logging` / `stop_*_logging`, the oslog and syslog streams, log
filters, crash reads, the five plist-watch tools, the screenshot timeline and
the live preview — had nowhere to go. They are not `device.read`: they
control the *observation plumbing* rather than reading device state, and
starting a log stream is not the same kind of event as fetching a UI tree.

That the trace machinery itself had no category, in a spec whose purpose is a
combined trace, is the strongest argument for classifying against a real
inventory rather than reasoning from memory.

Plist watch belongs in `logs` because it genuinely is one: `LogSource`
already has a `PLIST_WATCHER` member and its events land in the same ring
buffer.

**`device.action` had to be broadened.** As first written it covered "tap,
swipe, type, press, launch, install" — leaving the five device-settings tools
(`set_locale`, `set_location`, `set_font_scale`, `set_display_density`,
`set_hardware_keyboard`) and the six app-data ones (`save_app_state`,
`set_app_plist_value`, …) ambiguous. Both are writes to the device, so the
definition is now "any write", not "input". Splitting settings and app data
into their own categories was considered and rejected: it would trade a real
distinction for two categories nobody would remember the boundary of.

**`server.lifecycle` has one MCP tool and still earns its place**, because
most of what it covers is not tool-driven at all — startup, port reclaim,
daemonization and update checks are the things you most want to filter to
when the server itself is misbehaving.

### The collision worth knowing about

`LogSource` already exists, with `BUILD`, `PROXY`, `CRASH`, `PLIST_WATCHER`
and `SERVER` among its members. So `source=proxy` and `category=proxy` are
both valid filters and mean **different things**: the first is "entries the
mitmdump adapter produced", the second is "quern's own logging about proxy
control". Same for `build`.

That is genuinely confusing, and the resolution is to be explicit rather than
to rename: **`category` describes what quern was doing; `source` describes
who produced the entry.** For server-side entries `source` is always
`server`, so within the subject of this spec the two never compete. Say this
in the query API's docs, because someone will otherwise filter `source=proxy`
expecting quern's proxy actions and get captured HTTP flows.

### Logger names are `__name__`, and that is a different axis

An earlier draft declined to rename the 30 loggers on the grounds that it
would break anyone filtering on `process`. That reasoning was wrong and is
withdrawn: logger names are not exposed anywhere user-facing, nothing
configures levels by prefix, and the `quern-debug-server` strings in `mcp/`
are the *binary* name, not loggers. There was no one to break.

The conclusion stands for a better reason. Renaming loggers **to match the
categories** would be a mistake, because the two answer different questions:

- the **logger name** says *where in the code* a line came from
- the **category** says *what quern was doing*

`controller_ui.py` legitimately emits both `device.action` and `device.read`,
so no single logger name can carry its category. Making them match collapses
two useful axes into one and leaves two taxonomies to drift apart.

So: **every logger is `logging.getLogger(__name__)`.** It is the Python
convention, it costs nothing to maintain, new modules get it free, and it
makes `process` discriminating — `server.device.sim_input` rather than
`quern-debug-server.device` shared across six modules, which could not
distinguish anything. Category stays orthogonal, and the trace gets two real
axes instead of one.

Done: 40 modules renamed, and the ten `caplog.at_level(logger=...)` call sites
in the suite now use the `server.device` parent. Verified by mutation that
capture still works, since a rename that quietly broke `caplog` would leave
the two absence-asserting tests passing for the wrong reason.

### What "28 modules have no logger" is not

#238 counts modules with no logger as a defect. Adding `logger =
logging.getLogger(__name__)` to a module with nothing to say is an unused
variable pretending to be coverage, and `models.py` does not need one.

The rule is **"a module that logs uses `__name__`"**, which is lint-checkable
and true today. The useful part of that count is the modules that *should* be
logging and are silent -- `auth.py`, `proxy/flow_store.py`,
`processing/classifier.py` -- and those are found by asking what is
undiagnosable, not by counting.

### How it is set

Standard `logging` `extra=`, read by the existing handler. No new
infrastructure, no parallel logger. As shipped in `server/logging_ext.py`:

```python
def log(logger, level, msg, *args, category, udid=None, **kwargs):
    if category not in CATEGORIES:
        logger.warning("Unknown log category %r ...", category)
    extra = {"quern_category": category}
    if udid:
        extra["quern_udid"] = udid
    logger.log(level, msg, *args, extra=extra, **kwargs)
```

with `info`/`warning`/`error`/`debug` wrappers named for the level policy, so
the call site reads as the policy does. `category` is keyword-only and
**required**: a default would make the uncategorised call the convenient one,
which is how the field came to be empty everywhere in the first place.

An unknown category **warns and still logs** rather than raising. These calls
sit on paths that are frequently already reporting a failure, and turning a
typo into an outage would be a worse bug than the one it guards against.

The handler reads it back with a helper rather than a bare `getattr`, so the
attribute name lives in one place:

```python
category=category_of(record),
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
outcome: str = ""            # see OUTCOMES in server/logging_ext
```

`outcome` is a closed vocabulary, and two of its values carry judgements
worth stating:

| outcome | level | means |
|---|---|---|
| `ok` | INFO | it worked |
| `failed` | ERROR | the caller did not get what they asked for |
| `suspect` | **WARNING** | quern did it and the result is not to be trusted |
| `not_found` | INFO | an answer, not a fault — the element was not there |
| `ambiguous` | INFO | several matches; also an answer |
| `started` | DEBUG | a begin entry, carrying no duration |

`suspect` is the level policy's WARNING row made queryable. Typing that
reports success into a field that is still empty is the case it exists for; a
tap into a device whose input services Device Hub has taken is the same shape.
Reporting either as `failed` overstates — the request did happen — and logging
it at ERROR trains the reader to ignore errors.

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

Reviewing #250 turned that from a logging preference into a correctness rule.
`tap_element` resolved the udid for its advisory and then passed `body.udid`
onward to the tap and to both screenshot helpers, each of which resolves the
active device again. A concurrent request that changes the active device
between them lets the tap, the screenshots and the advisory describe three
different devices. **Resolve once per request and thread it through** — the
action entry is then a record of what happened rather than a fourth
independent guess at it.

### What counts as an action

Everything quern does that changes device or proxy state, or that a later
question might be asked about — not just the UI verbs.

A first pass covered `server/api/device_ui.py` only, which left the `proxy`
category with eighteen tools and no entries at all. Installing a CA
certificate through `install_proxy_cert` changes the device and takes seconds,
and produced nothing in the trace; so did booting a simulator, starting the
proxy, and building an app.

The test is **who invoked it**, not which module it lives in. A thing quern
did on a caller's behalf is an action. The same function reached from a
terminal by a person running `quern setup` is not — they are watching it
happen.

And the second line, once you are inside the API: **what the route touches**,
not whether it reads. A read *of the device* is an action — `get_ui_tree`
talks to the device and takes time, which is exactly why `device.read` exists
as its own category, so reads can be dropped from a trace in one clause. A
read of quern's own state is not: querying the trace is not part of the
trace, and a liveness probe is not a thing that happened to anyone.

### Not middleware, and why

Wrapping every request uniformly is the obvious way to get coverage without
touching 122 handlers. It was tried and rejected, for reasons worth recording
so it is not re-proposed:

- **It guesses the outcome.** All a middleware has is the status code, which
  cannot express `suspect`, `ambiguous` or `not_found` — the three outcomes
  that exist precisely because they are not failures. Flattening them to
  200/404 discards the judgement the vocabulary was built to carry.
- **It cannot know the resolved udid**, which is the join key. That has to
  come from the handler either way.
- **Streaming breaks it.** Three endpoints stream — video, logs SSE, proxy
  SSE. A middleware must wrap `send` to see them finish, which is where
  hand-rolled ASGI goes wrong, and the "duration" it would record is how long
  somebody watched a stream.
- **The existing pure-ASGI middlewares earn it.** `APIKeyMiddleware` must run
  before everything and must not break `is_disconnected()`; that is a real
  requirement. Action logging has no such need, and adding an ASGI layer
  without one is cost for nothing.

Deciding which routes are actions is the work, not an obstacle to it.

### Keeping coverage from rotting

`tests/test_action_coverage.py` walks every route with `ast` and requires each
to be either wrapped in `action(...)` or named in an explicit non-action list.
A route in neither fails.

It landed with **84 of 122 routes unclassified**, recorded as a backlog rather
than as 84 failing tests, so the guard is live immediately: a route added
tomorrow is in neither list and fails. Two further tests keep both lists
honest — neither may name a route that no longer exists, nor one that has
since been wrapped, because a stale entry exempts nothing and hides the next
route to take that name.

| router | category | emits today |
|---|---|---|
| `device_ui.py` | `device.action` / `device.read` | yes |
| `device.py` | `device.lifecycle` | boot repair only |
| `proxy.py`, `proxy_certs.py` | `proxy` | **no** |
| `logs.py` | `logs` | **no** |
| `landmarks.py` | `knowledge` | **no** |
| build endpoints | `build` | **no** |

Reads are worth including but are the easy thing to over-log: a sweep issues
many, and one entry per `get_ui_tree` inside a scroll would bury the action
that asked for it. They stay `device.read`, which is exactly so they can be
filtered out of a trace in one clause.

## 4. Correlation

**What is not achievable:** stamping an id of our own onto proxy flows. The
proxy is a separate `mitmdump` process and the requests are the app's. There
is no header to add and no context to inherit.

**What is achievable:** the flows already identify themselves. Every regime we
care about has a join key that exists today — it just needs using.

### The three mobile regimes

| regime | flow carries | joins to a device by | identifies the app? |
|---|---|---|---|
| **simulator + local capture** | `source_pid`, `source_process`, `simulator_udid` | `simulator_udid`, resolved from the pid | **yes** — `source_process` |
| **physical device + Wi-Fi proxy** | `client_ip` | `client_ip` → udid, from `wifi_proxy_configs` | no |
| **simulator + Wi-Fi proxy** | `client_ip` (loopback) | nothing device-distinguishing | no |

**Simulators under local capture** are the strong case.
`server/proxy/addon.py` patches `LiveConnectionHandler` to read `pid` and
`process_name` off the connection, then walks parent PIDs until it finds a
`launchd_sim` whose command line carries a UDID. That gives device *and* app,
so two concurrent actions against different apps on one simulator are
separable — something a time window could never do.

**Physical devices** join on `client_ip`, and the mapping already exists:
`record_device_proxy_config(udid, ssid, proxy_host, port, client_ip)` writes
`client_ip` per SSID into that device's cert state. A flow's `client_ip`
therefore resolves to a udid without anything new being recorded.

Two cautions, because this mapping is weaker than the simulator one:

- **It is recorded once, at proxy setup, and DHCP can reassign.** A stale
  `client_ip` does not fail — it attributes another device's flows to this
  one, which is worse. The resolver must treat the mapping as a *hint* and
  re-record on each `record_device_proxy_config`; a trace should mark a
  device whose recorded IP was last set long ago rather than asserting it.
- **It identifies the device, not the app.** Everything the device sends
  shares one IP, so `source_process` has no equivalent. For physical devices,
  app attribution has to come from the action log's own `udid` plus the
  interval, not from the flow.

**Simulators without local capture** are the genuinely weak case: traffic
arrives from the host, so `client_ip` is loopback and distinguishes nothing.
Only the interval is left. Given local capture is available for simulators,
the right answer is to recommend it whenever a trace is wanted, in the
`set_local_capture` tool description rather than leaving it to be discovered.

### Interval, and marking what cannot be told apart

In every regime the action entry contributes `(udid, start, duration)`. Where
a flow resolves to a udid, the join is `udid + interval`, narrowed by
`source_process` when present. Where it does not, only the interval remains.

Two concurrent actions on one device with no app distinction produce
overlapping intervals that cannot be separated. Quern largely serializes per
device, so this is rare rather than absent — but the export must **mark** an
interval it cannot attribute cleanly instead of picking one. Marking
ambiguity is the point; inventing a join is how a trace becomes confidently
wrong, which is the recurring failure in this codebase.

### The in-process id is still worth having

A `contextvars` correlation id ties the nested reads inside one `tap_element`
to that action, for *server* entries, where it does work. Build it in step 2.
Just do not name it in a way that implies it reaches the proxy.

## 5. The `print()` rule

A `print` never reaches `logging`, so it never reaches the ring buffer. In
daemon mode it lands in the log file with no level, no timestamp and no
category, and `query_logs` cannot see it at all.

**There is nothing to clean up.** An earlier draft of this section, following
#238, said the real work was "~91 calls, 79 of them in `device/tunneld.py`".
That was wrong, and so was #238's version. Measured properly with an AST walk
rather than `grep`:

| | |
|---|---|
| `grep 'print('` across `server/` | 511 |
| actual `print()` calls | **500** |
| of those, on a request path | **0** |

The 11 missing are `fingerprint(`, matched as a substring. #238 named
`device/controller_ui.py`, `proxy/cert_manager.py` and `api/proxy_certs.py` as
server-path offenders; all three are that false positive. It also named
`device/tunneld.py`, which is the `quern tunneld install|status|restart` CLI —
every one of its 79 prints is in a subcommand implementation.

All 500 are in terminal-facing modules: `setup.py` (134), `main.py` (122),
`tunneld.py` (79), `__main__.py` (62), `updater.py` (53), `menubar.py` (24),
`daemon.py` (18), `capture_env.py` (7). A user running `quern setup` wants
stdout, not a ring-buffer entry.

The single exception on a server path is deliberate and stays: `_task_done` in
`server/sources/server_log.py` prints to stderr because logging from inside
the log handler recurses.

**So the rule is a guard, not a migration.** `tests/test_no_server_path_prints.py`
walks `server/` with `ast` and fails on a `print()` outside an explicit
terminal-facing list, naming the file and function. Two further tests keep the
exemption list honest — it must not name a module that no longer exists, and
must not exempt one that no longer prints, because a stale exemption silently
widens the next time a file takes that path.

### The caller is the test, not the file

A per-file exemption is a proxy for the real rule, and the proxy can come
apart. `tunneld.py` is exempt because `quern tunneld install` is a person at a
terminal — but `install_daemon()` is an ordinary function. The day something
calls it from a request handler, every one of its twenty prints becomes
invisible logging on a server path, and a file-level allowlist says nothing.

The same module can legitimately be both. Installing a CA certificate is the
clearest case: `quern setup` does it with a person watching, and
`install_proxy_cert` does it through the API where the only record is whatever
we log.

So the exemption list carries a second condition: **an exempt module must not
be reachable from `server/api/`.** Verified today — none of the eight are
imported there, directly or transitively. The guard asserts it, so the
exemption cannot quietly widen into a request path.

Where a function genuinely needs both, the answer is not a `print` plus a log
line. It is to log unconditionally and let the CLI print its own output at the
call site, where it knows a terminal is watching.

That is the only version of this rule that stays true. The count was never the
problem; the next one added is.

## 6. Order, with acceptance criteria

Each step is independently useful and independently mergeable.

**Step 1 — category plumbing. Done.** `quern_category` handling in
`_BufferHandler.emit`, `server/logging_ext.py`, `_LEVEL_MAP` deleted, the
`NOTICE` docstring fixed, and `device.lifecycle` piloted on `sim_input.py`.

Two things were added to this step once it met contact with a running server:

- **`/logs/query` could not filter on `category` at all** — the predicate
  existed only on the SSE `/stream` endpoint, so the parameter was silently
  dropped and every category matched everything. Populating the field would
  have been useless without this.
- **Every logger is now `getLogger(__name__)`** (40 modules), so `process`
  discriminates and is orthogonal to `category`.

*Done when:* `GET /api/v1/logs/query?source=server&category=device.lifecycle`
returns the input-repair entry and nothing else — **verified live**, with a
`category=build` control returning zero, which is the half that proves the
filter runs rather than matching everything.

**Step 2 — the action log.** Add the four `LogEntry` fields; emit from the
API handlers; convert the `[PERF]` lines (START → `DEBUG`, SUCCESS → the
action entry).
*Done when:* one `tap_element` produces exactly one `INFO` entry carrying
action, resolved udid, outcome and duration, and a test fails if the udid is
the requested one rather than the resolved one.

**2a — `device/ui`. Done**, and verified live: a tap sent with no udid
recorded the resolved device, and `QUERN_LOG_LEVEL=debug` restored the pair.

**2b — the rest of the API. Done.** All 122 routes are classified:
`_UNCLASSIFIED` is empty, and a route added tomorrow is in neither list and
fails.

Two forms, and the choice between them is about handler shape rather than
taste. A `with action(...)` block is the default and reads best. The
`@logged_action` decorator exists for handlers too long to wrap without
re-indenting the body — `boot_device` is ~90 lines, and a block around only
the boot would stop the clock before the cert install and proxy auto-start
that the caller is still waiting on. Attempting that re-indent broke the
file once, which is the argument.

*Verified live*, not only in tests: a workflow against an iOS 27 simulator
produced entries across `device.action` and `device.read`, including a
genuine failure — `get_screen_summary` against SpringBoard with no frontmost
app, recorded as `failed` at ERROR with its duration.

**Step 3 — the `print` guard. Done.** There was nothing to convert: measured
with `ast` rather than `grep`, zero of the 500 real `print()` calls are on a
request path. The work was the guard that keeps that true.
*Done when:* the check fails on a `print()` added to `server/device/` and
passes for the terminal-facing modules — verified by mutation, including that
the failure names the offending file and function. **Plus:** no exempt module
is reachable from `server/api/`, so the exemption cannot widen into a request
path without the guard noticing.

**Step 4 — the trace export.** Join action entries with flows and device logs
on `(udid, interval)`, marking overlaps.
*Done when:* a scripted API test yields one timeline, and an export with two
overlapping actions on one device marks them rather than guessing.

Steps 1–3 are worth doing regardless of whether step 4 ever happens.

## What this does not propose

**Renaming loggers to match the categories.** The rename to `__name__` is
done (see "Logger names are `__name__`"), but aligning logger names *with the
category vocabulary* is explicitly rejected: they are different axes, and
`controller_ui.py` emits two categories from one module.

**A new query endpoint.** `category` is a parameter on the existing
`/logs/query`, and `source`, `level`, `process` and `search` already compose
with it. A trace view is step 4's job, not a new filter surface.

**Structured JSON log output.** Tempting, and orthogonal: the ring buffer is
already structured, and the file log is for humans reading a terminal. If the
action log turns out to need more fields than step 2 defines, that is the
moment to revisit — not before.
