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
# Apple ID credentials, with an app-specific password (not your Apple ID
# password). The profile name is yours to choose -- pass the same one to the
# script below.
xcrun notarytool store-credentials my-notary-profile \
  --apple-id "you@example.com" --team-id TEAMID --password "app-specific-pw"
```

An App Store Connect API key works too, and avoids storing a password:

```sh
xcrun notarytool store-credentials my-notary-profile \
  --key AuthKey_XXXXXXXXXX.p8 --key-id XXXXXXXXXX --issuer "issuer-uuid"
```

Per release, in two phases. The full procedure is in
`docs/release-channels.md`; this is the part that runs here.

**Before cutting the tag** — build, sign and notarize:

```sh
DEVELOPER_ID_APP="Developer ID Application: Your Name (TEAMID)" \
NOTARY_PROFILE="my-notary-profile" \
  ../../scripts/release-menubar.sh --app-only 0.15.0     # no leading v
```

This leaves a signed, notarized `Quern.app` in `dist/` and prints the publish
command for later. Doing it first means a notary-service failure costs you
nothing: no tag has been cut and no Release exists yet.

**After the tag and Release exist** — assemble and upload:

```sh
DEVELOPER_ID_APP="Developer ID Application: Your Name (TEAMID)" \
  ../../scripts/release-menubar.sh --publish v0.15.0
```

That assembles `dist/quern-<version>.tar.gz` (source tree at the tag + the
signed `Quern.app` at the top level) and uploads it as a release asset. Both
the updater (`_select_asset_url` in `server/lifecycle/updater.py`) and the
install script on quern.dev prefer this asset over GitHub's generated source
tarball, so a fresh install and an upgrade both get the app.

`--publish` re-verifies the staged app before uploading anything: stamped
version against the tag, signature validity, stapled ticket, Gatekeeper
acceptance, and that the signing team matches `DEVELOPER_ID_APP` — which is
why that variable is needed in both phases. A staged app can come from
anywhere, including a previous release.

The one-shot form, `release-menubar.sh v0.15.0`, still does everything in a
single run, but it needs the tag and Release to already exist.
