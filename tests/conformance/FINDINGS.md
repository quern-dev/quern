# Conformance findings

Bugs and suspicious behaviour turned up while building the live conformance
suite. Nothing here is fixed on this branch — the branch builds the suite; fixes
get triaged separately so the eventual PR stays reviewable.

Each entry records what was observed, not what was inferred. Where a finding was
seen once under conditions that have since passed, it says so: a finding that
overstates its own evidence wastes the time of whoever picks it up.

Status key: **open** (stands, needs triage) · **confirmed** (reproduced
deliberately) · **dismissed** (investigated, not a bug) · **fixed**.

---

## F1 — `SimctlBackend.is_available()` can hang `/tools` indefinitely

**Status:** open — mechanism confirmed by reading; live symptom observed once.

`server/device/simctl.py:60` probes availability by running `xcrun simctl help`
and awaiting it:

```python
proc = await asyncio.create_subprocess_exec("xcrun", "simctl", "help", ...)
await proc.communicate()          # no timeout
```

There is no timeout and no cache. `DeviceController.check_tools()` awaits it, and
`/tools` awaits that. So for as long as `simctl` fails to return, `/tools` does
not respond.

**Why this is not hypothetical.** `xcrun simctl help` hangs — not errors, hangs —
while Xcode's first-launch tasks are running, which is the normal state of a
machine for some minutes after an Xcode upgrade. Observed directly on
2026-09-15 during the Xcode 27.0 upgrade on this machine:

- `timeout 30 xcrun simctl help` → exit 124 (timed out)
- `xcodebuild -checkFirstLaunchStatus` → exit 69 (first-launch incomplete)
- `curl -s -m 20 http://127.0.0.1:9100/tools` → no response within 20s
- ~40 minutes later, first-launch complete: `simctl help` returns in 0.13s,
  `/tools` responds in 0.175s

**Why it matters.** `/tools` is documented as the endpoint backing `quern doctor`
and `quern status`. The docstring in `server/main.py` explains that `/tools` was
split out of `/health` *precisely* so that "the health ping stays fast (tool
probes can take several seconds)" — the design anticipated slow probes but not
non-returning ones. The user-visible result is that `quern doctor`, the command
someone runs *because* something is wrong, is the command that hangs. Immediately
after an Xcode upgrade is exactly when a user reaches for it.

The sibling backends should be checked for the same shape rather than only this
one; `adb`, `idb`, `devicectl` and `pymobiledevice3` all have an `is_available`.

**Suggested fix.** Wrap the probe in `asyncio.wait_for` and treat a timeout as a
third state — not `True`, and not the `False` that means "not installed", since
"wedged" and "absent" want different advice from `doctor`.

**Guard:** `tests/conformance/test_00_environment.py::test_tool_discovery_answered`
fails (rather than skipping) when `/tools` does not answer inside 25s.

---

## F2 — `/tools` reports availability with no freshness signal

**Status:** open — design observation, lower confidence than F1.

Related to F1 but distinct. `check_tools()` returns a flat `dict[str, bool]`.
`tool_sites()` deliberately does not fold into it, and its docstring explains
why: the booleans are consumed by truthiness, so widening the values "would make
every tool read as available, including the missing ones."

That reasoning is sound and it also bounds what `/tools` can express. A tool that
is installed but not working can only be reported as `true` or `false`, and
either answer misleads: `true` says healthy, `false` says not installed. F1's
window is the concrete case.

Worth confirming against `quern doctor`'s actual output before filing — `doctor`
may already draw on `/api/v1/device/tools/sites`, which carries `diagnostic` and
`detail` fields and can express the third state.

---

## F3 — `DELETE /api/v1/proxy/mocks/{rule_id}` reports success for a rule that never existed

**Status:** confirmed — reproduced by a test on 2026-09-15, server v0.17.0.

```
DELETE /api/v1/proxy/mocks/e16d6dcc-be41-4597-8891-37f941641871
→ 200 {"status":"deleted","rule_id":"e16d6dcc-be41-4597-8891-37f941641871"}
```

The id was a freshly generated UUID that had never been a rule.

`server/api/proxy_intercept.py:254`:

```python
@router.delete("/mocks/{rule_id}")
async def delete_mock(request: Request, rule_id: str) -> dict:
    adapter = _require_running_proxy(request)
    await adapter.clear_mock(rule_id=rule_id)      # no existence check
    return {"status": "deleted", "rule_id": rule_id}
```

**Why it matters.** The sibling verb disagrees: `PATCH /mocks/{rule_id}` maps the
adapter's `ValueError` onto 404 and answers "not found" for the same id. So the
same unknown id is a 404 to one verb and a 200 "deleted" to another, and the
`{"status": "deleted"}` body actively asserts something that did not happen.

The consequence is not cosmetic. Teardown code that deletes rules by id and
checks the response — which is what this suite's own `mock_sandbox` fixture does
— cannot detect that it failed to remove a rule. A leaked mock rule does not sit
there inertly; it goes on matching and serving synthetic responses to real
traffic, and the next person to wonder why an app gets a 418 has no reason to
suspect a mock that something already reported as deleted.

This is the "a failed check must never read as a passing one" shape that
`CONTRIBUTING.md` lists under *Code conventions*, applied to a write instead of
a read.

**Suggested fix.** Have `clear_mock` report whether it removed anything and map
"nothing removed" to 404, matching `update_mock`. `DELETE /mocks` (clear-all)
already returns a `count` and should keep its current semantics — clearing an
empty set is legitimately a success.

**Guard:** `test_proxy_mocks.py::test_deleting_an_unknown_rule_reports_not_found`
(currently failing — this is the bug, not a broken test).

---

## F4 — `level` is a severity floor, and nothing says so

**Status:** open — documentation, low severity, but with direct evidence.

`GET /api/v1/logs/query?level=error` returns `fault` entries too. That is
correct and deliberate: `server/storage/ring_buffer.py:125` filters on
`LogLevel.at_least(params.level)`, and `LogLevel` is documented in
`server/models.py` as "ordered from least to most severe".

Nothing the caller can see says this. The query parameter has no `description=`,
so it is absent from `/openapi.json` and from `/docs`; `docs/api-reference.md`
describes the endpoint only as "Query logs with filters and pagination"; the MCP
tool description does not mention it either.

**Evidence that it misleads:** the first version of
`test_a_level_filter_returns_only_that_level` in this suite asserted exact-match
semantics and failed against a correct server. That test was written from the
documentation, by a reader who had the source open.

The misreading is worse in the other direction than this one. Someone who
assumes exact match and queries `level=warning` to count warnings gets warnings
plus errors plus faults, and reports a warning count that is silently inflated
by the two categories they were trying to separate out.

**Suggested fix.** One `description=` on the `level` query parameter
(`server/api/logs.py:176`) saying it is a minimum, which propagates to the
OpenAPI schema, `/docs`, and anything generated from them. A line in
`docs/api-reference.md` alongside it.

**Guard:** `test_logs.py::test_a_level_filter_returns_that_level_and_above` and
`::test_the_most_severe_level_filter_is_exact` — the pair pins the threshold
semantics from both sides, so a future change to exact-match breaks a test
rather than a caller.

---

## F5 — a sim-bridge failure escapes as an undiagnosable HTTP 500

**Status:** confirmed — reproduced on 2026-09-15, server v0.17.0.

`POST /api/v1/device/ui/tap-element` returns `500` with the body
`Internal Server Error` and nothing else. The traceback in `~/.quern/server.log`:

```
  File "server/api/device_ui.py", line 362, in tap_element
  File "server/device/controller_ui.py", line 1646, in tap_element
  File "server/device/sim_bridge.py", line 525, in tap
  File "server/device/sim_bridge.py", line 407, in _send_admitted
    raise RuntimeError(f"sim-bridge: {error}")
RuntimeError: sim-bridge: tap failed
```

`_send_admitted` raises a bare `RuntimeError`. The handler in `device_ui.py`
catches `DeviceError` only, so the `RuntimeError` is never converted and FastAPI
turns it into a generic 500.

**Why it matters.** The caller is told nothing. `"Internal Server Error"` does
not say which backend failed, or that a backend was involved at all — the
server knew it was "sim-bridge: tap failed" and discarded that on the way out.
Every UI endpoint routed through this backend has the same hole, so a wedged
sim-bridge presents as the whole UI API returning 500 for reasons no caller can
see. It took reading the server log to find out, which is not available to an
agent driving the API from another machine.

This is the *"every failure reaches the caller"* half of the error-path
conventions in `CONTRIBUTING.md`, and the "catch base classes, not the
subclasses you have seen" rule applied from the other side: the raiser should
be throwing something the layer above already catches.

**Suggested fix.** Raise `DeviceError` from `_send_admitted` — it is a device
operation failing, which is exactly what that exception is for — and keep the
`sim-bridge: <error>` text as its detail.

**Guard:** the whole of `test_ui.py` fails against this; there is no single
narrow test, because the defect is that *every* tap-driven endpoint 500s.

---

## F6 — backend selection is latched at startup and never re-checked

**Status:** confirmed — root cause of F5's trigger, on this machine.

`server/main.py:298` records the decision once, at startup:

```python
device_controller._sim_bridge_ok = tools.get("sim_bridge", False)
```

`sim_bridge.is_available()` returns True only when
`$(xcode-select -p)/Library/PrivateFrameworks/SimulatorKit.framework` exists.

The timeline on this machine:

| When | What |
|---|---|
| 2026-09-14 22:56 | server starts; SimulatorKit present; `_sim_bridge_ok = True` |
| 2026-09-15 00:21 | Xcode.app replaced by the 27.0 upgrade |
| now | `SimulatorKit.framework` no longer exists at that path |
| now | `/tools` correctly reports `sim_bridge: false` |
| now | the server still routes every tap to the sim-bridge backend |

So the server and its own health endpoint disagree: `/tools` — and therefore
`quern doctor` — reports the backend as unavailable while the running server
goes on using it. An operator who checks doctor sees the correct answer and no
reason to connect it to the 500s, because doctor is reporting the thing that is
*not* being used.

**Why it matters.** Upgrading Xcode while Quern runs is not an exotic sequence;
it is what happens on the day Xcode updates, and Quern is a long-running daemon
precisely so nobody restarts it. The result is UI automation broken until
someone thinks to restart the server, with a 500 that names nothing (F5) as the
only symptom.

Note that the reverse is equally live: a server started *without* Xcode never
picks the backend up when Xcode is installed later.

**Suggested fix.** Re-evaluate before use rather than latching, or invalidate on
a failed send — the backend already knows when its own send failed, which is a
strong signal the capability has gone. If the decision must stay cached,
`/tools` disagreeing with it should be surfaced rather than reported as health.

**Guard:** none yet. A conformance test cannot easily stage an Xcode upgrade;
the reachable assertion is the narrower one — that a backend the server reports
as unavailable is not the one it uses.

---

## F7 — Xcode 27 moved SimulatorKit, and all simulator HID automation is broken

**Status:** confirmed — 2026-09-15, Xcode 27.0, server v0.17.0. **This is the
most severe finding so far: tap, type, swipe and press do not work on a
simulator at all.**

Xcode 27 relocated the framework:

| | Path |
|---|---|
| Expected (Xcode ≤ 26) | `Xcode.app/Contents/Developer/Library/PrivateFrameworks/SimulatorKit.framework` |
| Actual (Xcode 27.0) | `Xcode.app/Contents/SharedFrameworks/SimulatorKit.framework` |

Verified on this machine: the first path does not exist, the second does. Note
it is not a move *within* `Contents/Developer` — `SharedFrameworks` is a sibling
of `Developer`, so no search rooted at the developer directory can reach it.

Quern hardcodes the old layout in three places:

- `server/device/sim_bridge.py:112` — `is_available()` builds
  `Path(dev_dir) / "Library" / "PrivateFrameworks" / "SimulatorKit.framework"`,
  so sim-bridge now reports unavailable.
- `tools/sim-bridge.swift:47` and `:81` (`hasSimulatorKit`) — the `dlopen` path
  and the probe behind `scanApplications()`, which walks `/Applications` looking
  for *any* Xcode with the framework at the old location and therefore finds
  none.
- `tools/sim-bridge.swift:694` — the same relative path again.

**The fallback is broken too, independently.** With sim-bridge correctly
reporting unavailable after a restart, `tap_element` falls through to idb, which
fails the same way — idb is third-party and looks in the old location as well:

```
POST /api/v1/device/ui/tap-element -> 500
{"detail":"[idb] idb ui failed: SimulatorKit is required for HID interactions:
 ... Attempting to load a file at path
 '/Applications/Xcode.app/Contents/Developer/Library/PrivateFrameworks/SimulatorKit.framework',
 but it does not exist"}
```

So there is no working path to simulator HID on Xcode 27. `get_ui_tree`,
`get_element`, `screenshot` and app install/launch all still work — the
accessibility and CoreSimulator paths are unaffected — which makes this worse,
not better: the server looks healthy and reads the screen correctly, and only
interaction fails.

**Why it matters.** This is a headline feature broken by an OS-vendor upgrade
that every user of this tool will take. It is silent until someone taps, and
`quern doctor` reports `sim_bridge: false` without connecting it to anything.

**Suggested fix.** Search both layouts. `Contents/SharedFrameworks` first for
Xcode 27+, falling back to `Contents/Developer/Library/PrivateFrameworks`, in
all three sites plus `scanApplications()`. A version check is the wrong shape
here — checking both paths costs one `stat` and keeps working across whichever
layout the next Xcode ships.

Worth confirming against a machine with Xcode 26 before shipping, to be sure the
old path is still needed rather than merely still present.

**Guard:** `test_ui.py` in full. Its failure mode against this bug is
unmistakable — every interaction test fails while every read test passes.

---

## F8 — `clear_text` on Android deletes one character and reports success

**Status:** confirmed — reproduced directly on 2026-09-15 against a Pixel 3 XL
(Android 10), server v0.17.0.

```
after type : 'conformancab_CD!2@xABCDEFGHIJxyzkeep-meto-be-clear'
clear #1 -> HTTP 200 {"status":"ok"}   value now: '…to-be-clea'
clear #2 -> HTTP 200 {"status":"ok"}   value now: '…to-be-cle'
clear #3 -> HTTP 200 {"status":"ok"}   value now: '…to-be-cl'
```

One character per call, `{"status":"ok"}` every time.

**Root cause.** `server/device/u2_client.py:541`, `select_all_and_delete`:

```python
subprocess.run([... "input", "keyevent", "KEYCODE_MOVE_HOME"], ...)
subprocess.run([... "input", "keyevent",
                "--longpress", "KEYCODE_SHIFT_LEFT", "KEYCODE_MOVE_END"], ...)
subprocess.run([... "input", "keyevent", "KEYCODE_DEL"], ...)
```

`input keyevent` given several keycodes sends them **in sequence, not as a
chord**. There is no way to hold Shift across separate keycodes this way, and
`--longpress` applies to the sequence rather than making Shift a held modifier.
So nothing is ever selected: the caret goes to the start, then to the end, and
the single `KEYCODE_DEL` backspaces one character. The observed behaviour is
exactly what the code does.

**Compounding problems in the same function:**

- **No return code is checked.** All three `subprocess.run` calls pass
  `capture_output=True` and discard the result, so every one of them could fail
  and the function would still return normally — `CONTRIBUTING.md`'s "every
  failure reaches the caller", missed three times in nine lines.
- **No verification.** The function reports success without reading the field
  back. The web-input path in `controller_ui.clear_text` does verify
  (`_clear_web_input` "reports success only when the element's value is actually
  empty") and even refuses rather than half-clearing when it cannot. The native
  path has neither guard, so the weakest path is the one with no check.
- **`adb` is taken from `PATH`** rather than the resolved binary the rest of the
  backend uses.

**Why it matters.** Clearing a field is setup, not an assertion — it runs before
the interesting part of a test. A clear that silently leaves 49 of 50 characters
means the next `type_text` appends, and the failure surfaces later as a
mismatched string in an unrelated assertion. That is precisely how it presented
here: three tests failed, only one of which was about clearing.

It also makes `clear_text` unusable as a reset between test steps on Android,
which is the main thing it is for.

**Suggested fix.** uiautomator2 already exposes this — `device(focused=True)
.clear_text()`, or `set_text("")` on the resolved element — and it is what the
`_connect(udid)` handle on the line above is for. Failing that, `input
keycombination KEYCODE_CTRL_LEFT KEYCODE_A` (API 31+) sends a real chord, or
move to the end and send `KEYCODE_DEL` once per character as the iOS path does.
Whichever is chosen, read the value back and raise when it is not empty.

**Guard:** `test_ui.py::test_clearing_a_named_field_empties_it[android]` and
`::test_clearing_names_the_field_it_was_told_to[android]`.

### F8 addendum — it is not a missing IME

Checked, because "the keyboard support isn't installed" is the obvious first
theory and it is wrong:

```
$ adb shell settings get secure default_input_method
com.github.uiautomator/.AdbKeyboard          ← installed AND active
$ adb shell pm list packages | grep uiautomator
package:com.github.uiautomator
$ python -c "import importlib.metadata as m; print(m.version('uiautomator2'))"
3.7.0
```

The uiautomator2 companion app is installed and its `AdbKeyboard` IME is the
device's active input method. That is why **typing works** — including the
shift-character string `ab_CD!2@x`, which arrives verbatim. Quern also patches
`_setup_ime` (`u2_client.py:226`) to install its own driver APK rather than the
bundled openatx one, so this path is deliberately maintained.

So the IME is fine and nothing needs installing. The defect is narrower than
that: `select_all_and_delete` *abandons the u2 handle it has just opened* on the
line above and shells out to `adb shell input keyevent` instead —

```python
device = self._connect(udid)
device.click(int(x), int(y))     # u2 used here…
import subprocess                # …and then not for the part that matters
subprocess.run(["adb", "-s", adb_serial, "shell", "input", "keyevent", ...])
```

— and uiautomator2 3.7.0 exposes `Device.clear_text()` directly (confirmed
present). The fix is to call it on the handle already in hand, not to install
anything.
