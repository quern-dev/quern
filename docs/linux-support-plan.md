# Linux Support Plan

> **Revised 2026-09-21.** This supersedes the 2026-05-11 draft (`d44ee1f`), which
> is still in git history. Three things changed: the target is now a **headless
> server** rather than a Linux desktop, **Windows is dropped** rather than
> deferred, and **both preview paths are dropped** rather than one. The original
> estimated 3–5 days against a codebase that has since taken 659 commits; the
> revalidated findings are below.

## Goal

Run Quern on a headless Linux server driving Android emulators and adb-attached
devices, for one or more agents. iOS features remain macOS-only — a platform
constraint, not a priority call: Xcode, simctl, CoreSimulator and CoreMediaIO
do not exist on Linux.

## Scope

**In:** server startup and daemon mode, the HTTP API, the MCP wrapper, Android
device and emulator management (adb, uiautomator2), logcat capture, the network
proxy, Android certificate install, screenshots, and a Linux install path.

**Out, and deleted rather than ported:**

| Dropped | Why |
|---|---|
| All iOS device backends | No Apple toolchain on Linux |
| `server/proxy/system_proxy.py` | Exists because iOS simulators share the host network stack. Android proxies per-device (`adb shell settings put global http_proxy`, or `10.0.2.2` on an emulator) — there is nothing for a host system proxy to do |
| `server/proxy/extension.py` (local capture) | A macOS System Extension. No Linux equivalent is needed for the same reason as above |
| `server/proxy/cert_preflight.py` | "Scoped to booted simulators on purpose" (`cert_preflight.py:9-13`) |
| `server/sources/oslog.py`, `syslog.py`, iOS half of `crash.py` | Apple log transports. logcat is the Android equivalent and already exists |
| `server/lifecycle/menubar.py` + `macos/` | A GUI app on a headless server |
| **Both preview paths** — `preview.py` (CoreMediaIO) *and* `scrcpy_preview.py` | scrcpy spawns an SDL window. There is no display. Screenshots remain the visual channel |
| **Windows** | See below |

### Why Windows is dropped, not deferred

The original draft called Windows a "separate effort, if ever." That is correct,
and the reason is worth recording because it also explains why *Linux* is cheap.

Windows fails on core primitives, not on features:

- `import fcntl` at `server/config.py:5` — the package will not import at all.
  Also `lifecycle/state.py:10`, `device/pool.py:6`, `proxy/cert_state.py:12`,
  `lifecycle/update_check.py:20`.
- `os.kill(pid, 0)` is used throughout as a liveness probe (`main.py:1047`,
  `lifecycle/ports.py:107`, `sources/proxy.py:224`). On Windows CPython maps
  `os.kill` to `TerminateProcess` — **the check kills the process.**
- `os.WNOHANG` (`daemon.py:129`), `signal.SIGKILL` (four sites),
  `os.killpg`/`os.getpgid` (`tool_probe.py:76`) — all `AttributeError`.
- `Path.chmod(0o600)` on the API key silently does nothing.

Supporting Windows means a process/locking/tempdir **abstraction layer**.
Supporting Linux means **feature gating**. Every primitive above works unchanged
on Linux. Dropping Windows is what keeps this plan small — so if Windows is ever
revisited, it should be scoped as its own project, not as a phase here.

## Platform detection

Add `server/platform.py`:

```python
import platform
import shutil

MACOS = platform.system() == "Darwin"
LINUX = platform.system() == "Linux"

HAS_SIMCTL = MACOS and shutil.which("xcrun") is not None
HAS_ADB = shutil.which("adb") is not None
```

There are currently only ~14 `platform.system()` / `sys.platform` checks in the
whole tree, 11 of them inside `setup.py` and `menubar.py`. This module is where
the rest belong.

**API and MCP surface stay identical across platforms.** All routers stay
mounted and all tools stay registered; iOS-only ones return a clear error on
Linux. The tool count should not vary by platform — docs and agent expectations
both depend on it being stable.

## The work

### 1. Gate and delete

Mechanical. Remove the modules in the Out table, gate the iOS routers behind
`HAS_SIMCTL`, and stop `DeviceController.__init__` (`controller.py:48-56`) from
unconditionally instantiating all seven iOS backends on every start.

Also drop the unconditional `_fix_developer_dir()` call at `main.py:933`, which
on Linux runs `xcrun simctl help`, then `xcode-select -p`, then globs a
nonexistent `/Applications` — three swallowed failures on every startup.

### 2. Fix the macOS-shaped defaults

Individually small, collectively the thing that makes Linux behave sanely.
Important but not hard.

| Site | Problem |
|---|---|
| `device/controller.py:368` | `_device_type()` defaults an unknown UDID to `SIMULATOR` — on a machine with no simulators |
| `device/controller.py:378` | `_require_simulator()` only tests `_is_physical`, so **Android UDIDs fall through to simctl**. Affects `erase_device`, `clear_app_data`, and all 8 app-state endpoints |
| `device/adb.py:302` | Emulator vs physical is decided by `serial.startswith("emulator-")`. A **remote emulator** reached over `adb connect` has serial `host:port` and is misclassified as physical — losing boot, shutdown, `adb emu` GPS, and the rootable-emulator cert path. Probe `ro.kernel.qemu` / `ro.boot.qemu` instead |
| `controller_ui.py:44`, `screenshot_timeline.py:16`, `api/device.py:82` | Hardcoded `/tmp/quern/...`. Use `tempfile.gettempdir()` and scope per instance — see Multi-agent below |
| `device/screenshots.py:102,205` | `/System/Library/Fonts/Helvetica.ttc` for annotated screenshots. Falls back to `load_default()`, so labels degrade rather than break |
| `proxy/system_proxy.py:68`, `setup.py:2228` | `route -n get default` → `ip route get 1.1.1.1` |
| `lifecycle/state.py:249,298` | `ifconfig` parsing → `psutil.net_if_addrs()` |
| `lifecycle/state.py:365,381` | `networksetup -getairportnetwork` for SSID → `nmcli` / `iwgetid`. Feeds the `wifi_proxy_stale` warning, which still matters for Wi-Fi-attached Android devices |
| `lifecycle/ports.py:31`, `sources/proxy.py:189` | `lsof -ti` / `ps` → `psutil`. `lsof` is often not installed on modern distros |
| `capture_env.py:183,232`, `setup.py:539,612,2594,2664` | PATH split on hardcoded `":"` rather than `os.pathsep` |
| `api/proxy_certs.py:727-761` | `scutil --nc list` VPN check runs ungated on every `/setup-guide` call |
| `device/tool_versions.py:456` | `_android_sdk_adb()` checks only the macOS SDK path — inconsistent with `adb.py:36-40`, which already knows `~/Android/Sdk` |

### 3. Headless specifics

- **`headless` is not exposed in MCP.** `adb.boot_emulator()` already takes it
  and appends `-no-window` (`adb.py:398-441`), and it is plumbed through
  `models.py:961` → `api/device.py:291`. But `boot_device` in
  `mcp/src/tools/device.ts:85-91` accepts only `udid` and `name` — so an agent
  driving through MCP cannot boot headless. On a box with no display that is the
  default path failing. One-line schema addition.
- **Remote adb is unmanaged.** No `adb connect` / `tcpip` / `ADB_SERVER_SOCKET`
  support anywhere. Externally-connected devices are picked up by
  `adb devices -l` and work, but nothing reconnects a dropped device or reports
  connection state. Decide whether that is in scope or an operator concern.
- **Network exposure is already the default.** `--host` defaults to `0.0.0.0`
  (`main.py:702`), with API-key auth on every path outside `PUBLIC_PATHS`
  (`auth.py:26`). This wants a deliberate decision rather than an inherited
  default: on a Mac it is a loopback daemon; on a server it is a network service
  granting full device control and traffic interception. Note CORS is pinned to
  localhost (`main.py:576`), so browser clients will not work remotely — MCP and
  curl are unaffected.
- **Setup must run with no tty.** `_can_prompt()` opens `/dev/tty`
  (`setup.py:354`). `-y` already exists and must be the documented path.

### 4. Linux setup path

`check_homebrew()` is a **hard halt** — `print_summary()` then `return 1`
(`setup.py:2694-2701`). Nothing else runs without brew, so this is the single
gate the whole install sits behind.

Of `run_setup()`'s 20 steps: 6 drop entirely, 5 need a different mechanism
(package manager, VPN check, Node install, shell rc detection, Python
remediation), and 9 carry over unchanged. The Android block
(`setup.py:3109-3161`) is ~50 lines and only needs its installer swapped —
`adb` is already detected but never installed.

Two smaller things: `_detect_shell_rc()` (`setup.py:437-457`) returns `None` on
Linux for an unrecognised `$SHELL`, silently dropping the PATH offer; and
`_home_is_on_external()` (`setup.py:286`) hardcodes `/Volumes/`.

**`install.sh` is not in this repo.** It lives in the sibling `quern.dev` repo at
`public/_install.sh`, reached by `tests/test_release_source.py:43` via
`git rev-parse --git-common-dir` and skipped in CI — the test's own comment calls
it "a real gap … the one fetcher no test here can reach." Port work needs that
repo checked out alongside.

## The proxy: control transfers, attribution does not

The proxy is the most portable subsystem here and simultaneously holds the one
gate that blocks the target deployment. Those are different halves and worth
keeping apart.

### What transfers unchanged

`mitmproxy` 12.2.3 declares `MacOS`, `POSIX` and `Microsoft :: Windows`; the
only Apple-bound piece is the separate `mitmproxy-macos` package behind local
capture, which we are dropping anyway. Linux additionally offers transparent
mode (iptables/nftables) and WireGuard mode, which macOS does not.

**Mock, intercept, replay and bypass have zero device coupling.** They operate
on mitmproxy flow objects and `flowfilter` patterns: mock at `addon.py:528-557`,
intercept at `addon.py:559-570` and `767-821`, bypass by `fnmatch` on hostname
at `addon.py:431-451`. `server/api/proxy_intercept.py` contains no reference to
a udid, serial, client IP or device type. The whole programmable surface works
on Android as-is.

The corollary matters for this target: because nothing is device-scoped, **a
mock applies to every device and to host traffic at once.** On a single-device
Mac that is invisible. On a headless box serving several agents it means one
agent's mock silently rewrites another's traffic.

### The gate: per-device flow attribution

`FlowStore._filter` (`proxy/flow_store.py:128-130`) offers exactly two
discriminators, and Android populates neither usefully:

- `simulator_udid` comes from a PID walk to a `launchd_sim` ancestor
  (`addon.py:145-191`), fed by process info that only exists in macOS
  local-capture mode. Android always emits `None`, so
  **`query_flows(simulator_udid=<serial>)` returns zero flows, always.**
- `client_ip` is the peer address (`addon.py:624-628`). Emulator traffic
  arrives through NAT via `10.0.2.2`, so **every emulator on a host presents as
  `127.0.0.1`** — indistinguishable from each other and from host traffic. The
  code already records this for simulators (`addon.py:497-499`); it holds for
  emulators and is undocumented.

Consequences: per-device capture sessions do not work for emulators
(`capture_session.py:23-26`), summaries cannot be filtered by device
(`summary.py:44-46`), and TLS-rejection reporting collapses every emulator into
one bucket (`sources/proxy.py:596-638`).

Physical Android devices on Wi-Fi do get distinct LAN IPs, so `client_ip`
attribution works there.

This blocks both target workflows. Agents doing realtime debug each need their
own device and their own flows; API scripts sharding tests across devices need
per-device traffic. Neither survives a shared flow store that cannot say which
emulator a request came from.

### The fix: one listener per device

Verified against the installed mitmproxy 12.2.3:

- `options.mode` is `Sequence[str]` — *"The proxy server type(s) to spawn. Can
  be passed multiple times."* One `mitmdump` can serve many listeners; no extra
  processes.
- `mitmproxy.connection.Client` has both `peername` and **`sockname`**, the
  local address the client connected to. Quern captures `peername` and never
  `sockname`.

So: allocate a port per device, spawn `--mode regular@<port>` per device, point
each emulator's `http_proxy` at its own port, and tag each flow by
`flow.client_conn.sockname[1]`. Physical devices keep working through
`client_ip`. `FlowRecord` needs a device field and the store a filter on it.

At the scale this targets — a handful of devices, one owner — that is a small
contiguous port range and a dict, not a port allocator. Size it accordingly.

This is also what makes **per-device mock and intercept scoping** possible,
since the owning device becomes known before the mock decision rather than
after it. That is the fix for the global-mock problem above, and it is a
prerequisite for parallel agents rather than a refinement.

### #259 already builds the frame this plugs into

Do not design an attribution model — **one exists**, on `feat/logging-trace-export`
(#259, mergeable and green at the time of writing). It supplies the hard part:

- `Ownership` (`trace.py:115-142`) as an explicit four-value enum —
  `OWNS` / `FOREIGN` / `UNKNOWN_WORK` / `UNSCOPED_ACTION` — with `owns()` as a
  predicate rather than three inline udid comparisons.
- **"Ambiguity is marked, never guessed."** Overlapping intervals yield a
  caveat, not a claim. This is what makes an unattributable Android flow *safe*
  today: it is reported as unknown rather than assigned to whoever was nearest.
- Staleness carried through `ip_to_udid` (`trace.py:157-198`), so an aged
  `client_ip` mapping is flagged rather than trusted.
- 698 lines of `test_trace.py` behind it.

The seam is a single function, `device_of` (`trace.py:207-219`):

```python
if flow.simulator_udid:                              # iOS local capture only
    return flow.simulator_udid, True
if flow.client_ip and flow.client_ip in ip_map:      # physical, Wi-Fi proxy
    return ip_map[flow.client_ip]
return None, False                                   # every Android emulator
```

An Android emulator has no `simulator_udid` and a `client_ip` of `127.0.0.1`,
which is never in `ip_map` — that map is built from recorded Wi-Fi proxy
configs. So it returns `(None, False)`. The module's own regime table
(`trace.py:16-19`) lists three regimes and has no Android row; the emulator
falls into the third, *"nothing; interval only"*.

**Note which regime Linux loses.** The only row that "tells apps apart" is
*simulator + local capture*, and `simulator_udid` comes from the macOS System
Extension. A headless Linux build cannot have it at all — so Linux needs the
port-based branch more than macOS does, not less.

Adding it is one branch in `device_of`, plus a `listener_port` on `FlowRecord`
populated from `sockname` in `_serialize_flow`:

```python
if flow.listener_port and flow.listener_port in port_map:
    return port_map[flow.listener_port], True
```

That branch is **exact rather than inferential** — the port is a property of the
connection, not a time-window guess — so it ranks with `simulator_udid` above
`client_ip`, not below it. Worth adding the Android row to that table at the
same time; its absence currently reads as an oversight rather than a known gap.

### Routing coupling to unpick

One mechanism is implemented, and it is wired oddly
(`proxy/cert_manager.py:389-395`):

- **Port `9101` is hardcoded**, not read from the adapter's listen port.
- **It is a side-effect of a successful system-cert install.** A non-rootable
  emulator raises first, so it gets no proxy configuration at all — not even
  for plaintext HTTP.
- **Physical Android devices are type-gated out**, though
  `settings put global http_proxy` works over USB on many of them.
- **There is no unset path.** The proxy setting persists across reboot while a
  tmpfs-mounted cert does not, so a rebooted device points at a proxy it no
  longer trusts and every HTTPS request fails, undetected.

### Certificates, given the debug-build assumption

Quern's implemented path installs to the **system** trust store —
`/system/etc/security/cacerts/` via remount, tmpfs overlay, or API-34 `nsenter`
APEX injection (`device/adb.py:559-697`). That needs `adb root`, hence Google
APIs images and not Google Play ones. It is the strongest option because it
works against any app without app changes.

Quern's target case is narrower and easier: the app under test is the owner's,
in development, and its debug build can be configured to trust the CA. That
removes root from the critical path and admits Google Play images and physical
unrooted devices:

1. **Bundle the CA in the debug build** — `<certificates src="@raw/...">` in
   `network_security_config.xml`. No device-side install at all, works on every
   configuration. Costs a rebuild when the CA rotates.
2. **`<certificates src="user">` plus a user-store install.** Quern has **no
   user-CA install path** today — there is no `INSTALL_CA_CERTIFICATE` intent
   anywhere in the tree, contrary to what the previous revision of this document
   claimed. Worth noting Quern could drive the Settings flow with its own
   uiautomator2 backend.
3. **System store**, as implemented, for the rootable-emulator case and for
   third-party apps nobody controls.

Routing is gated behind (3) today. Decoupling it is what makes (1) and (2)
usable, and it is a small change.

## Multi-agent is a dependency, not a nice-to-have

**Scope bound first, because it decides how much machinery this deserves: the
target is several agents belonging to one owner, working on one project, across
a handful of devices. It is not a click-farm host.** No tenant isolation, no
quotas, no per-user state directories, no dynamic device-pool leasing across
untrusting parties. "Shared" here means *concurrent*, not *multi-tenant* — and
the difference is roughly an afternoon against a subsystem.

With that bound, the concurrency problems below are still real, because they
bite at two agents, not at fifty.

`_active_udid` is a single value on the controller (`controller.py:81`),
persisted to a sidecar. `resolve_udid(explicit)` sets it, so with two agents it
is last-writer-wins: agent A can tap agent B's device and nothing says so. On a
developer's Mac that is an edge case. On a shared headless server it is the
normal case. This promotes **#254 (device sessions)** from a roadmap item to a
prerequisite for the deployment model.

`_active_udid` is a single value on the controller (`controller.py:81`),
persisted to a sidecar. `resolve_udid(explicit)` sets it, so with two agents it
is last-writer-wins: agent A can tap agent B's device and nothing says so. On a
developer's Mac that is an edge case. On a shared headless server it is the
normal case.

The related pieces: **#255** (shared ring buffer evicts silently, crash entries
share a budget with the firehose) has the same shape — a single global resource
with no per-consumer accounting. And the hardcoded `/tmp/quern/screenshots`
above is the filesystem instance of it.

None of this is Linux-specific work. It is work the Linux target makes
load-bearing, and it should be sequenced accordingly rather than discovered
during rollout.

## Feature parity is a separate track

Subtracting iOS leaves **~83 of 110 MCP tools** (44 Android-capable, 39
device-agnostic, 27 iOS-only). The gaps are real and already on the general
roadmap — listed here so the port is not blamed for them:

- **No crash reporting.** Zero occurrences of `tombstone`, `ANR`, or
  `bugreport` in `server/`. iOS has full crash triage; Android has none.
- **No build integration.** Zero occurrences of `gradle`. `build_and_install`
  is Xcode-only (`api/build_app.py:58-72`).
- **No app-state / plist equivalent** — 14 iOS-simulator-only tools, the whole
  checkpoint and feature-flag workflow.
- Open Android bugs: **#232** (sweep cannot reach a deep row at the default
  budget), **#78** (`open_url` reports success when no app handles the URL).
- **#245** already scopes Android profiling (perfetto, gfxinfo) as the
  counterpart to xctrace.

A Linux build ships whatever Android parity exists at the time. These do not
block the port; they determine how good the product is when it lands.

**The pattern behind the list is worth naming, because it predicts where the
next gap will be.** Features here are built for iOS first and extended to
Android afterwards — sometimes thoroughly, sometimes not. So the reliable place
to look for a gap is any feature whose iOS implementation came first, which is
nearly all of them. Three shapes recur:

- **Silently absent.** Crash reporting and Gradle have no Android code at all —
  not a stub, not an error, simply nothing to find.
- **Present but unguarded.** `_require_simulator` tests only `_is_physical`, so
  Android UDIDs fall through to `simctl` rather than hitting the clear refusal
  the guard exists to give (see §2).
- **Present but iOS-shaped.** Errors from the Android UI path are labelled
  `[idb]` (#186); `/proxy/cert/verify` routes Android through `_verify_simulator`
  and reports `status: "never_booted"` for a device whose cert is installed.

An Android-only build removes the iOS implementation that was masking each of
these, so it converts "works on my Mac" into the whole product surface. Budget
for finding more of them than this list names.

## Testing and CI

The original plan under-weighted this, and it was the main unknown. **It is now
measured, and the answer is far better than assumed** — see the result below.

The suite is **106 files, ~53,180 lines**. It has **no platform skip markers of
any kind** — the only markers in use are `asyncio`, `parametrize`,
`release_download`, `integration` and `usefixtures`. Until #261 it had **never
executed on a non-Darwin host**: `.github/workflows/ci.yml` ran `test` and
`menubar-build` on `macos-latest`, and only `mcp-build` on `ubuntu-latest`.

### The first Linux run (#261)

```
44 failed, 3362 passed, 3 skipped, 15 deselected in 77.35s
```

**Zero collection errors, and `pip install -e ".[dev]"` succeeded.** Nothing in
the tree fails to import on Linux, `fb-idb` and its grpcio/protobuf chain
install cleanly, and a 98.7% pass rate says the server really is close to
cross-platform for everything that is not an Apple tool. That materially
de-risks the port; the earlier "~90% cross-platform" claim was, if anything,
pessimistic.

The 44 failures are far more concentrated than a scattered-coupling scenario:

| File | Failures | Cause |
|---|---|---|
| `test_device_controller.py` | **39** | `FileNotFoundError: 'xcrun'` |
| `test_resolution_protocol.py` | 2 | same |
| `test_capture_env.py` | 1 | no `python3.9` on the runner |
| `test_release_source.py` | 1 | menubar fetcher never runs, so the asserted URL list is empty |
| `test_mcp_build.py` | 1 | expected failure at `npm run`, got it at `npm install` |

**41 of 44 are one cause** — a real `xcrun` subprocess — and 39 of those sit in
a single file.

### Green, and what that does and does not mean

Fixing that one cause took it to 3, and the remaining three were test-side
rather than product bugs. All of it landed together in #261:

- `list_devices` catches `OSError` alongside `DeviceError`, so a tool that is
  **absent** is handled by the branch that already said "unavailable".
- `test_capture_env` no longer dies on a missing interpreter before it can
  reach the `pytest.skip` written for that case — the same "ran and failed"
  vs "is not there" confusion as the source bug.
- `test_mcp_build` sets the install stamp explicitly ahead of the manifests.
  `needs_install` compares `>=`, so a checkout landing both in one timestamp
  tick read as stale. Timing, not platform.
- `test_release_source` gained the suite's **first platform skip**, for a
  menu-bar path that is macOS-only by construction.

**What the job now buys.** A regression backstop for shared code: the moment a
macOS assumption enters a shared path, CI says so on that commit rather than
during the port. It is also, incidentally, the only check that catches a test
reaching the real machine instead of mocking it — the 39 above were spawning a
real `xcrun` each and passing regardless.

**What it does not buy, and this matters.** Green does not mean quern runs on
Linux. Most of the suite mocks its subprocesses, so what is proven is that the
*Python* is portable, not the product. Every item in §1 and §2 of this document
is still true: `__init__` builds all seven iOS backends, `_fix_developer_dir()`
runs unconditionally at startup, `/tmp/quern/screenshots` is hardcoded, and the
network introspection is BSD. None of it is reachable from a unit test, and the
server has never been started on Linux — not once.

Treat green as a **good start**: a known-good baseline to port *from*, not
evidence the port is done. The next real step is standing up a Linux host and
running the thing.

### What that actually exposes

This is not Linux breakage. It is **`DeviceController.__init__` instantiating
all seven iOS backends unconditionally** (`controller.py:48-56`), surfacing
through tests that construct a real controller and let it shell out.

`CONTRIBUTING.md` already forbids this — *"Mock subprocesses — never call real
simctl/idb in tests."* The violation is invisible on macOS because `xcrun`
exists there, so the call succeeds and the test passes for the wrong reason.
**The Linux job is, incidentally, an unmocked-subprocess detector**, and worth
keeping for that alone. The runner's own teardown log makes the same point from
the other end: `Terminate orphan process: pid (2712) (adb)`.

So the gating work in §1 and the test fixes are the same work. Fix the
constructor and most of the 42 should follow.

Rough composition: ~35% iOS-specific (~18,800 lines), ~16% install/delivery,
~45% device-agnostic, and **~2.4% Android (~1,296 lines)** — against ~18,800 for
iOS. That asymmetry is the coverage risk in an Android-only build, independent
of which OS it runs on.

What already exists to build on: 8 sites monkeypatch the platform rather than
skip (`test_setup.py:121,131,656,674,693,701`, `test_menubar_cli.py:586`,
`test_service_health.py:352`), so the non-Darwin branches of `check_platform`,
`check_vpn` and `configure_crash_reporter_dialog` are exercised today.

This was the planned first move and it has now happened, as the non-blocking
`test-linux` job in #261. Keep it `continue-on-error` until the 44 are cleared,
then fold it into the `test` matrix as a real gate.

One caution from `CONTRIBUTING.md`: the suite has historically deleted the
developer's real `~/.local/bin/quern`. The backstops are autouse fixtures in
`tests/conftest.py:154,225,280,391`, and they only protect branches that contain
them. Treat a first Linux run as untrusted.

## Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| ~~Test suite has undiscovered Darwin coupling~~ | ~~High~~ → **Measured** | Low | Resolved by #261: 44 failures, 42 of one cause, 39 in one file. No longer a risk |
| Android parity gaps read as "the Linux build is broken" | Medium | High | Document the supported surface explicitly at launch |
| Two agents silently share a device | **High** on a shared server | High | #254 is a prerequisite, not a follow-up |
| Emulator flows are indistinguishable in the store | **Certain** with >1 emulator | **High** | Listener-per-device + `sockname` tagging. Gates both target workflows |
| One agent's mock rewrites another agent's traffic | **High** on a shared server | High | Falls out of the same fix — device known before the mock decision |
| Rebooted device keeps a proxy setting whose cert is gone | High | Medium | Clear the proxy on teardown; detect the missing cert rather than trusting the record |
| Remote emulators misclassified as physical | High if `adb connect` is used | Medium | Property probe instead of serial prefix |
| Network-exposed server with device control | Medium | High | Deliberate bind/firewall decision; do not inherit `0.0.0.0` |
| Android tools behave differently on Linux | Low | Medium | adb and uiautomator2 are well-tested cross-platform |
| Maintenance burden of two platforms | Medium | Medium | Capability flags + CI matrix |

## Sequencing

1. ~~**CI matrix**~~ — **done** (#261). 3362 pass, 44 fail, no collection errors.
2. **Platform module + gating** — `server/platform.py`, gate iOS routers, stop
   instantiating iOS backends, delete the Out table. This is also the fix for
   the 42 `xcrun` test failures, so the two are one job rather than two.
3. **Defaults** — the table in §2. Mostly independent, parallelisable.
4. **Headless** — MCP `headless` param, emulator classification, bind decision,
   `-y` setup path.
5. **Linux setup + install.sh** — needs the `quern.dev` repo alongside.
6. **Per-device flow attribution** — listener per device, `sockname` tagging,
   a device field on `FlowRecord`, and decoupling the proxy setting from the
   system-cert install.
7. **Multi-agent (#254)** — sequenced before shared deployment, not after.

Steps 2–5 are close to the original 3–5 day estimate; the drift added surface
(`vision_ocr.py`, `proxy/extension.py`, `lifecycle/menubar.py`,
`webinspector.py`, `_xcode.py`) but all of it falls in the delete column. Step 1
sizes step 2.

Steps 6 and 7 are the genuinely new work, and neither is Linux-specific — both
are latent on macOS today and merely invisible there, because one developer with
one device never collides with themselves. The headless target is what promotes
them from roadmap to gate. **6 is a hard prerequisite:** an agent doing realtime
debug needs its own flows, and an API script sharding tests across devices needs
per-device traffic; a shared store that cannot name the originating emulator
defeats both. It should be scheduled against the deployment model rather than
against the port.
