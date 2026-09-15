# Quern conformance suite

Live tests that exercise a **running** Quern server over its REST API, on
whatever hardware the machine actually has. Three jobs:

1. **Release gating** — run before a release, catch regressions the unit suite
   cannot see because it mocks every subprocess.
2. **Bug finding** — the unit suite asserts that Quern calls `simctl` correctly.
   This asserts that the result is right.
3. **Portability** — the same suite, unchanged, on a different desk with
   different phones.

It is separate from, not a replacement for, `tests/test_*.py`. Those mock every
external call by design (`CONTRIBUTING.md`: *"Mock subprocesses — never call real
simctl/idb in tests"*). This directory is where the opposite rule applies, which
is why it is fenced off behind a marker.

## Running it

```shell
# Everything the machine can do, without touching host state:
.venv/bin/python -m pytest tests/conformance -m integration

# One category:
.venv/bin/python -m pytest tests/conformance/test_proxy_mocks.py -m integration

# Add the physical-device tier (needs a phone attached and unlocked):
QUERN_CONFORMANCE_PHYSICAL=1 .venv/bin/python -m pytest tests/conformance -m integration

# Add the destructive tier — changes system proxy settings, certificates,
# update channel. Read `UNSAFE_TO_PROBE` and the marker list before using it.
QUERN_CONFORMANCE_DESTRUCTIVE=1 .venv/bin/python -m pytest tests/conformance -m integration
```

A plain `pytest` run does **not** collect these: `pyproject.toml` sets
`addopts = "-m 'not integration'"`, and `pytest_collection_modifyitems` stamps
`integration` onto everything in this directory automatically, so a new test
cannot leak into the unit suite by forgetting a decorator.

### Pointing at a different server

| Variable | Effect |
|---|---|
| `QUERN_SERVER_URL` | Target another host or port. Default: `~/.quern/state.json`, else `http://127.0.0.1:9100` |
| `QUERN_API_KEY` | Override the key. Default: `~/.quern/api-key` |
| `QUERN_CONFORMANCE_PHYSICAL` | Enable physical-device tests |
| `QUERN_CONFORMANCE_DESTRUCTIVE` | Enable host-mutating tests |

Resolution is read from the **real** home directory on purpose. The root
`tests/conftest.py` repoints `QUERN_STATE_DIR` at a temp directory before any
test module loads, so anything importing `server.config` would read an API key no
server has ever issued.

## How it stays portable

No test names a device. Every run begins with one discovery pass
(`capabilities.py`) that asks the server what tools it has and what is attached,
maps the answer onto four **roles**, and prints a block like:

```text
Quern conformance environment
  server    http://127.0.0.1:9100  (v0.17.0, via default + ~/.quern/api-key)
  auth      ok
  tools     adb, devicectl, idb, pymobiledevice3, simctl, tunneld
  missing   sim_bridge
  ios_simulator     21 available (using iPhone 17 Pro / iOS 26.5 / shutdown […])
  ios_device        3 available (using J iPad Air M2 / iOS 26.6.2 / booted […])
  android_emulator  3 available (using Medium_Phone_API_36.1 / 16 / shutdown […])
  android_device    1 available (using Pixel 3 XL / 10 / booted […])
```

A test asks for `ios_simulator` or `any_android` and gets whatever this machine
has; if there is none, it skips with the reason attached.

**Absent is not the same as broken.** A missing `adb` is a machine that was never
going to run those tests. An `adb` that is present and hangs is a finding, and
the report marks it `!!` rather than `—`. This is not a hypothetical distinction:
see F1 in [`FINDINGS.md`](FINDINGS.md), where `/tools` — the endpoint behind
`quern doctor` — stopped responding entirely for the duration of an Xcode
first-launch, and a naive probe would have recorded "no simulators on this
machine."

A run where *every* role is unavailable fails rather than passes. A conformance
suite that skips everything has not passed; it has not run.

## Safety

- The default tier touches no host state. The root conftest's machine-mutation
  guard is re-run over every non-destructive test in this directory, so a test
  that changes `~/.quern` or a shell profile fails and names itself.
- Fixtures restore what they change, by difference rather than by clearing —
  `bypass_sandbox` removes only the patterns it added, so a developer's own
  bypass list survives the run.
- The auth sweep calls every endpoint unauthenticated, which would be dangerous
  if it found a hole. It sends a JSON array as the body so Pydantic rejects it
  before any handler runs, and the handful of endpoints that take no body and do
  something irreversible are listed in `UNSAFE_TO_PROBE` and covered
  schema-side instead.

## Categories

Status as of the current branch.

| # | Category | Module | State |
|---|---|---|---|
| 1 | Discovery & device mapping | `test_00_environment.py` | **done** — 9 tests |
| 2 | Authentication & public surface | `test_auth.py` | **done** — 7 tests, sweeps all 122 protected operations |
| 3 | Mocks & bypass list | `test_proxy_mocks.py` | **done** — 23 tests, found F3 |
| 4 | Logs: query, summary, cursors | `test_logs.py` | **done** — 20 tests, found F4 |
| 5 | System, update channel, doctor | `test_system.py` | todo |
| 6 | Proxy status, flows, capture sessions | `test_proxy_flows.py` | todo |
| 7 | Intercept & replay | `test_intercept.py` | todo |
| 8 | Device pool: resolve, ensure, active | `test_device_pool.py` | todo |
| 9 | Device lifecycle: boot, shutdown, erase | `test_device_lifecycle.py` | todo (erase is destructive) |
| 10 | App install / launch / terminate / list | `test_apps.py` | todo — needs fixture app |
| 11 | UI: tree, tap, type, swipe, scroll, wait | `test_ui.py` | todo — needs fixture app |
| 12 | Screenshots, annotation, timeline | `test_screenshots.py` | todo |
| 13 | App state checkpoints & plist | `test_app_state.py` | todo — needs fixture app |
| 14 | Landmarks & screen identification | `test_landmarks.py` | todo — needs fixture app |
| 15 | Device configuration (locale, font, density, GPS) | `test_device_config.py` | todo |
| 16 | Certificates & trust | `test_certs.py` | todo — destructive |
| 17 | Builds & crash parsing | `test_builds.py` | todo |
| 18 | Preview & video streaming | `test_preview.py` | todo |
| 19 | WDA lifecycle | `test_wda.py` | todo — physical tier |
| 20 | Android-specific backends | `test_android.py` | todo |

### What blocks the rest

Categories 10, 11, 13 and 14 need an app whose labels, plist keys and log output
are known and stable. Driving Settings or Safari instead makes the assertions
fuzzy and ages them against every OS release. Two fixture apps are the agreed
approach — one SwiftUI, one Kotlin, mirrored so a single test body can assert the
same behaviour across both backends. They do not exist yet; that is the next
piece of work.

## Findings

[`FINDINGS.md`](FINDINGS.md). Bugs are recorded there, not fixed on this branch —
the branch builds the suite, and interleaving fixes would make the eventual PR
unreviewable. A test that currently fails because it found a real bug is left
failing and cross-referenced, rather than being marked `xfail`: a release run
should report a known bug as a failure, because it is one.

## Conventions

- **Assert relationships, not snapshots.** The machine's state is not under the
  suite's control. `total` agreeing with `has_more`, a filter returning a subset,
  two endpoints reporting the same count — those hold whatever is in the buffer.
- **Never pass vacuously.** A test that would be trivially true on empty data
  skips and says why. `logs_present` is the pattern.
- **Don't import the server's own constants to assert against.** `LEVELS_BY_SEVERITY`
  in `test_logs.py` is duplicated deliberately — importing `LogLevel` would make
  the test agree with the implementation by construction, including when the
  implementation is wrong.
- **A failing test is a claim.** Check it against the source before recording a
  finding. The first version of the level-filter test asserted exact-match
  semantics and was simply wrong about the contract; see F4.
