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

The wrapper resolves its own directory, through symlinks, and runs
`.venv/bin/python -m server` from there. It does that rather than calling
`python3` because `-m` puts the *caller's* working directory on the import
path, not the script's: a cwd-dependent wrapper works inside the clone and
fails everywhere else, and the failure reads `No module named server` prefixed
with whichever `python3` was first on `PATH`. That names the reader's
interpreter and says nothing about the wrapper, so the report arrives pointing
at the wrong thing. It cost a session to diagnose once.

Before setup has run there is no venv, so the wrapper falls back to `python3`.
Selecting the project environment then happens one level down, in
`_maybe_reexec_in_venv` (`server/__main__.py`): if the current interpreter is
not already in a virtualenv, the bootstrap `os.execv`s itself under
`.venv/bin/python` before doing anything that needs project dependencies. That
fallback is what makes `./quern setup` work on a fresh clone; it is not a
general-purpose path, and it will not help a command that bypasses the
bootstrap.

Note that re-exec trusts `.venv/bin/python` to be a working venv. If it is not
— a deleted `pyvenv.cfg` is enough, and Python then reports the *base*
interpreter's path rather than the venv's — the re-exec lands in the same
state and repeats. Recreate the venv rather than patching around it.

`quern setup` also writes a `~/.local/bin/quern` with the project path baked
in. Two copies can therefore be on `PATH` at once, and zsh caches the first one
it resolves: `type -a` re-scans `PATH` and shows what *should* run, while the
shell keeps running what it hashed earlier. `rehash` after setup, or read a
stale wrapper as the answer.

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
  - **Tests never touch the real machine.** `QUERN_STATE_DIR` redirects
    `~/.quern` and `setup.WRAPPER_PATH` redirects the wrapper; a path with no
    redirect wants one adding rather than a `Path.home` patch at the call site.
    An autouse fixture in `tests/conftest.py` is the backstop, failing any test
    that changes a known install path -- including other tools' config, which
    `_remove_mcp_registrations` legitimately rewrites in production and no test
    has business touching. It is a backstop rather than the mechanism: it knows
    only the paths someone listed, so redirection is still the thing to get
    right. This is not hypothetical. The uninstall tests patched six things and
    not `Path.home`, so every full run deleted the developer's own `quern`
    command, and it presented as the CLI working intermittently for months.

    **A branch that predates the fix still has the bug.** The guard lives in
    `tests/conftest.py`, so it only protects branches that contain it, and
    running an older branch's suite deletes the wrapper exactly as before --
    silently, with every test passing. Merge or rebase `main` into a branch
    *before* running its full suite, and especially before handing it to
    review agents, which run the suite many times over. Measured: three
    passing uninstall tests on a branch forked one commit early removed
    `~/.local/bin/quern`, and the first sign of it was the menu-bar app
    reporting it could not find the command.
  - **Tests inject every external lookup.** A test that reaches PyPI, brew or the network is a bug — slow, nondeterministic, broken offline. Pass the fetcher in, the way `probe_container` takes `describe_point`. When a call site moves, re-audit every test that reaches it; moving one report ahead of a return turned two passing tests into network calls.
  - **Mutation-test the guard.** Revert the fix and confirm a test actually fails. Two tests here passed against the bug they claimed to cover — one injected a fake at the wrong layer, one asserted on empty input.
- **A stacked PR is never auto-reviewed.** CodeRabbit skips any pull request whose base is not the default branch — it posts "Auto reviews are disabled on base/target branches other than the default branch" and does nothing else. Retargeting does not wake it up either: measured twice, a PR retargeted to `main` when its parent merged sat 13–14 minutes with no review and no bot activity until asked explicitly. So on a stack, the `@coderabbitai review` that `merge-pr.sh --ask` sends is not a belt-and-braces re-check, it is the *only* review that will ever happen. #108 sat open for 15 hours with 968 unreviewed lines because of this, and the first look it got found a real bug. Ask at every level, or don't stack.

- **Not every CodeRabbit finding is a review thread.** Inline comments are, and
  those are what the GraphQL `reviewThreads` query returns. *Outside diff
  range*, *Additional*, *Nitpick* and *Duplicate* findings are not: they live
  in the review **body**, inside collapsed `<details>` blocks. So a check that
  walks review threads reports "1 finding" on a review that made two, with
  nothing to indicate the gap. Run `scripts/cr-findings.sh <number>`, which
  reads both. Measured on #145: the thread list showed one finding while a
  second, rated Major, sat unread in the body — a queued value equal to the one
  just written was written again, which for the update channel discards the
  cached update check a second time. It was found by a person scrolling the
  page, and it was also the finding that exposed one of our own tests asserting
  the bug as expected behaviour.

- **A draft PR is never reviewed either, and it hangs the merge.** CodeRabbit
  does not auto-review a draft, and `gh pr merge` cannot merge one, so
  `merge-pr.sh` asks for a review that never arrives and waits indefinitely. The
  tell is misleading in the same way as the stacked case: `scripts/cr-findings.sh`
  returns *completely empty*, which is indistinguishable from "reviewed, found
  nothing". Measured on #176 — zero reviews, two bot acknowledgements, and a
  merge script killed after minutes of silence; `gh pr ready 176` produced a
  review in about two minutes, and it found two real defects. Check
  `gh pr view <n> --json isDraft` before starting a merge, and treat an empty
  findings list as a question rather than an answer.

- **Merging a PR: use `scripts/merge-pr.sh <number>`**, not `gh pr merge`. `.coderabbit.yaml` sets `auto_review.enabled: true` with `auto_incremental_review: false`, so a review fires when a PR is *opened* and never again — **pushing to an open PR does not trigger one**. That is deliberate: review runs are capped per hour, and re-reviewing every push spends the budget on intermediate states nobody merges. The consequence is that an "0 unresolved threads" reading goes stale the moment you push, not because a new review contradicted it but because the code moved out from under it, and it still reads exactly like all-clear. The script refuses to merge unless the newest review is newer than the newest commit, and internally runs `pr-review-status.py --ask`, which requests the missing review when the head has moved past it. `--force` overrides deliberately. `scripts/pr-review-status.py` is the same check on its own; don't pipe it if you care about the exit code — `| sed` or `| tee` reports the pipe's status, not the script's, which reads as success.

  **When reviews are rate limited, it says so and stops.** CodeRabbit answers the request with "Review rate limited" within seconds. The script used to treat that as no answer, poll out its whole ten-minute timeout in silence, and report only "still awaiting review" — twice on #202 (#207). It now reports the limit and when it lifts, reads the summary comment first so a limit already on the page costs no comment, and does not ask again before that time. Note what a rate-limited head looks like: CodeRabbit still resolves the threads whose fixes it can see, so the PR shows zero open findings on a commit no review has read. #199 and #202 both merged in that state.

## Design decisions worth knowing

- **State file is the contract.** All consumers discover the server via `~/.quern/state.json`. Never hardcode ports.
- **Cursor-based summaries.** `/logs/summary` and `/proxy/flows/summary` return a `cursor` for delta updates. Critical for token-efficient AI workflows.
- **Template-based summaries, not LLM-generated.** No external API calls needed.
- **A warning only the server log carries has not been delivered.** The caller
  driving quern over MCP sees the JSON body and nothing else, so a condition
  that changes what a result *means* belongs on the response. Quern detected
  Xcode 27's Device Hub taking a simulator's input services, and logged it —
  while returning `{"status": "ok"}` for taps that were accepted and
  discarded, to the one audience that could act on it and could not see it.
  The log line is for the person reading afterwards; the response field is for
  the agent deciding what to do next. A check worth making is worth
  delivering.
- **Report the outcome, not the request.** `simctl launch` reports the launch
  it was *asked* for and hands back a pid, so `launch_app` reported success
  for apps that never started -- on iOS 27, any app that has not adopted the
  UIScene lifecycle. The failure then surfaced several calls later as
  `tap_element` finding no element, which sends the reader somewhere unrelated
  to the cause. Where a tool can confirm the state it claims to have produced,
  confirm it.
- **Hybrid proxy storage.** Summary log entries go in the ring buffer (so log queries include network events). Full flow records go in a separate FlowStore.
- **Mock/intercept patterns use mitmproxy filter syntax.** Valid operators: `~d` (domain), `~u` (URL), `~m` (method), `~c` (status code), `~b` (body), etc. Note: `~p` (path) does NOT exist — use `~u` for path matching.
- **Server-side filter validation.** Invalid mitmproxy filter patterns are rejected with 400 before reaching the addon.

## Proxy behaviour

The proxy follows an **opt-in capture** model:
1. Proxy server runs in background (always ready after `./quern start`)
2. System proxy is OFF by default — user's browser works normally
3. Agents call `configure_system_proxy` when ready to capture, `unconfigure_system_proxy` when done

Never auto-configure the system proxy. Never leave it configured when not actively testing.

**The proxy never intercepts quern's own traffic.** `ALWAYS_BYPASS` in
`server/proxy/addon.py` passes `quern.dev` through at `tls_clienthello`, before
TLS is terminated, so no certificate is replaced and there is nothing to fail.
Without it, configuring the system proxy made quern man-in-the-middle its own
update check: urllib honours the macOS system proxy, the certificate stopped
verifying, and the update check failed on precisely the machines running quern.
It is deliberately *not* seeded into the user's bypass list — `clear_bypass`
empties that, so a seed there would be silently removable, which is the same
failure with one more step in front of it. The cost is that quern's own site
cannot be captured through quern, which is not what this proxy is for.

**The same rule governs the CA.** Installing a MITM root certificate authority
is a *larger* commitment than a system-proxy toggle, not a smaller one: it
persists across sessions, outlives the capture window that motivated it, and
the user has to know it happened in order to undo it. So enabling capture
refuses with 428 when a booted simulator does not trust the CA, names the
devices, and offers three ways out rather than one -- offering only "install
the certificate" railroads every user into trusting a CA, which is the outcome
the refusal exists to make deliberate.

Every path that begins routing a device's traffic through the proxy shares one
`_ensure_ca_is_trusted` helper: `configure_system_proxy`, `set_local_capture`,
and `start_proxy` when `system_proxy` is set. The second had no gate at all
until 0.18.0, which is how a field report reached exactly the failure the first
one exists to prevent -- and the third was found during that work, reachable in
one call from the tool the refusal had just told the caller to stop using. A
guard on one path does not cover its siblings, twice over now, so a new capture
path calls the helper rather than repeating the block.

Only *enabling* is gated. Refusing to disable capture would trap someone in the
state they are trying to leave, and starting the bare listener routes nothing.

Two surfaces cannot use the helper and are handled in kind. `quern
enable-local-capture` writes `config.json` in one process and the lifespan
starts routing from it in another, so it runs the same preflight in-process --
a `DeviceController` needs no server -- and refuses before the write, exiting
non-zero. Server startup cannot refuse at all: the decision was made in an
earlier process and failing to boot over one device's certificate is worse than
the state it prevents, so it warns, and installs when `auto_install_cert` says
to.

**`auto_install_cert` means the same thing at all five.** A setting honoured in
four of five places is worse than one honoured nowhere: it works until the day
the user takes the fifth path. What it does not buy is a failed install
succeeding -- that is reported everywhere, because enabling capture that cannot
work is the state all of this exists to prevent.

**A trust record is not trust.** `is_cert_installed` always asks the device;
there is no cache in front of it, and the `verify` parameter is gone. The cache
it replaced saved 0.11 ms and cost an hour in which an erased simulator went on
reporting `cert_installed: true`. A reader may report the record *as* the
record -- `/proxy/cert/status` does, and says so -- but rendering it as current
fact is the defect. `cert_trust_stale` in `proxy_status` is how a contradicted
record is reported without the stored field disagreeing with the file.

`update_cert_state` merges per field: **omission preserves, naming overwrites
even with None.** So a caller writes what it actually learned, never a whole
`model_dump()` -- naming a field is how you clear it, and a verification that
dumps the model erases the fields it has no opinion about.

`auto_install_cert` in `~/.quern/config.json` answers the question once. It is
surfaced in `proxy_status` and in the menu-bar app's Settings pane on purpose:
a silent, persistent CA-install policy would be worse than the failure it
prevents. Anything other than a literal boolean reads as unset, on both sides
-- a typo should mean "ask me", never consent.

## Verifying, reviewing, and mutation testing

Three rules, each of which exists because skipping it cost a release cycle.

### Live-test before opening a PR

A green suite says the tests agree with the code. It does not say the thing
works. Every expensive find in the 0.18.0 cycle came from running the software,
and none of them would have been caught by any test that existed or that anyone
would have thought to write:

- The beta channel offered every tarball user a **downgrade** to a release three
  minor versions old, and pinned them there. Found by resolving the channel
  against the real GitHub API after the release was cut.
- WDA's `/source` took **7.52s** on an iPhone 11 against a 6s budget, and the
  response to a timeout is to destroy the runner — so one second of latency made
  a physical device unautomatable. Found by timing the call. Three plausible
  theories (USB re-enumeration, tunnel rotation, a broken WDA) were each
  disproved by measurement first.
- The `am start` failure markers were taken from an issue report and were
  **wrong**: the spelling they used never appears on Android 10, and one was
  unanchored enough to match a URL. Found by running it against a real device.

So: exercise the actual path on the actual hardware, with an isolated
`QUERN_STATE_DIR`, before the PR. Prove the failure exists before the fix and is
gone after. If that is impossible, say so in the PR rather than leaving the
reader to assume it was done.

### Mutation-test, and never restore with `git checkout`

A test that passes against the bug is worse than no test, and this repo ships
them regularly — several commits exist only to fix tests that were green against
the defect they named. Mutating the fix and watching the test fail is the only
cheap proof.

**Restore from a copy, not from git.** `git checkout -- <path>` restores to HEAD
and silently deletes uncommitted work in that path. It happened twice in one
session, each time destroying a fix that had just been written:

```sh
tmp=$(mktemp -d); git archive --format=tar HEAD | tar -x -C "$tmp"
# run the suite from inside $tmp with this checkout's .venv/bin/python
```

Clear `__pycache__` between mutations: a same-size swap can leave stale bytecode,
and the false result reads as "my fix does not work" while the source in front of
you says otherwise.

### Agent review runs in a worktree

Launch any review agent that mutates code with `isolation: "worktree"`. A shared
tree means two mutating agents cannot run at once, a mutating agent invalidates
anything else being measured at the same time, and the agent is handed
`git checkout` on a tree that may hold uncommitted work. Warning it off in the
prompt is a workaround for shared state, not a safeguard.

A worktree has no `.venv`, and does not need one: run with cwd inside it and the
main checkout's venv imports `server` from the worktree, because cwd precedes
site-packages on `sys.path`. Measured — 103 tests in 1.9s, and the TS-parsing
tests work because they read `mcp/src/*.ts` rather than `node_modules`.

It isolates **source only**. `~/.quern`, `~/.local/bin/quern`, MCP registrations
and the Swift build cache under `CONFIG_DIR/bin` are shared from every checkout,
so an agent that runs the app rather than the tests still needs
`QUERN_STATE_DIR` and usually a sandboxed `HOME`.

`QUERN_STATE_DIR` redirects `~/.quern` and **not** `~/.mitmproxy`. The proxy CA
is shared from every checkout, which cuts both ways: live-testing capture from
a worktree works against an already-trusted simulator without installing a
second root CA, and a worktree cannot be assumed to have a CA of its own.

Nor does it isolate the **SwiftPM build lock**. A `swift test` that is killed
can leave `swift-test` and `swiftpm-testing-helper` processes holding the lock
on a shared `--scratch-path`, and the next run then blocks for its full 600s
timeout -- which looks exactly like the hang you were trying to fix. Before
believing a Swift hang:

```sh
pgrep -fl "swift-test|swiftpm-testing-helper"
```

**`git worktree remove` can half-succeed, and only its exit code says so.** It
deregisters the worktree, deletes most of the tree, and then refuses a
directory something else has written into -- a Finder `.DS_Store` is enough.
What is left is files with no git record of them: `git worktree list` shows the
worktree gone and `git worktree prune` finds nothing to do, because there is
nothing left to prune. Measured once at 85 files and 6.7MB. So check the
directory, not the registration:

```sh
git worktree remove --force "$d"; [ -d "$d" ] && echo "STILL THERE: $d"
```

Give reviewers the failure mode to hunt, not just the diff. The briefs that found
real defects named this repo's habit — tests that pass for the wrong reason — and
listed concrete recent examples to calibrate against.

### The primary checkout stays on `main`

`/Volumes/Home/jham/Dev/quern` is the venv host and the reference tree. Branch
work happens in worktrees -- there are usually a dozen -- and the primary is
not one of them. Several sessions share this machine, and whoever checks a
branch out there silently changes the ground under everyone else.

The reason is not tidiness. `~/.local/bin/quern` is
`exec <primary>/.venv/bin/python -m server "$@"`, and `-m` puts the *caller's*
cwd ahead of the editable install on `sys.path`. So resolution follows the
directory you are standing in:

```text
from /tmp                 -> <primary>/server/__init__.py
from ~/Dev/quern-hid      -> ~/Dev/quern-hid/server/__init__.py
```

Two things follow, and they pull in opposite directions.

**The primary is what `quern` means when you are not standing in a worktree**,
which is where a person invokes it from -- their own terminal, their own
daemon. A branch left checked out there is running on their machine, as the
command they type and the server they have up. That is the state to avoid.

**A worktree needs no exception for live-testing.** `cd <worktree> && quern
start -f` runs *that* worktree's server on the primary's venv, no venv of its
own. So "live-test before opening a PR" is satisfied without ever moving the
primary, which is the objection this rule otherwise invites.

**And the same command in two directories runs different code, with nothing
to say so.** `quern status` from a worktree and from `~` are not the same
program. That is this project's recurring shape -- working and broken look
identical -- so when a result surprises you, check where you are standing
before you believe it.

The corollary for anything that is *not* source: a worktree isolates the tree
and nothing else, so `~/.quern`, `~/.local/bin/quern` and the MCP
registrations are still shared. See the worktree notes above.

### The Linux job is a backstop, not a readiness signal

CI runs the suite on `ubuntu-latest` as well as macOS. Two things to know, and
the full reasoning is in the job's own comment in `.github/workflows/ci.yml`
and in [`docs/linux-support-plan.md`](docs/linux-support-plan.md).

**It catches tests that reach the real machine.** The first Linux run failed 44,
and 41 were one bug: `list_devices` caught `DeviceError` but not the `OSError` a
missing binary actually raises. On a Mac those tests shelled out to a real
`xcrun`, got a list their fake UDID was not in, and passed regardless — the
house habit, caught by a runner that simply does not have the tool. When this
job goes red it is usually right, and usually about a macOS assumption that has
just entered shared code. Fix the assumption. A platform skip is correct only
when the thing under test is genuinely macOS-only; `tests/test_release_source.py`
holds the one precedent and states the bar.

**Green does not mean quern runs on Linux.** Most of the suite mocks its
subprocesses, so a pass says the Python is portable, not the product. The server
has never been started on Linux. Do not cite a green matrix as evidence the port
works.

## Where the API is documented

Deliberately not restated here. [`docs/api-reference.md`](docs/api-reference.md)
is the reference, and `tests/test_readme_sync.py` checks it against the routes
and MCP tools actually registered — a hand-maintained summary in a third file
would drift silently, with nothing to catch it.
