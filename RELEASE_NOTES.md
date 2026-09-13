A feature release. Three new commands, the two bugs reported against v0.16.1, and a menu-bar app that now tells you when something failed.

**If it has been a while since Quern told you about an update, that was a bug and it is fixed.** The endpoint that answers the update check only ever compared commit SHAs, and a tarball install has no `.git` and so no SHA to send. It therefore answered "no update" to every tarball install, always. One user sat on v0.13.2 from late May until September across three minor versions with nothing telling them otherwise. The fix is server-side and already live, which is why you may be reading this after a long silence. Nothing to install for that part.

## Quern was intercepting its own update check

`urllib` honours the macOS system proxy, so the moment Quern configured that proxy it began man-in-the-middling its own request to `quern.dev`. The certificate stopped verifying and the update check failed — on precisely the machines running Quern, and only while capture was enabled.

The proxy now passes Quern's own hosts through before TLS is terminated, so no certificate is replaced and there is nothing to fail.

## `get_screen_summary` returned HTTP 500 against healthy simulators

Reported against v0.16.1. Xcode 26 registers simulators as CoreDevices, so they appear in `devicectl` alongside real hardware — and Quern labelled every one of them a physical device, ignoring the field that says otherwise. `resolve_device` then wrote `{"type": "device"}` for a simulator, UI reads routed to WDA instead of sim-bridge, and with no WDA build present that is a hard failure. Passing `type: "simulator"` explicitly did not override it.

Present well before v0.16.1; it became visible when an update cleared an existing WDA build.

## `quern update --tools` could not upgrade a global pipx install

Also reported against v0.16.1. It always emitted `pipx upgrade <name>`, which only looks in the per-user `PIPX_HOME` — so on a tool installed with `--global` it failed with *"Package is not installed"*, naming a path you never chose for a tool that is plainly working.

Not an edge case: setup steers machines whose home is on an external volume towards a global install, because the tunneld daemon starts at boot and cannot reach a volume that mounts at login.

## You can ask for updates instead of waiting

```sh
quern check-updates
```

The update hint is a cache refreshed at most once a day, so a release landing this afternoon was not offered until tomorrow. The menu bar has **Check for Updates** for the same thing, and Settings now carries **Check for updates automatically** if you would rather it stopped asking on its own — that governs the automatic check alone, so asking still works when it is off.

## `quern capture-env`

Writes the facts Quern's environment checks read — where each `pymobiledevice3` lives, what it resolves to, PATH order — as a JSON file suitable for attaching to an issue. What it may contain is pinned by a test: it never reads your API key, server state, certificate state or device pool, and a new field fails the build until someone decides it is safe to publish.

## The menu-bar app

It reports failures now. Start, stop and restart discarded the CLI's exit status, so a failure left the menu saying exactly what it said before — indistinguishable from a dead menu item. Work in progress shows beside the icon rather than only in a menu that closes when you click it. It has tests, and CI runs them.

## `quern doctor` no longer needs a running server

It exited at the first check it could not make, which withheld its most useful output — whether your venv matches `pyproject.toml` — behind exactly the condition that sends people looking for it.

---

Twenty-eight other fixes, most of them in the menu-bar app and the update path. Full detail in [the changelog](https://github.com/quern-dev/quern/blob/v0.17.0/CHANGELOG.md#0170---2026-09-13).

```sh
quern update
```
