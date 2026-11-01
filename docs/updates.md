# Updates and diagnostics

How quern checks for and applies updates, how it keeps its own dependencies and the external tools it uses current, and the `doctor` and `capture-env` commands for when something on a machine is not right. For switching between the stable and beta channels step by step, see the [update channels guide](guides/update-channels.md).

## Update checks

When started as a daemon, Quern makes a single HTTPS request to `quern.dev/api/check-update` to check if a newer version is available. This request includes only your current version number (and commit SHA for git-based installs) — no device info, no IP logging, no telemetry. Cloudflare's edge analytics count daily requests, giving us a rough sense of how many people use Quern. No data is stored.

To turn the automatic check off, untick **Check for updates automatically** in the Quern app's Settings, or run `quern set-update-check off`. That governs the automatic check alone — `quern check-updates` and the menu bar's **Check for Updates** keep working, so turning it off means "do not call home unprompted" rather than "never check". The underlying setting is `"update_check": false` in `~/.quern/config.json`.

## Update channels

Quern ships on two channels. `stable` (the default) tracks tagged releases; `beta` is opt-in early access to the next release's content.

```bash
quern set-channel beta     # opt in
quern set-channel          # print the current channel
quern set-channel stable   # switch back
```

Setting the channel only writes `~/.quern/config.json` — nothing changes until the next `quern update`. Git-clone installs follow the `release/stable` / `release/beta` pointer branches; tarball installs follow GitHub Releases, filtered on the prerelease flag. Full details, including the maintainer release procedure, are in [`docs/release-channels.md`](release-channels.md).

## Keeping dependencies current

`quern update` brings its own venv forward eagerly: pip's default strategy leaves
any version that already satisfies a constraint alone, so a venv drifts arbitrarily
far behind while every declared floor stays satisfied. Every dependency floor quern
declares is a `>=` and none is near what currently ships — `pymobiledevice3` sits
three majors above its own — so "satisfies the constraint" says very little about how
current the venv is.

Starting the server stays non-eager: it compares two mtimes, and invokes pip only when
the venv is actually stale or a previous install failed — never the eager transitive
upgrade `quern update` performs. `quern doctor --fix` is also non-eager, because it
repairs a broken venv and pulling every transitive dependency forward mid-repair
changes more than the fault being fixed.

External tools are a separate matter. `pymobiledevice3` is installed twice on a
typical machine — a library in the venv and a `pipx` binary — and they drift apart.
Every update run reports external tools that have fallen behind, with the exact
command to move each one; `--tools` runs those commands for you.

Reporting is the default because a `pipx` or `brew` upgrade changes state for every
other consumer on the machine, not just quern. Tools that arrived as a dependency of
something else are reported but not offered — upgrading those directly can be undone
by whatever pulled them in. Tools quern does not manage (node under `fnm`, `adb`
inside Android Studio's SDK) are named along with who does manage them, decided by
where the tool came from rather than by its name.

`quern doctor` shows the same analysis for *every* install site, not just the stale
ones, including which other brew formulae depend on each — the thing that turns a
later upgrade into a decision rather than a command. Use it when two machines
disagree about behaviour: a boolean "is pymobiledevice3 installed" reads identical on
both while one runs a 9.15.1 binary and the other an 11.3.1 one.

`quern doctor --fix` reconciles the venv and stops there. It does not upgrade external
tools, and says so when it finds some it cannot help with, rather than printing
"nothing to do" above a tool it just flagged as behind.

## Reporting an environment problem

`quern capture-env` writes the
facts these checks read — where each `pymobiledevice3` lives and what it resolves to,
the order of `PATH`, and what the tunneld daemon has baked in. Attach it to an issue
and the configuration can be replayed as a test rather than guessed at from a
description; `tests/fixtures/envs/` holds the ones that have already found bugs.

It is read-only and deliberately narrow. It never opens `~/.quern/api-key`,
`state.json`, the certificate state or the device pool. The output is meant for a
public issue, so what it may contain is pinned by a test rather than remembered:
`tests/test_capture_env.py` watches every read through an interpreter audit hook,
below any particular way of opening a file, and fails if the capture ever reaches
for one of those.

`PATH` is filtered to the entries that matter: anything recognisably a toolchain
quern cares about, plus any directory that actually holds a tool it uses. That second
clause is what keeps the filter honest — a whitelist alone would hide an unexpected
directory a tool is genuinely resolved from, which is the one surprise worth
reporting. Each survivor keeps its original index, so the order that decides which
copy wins is still reconstructible without publishing the rest of your `PATH`.

It is a filter, not a redactor. A kept entry is published in full, so a directory
matching on a toolchain name carries whatever else is in its path. Read it before
attaching it if that matters to you.

If quern is broken enough that the command will not run, `python3
scripts/capture-env.py` does the same thing. Both record what `which` resolves
once setup has prepended its venv, as well as the plain answer. Those differ, and
the first is the one the checks act on. It is stdlib-only and works on the
Python that ships with Xcode's Command Line Tools, so it does not need the venv
that may be the problem.

## Doctor

**Doctor does not need a running server.** Only the device-tool section does, and when
that cannot be reached doctor names the reason and reports everything else anyway —
the venv, the external tools, service health. That matters because a stale venv is a
good way to stop the server coming up, so the check most likely to explain the failure
used to be withheld by the failure. The exit status still reflects the gap: a section
that could not be checked is not one that passed.

Doctor also reports **service health**, which is a different question from whether a
tool is installed:

- **tunneld** — a failed device pairing can leave the daemon alive but not serving. It
  holds no listener and never exits, so `KeepAlive` never fires and launchd reports it
  healthy indefinitely. Doctor separates that *wedged* state from a merely *stopped*
  one by pairing the HTTP probe with the launchd job state.

  Since v0.15.0 Quern can also recover it. Getting the stuck process to exit is the
  entire fix, because the plist sets `KeepAlive` and launchd respawns it against clean
  state in about a second. That needs root, so the authorisation is taken once and
  explicitly with `quern tunneld grant-recovery` — a single `NOPASSWD` rule for one
  signal to one job, nothing else. Without the grant the behaviour is unchanged: the
  wedge is reported, not healed.
- **local capture extension** — the mitmproxy macOS system extension is approved once
  by a human and then upgraded underneath that approval by ordinary dependency updates.
  When the version macOS runs falls behind the version the installed wheel ships, local
  capture reports itself enabled while capturing nothing. Doctor compares the two, and
  `--fix` re-runs the shipped app so macOS can activate the newer one (you still have
  to approve it in System Settings).

Neither check needs a password: `launchctl print` on a system job and
`systemextensionsctl list` are both readable unprivileged.
