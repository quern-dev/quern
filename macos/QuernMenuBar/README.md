# Quern Menu-Bar Daemon Manager

A lightweight macOS menu-bar app (`LSUIElement`, no Dock icon) that surfaces
the Quern daemon's state and drives it through the `quern` CLI — an Ollama-style
manager with a **Restart to Update** action.

## What it does

- **Status** — running/stopped + uptime, read from `~/.quern/state.json`.
- **Active device & proxy** — from `~/.quern/active-device.json` and `state.json`.
- **Start / Stop / Restart** — shells out to the installed `quern` CLI.
- **Restart to Update** — appears only when `~/.quern/update-info.json` reports
  `update_available`; runs `quern update`, then relaunches into the new build.
- **Settings** — full state, stable/beta channel picker, launch-at-login toggle,
  docs link.
- **Quit** — exits only the menu bar; ⌥ reveals "Quit and Stop Server".

It is a **monitor + manual controller**, not the daemon's owner — it coexists
with `quern start` and the MCP `ensure_server` tool. State comes from the
unauthenticated `~/.quern/*.json` files, so no API key / HTTP is needed.

## Architecture

| File | Responsibility |
|------|----------------|
| `Sources/main.swift` | Accessory-app bootstrap |
| `Sources/AppDelegate.swift` | Status item + menu construction + actions |
| `Sources/StateReader.swift` | Reads `~/.quern/*.json`; poll + directory watch |
| `Sources/QuernCLI.swift` | Resolves & runs the `quern` CLI |
| `Sources/Updater.swift` | "Restart to Update" + self-relaunch |
| `Sources/SettingsWindow.swift` | SwiftUI settings + `SMAppService` login item |

Requires macOS 13+ (for `SMAppService`).

## Build (development)

```sh
# Native-arch, unsigned dev build → macos/QuernMenuBar/build/Quern.app
UNIVERSAL=0 ./build.sh
open build/Quern.app
```

`build.sh` compiles all `Sources/*.swift` with `swiftc` (no Xcode project),
stamps the version from the repo's `pyproject.toml` into `Info.plist`, and
assembles the `.app` bundle. Icons are optional — drop `Assets/AppIcon.png`
and/or `Assets/StatusIcon.png` to override the SF Symbol fallback.

> Dev builds are unsigned, so launch-at-login and macOS permissions may be
> unstable across rebuilds (unstable code-signing identity). That's expected
> for local iteration — releases are signed (below).

## Release (signed + notarized)

Releases are cut by a maintainer on a Mac with a Developer ID identity. The
menu-bar app ships **inside the release tarball asset** and updates through
Quern's existing updater (Option A — no Sparkle, no second update path).

One-time credential setup:

```sh
# Store an App Store Connect API key (or Apple ID) for notarytool.
xcrun notarytool store-credentials quern-notary \
  --apple-id "you@example.com" --team-id TEAMID --password "app-specific-pw"
```

Per release (also documented in `docs/release-channels.md`, step 5):

```sh
DEVELOPER_ID_APP="Developer ID Application: Your Name (TEAMID)" \
NOTARY_PROFILE="quern-notary" \
  ../../scripts/release-menubar.sh v0.14.0
```

That script builds a universal binary, signs with hardened runtime, notarizes
and staples, assembles `dist/quern-<version>.tar.gz` (source tree + signed
`Quern.app` at the top level), and uploads it as a release asset. The updater
(`_select_asset_url` in `server/lifecycle/updater.py`) prefers this asset.
