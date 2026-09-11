# Quern Menu-Bar Daemon Manager

A lightweight macOS menu-bar app (`LSUIElement`, no Dock icon) that surfaces
the Quern daemon's state and drives it through the `quern` CLI — an Ollama-style
manager with a **Restart to Update** action.

## What it does

- **Status** — running/stopped + uptime, read from `~/.quern/state.json`.
- **Active device & proxy** — from `~/.quern/active-device.json` and `state.json`.
  The sidecar carries the device's name and type as well as its UDID, so the
  row reads `iPhone 16 Pro (Simulator)` rather than a 36-character identifier.
  An absent type is shown unqualified rather than guessed.
- **Start / Stop / Restart** — shells out to the installed `quern` CLI.
- **Restart to Update** — appears only when `~/.quern/update-info.json` reports
  `update_available`; runs `quern update`, then relaunches into the new build.
- **Start on launch** — launching the app starts the daemon, unless it is
  already running or you turn it off in Settings. On by default: you opened the
  Quern app, and a menu that greets you with "stopped" and a button to press is
  a step that did not need to exist. A failure here goes to the menu and the
  Console rather than an alert, because this can fire at login and a modal
  stealing focus as you open your laptop is worse than the failure it reports.
- **Settings** — full state, the capture-certificate policy, stable/beta channel
  picker, launch-at-login and start-on-launch toggles, docs link. The certificate toggle writes
  `auto_install_cert` via `quern set-auto-install-cert`; it is surfaced here
  deliberately, because a standing policy to install a root certificate
  authority should be visible and reversible rather than living only in a
  config file. It reads a *literal* JSON boolean — `JSONSerialization` hands
  back `NSNumber` for numbers too, and `as? Bool` accepts a numeric 1, which
  would show the policy enabled while the server treated it as unset.
- **Quit** — exits only the menu bar; ⌥ reveals "Quit and Stop Server".

It starts the daemon but does not own it: quitting the menu bar leaves the
server running (⌥ reveals "Quit and Stop Server" for when you mean both), and
it coexists with `quern start` and the MCP `ensure_server` tool. Start-on-launch
is a setting rather than unconditional behaviour because the app also registers
itself as a login item, so "on launch" includes every login — a daemon running
because you installed a menu bar app is worth being able to decline. Starting
the server opens the HTTP listener and a crash-report watcher; syslog and OSLog
capture stay off, and the proxy and its certificate stay behind their own
consent gates. State comes from the
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

**The asset is not the only delivery path, and cannot be.** v0.15.0 reached
every existing user without the app: their updater predated the asset
preference, so it fetched GitHub's generated source tarball — and the code
that prefers the asset shipped *inside* the asset, so it could not help
itself. Any future capability delivered only through the asset has the same
bootstrap problem.

So `quern setup` fetches the app when a release install is missing it, which
is the first code of ours that runs on an affected machine. It verifies before
installing — a designated requirement anchored to Apple and pinned to this
team, Gatekeeper acceptance, and the bundle's stamped version against the
release requested — because that path downloads an executable and then
launches it. A git checkout is left alone; developers build their own.

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
  ../../scripts/release-menubar.sh --app-only 0.16.0     # no leading v
```

This leaves a signed, notarized `Quern.app` in `dist/` and prints the publish
command for later. Doing it first means a notary-service failure costs you
nothing: no tag has been cut and no Release exists yet.

**After the tag and Release exist** — assemble and upload:

```sh
DEVELOPER_ID_APP="Developer ID Application: Your Name (TEAMID)" \
  ../../scripts/release-menubar.sh --publish v0.16.0
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

The one-shot form, `release-menubar.sh v0.16.0`, still does everything in a
single run, but it needs the tag and Release to already exist.
