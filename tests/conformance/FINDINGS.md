# Conformance findings

Bugs and suspicious behaviour turned up while building the live conformance
suite. Nothing here is fixed on this branch — the branch builds the suite; fixes
get triaged separately so the eventual PR stays reviewable.

Each entry records what was observed, not what was inferred. Where a finding was
seen once under conditions that have since passed, it says so: a finding that
overstates its own evidence wastes the time of whoever picks it up.

Status key: **open** (stands, needs triage) · **confirmed** (reproduced
deliberately) · **filed** (has a GitHub issue) · **dismissed** (investigated, not
a bug) · **fixed**.

Filed issues carry the `found-by-conformance` label, so what this suite turned
up can be counted separately from what a person noticed.

| Finding | Status |
|---|---|
| F1 `/tools` can hang on a wedged device CLI | [#180](https://github.com/quern-dev/quern/issues/180) |
| F2 `/tools` availability has no third state | [#181](https://github.com/quern-dev/quern/issues/181) |
| F3 mock DELETE reports success for an unknown id | [#182](https://github.com/quern-dev/quern/issues/182) |
| F4 `level` is an undocumented severity floor | [#183](https://github.com/quern-dev/quern/issues/183) |
| F5 sim-bridge failure becomes a bodyless 500 | [#178](https://github.com/quern-dev/quern/issues/178) |
| F6 backend selection latched at startup | [#179](https://github.com/quern-dev/quern/issues/179) |
| F7 Xcode 27 moved SimulatorKit | fixed, [#176](https://github.com/quern-dev/quern/pull/176) |
| F8 Android `clear_text` deletes one character | [#177](https://github.com/quern-dev/quern/issues/177) |
| F9 `scroll_to_element` intermittently misses a distant row | [#84](https://github.com/quern-dev/quern/issues/84), pre-existing |

---

## F1 — `SimctlBackend.is_available()` can hang `/tools` indefinitely

**Status:** filed as [#180](https://github.com/quern-dev/quern/issues/180) — open — mechanism confirmed by reading; live symptom observed once.

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

**Status:** filed as [#181](https://github.com/quern-dev/quern/issues/181) — open — design observation, lower confidence than F1.

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

**Status:** filed as [#182](https://github.com/quern-dev/quern/issues/182) — confirmed — reproduced by a test on 2026-09-15, server v0.17.0.

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

**Status:** filed as [#183](https://github.com/quern-dev/quern/issues/183) — open — documentation, low severity, but with direct evidence.

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

**Status:** filed as [#178](https://github.com/quern-dev/quern/issues/178) — confirmed — reproduced on 2026-09-15, server v0.17.0.

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

**Status:** filed as [#179](https://github.com/quern-dev/quern/issues/179) — confirmed — root cause of F5's trigger, on this machine.

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

**Status:** **fixed** on `fix/xcode-27-simulatorkit-path` (commit `d67fc26`).
Found 2026-09-15 against Xcode 27.0, server v0.17.0.

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

**Status:** filed as [#177](https://github.com/quern-dev/quern/issues/177) — confirmed — reproduced directly on 2026-09-15 against a Pixel 3 XL
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

### F7 resolution

Fixed on `fix/xcode-27-simulatorkit-path`, branched from `main`. Both layouts
are now checked, in one resolver per language rather than a constant repeated at
each call site — `find_simulator_kit()` in `server/device/sim_bridge.py` and
`simulatorKitPath(at:)` in `tools/sim-bridge.swift`. Each returns the path it
found rather than a boolean, so the availability answer cannot drift from the
file that actually gets `dlopen`ed. No version switch: two `stat`s, and the next
Xcode can move it again without this needing to know.

Checked before relying on it, rather than assuming a move is only a move:

```
$ nm -gU .../Contents/SharedFrameworks/SimulatorKit.framework/SimulatorKit \
    | grep IndigoHIDMessageForTrackpadEventFromHIDEventRef
000000000000c154 T _IndigoHIDMessageForTrackpadEventFromHIDEventRef
```

**Verified live**, not only by unit test. With a server running from the fix
branch and the cached `~/.quern/bin/sim-bridge` binary deleted so it recompiled
from the patched source:

| | before | after |
|---|---|---|
| `/tools` → `sim_bridge` | `false` | `true` |
| `test_ui.py -k ios` | 21 failed, 4 passed | **0 failed, 24 passed** |

Mutation-tested: removing the `../SharedFrameworks` entry from a `git archive`
copy fails `test_finds_the_xcode_27_layout` and nothing else.

Two things this did **not** fix, deliberately:

* **idb is still broken.** It is third-party and looks in the old location
  itself. It no longer matters here because sim-bridge is preferred when
  available, but a machine without sim-bridge — no `swiftc`, say — still has no
  working HID path on Xcode 27.
* **F5 and F6 stand.** The bodyless 500 and the latched startup decision are
  separate defects that this bug merely exposed. F6 in particular is why the
  failure survived an Xcode upgrade in the first place.

### F8 — prior art

Filed noting that [#98](https://github.com/quern-dev/quern/issues/98) (closed,
2026-09-04) was the *same* defect on the web path: select-then-single-backspace
where the selection silently does not take, one character goes, and the call
reports `ok`. That fix hardened the web branch of `controller_ui.clear_text`
with verification (`_clear_web_input`) and a refusal (`_MAX_DELETE`), and left
the native branch — which has no check at all — untouched. The Android backend
has the identical construction.

Worth remembering when reading a closed issue as coverage: #98 is closed and
accurate, and the same bug was live on another backend the whole time.

---

## F9 — `scroll_to_element` intermittently fails to reach a distant row

**Status:** reproduction of pre-existing
[#84](https://github.com/quern-dev/quern/issues/84), not a new finding.
Commented there rather than filed again.

Turned up on 0.18.2 as an intermittent failure of
`test_scroll_to_element_brings_an_offscreen_row_into_view[ios]`: `row_60` is
present, the sweep runs for minutes, and the call reports it absent.

```
attempt 1: passed,  91.4s
attempt 2: failed, 183.2s
```

What this run adds to the issue, which measured `tap_element(...,
scroll_to_find=True)` at `max_swipes=10`:

* It reproduces through `POST /api/v1/device/ui/scroll-to-element`, the
  dedicated endpoint — so both callers of `_ios_scroll_to_element` are
  affected, and a fix wants checking against both.
* `max_swipes=25` fails too. A 2.5x budget ruling nothing out is evidence
  against a ceiling being the cause, and fits the momentum and
  visibility-re-confirm hypotheses already in the issue.
* Android passes consistently, so the two backends can be traced against each
  other without building anything.

**Left failing deliberately.** The suite's rule is that a known bug reports as a
failure; `xfail` or a retry would make this quieter and less true, and a
three-minute wait to be told a visible element is absent is exactly the
user-facing behaviour worth keeping visible.

This is also the first finding the suite produced by *re-running* rather than
by being written — worth noting for a branch meant to serve as a bug factory,
since it means periodic full runs are themselves productive.

### F9 — root cause (2026-09-16)

The slow-success trace added in #204 answered this in one run. On iOS 18.6 a
0.3s sweep swipe **flung** the list: 1020pt of travel for a 389pt drag, more
than a screen. So from the top, the first swipe left `row_20` under the nav
bar (y=-43), and the next carried it out of the tree. The sweep had lost the
direction by then and restarted downward, and it took 32 swipes and 105.8s. It
was also why every between-swipe settle timed out: the flings outlasted them.

Measured travel for the same drag:

| Swipe | Travel |
|---|---|
| 0.3s, no hold | 1020pt |
| 1.0s | 600pt |
| 2.0s | 379pt |
| 0.3s + 0.15s hold (identical points) | 1000pt |
| 0.3s + 0.15s hold (points alternating by 0.0005) | 350pt, at rest on release |

#204 now holds every sweep swipe, drags 75% of the screen (625pt measured,
shorter than the visible area), and reads at rest. The scroll test went from
105s to 19s, and the whole suite from 436s to 300s.

## F10 — the screen-size table is wrong for every model it lists → #210

Found live-testing #204: the sweep logged `screen=440x926` on an iPhone 16 Plus
simulator whose app frame is 430×932. None of the table's eight entries is
right. Separately, the visibility check's 34pt bottom inset lets a row under an
83pt tab bar count as visible.

## F11 — WDA's status probe lets `RemoteProtocolError` escape as a 500 → #211

With the WDA runner down on an iPhone 11, `GET /ui` returned a bare 500. The
check catches three httpx subclasses, not the base class, so this one escapes,
and the `usbmux forward` process it started is never terminated.

## F12 — WDA's hit-test resolved every point on iOS 26 Settings to an overlay (fixed in #204)

`find_element_at_point` took the *last* element containing the point. iOS 26
Settings has full-screen `Other` views after its rows, so every point resolved
to one of them. Their frames never move, so the sweep's progress check called
a scrollable list static and gave up after one swipe: 3 of 4 targets returned
404 on an iPhone 11. It now takes the smallest containing element, and the
sweep no longer hit-tests at all.

## F13 — a simulator booted while the Mac is locked ignores all input → #231

Every tap, swipe, keystroke and button press returns success and does nothing;
reads, screenshots and `open_url` keep working. Both backends fail identically
(sim-bridge and idb), as does a sim-bridge binary built two days earlier, so it
is neither ours nor a regression. Unlocking and rebooting the simulator fixes
it immediately.

It cost an hour here, and two confident hypotheses on the way -- an Xcode 27
regression, then the iOS 26.5 runtime -- were both wrong. What eventually
distinguished them was a control: the *same* test on the simulator that had
been booted earlier, which also failed, so the variable was the boot rather
than the runtime.

## F14 — Android's sweep cannot reach a deep row at the default budget → #232

Reach is capped by `max_swipes`: at the default of 10, row 150 of a 200-row
list is unreachable, and the 404 cannot be told from "no such element".
`max_swipes=25` finds it, so the sweep works and the budget is the limit.

**The first version of this finding also said Android never turns around at
the end of a list. That was wrong**, and the measurement behind it was taken
against a stale fixture: the suite reinstalls the probe app from its own
worktree, so the label fix on `main` was being overwritten on every run and
every lookup by label missed. From the bottom, Android spends its downward
budget on swipes that move nothing and then sweeps up, finding the row within
the default budget -- wasted time, not a failure.

Both halves are now gated at the default budget by
`test_a_row_far_down_the_list_is_reached` (red on Android) and
`test_a_row_above_is_reached_from_the_bottom` (green, kept as the guard).

## F15 — typing and clearing do not work on iOS 26.5 simulators → #233

20 of 25 iOS UI tests pass on an iPhone 17 Pro (iOS 26.5, Xcode 27); the five
failures are exactly the typing and clearing ones. The same suite passes on
iOS 18.6, and the failure reproduces against main.

## F16 — this suite's Android row labels were wrong, and the skips hid it

`ANDROID.row_label_template` was `"Row {index}"`, copied from iOS; the fixture
labels its rows `row_41`. Nothing caught it because the four scroll tests skip
on Android -- they resolve rows by identifier, and Android's RecyclerView gives
every row the same one. Template fixed here.

**Still open:** making those four tests run on Android needs a row *locator*
(identifier where there is one, label otherwise) rather than
`contract.row_identifier`. Until then Android scrolling has no regression gate
in this suite, which is how #232 went unnoticed.


## F17 — the suite reinstalls the fixture from its own worktree

Worth knowing before trusting any fixture change: the `ios_probe` and
`android_probe` fixtures build and install the probe app from the checkout the
tests are running in. A change installed by hand from another worktree is
overwritten on the next run, silently, and the failures that follow look like
product bugs -- three Android scroll tests failed this way, and the
measurement they produced went into #232 as a claim that turned out to be
wrong.

Merge the branch that carries the fixture change before reading any result
from it.
