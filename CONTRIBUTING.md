# Contributing to Quern

Everything a person or an agent needs to work on Quern itself. Read this before
changing code here.

For using Quern from another project, see [`README.md`](README.md) and
[`docs/agent-guide.md`](docs/agent-guide.md) instead.

## What this project is

A debug server for AI-assisted iOS development. Captures device logs, intercepts network traffic, and controls simulators — all exposed through HTTP APIs and MCP tools so AI agents can see what your app is actually doing.

## Architecture

```text
AI Agent (Claude Code, Cursor, etc.)
    │
    ├── via MCP ──→ mcp/          (thin TypeScript stdio wrapper)
    │                  │
    └── via HTTP ──→ server/       (Python FastAPI, port 9100)
                       │
                       ├── sources/     Log capture (device, simulator, syslog, oslog, crash, build)
                       ├── proxy/       Network interception (mitmproxy subprocess, port 9101)
                       ├── device/      Simulator control (simctl, idb)
                       ├── processing/  Classify, deduplicate, summarize
                       ├── storage/     In-memory ring buffer
                       ├── lifecycle/   Daemon, state.json, port scanning, watchdog
                       └── api/         HTTP route handlers
```

The MCP server is intentionally thin — just translates tool calls into HTTP requests. All logic lives in the Python server.

## Project structure

```text
server/              Python FastAPI server (the core)
  api/               Route handlers: logs, proxy, device, device_pool, builds, crashes
  sources/           Log source adapters (syslog, oslog, crash, build, simulator)
  proxy/             mitmproxy addon, flow store, system proxy management
  device/            Simulator control (simctl, idb), device pool, screenshots
  processing/        Pipeline: classifier → deduplicator → summarizer
  storage/           Ring buffer
  lifecycle/         Daemon mode, state.json, port scanning, watchdog
mcp/                 TypeScript MCP-to-HTTP adapter
examples/            Example scripts for HTTP API automation
tests/               pytest suite + fixtures
docs/                Agent guide
```

## Tech stack

- Python 3.11+ / FastAPI / uvicorn
- TypeScript / Node.js 18+ (MCP wrapper)
- mitmproxy 10+ (network interception)
- Pillow (screenshots)
- xcrun simctl + idb (simulator control)
- macOS `log` command (OSLog capture)

## Running it

Use the `./quern` wrapper script:

```shell
./quern setup          # First-time setup
./quern start          # Start as daemon
./quern start -f       # Foreground (for debugging the server itself)
./quern status         # Check status
./quern stop           # Stop daemon
```

The wrapper itself is three lines — it just `exec`s `python3 -m server`. Selecting
the project environment happens one level down, in `_maybe_reexec_in_venv`
(`server/__main__.py`): if the current interpreter is not already in a virtualenv,
the bootstrap `os.execv`s itself under `.venv/bin/python` before doing anything
that needs project dependencies. So no activation step is required, and none of
this depends on which `python3` is first on `PATH` — but the wrapper is not what
arranges that, and it will not help a command that bypasses the bootstrap.

State lives at `~/.quern/state.json`, the API key at `~/.quern/api-key`, logs at
`~/.quern/server.log`. The full command list is in [`README.md`](README.md), which
`tests/test_readme_sync.py` checks against the actual CLI dispatch — so that is
the copy to trust, and the one to update.

## Code conventions

- Python: `async`/`await` throughout. Type hints on all signatures. Pydantic models for API schemas.
- Imports: stdlib → third-party → local, separated by blank lines.
- Source adapters: Inherit from `BaseSourceAdapter` (`server/sources/__init__.py`). Must implement `start()`, `stop()`, emit entries via callback. Must not crash the server.
- Testing: pytest with `pytest-asyncio`. Fixtures in `tests/fixtures/`. Mock subprocesses — never call real simctl/idb in tests.
- Git: No `Co-Authored-By` lines. No AI attribution in commits. Enforced by the `commit-msg` hook in `scripts/git-hooks/` — enable with `git config core.hooksPath scripts/git-hooks`.
- **Error paths get the same scrutiny as the happy path, on the first pass.** Every one of the nine review findings across #106 and #107 was in failure handling; not one touched feature logic. The recurring shapes, each of which cost a review round:
  - **Catch base classes, not the subclasses you have seen.** `except (OSError, subprocess.SubprocessError)`, not `except FileNotFoundError`. Three rounds went to adding one subclass at a time: `FileNotFoundError` missed `TimeoutExpired`, adding that missed `PermissionError` — which was propagating uncaught into the start path, so the failure was a crash rather than the stale state being fixed.
  - **A failed check must never read as a passing one.** Distinguish "asked, got nothing" from "could not ask" — `{}` vs `None`, not both as empty. Reporting something healthy on the strength of a lookup that never ran is a false all-clear, which is worse than no check.
  - **Every failure reaches the exit code.** A printed warning followed by `return 0` tells a script the opposite of what happened. If a step can fail, its result goes into the return value — and name the step, because "dependencies could not be installed" after a failed restart sends the reader to the wrong place.
  - **A success marker must not survive a failure.** Stamps and caches are written only on success *and* cleared on every failure path, including the one that raises. Not writing it is insufficient wherever the marker may already be current from an earlier success.
  - **Tests inject every external lookup.** A test that reaches PyPI, brew or the network is a bug — slow, nondeterministic, broken offline. Pass the fetcher in, the way `probe_container` takes `describe_point`. When a call site moves, re-audit every test that reaches it; moving one report ahead of a return turned two passing tests into network calls.
  - **Mutation-test the guard.** Revert the fix and confirm a test actually fails. Two tests here passed against the bug they claimed to cover — one injected a fake at the wrong layer, one asserted on empty input.
- **A stacked PR is never auto-reviewed.** CodeRabbit skips any pull request whose base is not the default branch — it posts "Auto reviews are disabled on base/target branches other than the default branch" and does nothing else. Retargeting does not wake it up either: measured twice, a PR retargeted to `main` when its parent merged sat 13–14 minutes with no review and no bot activity until asked explicitly. So on a stack, the `@coderabbitai review` that `merge-pr.sh --ask` sends is not a belt-and-braces re-check, it is the *only* review that will ever happen. #108 sat open for 15 hours with 968 unreviewed lines because of this, and the first look it got found a real bug. Ask at every level, or don't stack.

- **Merging a PR: use `scripts/merge-pr.sh <number>`**, not `gh pr merge`. `.coderabbit.yaml` sets `auto_review.enabled: true` with `auto_incremental_review: false`, so a review fires when a PR is *opened* and never again — **pushing to an open PR does not trigger one**. That is deliberate: review runs are capped per hour, and re-reviewing every push spends the budget on intermediate states nobody merges. The consequence is that an "0 unresolved threads" reading goes stale the moment you push, not because a new review contradicted it but because the code moved out from under it, and it still reads exactly like all-clear. The script refuses to merge unless the newest review is newer than the newest commit, and internally runs `pr-review-status.py --ask`, which requests the missing review when the head has moved past it. `--force` overrides deliberately. `scripts/pr-review-status.py` is the same check on its own; don't pipe it if you care about the exit code — `| sed` or `| tee` reports the pipe's status, not the script's, which reads as success.

## Design decisions worth knowing

- **State file is the contract.** All consumers discover the server via `~/.quern/state.json`. Never hardcode ports.
- **Cursor-based summaries.** `/logs/summary` and `/proxy/flows/summary` return a `cursor` for delta updates. Critical for token-efficient AI workflows.
- **Template-based summaries, not LLM-generated.** No external API calls needed.
- **Hybrid proxy storage.** Summary log entries go in the ring buffer (so log queries include network events). Full flow records go in a separate FlowStore.
- **Mock/intercept patterns use mitmproxy filter syntax.** Valid operators: `~d` (domain), `~u` (URL), `~m` (method), `~c` (status code), `~b` (body), etc. Note: `~p` (path) does NOT exist — use `~u` for path matching.
- **Server-side filter validation.** Invalid mitmproxy filter patterns are rejected with 400 before reaching the addon.

## Proxy behaviour

The proxy follows an **opt-in capture** model:
1. Proxy server runs in background (always ready after `./quern start`)
2. System proxy is OFF by default — user's browser works normally
3. Agents call `configure_system_proxy` when ready to capture, `unconfigure_system_proxy` when done

Never auto-configure the system proxy. Never leave it configured when not actively testing.

## Where the API is documented

Deliberately not restated here. [`docs/api-reference.md`](docs/api-reference.md)
is the reference, and `tests/test_readme_sync.py` checks it against the routes
and MCP tools actually registered — a hand-maintained summary in a third file
would drift silently, with nothing to catch it.
