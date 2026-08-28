# Changelog

All notable changes to Quern are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **`save_app_state` / `restore_app_state` can now capture the simulator keychain, making logged-in checkpoints actually work** — auth tokens live in the simulator keychain at `<device>/data/Library/Keychains/`, which sits outside every app container, so a checkpoint of containers alone *always* restored to a logged-out app no matter what was logged in when it was taken. Checkpoints labelled `logged_in` were silently aspirational: restore + launch landed on the login screen, and the app's own `keychainReady`-style flags came back saying credentials existed when they no longer did, so the failure read as a confusing auth bug rather than an obvious missing-state one. New `include_keychain` on both endpoints copies `keychain-2*.db*` alongside the containers; `restore_app_state` restores it automatically whenever the checkpoint carries one. Measured on iPhone 16 Pro / iOS 18.6: containers-only restore → login screen; keychain-only restore onto a signed-out container → login screen; both together → app comes up authenticated (profile populated, server-backed tabs load). Both halves must come from the same captured moment.
- Response metadata now carries a `keychain` block (`captured`/`restored` plus filenames), and restoring a checkpoint that has no keychain logs a warning naming the consequence, so the "why am I logged out" case is self-explaining instead of silent.

### Fixed
- **`get_data_container` no longer fails on a shut-down simulator** — `simctl get_app_container` errors with `Unable to lookup in current state: Shutdown`, but keychain capture *requires* the device to be shut down (it is a WAL-mode SQLite database held open by `securityd`; copying it while booted yields a torn snapshot, and writing it beneath a running `securityd` is ignored). Those two constraints were in direct contradiction and would have forced callers into a shutdown/boot/shutdown dance. Container lookup now falls back to scanning `Containers/Data/Application/*/.com.apple.mobile_container_manager.metadata.plist` for a matching `MCMMetadataIdentifier` — the same technique `get_app_groups` already used — so a single call can save or restore containers and keychain together against a powered-off device.

### Notes
- The keychain preconditions are checked *before* any container is wiped, so calling either endpoint against a booted device fails cleanly with the exact `xcrun simctl shutdown <udid>` command to run, rather than leaving a half-restored app.
- `include_keychain` defaults to false on save (unchanged behaviour, unchanged checkpoint layout). Existing keychain-less checkpoints keep working and now report `keychain.restored: false` with a reason.

## [0.13.4] - 2026-06-05

### Fixed
- **`proxy_status` / `proxy_setup_guide` now enumerate every active Mac interface** (#26) — when the Mac has more than one active interface on different subnets (Wi-Fi + Ethernet to different networks — the standard office dock setup), the previous `local_ip` field reflected only the OS default-route IP. That IP is the wrong answer to "what host should this physical device use as its proxy" for any device on the other interface's subnet, and the setup guide was telling users to disable the other interface. Both endpoints now return a `local_ips: list[InterfaceInfo]` field with one entry per interface — BSD device, IP, /24 subnet, `is_default_route` flag, and best-effort Wi-Fi SSID — plus a `warnings: ["multi_interface_active"]` flag when more than one /24 is in play. `setup_guide`'s physical-device step 5 now lists every Mac IP with its interface (and SSID when applicable) so the user can pick the one on the same subnet as the device, instead of advertising a single (possibly wrong) host. Loopback (127.0.0.0/8) and link-local autoconfig (169.254.0.0/16) are filtered so an Ethernet interface that never got a DHCP lease doesn't trip the warning spuriously. The underlying `_get_all_interface_ips()` helper already powered `record_device_proxy_config`'s subnet-aware path; this just brings the advisory endpoints in line.
- **MCP server gives a clear "Node too old" message instead of `SyntaxError: Unexpected token {`** (#34) — when Claude Desktop (or any other client) launches the MCP server with an old Node binary — common because Desktop enumerates `~/.nvm/versions/node/*/bin` into PATH and picks the lowest version first, which is often `v10.x` — parsing failed at the first ESM `import` with a cryptic syntax error and no mention of the actual problem. New CJS launcher (`dist/launcher.cjs`) gates on Node major version before loading the ESM entry, prints an actionable error showing the running Node version, the binary path, and a fix path (set `command` to an absolute Node 22+ binary). Two subtleties worth recording: the launcher itself is CommonJS so it parses on Node < 14, and the dynamic `import()` is wrapped in `new Function(...)` so its syntax is parsed at invocation time rather than load time — necessary because dynamic `import()` is itself a parse error on Node < 12.17. `package.json` `main`/`bin` now point at the launcher; `dist/index.js` remains as a separately-callable entry. README's stale "Node.js 18+" prereq bumped to 22+.
- **No more spurious "install developer tools" dialog on Android-only Macs** (#37) — on a clean Mac with no Xcode and no Command Line Tools, Quern unconditionally invoked `xcrun simctl` / `xcrun devicectl` in five places (`SimctlBackend.is_available` / `.list_devices`, `DevicectlBackend.is_available` / `.list_devices`, three `check_*` functions in `quern setup`). The macOS kernel-level loader for `/usr/bin/xcrun` pops the install dialog *before* the subprocess returns — Quern's `try/except DeviceError` blocks catch the eventual error, but the dialog has already fired and demanded the user's attention. The first-time experience for an Android dev was an install prompt during `./quern setup`, again on `quern start` (from the device-warmup task), and again on their first `list_devices` call even when the call asked for `type=android_emulator`. New `xcode_available()` preflight (lru_cached, sync) probes via `xcode-select -p` (which never triggers the dialog, unlike `xcrun`) and gates all five sites. `check_xcode_cli_tools()` now reports a clear "Not installed (no developer directory configured) — if you only need Android, this is safe to ignore" instead of producing the dialog itself.

### Added
- **QuernProbe** (`tools/probe-app/`, see #33) — a no-Xcode UIKit playground app built straight from `swiftc` into a hand-assembled `.app` bundle (same compile-on-demand pattern as `sim-bridge.swift`). Four tabs (text input with keyboard-type matrix, CoreLocation readout, switch/slider/segment/stepper, 200-row scroll list), every element carrying a stable accessibility identifier, nothing touching the network. Includes a `selftest.py` that drives the app through the REST API by identifier only. Verifying Quern features against Safari/Settings was fragile across iOS releases — address-bar focus, autofill prompts, web-view hit testing all shift. The probe app is the canonical regression target going forward and was the controlled fixture that pinned down both the 0.13.3 shift-drop root cause and the stale HID client bug.

## [0.13.3] - 2026-06-05

### Fixed
- **Shift-drop bug on synthetic typing eliminated** — `type_text` was silently dropping shifted/uppercase characters on simulators (mixed-case usernames typed as lowercase, exclamation marks typed as `1`). Deterministic on fresh-boot iOS 17.5, intermittent on iOS 26.5 under parallel-fleet load. Root cause: `backboardd` only honors modifier state from synthetic keyboard events while CoreSimulator's `hardwareKeyboardEnabled` is `true`. On a freshly booted simulator that flag defaults to `false`, so every shifted character lost its modifier — regardless of HID message class, delays, or priming. `doTypeText` now attaches the hardware keyboard before sending key events, matching the call Simulator.app makes. (This is why GUI-touched simulators typed correctly while untouched ones mangled — Simulator.app and the old `osascript Cmd+V` workaround were both attaching the keyboard as a side effect of focus.)
- **Switched typing to dedicated keyboard HID messages** — `IndigoHIDMessageForKeyboardArbitrary` (keys) and `IndigoHIDMessageForModifierKeyBit` (modifiers) replace the generic `IndigoHIDMessageForHIDArbitrary` path, which `backboardd` was dropping on freshly-booted sims even under hardware-keyboard mode. Same message class Simulator.app's Mac-keyboard passthrough uses; the previous generic-HID path is retained as a fallback when the keyboard symbols can't be resolved.
- **Stale `SimDeviceLegacyHIDClient` after a device reboot is now detected and recreated** — the cached HID client held a connection to the boot session it was created under. After a device reboot the mach port was dead and every subsequent tap/type was silently dropped (the prior `sendHIDMessage` call passed `completion: nil`, so nothing noticed). `ensureHIDClient` now probes the cached client with a harmless message before returning it; on `machPortInvalid` (or timeout) the client is dropped and recreated. New `sendHIDMessageChecked` waits up to 1s for delivery confirmation.
- **Keyboard service primed at start of each type command** — `backboardd` creates the keyboard HID service lazily on the first KEY event, and modifier-bit messages sent before that were silently discarded. The first shifted character typed by a fresh client lost its shift (`First_A!` → `first_A!`). A bare shift keypress now creates the service without producing text. The prime lives in the typing path, not in client creation, because keyboard events flip the simulator into hardware-keyboard mode (hiding the soft keyboard) which must not happen as a side effect of taps.
- **Oversized sim-bridge stdout lines no longer kill the reader** — the subprocess was created without a stream limit, inheriting asyncio's 64 KiB default. A `describe-ui` response for a dense screen (~120 KiB / 436 elements) raised `LimitOverrunError`, the reader task died, and every UI endpoint returned 500 until the app left that screen. Now passes `limit=STREAM_LIMIT` (32 MiB) to `create_subprocess_exec`.
- **Reader death no longer leaks sim-bridge subprocesses** — the reader's cleanup path dropped the process reference without killing the (still healthy) subprocess. Each respawn left a zombie behind (nine concurrent sim-bridge processes observed after a session of crash loops, all holding competing HID clients). The reader's `finally` block now kills the process before cleanup.
- **sim-bridge stderr is now drained** — the Swift helper logs `[PERF]` / `[ax]` / `[hid]` diagnostics to stderr; once the 64 KiB pipe buffer filled, the subprocess blocked mid-write and every command timed out. A new drain task forwards stderr lines to debug logging.

### Added
- **`set_hardware_keyboard` MCP tool / `POST /api/v1/device/keyboard` endpoint** — exposes CoreSimulator's `-[SimDevice setHardwareKeyboardEnabled:keyboardType:error:]` (the same call behind Simulator.app's "Connect Hardware Keyboard" / shift-cmd-K toggle). Use cases: restore the software keyboard after `type_text` when a later step asserts keyboard visibility; suppress the software keyboard during form filling for smaller UI trees and unobstructed screenshots; exercise hardware-keyboard layouts. Simulator-only, requires the sim-bridge backend.

### Notes
- **Side effect**: typing now attaches the hardware keyboard, which causes the software keyboard to hide. The `type_text` tool description documents this with a pointer to `set_hardware_keyboard enabled=false` for restoring the software keyboard.
- The probe app under `tools/probe-app/` (added this cycle, see #33) was the controlled fixture that isolated both the shift-drop root cause and the stale HID client bug.

## [0.13.2] - 2026-05-28

### Fixed
- **SwiftUI toolbar items on pushed nav controllers are now discovered** — when a SwiftUI view in a `UIHostingController` is pushed onto a `UINavigationController`, the nav-bar accessibility container exposes as a plain `AXGroup` with an empty `role_description`, an identifier set to the screen's nav title, and no enumerated children. Its `.topBarTrailing` / `.topBarLeading` toolbar buttons never appeared in `describe_all` output, even with an explicit `.accessibilityIdentifier(...)`. `is_probeable_container` now also probes top-of-screen `AXGroup`s (y < 120pt, height ≤ 80pt) that carry an identifier (`identifier` or `AXUniqueId`) and have no children — the SwiftUI pushed-nav-bar signature — so `find_empty_containers` hit-tests them and surfaces the toolbar items. Verified live against an iOS 26.5 simulator (Geocaching app): the Profile screen's gear icon, Friends button, and a list's "+" toolbar item all surface without coordinate-based fallbacks. Adds 5 unit tests covering positive (`identifier` / `AXUniqueId`) and negative (no identifier, below the nav-bar zone, too tall) cases.

## [0.13.1] - 2026-05-28

### Changed
- **tunneld log path moved to `/Library/Logs/com.quern.tunneld.log`** — previously the LaunchDaemon plist baked `~/.quern/tunneld.log` (resolved to an absolute path at install time) into `StandardOutPath` / `StandardErrorPath`. For users whose home directory lived on an external volume, launchd would pre-create the home-directory path at boot before the volume mounted, blocking the real volume from mounting under its expected name (it would mount as `Home 1` instead, breaking login). The daemon now logs to a system-owned location that always exists at boot and never touches the user home.

### Fixed
- **`./quern tunneld install` correctly upgrades an already-loaded daemon** — the previous flow ran `launchctl bootstrap` against a loaded service (returns EIO 5) and fell back to `kickstart -k`, which sent SIGKILL to the running process and hung launchctl on macOS 15. Install now does `bootout → cp → chown → chmod 644 → bootstrap`, matching Apple's documented plist-swap dance.
- **macOS 26 (Tahoe) launchd race handled** — bootstrap immediately after bootout fails with `Bootstrap failed: 5: Input/output error` on Tahoe (launchd needs a moment to fully release the unloaded service before accepting a new bootstrap). `install_daemon` now sleeps 1.5s after bootout and retries once with a longer delay if the first bootstrap still races.
- **Plist permissions normalized to `644`** — `NamedTemporaryFile` produces mode `600`, which `cp` preserves. `600` worked but isn't Apple-recommended; install now `chmod 644`s after copy.
- **Hung launchctl no longer crashes the install script** — `subprocess.TimeoutExpired` and `OSError` are caught with a warning instead of propagating as an unhandled traceback.

### Added
- **`./quern setup` detects when `$HOME` is on an external volume** (e.g. `/Volumes/Home/<user>`) and prefers `sudo pipx install --global pymobiledevice3` over the per-user install. The default user-pipx path puts the binary under `/Volumes/<vol>/<user>/.local/pipx/`, which isn't reachable at boot before the volume mounts — meaning the tunneld LaunchDaemon can't start until login completes. `--global` lands the venv under `/opt/pipx/venvs/pymobiledevice3/` on the internal disk, where it's always available. The new `pipx_global` manifest category is also tracked for `./quern uninstall` (which removes those entries with `sudo pipx uninstall --global`).
- **`check_pymobiledevice3()` flags existing installs that live under an external home volume** and points at the global-install fix. Affects users who installed via per-user pipx before this release; setup will prompt to reinstall system-wide on the next run.
- **Post-global-install cleanup prompt** — after `sudo pipx install --global pymobiledevice3` succeeds, setup detects if a per-user pipx copy of pymobiledevice3 also exists and offers to remove it. pipx's `ensurepath` puts `~/.local/bin` ahead of `/usr/local/bin` in user shells, so leaving the per-user copy in place keeps it shadowing the system-wide install for any tool that calls `shutil.which` — including `check_pymobiledevice3()`, which would otherwise keep reporting the misplaced install on every setup run.
- **`installed_plist_is_current()` also checks the binary path** — previously only the log path was compared. Now also verifies that `ProgramArguments[0]` matches `find_pymobiledevice3_binary()`, so the existing migration prompt fires when the plist bakes in a stale per-user pipx path while a global install has replaced it.

### Migration
- Existing installs are detected automatically. `./quern setup`, `./quern status`, `./quern start`, and `./quern tunneld status` all surface a warning when the installed plist still references the old user-home log path.
- To migrate: run `./quern tunneld install` (or accept the prompt in `./quern setup`). The command unconditionally overwrites the plist, sets root ownership, and reloads the daemon.
- The orphaned old log file at `~/.quern/tunneld.log` is left in place — delete manually if desired.
- **Home-on-external users**: after accepting the global pymobiledevice3 reinstall, run `pipx uninstall pymobiledevice3` (your old per-user copy) and `./quern tunneld install` once more to rebake the plist against `/usr/local/bin/pymobiledevice3`.

## [0.13.0] - 2026-05-13

### Added
- **sim-bridge — native simulator control** — a new Swift helper binary that replaces idb for simulator UI automation on Xcode 26+ Apple Silicon hosts. Talks to `CoreSimulator` / `SimulatorKit` / `AccessibilityPlatformTranslation` directly via `dlopen`, runs as a long-lived subprocess over JSON-Lines on stdin/stdout (same pattern as `ios-preview`), and auto-compiles on first use. Removes the `idb_companion` daemon, the per-call subprocess spawn, and the simctl detour for screenshots. idb stays as the automatic fallback when sim-bridge can't be built (Intel Macs, pre-Xcode-26). See [`docs/sim-bridge-spec.md`](docs/sim-bridge-spec.md).
- **Server-side `objectAtPoint` hit-test in sim-bridge** — calls `AXPTranslator`'s 3-arg `objectAtPoint:displayId:bridgeDelegateToken:` to resolve the deepest accessibility element under a given point. Used to drive group-children probing: SwiftUI tab bars / nav bars / toolbars routinely enumerate as childless even though they clearly contain interactive subviews (the bug behind facebook/idb#767 and Quern's patched-companion). Sim-bridge now grids the interior of each empty container with hit-tests at 20pt intervals and merges the discoveries into the flat element list, matching the behavior agents already got from the patched idb companion.
- **Shared probing module** (`server/device/probing.py`) — the empty-container detection, hit-test grid, dedup-by-frame, and merge-into-flat logic is now backend-agnostic. Both `IdbBackend` and `SimBridgeBackend` call into it with their own `describe_point` callable; future backends (e.g. a remote sim host) plug in the same way.
- **RadioButton and CheckBox in screen summaries** — iOS tab-bar items expose as `AXRadioButton` with `role_description="AXTabButton"`. They were landing in the parsed element list with the right labels and identifiers but `get_screen_summary` was dropping them because `_INTERACTIVE_TYPES` only covered Button / TextField / Switch / Slider / Link / SearchField. They now show up in `interactive_elements` (with their `value` field reflecting which tab is selected) and route to `navigation_chrome` so they're never truncated.

### Changed
- **Setup skips idb install on Xcode 26+ Apple Silicon** — `quern setup` detects when sim-bridge will be used and short-circuits the two idb prompts (`idb_companion` and `fb-idb`) with a SKIPPED status and a one-line note instead of installing them. Existing idb installs are left alone (they still work as a fallback). Older Xcode or Intel hosts keep the existing idb install flow unchanged.
- **`get_screen_summary` and `tap_element` no longer say "Requires idb."** — the MCP tool descriptions used to advertise an idb requirement that hasn't been true since `WdaBackend` (physical iOS) and `U2Backend` (Android) landed. With sim-bridge added on top, the strings were actively misleading. Descriptions now describe the tool itself and let the server pick the right backend per-device.

### Fixed
- **`press_button` accepts idb's name set** — `sim-bridge`'s `doButton` was matching lowercase Swift names only (`home` / `lock` / `volumeUp` / `volumeDown`) while the MCP tool contract uses idb's uppercase set (`HOME`, `LOCK`, `SIDE_BUTTON`, `SIRI`, `APPLE_PAY`). Names are now normalized (lowercase, strip `_`/`-`) and the full set is wired through: `HOME` / `LOCK` / `SIDE_BUTTON` press the indigo button, `SIRI` is long-press side, `APPLE_PAY` is double-press side.
- **`clear_text` actually clears the field on sim-bridge** — the select-all + delete path sends `\x08` through the type-text command, but the character-to-HID-usage map had no entry for ASCII backspace or DEL, so the keypress was silently dropped after the triple-tap selection. Both 0x08 and 0x7F now map to HID usage 0x2A (Keyboard Delete).

### Internal
- New `tests/test_sim_bridge.py` (7 tests) — describe_point hit / miss, describe_all probing of an empty tab bar, no-probe path when containers are full, dedup of probed-vs-existing frames, describe_all_nested round-trip.
- Verified end-to-end against Metatext on iOS 17.5 / 18.6 / 26.4 simulators: tap_element resolves the four tab buttons (`tab.timelines` / `tab.explore` / `tab.notifications` / `tab.messages`) and routes a real tap to each.

## [0.12.0] - 2026-04-28

### Added
- **Network change monitor** — a background poll (~15s cadence) detects shifts in the Mac's Wi-Fi SSID and outward-facing IP. Surfaced as `network_state` on the `proxy_status` response with `last_changed_at`, a `last_change_reason` (`ssid_changed` / `ip_changed_same_ssid` / `ssid_and_ip_changed`), and a small ring of recent changes. Combined with the existing per-device `wifi_proxy_stale` flag, an agent reading any routine status call now sees both *what just changed* and *which devices need their proxy reconfigured* — without anyone having to remember to ask after moving between networks.
- **`selected:` landmark field** — for tabs, switches, radios, and checkboxes, `selected: true` matches elements whose UI selection state is on. iOS's RadioButton-style tabs and Android's BottomNavigationView/TabLayout tabs are both handled (Android `selected="true"` now normalizes to `AXValue="1"` for parity with the iOS convention). Required for screens distinguished only by which tab is active.
- **`include_raw=true` on `get_ui_tree`** — opt-in flag that adds `extra_attrs` per element with the raw source attributes from the underlying provider (full uiautomator2 XML attribute set on Android). Lets agents debug the platform normalizer without dropping to `adb shell uiautomator dump`. Default off to keep payloads compact.
- **Active device sidecar** — the active UDID set via `resolve_device` now persists in `~/.quern/active-device.json` (separate from `state.json`, which is server-runtime data). Survives `quern stop` and stop/start cycles; the user no longer has to re-resolve their device after every restart.
- **`udid` parameter on `resolve_device` MCP tool** — the HTTP route already accepted it, but the MCP wrapper's Zod schema didn't expose it. Agents that already know which device they want (e.g., from `list_devices`) can now switch active device without round-tripping through name/os_version matching.
- **Categorized `skipped[]` on `load_landmarks`** — knowledge bases that pre-date the `landmarks:` schema (April 2026) used the older `identify_by:` field. The loader now returns these in a `skipped` array with reason codes (`legacy_format`, `no_landmarks`, `no_frontmatter`, `yaml_error`, `invalid_entries`). The `legacy_format` entries echo back the original `identify_by` data so an agent can propose a per-file migration with user review.
- **Migration agent skill** — new `quern-landmark-migration` skill walks an agent through migrating an `identify_by`-era knowledge base. Tiered batching (mechanical / schema-translation / prose) plus a live verification phase that catches drift the YAML rewrite can't see. Auto-installed via `quern setup`.
- **Per-landmark detail in `identify_screen` failure responses** — `partial_matches` now includes every evaluated non-fully-matched screen (including zero-match), with full per-landmark results so an agent can see which selectors hit and which missed without re-running identification. Sorted by descending match count so the best candidate appears first.
- **Pre-commit checklist hook** — `quern setup` installs a Claude Code `PreToolUse` hook into `~/.claude/settings.json` that surfaces a short checklist whenever an agent runs `git commit` in a project that uses Quern (signaled by a `.quern/knowledge/` directory at the project root). The reminder covers KB drift, landmark verification, identifier consistency, and other discipline that's easy to forget when committing app or KB changes. Stays silent in projects that don't use Quern. Re-installable standalone via `quern install-precommit-hook`.
- **Version visible in `quern start` and `quern status`** — daemon-mode banners now show `Quern v0.12.0 running` instead of just `Quern running`.

### Changed
- **`install_proxy_cert` filters physical devices** — the no-UDID batch path now only runs on simulators and Android emulators, both for the explicit `udid` case (returns 400 with manual-install guidance) and the no-UDID case (silently skips physicals). Physical iOS cert install requires manual installation via Settings > General > VPN & Device Management; Android physicals require root.
- **`install_proxy_cert` honors the active device** — when no UDID is supplied and an active device has been set via `resolve_device`, that device is used instead of falling through to "all booted devices". Falls back to the all-booted batch only when no active device is set, preserving the fresh-startup workflow. Body is now optional — no-arg calls no longer 422.
- **`proxy_status` filters offline devices in `cert_setup`** — deleted simulators and disconnected physical devices are hidden from the routine response (the persisted `cert-state.json` is unchanged). Pass `include_offline=true` to see the full historical record.

### Documentation
- **Knowledge-base authoring guide** (`docs/app-knowledge-guide.md`) updated to introduce landmarks as the primary identification field, document the `selected:` selector, walk through migrating a legacy knowledge base, and explain how to keep landmarks in sync as the app evolves (KB-as-living-artifact framing).
- **Screen landmark spec** (`docs/screen-landmarks.md`) updated to reflect the shipped `selected:` field, the per-landmark detail in `partial_matches`, and the `skipped[]` response from `load_landmarks`.
- **Agent guide** (`docs/agent-guide.md`) gets a new "Identifying Screens with Landmarks" workflow section so agents discover the landmark tools at session start rather than stumbling onto them later. Plus a discovery line in the MCP server preamble for the same reason.
- **Pre-commit checklist** (`docs/agent-precommit-checklist.md`) ships with the repo and is installed alongside the hook.

### Notes
- All work in this release came out of a focused dogfooding session: rough edges discovered by actually using Quern on a real app surfaced as 19 commits' worth of fixes and quality-of-life improvements, with the skill and the pre-commit hook designed to make the next agent's first session smoother than this one's was.

## [0.11.0] - 2026-04-26

### Added
- **Screen landmarks** — machine-evaluable screen identification via structured selectors in `screens/*.md` frontmatter. Replaces the freeform `identify_by` field with conjunctive landmarks (identifier, label, label_contains, absent) that the server can evaluate against a UI tree. New endpoints: `/landmarks/load`, `/landmarks/identify`, `/landmarks/list`, `/landmarks/unload`, `/landmarks/validate`. Five matching MCP tools. `GET /ui/summary?identify=true` adds `identified_as`/`confidence` to the response. Multi-app scoping and collision detection included. See [`docs/screen-landmarks.md`](docs/screen-landmarks.md).

### Notes
- This release rolls up everything from `0.11.0-rc1` plus screen landmarks.
- A future `docs/proposals/navigation-recipes.md` outlines a forward-looking idea for procedural navigation built on top of landmarks; not yet built.

## [0.11.0-rc1] - 2026-03-30

### Added
- **Screen context & auto-capture** — automatic screen context (screenshot + UI summary) on error responses for `tap_element` and `wait_for_element`. Opt-in screen context on action success responses. Auto screenshot capture on errors. Before/after screenshot pairs on action endpoints. Screenshot timeline with auto-capture middleware. Tunable `settle_delay`.
- **Coordinate grid overlay** — `take_annotated_screenshot` auto-draws a point-coordinate grid when no interactive accessibility elements are found. Grid coordinates match the `tap` coordinate system — no pixel math needed. `grid` parameter: `true` for 50pt default, number for custom spacing, `0` to disable.
- **Custom idb companion** — bundled patched `idb_companion` with flat accessibility tree mode, integrated into `quern setup`.

## [0.10.2] - 2026-03-23

### Fixed
- **Android emulator cert auto-install** — device type cache wasn't populated after Android emulator boot, causing cert installation to incorrectly use the iOS simctl path. Certs now auto-install reliably for all Android API levels.
- **API < 34 cert installation** — when `adb remount` fails on newer emulator binaries (common on API 33), falls back to a tmpfs overlay method instead of failing silently.

### Changed
- **GPS anti-spoof hardening** — `set_location` now sends a default satellite count of 4 with Android `geo fix` commands, avoiding anti-spoof heuristics that flag 0-satellite GPS fixes.

## [0.10.1] - 2026-03-23

### Changed
- **Build result summaries** — `build_and_install`, `get_build_result`, and `parse_build_output` return a concise text summary on success instead of full JSON. Cuts typical MCP response from ~12k tokens to a few lines. Full diagnostics still returned on build failure.
- **Per-project app knowledge config** — `config.json` in the knowledge base for project-specific settings.
- **Auto-detect renamed Xcode** — server now finds Xcode even if the `.app` bundle was renamed, and fixes `DEVELOPER_DIR` at startup.

## [0.10.0] - 2026-03-22

### Added
- **App knowledge base** — scaffolding, guided tour workflow, screen stubs, alerts, states, environments, glossary. Agents can build a complete map of an app's screens, navigation, and quirks.
- **Plist watcher** — live monitoring of app plist changes fed into the log pipeline. Supports auto-start, key prefix filtering, multiple watch targets per bundle.
- **Batch plist operations** — `set_app_plist_values` for bulk updates, `diff_app_plist` for comparing against checkpoints.
- **Proxy bypass allowlist** — `set_bypass` and `clear_bypass` tools for cert-pinned domains.
- **SSE streaming for proxy flows** — real-time flow event streaming.
- **On-demand oslog streaming** — host-level oslog capture via MCP.
- **Flexible element matching** — `label_contains` and `label_prefix` on `tap_element` for elements with dynamic or long labels.
- **`quern grant-full-perms`** — single command to allow all quern MCP tools in Claude Code without per-tool approval prompts.

### Fixed
- **Android emulator boot** — device pool was hardcoded to simctl; now correctly routes to adb for Android emulators.
- **Android app launch** — replaced unreliable `monkey` command with `am start` via `resolve-activity`. Works on all Android versions.
- **Android driver crash on API 34+** — AdbKeyboard broadcast receiver now specifies `RECEIVER_EXPORTED` as required by Android 14.
- **Active device persists across restarts** — target device is no longer lost when the server restarts.
- **Orphaned proxy cleanup** — proxy subprocess cleaned up properly after SIGKILL.

### Changed
- **Knowledge base path** — moved from `app-knowledge/` to `.quern/knowledge/`.

### Infrastructure
- CI workflow and Dependabot configuration.
- Node.js 22+ minimum.
- Zod 4 compatibility.

## [0.9.0] - 2026-03-17

First versioned release — MVP with iOS and Android support.

### Added
- iOS simulator and physical device control (screenshots, UI automation, logs, crash reports).
- Android emulator and device support (adb, scrcpy, uiautomator2).
- Network interception and mocking via mitmproxy.
- MCP server for AI-assisted development (Claude Code, Cursor, etc.).
- Live device preview (CoreMediaIO for iOS, MJPEG streaming for Android).
- `quern --version` command.

[0.13.4]: https://github.com/quern-dev/quern/releases/tag/v0.13.4
[0.13.3]: https://github.com/quern-dev/quern/releases/tag/v0.13.3
[0.13.2]: https://github.com/quern-dev/quern/releases/tag/v0.13.2
[0.13.1]: https://github.com/quern-dev/quern/releases/tag/v0.13.1
[0.13.0]: https://github.com/quern-dev/quern/releases/tag/v0.13.0
[0.12.0]: https://github.com/quern-dev/quern/releases/tag/v0.12.0
[0.11.0]: https://github.com/quern-dev/quern/releases/tag/v0.11.0
[0.11.0-rc1]: https://github.com/quern-dev/quern/releases/tag/v0.11.0-rc1
[0.10.2]: https://github.com/quern-dev/quern/releases/tag/v0.10.2
[0.10.1]: https://github.com/quern-dev/quern/releases/tag/v0.10.1
[0.10.0]: https://github.com/quern-dev/quern/releases/tag/v0.10.0
[0.9.0]: https://github.com/quern-dev/quern/releases/tag/v0.9.0
