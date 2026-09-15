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
