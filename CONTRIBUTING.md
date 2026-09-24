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

- **Merging a PR: use `scripts/merge-pr.sh <number>`**, not `gh pr merge`. `.coderabbit.yaml` sets `auto_review.enabled: true` with `auto_incremental_review: false`, so a review fires when a PR is *opened* and never again — **pushing to an open PR does not trigger one**. That is deliberate: review runs are rate limited, and re-reviewing every push spends the budget on intermediate states nobody merges. The consequence is that an "0 unresolved threads" reading goes stale the moment you push, not because a new review contradicted it but because the code moved out from under it, and it still reads exactly like all-clear. The script refuses to merge unless the newest review is newer than the newest commit, and internally runs `pr-review-status.py --ask`, which requests the missing review when the head has moved past it. `--force` overrides deliberately. `scripts/pr-review-status.py` is the same check on its own; don't pipe it if you care about the exit code — `| sed` or `| tee` reports the pipe's status, not the script's, which reads as success.

  **When reviews are rate limited, it says so and stops.** CodeRabbit answers the request with "Review rate limited" within seconds. The script used to treat that as no answer, poll out its whole ten-minute timeout in silence, and report only "still awaiting review" — twice on #202 (#207). It now reports the limit and when it lifts, reads the summary comment first so a limit already on the page costs no comment, and does not ask again before that time. Note what a rate-limited head looks like: CodeRabbit still resolves the threads whose fixes it can see, so the PR shows zero open findings on a commit no review has read. #199 and #202 both merged in that state.

  **Do not plan against an interval — it is not a fixed number.** This was recorded here and elsewhere as one review per hour. The subscription was upgraded on 2026-09-23 and the window is now shorter and variable: measured that day, a refusal quoting 25 minutes, a banner on #275 quoting 39, two grants on #277 about eleven minutes apart, and a grant on #164 that arrived immediately. A refusal costs nothing, so retry rather than compute when a window *should* have reopened, and read the acknowledgement body — `Action not completed` is the refusal, and the walkthrough's "Review limit reached" banner is a stale edit that lies. What still holds is that the window is shared across PRs and across agents, so never ask on two at once, and ask the sessions working other open PRs before taking a slot.

  **A clean-looking PR page is not evidence of a review.** Three PRs opened during a limited window on 2026-09-23 showed `CLEAN` with zero unresolved threads and had never been reviewed at all — the auto-review-on-open was refused and nothing said so. The reliable check is the coverage marker, which must equal your head:

  ```sh
  gh api "repos/quern-dev/quern/issues/<N>/comments?per_page=100" --paginate \
    -q '.[] | select(.user.login=="coderabbitai[bot]") | .body' \
    | grep -oE 'coveredCommitId":"[a-f0-9]+' | tail -1
  ```

  If it does not match, ask for `@coderabbitai full review` rather than the plain form, which reviews forward from the last commit it saw. Measured: the plain form did cover a four-commit gap on #164. The case to distrust is a force-push, where the commit the last review covered no longer exists in the branch — re-read the marker afterwards rather than assuming.

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

**`HEAD` is the commit you are standing on, which on a fix branch is the
fix.** This line used to say only `HEAD`, and following it literally on
`fix/296-...` extracted the *fixed* source, ran the new tests against it, and
reported 5/5 green — read as "these tests do not detect the bug" when the truth
was "this copy does not contain the bug". It cost a round of disbelief before a
review agent reproduced the same false reading from the same line.

To mutate *against the pre-fix state*, take the branch point rather than
counting commits back:

```sh
base=$(git merge-base origin/main HEAD)
tmp=$(mktemp -d); git archive --format=tar "$base" | tar -x -C "$tmp"
```

`HEAD~1` is **not** it, except by luck. The first version of this very
paragraph said `HEAD~1`, and CodeRabbit pointed out on the PR carrying it that
the branch had two commits by then — so `HEAD~1` was the fix commit and the
advice reproduced the bug it was written to prevent. Third time the same
mistake was made in one change, which is the argument for a form that does not
depend on how many commits you happen to have.

So assert the copy is what you think before believing a green run — grep the
extract for a symbol the fix introduced and stop if it is there. A mutation
harness that cannot tell "the bug is absent" from "the test is blind" is the
same can't-fail check this file warns about, aimed at the check itself.

Clear `__pycache__` between mutations: a same-size swap can leave stale bytecode,
and the false result reads as "my fix does not work" while the source in front of
you says otherwise.

And note the archive root wins over cwd. `tests/__init__.py` exists, so pytest
puts the extract's root on `sys.path` ahead of the directory you launched from
— which is what makes this recipe work at all. It does **not** hold for a
script run by path: `python /abs/path/script.py` puts the *script's* directory
at `sys.path[0]`, and the editable install then resolves `server` to the
primary checkout rather than to the tree you meant.

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

**The media suite runs `--no-parallel`, and that is not caution.** CI runners
are VMs where VideoToolbox falls back to software encoding, and 72 tests at
once on three cores made every real-time deadline in the suite miss. The
failures read as corruption rather than contention -- the tell was a
diagnostic message naming a timeout, not the assertion that fired. Keep the
flag, and suspect contention before correctness when a timing test fails only
on CI.

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

That only helps the session doing the removing. To audit afterwards -- or when
someone else removed it and nobody saw the exit code -- the three checks
disagree, which is the point:

```sh
git worktree list                 # the registration
ls -A .claude/worktrees/          # the directory
ls -A .git/worktrees/             # git's admin dirs, one per live worktree
```

A `.DS_Store` in the *parent* is harmless; the hazard needs one inside the tree
being removed. Expect them either way -- Finder writes them wherever anyone
looks, so the trigger is ambient rather than unlucky.

**None of those find a `git archive` copy.** Mutation testing works from
extracted tarballs, which are not worktrees and never appear in any of the
three. A cleanup sweep that reports "none of the worktrees are mine" can be
true and incomplete at the same time; scratch copies want their own pass.

And keep the sweep narrow. Globbing `quern-*` under `/tmp` returns log files,
`QUERN_STATE_DIR` directories and stray markdown, which reads as a pile of
findings and is an artefact of the question. The one that answers it is "a
directory with `server/` and `tests/` and no registration".

`PYTHONPATH=$PWD` is redundant when running from inside a worktree: cwd already
precedes site-packages. Harmless, but it implies the import needs help it does
not, which is worth not teaching.

### Naming a worktree

```text
~/Dev/quern-<session>-<what>          <what> is the branch slug, or pr<N> for a review
~/Dev/quern-scratch/<session>-<what>/ mutation copies, which are not worktrees
```

`<session>` carries no hyphen of its own: `quern-dev69-...`, not
`quern-dev-69-...`, which reads as a double hyphen and leaves the field
ambiguous to parse. `ListAgents` reports the name *with* the hyphen, so this
needs saying or the two forms both get produced.

**The session prefix is the point, and its beneficiary is never the author.**
Nobody is unsure which trees are theirs; the prefix exists for whoever is doing
a sweep, who by definition is someone else. So "it reads fine without one" is
true of every tree taken individually and the aggregate is still unusable --
which is why it has to be a convention rather than each session's judgement.
The cost is a longer path for the owner; the benefit goes to everyone else.

That cost buys the removal of a specific, measured blockage. One session left
nine finished worktrees untouched because two were `locked` and they could not
tell whose sessions were live; another refused to delete five clean,
fully-merged trees for the same reason, and needed to be *told* one of them was
ours before removing it. Anonymous paths do not cause confusion, they make the
cautious choice the wrong one, and cleanup then never happens.

Git cannot answer it either: commits here carry no AI attribution, so every
commit is the same author. The path is the only place ownership can live.

**But the name is a creation-time hint, not a claim.** Worktrees outlive the
sessions that make them -- `quern-media-engine` was created by one session and
inherited by another -- and session names are not unique over time. Every one
seen here is `dev-` plus two hex digits (`dev-3a`, `dev-d6`, `dev-34`), which
is a space of 256: two sessions share a suffix at better than even odds by the
twentieth, and at 83% by the thirtieth. So a prefix can be confidently wrong
rather than merely stale, and by collision rather than by anyone reusing a
name deliberately. A stale prefix read as
current fact is this repo's "a trust record is not trust" rule arriving by a
new route. So:

- the **name** says whom to ask,
- `ListAgents` says whether that session is still there,
- `git log -1 --format='%cr' <branch>` (or the directory's mtime) says whether
  it is worth asking.

Never the name alone -- and note that a prefix match is a hint about *whom to
ask*, not proof of whose tree it is. The collision is concurrent as well as
historical: two live sessions can hold the same prefix at the same instant, so
a match may name either of them. `ListAgents` is built for this, appending a
`[ref]` when two rows share a name, which is as plain a statement as you could
want that the name alone does not identify anyone.

**Re-homing a tree you have adopted is explicitly
allowed** -- a stale prefix is not somebody's claim on a tree you are the one
using, and leaving it there to be polite is how it ages into the confusion the
prefix exists to remove.

**`<what>` is one field with two uses, not two rules.** A branch slug for
branch work, `pr<N>` for a review. Two naming rules would make a reader
classify a directory before they could parse it, and the classification is not
recoverable from the path. The *lifecycles* differ -- a review tree is
re-pointed with `checkout --detach` as the branch moves and is cleared when the
PR merges -- but that is not something the name has to carry.

**The number goes in at creation or never.** It is right when the issue
preceded the branch, which is common: #270 was filed, then branched, and the
number never churned. It is wrong to add later -- one branch here ran eight
days with no PR before becoming #164, so a path would have been absent, then
wrong, then correct -- and renaming breaks shell history for a handle people
already use. `gh pr list --head <branch>` derives it whenever it is wanted.
Numbers earn their place by matching how the work is actually discussed
("261, then 256"), not by saving a lookup.

**`~/Dev`, not `/tmp`.** Not because /tmp is being cleaned -- this machine has
no `periodic/daily/*clean-tmps` and eight days of uptime, and /tmp worktrees
here hold thousands of files intact. The reason is that its lifetime is not
ours to depend on, and the failure it would cause is a quiet one: a registered
worktree whose contents have gone still passes `worktree list` and `prune`, and
a suite run inside it reports on files that are no longer there. "103 tests
passed" from a tree missing half its suite is the house failure shape, arriving
as what looks like a git problem.

**Moving an existing worktree needs more than `git worktree move`.** That is
`rename(2)`, so it cannot cross filesystems -- and `/tmp` (`disk3s5` here) and
`~/Dev` (`disk7s1`) are different ones, which is precisely the migration this
section asks for:

```text
fatal: failed to move ... : Cross-device link
```

When the tree is clean and pushed -- check `git status --porcelain` is empty
and `git rev-list --count origin/<branch>..HEAD` is 0, because this discards
the tree rather than moving it:

```sh
git worktree remove --force "$src"
[ -d "$src" ] && rm -rf "$src"          # the half-success check, which does fire
git worktree add "$dst" "$branch"
```

With uncommitted work, `cp -r` and then `git worktree repair "$dst"` instead;
it preserves the tree. (Removals that left debris behind: twice in about ten.)

**Scratch copies need the parent directory, not a name.** Mutation testing
works from `git archive` extracts, which no worktree check will ever report --
so one `ls ~/Dev/quern-scratch/` is the only thing that covers the class. It
must be a sibling of the checkouts and never inside one, or a mutated copy
turns up in someone's `git status`. Delete them when the review ends: outside
/tmp they no longer expire on their own, and they run to ~12MB each.

A harness built on `mktemp -d` defeats this without meaning to: it lands in
`/var/folders`, so it is invisible to a sweep of `quern-scratch` *and* to one
of `/tmp` -- three places to look instead of one. Point it at the single place
with `TMPDIR=~/Dev/quern-scratch mktemp -d`.

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
