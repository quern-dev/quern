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

## Multi-agent is a dependency, not a nice-to-have

A headless server exists to be shared. That promotes **#254 (device sessions)**
from a roadmap item to a prerequisite for the deployment model.

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

## Testing and CI

This is the part the original plan under-weighted, and it is the main unknown.

The suite is **106 files, ~53,180 lines, 2,273 collected tests**. It has **no
platform skip markers of any kind** — the only markers in use are `asyncio`,
`parametrize`, `release_download`, `integration` and `usefixtures`. It has
**never executed on a non-Darwin host**: `.github/workflows/ci.yml` runs `test`
and `menubar-build` on `macos-latest`, and only `mcp-build` on `ubuntu-latest`.

Rough composition: ~35% iOS-specific (~18,800 lines), ~16% install/delivery,
~45% device-agnostic, and **~2.4% Android (~1,296 lines)** — against ~18,800 for
iOS. That asymmetry is the coverage risk in an Android-only build, independent
of which OS it runs on.

What already exists to build on: 8 sites monkeypatch the platform rather than
skip (`test_setup.py:121,131,656,674,693,701`, `test_menubar_cli.py:586`,
`test_service_health.py:352`), so the non-Darwin branches of `check_platform`,
`check_vpn` and `configure_crash_reporter_dialog` are exercised today.

**First move, before any code changes: add `ubuntu-latest` to the `test`
matrix.** It is a few hours and it converts every estimate in this document into
a concrete list of failures. Given ~45% of the suite is device-agnostic, a large
fraction should pass unchanged, and what breaks becomes the real scope document.

One caution from `CONTRIBUTING.md`: the suite has historically deleted the
developer's real `~/.local/bin/quern`. The backstops are autouse fixtures in
`tests/conftest.py:154,225,280,391`, and they only protect branches that contain
them. Treat a first Linux run as untrusted.

## Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Test suite has undiscovered Darwin coupling | **High** | Medium | CI matrix first, before code changes |
| Android parity gaps read as "the Linux build is broken" | Medium | High | Document the supported surface explicitly at launch |
| Two agents silently share a device | **High** on a shared server | High | #254 is a prerequisite, not a follow-up |
| Remote emulators misclassified as physical | High if `adb connect` is used | Medium | Property probe instead of serial prefix |
| Network-exposed server with device control | Medium | High | Deliberate bind/firewall decision; do not inherit `0.0.0.0` |
| Android tools behave differently on Linux | Low | Medium | adb and uiautomator2 are well-tested cross-platform |
| Maintenance burden of two platforms | Medium | Medium | Capability flags + CI matrix |

## Sequencing

1. **CI matrix** — add `ubuntu-latest`, collect failures. No code changes.
2. **Platform module + gating** — `server/platform.py`, gate iOS routers, stop
   instantiating iOS backends, delete the Out table.
3. **Defaults** — the table in §2. Mostly independent, parallelisable.
4. **Headless** — MCP `headless` param, emulator classification, bind decision,
   `-y` setup path.
5. **Linux setup + install.sh** — needs the `quern.dev` repo alongside.
6. **Multi-agent (#254)** — sequenced before shared deployment, not after.

Steps 2–5 are close to the original 3–5 day estimate; the drift added surface
(`vision_ocr.py`, `proxy/extension.py`, `lifecycle/menubar.py`,
`webinspector.py`, `_xcode.py`) but all of it falls in the delete column. Step 1
sizes step 2. Step 6 is the one that is genuinely new work, and it is not
Linux-specific.
