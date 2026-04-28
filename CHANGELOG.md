# Changelog

All notable changes to Quern are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Network change monitor** — a background poll (~15s cadence) detects shifts in the Mac's Wi-Fi SSID and outward-facing IP. Surfaced as `network_state` on the `proxy_status` response with `last_changed_at`, a `last_change_reason` (`ssid_changed` / `ip_changed_same_ssid` / `ssid_and_ip_changed`), and a small ring of recent changes. Combined with the existing per-device `wifi_proxy_stale` flag, an agent reading any routine status call now sees both *what just changed* and *which devices need their proxy reconfigured* — without anyone having to remember to ask after moving between networks.
- **`selected:` landmark field** — for tabs, switches, radios, and checkboxes, `selected: true` matches elements whose UI selection state is on. iOS's RadioButton-style tabs and Android's BottomNavigationView/TabLayout tabs are both handled (Android `selected="true"` now normalizes to `AXValue="1"` for parity with the iOS convention). Required for screens distinguished only by which tab is active.
- **`include_raw=true` on `get_ui_tree`** — opt-in flag that adds `extra_attrs` per element with the raw source attributes from the underlying provider (full uiautomator2 XML attribute set on Android). Lets agents debug the platform normalizer without dropping to `adb shell uiautomator dump`. Default off to keep payloads compact.
- **Active device sidecar** — the active UDID set via `resolve_device` now persists in `~/.quern/active-device.json` (separate from `state.json`, which is server-runtime data). Survives `quern stop` and stop/start cycles; the user no longer has to re-resolve their device after every restart.
- **`udid` parameter on `resolve_device` MCP tool** — the HTTP route already accepted it, but the MCP wrapper's Zod schema didn't expose it. Agents that already know which device they want (e.g., from `list_devices`) can now switch active device without round-tripping through name/os_version matching.
- **Categorized `skipped[]` on `load_landmarks`** — knowledge bases that pre-date the `landmarks:` schema (April 2026) used the older `identify_by:` field. The loader now returns these in a `skipped` array with reason codes (`legacy_format`, `no_landmarks`, `no_frontmatter`, `yaml_error`, `invalid_entries`). The `legacy_format` entries echo back the original `identify_by` data so an agent can propose a per-file migration with user review.
- **Migration agent skill** — new `quern-landmark-migration` skill walks an agent through migrating an `identify_by`-era knowledge base file by file, including the `value: "1"` → `selected: true` translation. Auto-installed via `quern setup`.
- **Per-landmark detail in `identify_screen` failure responses** — `partial_matches` now includes every evaluated non-fully-matched screen (including zero-match), with full per-landmark results so an agent can see which selectors hit and which missed without re-running identification. Sorted by descending match count so the best candidate appears first.
- **Version visible in `quern start` and `quern status`** — daemon-mode banners now show `Quern v0.11.0 running` instead of just `Quern running`.

### Changed
- **`install_proxy_cert` filters physical devices** — the no-UDID batch path now only runs on simulators and Android emulators, both for the explicit `udid` case (returns 400 with manual-install guidance) and the no-UDID case (silently skips physicals). Physical iOS cert install requires manual installation via Settings > General > VPN & Device Management; Android physicals require root.
- **`install_proxy_cert` honors the active device** — when no UDID is supplied and an active device has been set via `resolve_device`, that device is used instead of falling through to "all booted devices". Falls back to the all-booted batch only when no active device is set, preserving the fresh-startup workflow. Body is now optional — no-arg calls no longer 422.
- **`proxy_status` filters offline devices in `cert_setup`** — deleted simulators and disconnected physical devices are hidden from the routine response (the persisted `cert-state.json` is unchanged). Pass `include_offline=true` to see the full historical record.

### Documentation
- **Knowledge-base authoring guide** (`docs/app-knowledge-guide.md`) updated to introduce landmarks as the primary identification field, document the `selected:` selector, and walk through migrating a legacy knowledge base.
- **Screen landmark spec** (`docs/screen-landmarks.md`) updated to reflect the shipped `selected:` field, the per-landmark detail in `partial_matches`, and the `skipped[]` response from `load_landmarks`.

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

[0.11.0]: https://github.com/quern-dev/quern/releases/tag/v0.11.0
[0.11.0-rc1]: https://github.com/quern-dev/quern/releases/tag/v0.11.0-rc1
[0.10.2]: https://github.com/quern-dev/quern/releases/tag/v0.10.2
[0.10.1]: https://github.com/quern-dev/quern/releases/tag/v0.10.1
[0.10.0]: https://github.com/quern-dev/quern/releases/tag/v0.10.0
[0.9.0]: https://github.com/quern-dev/quern/releases/tag/v0.9.0
