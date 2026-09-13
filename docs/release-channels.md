# Release channels

Quern ships on two update channels: `stable` (the default) and `beta` (opt-in
prerelease testing). This doc covers both **what users see** and **how
maintainers cut releases** on each channel.

## TL;DR

| Channel  | What it tracks (git installs)                  | What it tracks (tarball installs)                  |
|----------|------------------------------------------------|----------------------------------------------------|
| `stable` | `origin/release/stable`                        | GitHub Releases marked **not prerelease**          |
| `beta`   | `origin/release/beta`                          | GitHub Releases marked **prerelease**              |

Switch with `quern set-channel beta` (or back with `quern set-channel stable`).
Setting the channel updates `~/.quern/config.json`; nothing else changes until
the next `quern update`.

---

## For users

### Default behavior

Out of the box, every install is on `stable`. New users never have to think
about channels.

### Opting into beta

```sh
quern set-channel beta
```

The next `quern update` will pull from the beta channel:

- **Git-clone install:** you must be checked out on `release/beta` (or already
  on `main` and willing to switch). If you're on a feature branch, the updater
  will tell you it sees beta updates available but won't auto-switch your
  workspace — that's a developer-clone-safety choice.
- **Tarball install:** the updater downloads whichever GitHub Release is the
  newest one flagged `prerelease: true`. If no prereleases exist yet, you
  silently fall through to the latest stable so you never see *older* content
  than a stable user.

### Switching back

```sh
quern set-channel stable
quern update
```

Same flow in reverse. Stable is always a safe target.

### What channel am I on?

```sh
quern set-channel        # no argument — prints current channel
```

Or from inside Claude Code:

> "What's my Quern channel?"

The MCP `ensure_server` tool surfaces channel + update status; Claude reads it
in the normal course of any session.

---

## For maintainers — release-cut procedure

### Channel branches: where they live, what they track

Two reserved branches on `origin`:

- **`release/stable`** — pointer branch for the stable channel. Should be
  fast-forwarded to the commit corresponding to each tagged stable release
  (`vN.M.K`).
- **`release/beta`** — pointer branch for the beta channel. Fast-forwarded
  whenever you want beta users to see new content — either a tagged prerelease
  (`vN.M.K-beta.X`), or just an arbitrary commit on `main` you want beta users
  to try.

Both branches are descendants of `main` at all times. Neither contains commits
that aren't already in `main`.

### Before any of it: the documentation pass

Every release so far has shipped at least one doc that contradicted the code,
and they were found by accident rather than by looking. The 0.16.0 cut found
four, one of which told users the opposite of what the release did. Work this
list before the version bump, because the release tarball is archived from the
tag and whatever is stale at that moment ships.

For each item, ask "did this release change what this file asserts?" — not
"did this release touch this file".

- [ ] **`README.md`** — the feature bullets, the CLI command block, and the
      `~/.quern/` state table. `tests/test_readme_sync.py` catches a *missing*
      command; it cannot catch a description that is now wrong.
- [ ] **`CONTRIBUTING.md`** — the design-decision and behaviour sections. A
      release that establishes a rule should say so here, or the next person
      re-derives it.
- [ ] **`docs/agent-guide.md`** — what an agent should reach for and what a new
      refusal means. Agents retry unexplained errors, so a new non-retryable
      failure has to be described as one. Check too that no step tells an agent
      to shell out around a Quern tool.
- [ ] **`docs/api-reference.md`** — new endpoints, new request fields, new
      status codes.
- [ ] **`docs/guides/*.md`** — the ones whose subject the release changed. These
      sync to quern.dev, so a stale guide is a stale public page.
- [ ] **`macos/QuernMenuBar/README.md`** — build and release steps for the app.
- [ ] **`CHANGELOG.md`** — rename `Unreleased`, date it, add the link ref.
- [ ] **Run the sync** — `python3 scripts/sync-docs.py --repo <quern>` in the
      quern.dev checkout, and commit what it changes. A guide corrected in this
      repo and never synced leaves the site serving the old text; that happened
      once for a week.
- [ ] **Pick the version deliberately.** New commands, new config fields, or a
      call that now refuses where it used to succeed are a minor bump, not a
      patch — regardless of how the work was framed while doing it.

### Cutting a stable release

```sh
# 1. Bump version, finalize CHANGELOG, commit on main.
git switch main
# (edit pyproject.toml, mcp/package.json, mcp/package-lock.json, CHANGELOG.md)
git commit -am "Bump version to vN.M.K and finalize CHANGELOG"
git push origin main

# 2. Tag the release commit.
git tag -a vN.M.K -m "vN.M.K — short release headline"
git push origin vN.M.K

# 3. Fast-forward EVERY channel branch you intend to move.
#    *** Do this BEFORE creating the GitHub Release. ***
#    Once a commit has a Release attached, GitHub silently refuses pushes
#    that point any branch at that exact commit (see "GitHub quirk" below).
git push origin main:refs/heads/release/stable
git push origin main:refs/heads/release/beta

# 4. Verify both actually moved — the rejection in step 3 is silent.
git fetch origin
git rev-parse origin/release/stable origin/release/beta origin/main   # expect three identical SHAs

# 5. Now create the GitHub Release.
gh release create vN.M.K --title "vN.M.K — short release headline" --notes-file RELEASE_NOTES.md

# 6. Attach the menu-bar app asset, using the app staged in step 0.
DEVELOPER_ID_APP="Developer ID Application: Your Name (TEAMID)" \
  scripts/release-menubar.sh --publish vN.M.K
```

**Step 0, before any of the above.** Build, sign and notarize the app first,
while nothing has been cut yet:

```sh
DEVELOPER_ID_APP="Developer ID Application: Your Name (TEAMID)" \
NOTARY_PROFILE="your-notarytool-profile" \
  scripts/release-menubar.sh --app-only N.M.K      # note: no leading v
```

That leaves a signed, notarized `Quern.app` in `dist/`, and prints the exact
`--publish` command to run at step 6.

Doing it first is the point of the split. Notarization is the slow step, the
one that depends on Apple's service being reachable, and the one that would
otherwise abort a release *after* the tag and Release existed — leaving
nothing to clean up except by hand. If Apple is having a bad day you find out
before anything is published.

`--publish` re-verifies the staged app rather than trusting it: stamped
version against the tag, signature validity, stapled ticket, Gatekeeper
acceptance, and that the signing team matches `DEVELOPER_ID_APP`. Hence that
variable is required in both phases.

The one-shot form, `scripts/release-menubar.sh vN.M.K`, still does everything
in a single run. It needs the tag and Release to already exist, so it belongs
at step 6, not step 0.

Both forms need a `notarytool` keychain profile; see
`macos/QuernMenuBar/README.md` for the one-time credential setup. Use whatever
name you gave it when you ran `store-credentials`.

**Why the ordering matters:** see the *GitHub quirk* section. Once the Release
in step 5 exists, you cannot retroactively move any branch to that commit. The
fast-forwards in step 3 have to happen first, and step 4 exists because a
rejection there is silent.

**Why `release/beta` too.** A stable release is by definition newer than
anything beta users are running, so leaving `release/beta` behind means
beta-channel users receive *older* content than stable users — the exact
inversion the channel exists to prevent. This is not hypothetical: the v0.14.0
cut advanced only `release/stable`, stranding `release/beta` 27 commits back,
and because the release commit was already published the branch could not be
moved onto it afterwards. It took the next beta cut to clear.

If you are cutting a **prerelease**, advance only `release/beta` — `release/stable`
should keep pointing at the last stable tag.

**The menu-bar app asset (step 5):** `scripts/release-menubar.sh` uploads a
`quern-<version>.tar.gz` asset that bundles a signed/notarized `Quern.app`
alongside the source tree. The tarball updater
(`server/lifecycle/updater.py`) prefers this asset over GitHub's
auto-generated source tarball, so users get the menu-bar app on update;
releases without the asset fall back to the source tarball automatically.
Run the same step for beta tags below.

### Cutting a beta release

There are two common beta flavors:

**(a) Tagged prerelease** — when you want a citable beta version that tarball
users can install:

```sh
# Tag on the commit you want beta users to land on (usually main HEAD).
git tag -a vN.M.K-beta.X -m "vN.M.K beta X"
git push origin vN.M.K-beta.X

# Fast-forward release/beta. Must happen BEFORE the Release is created.
git push origin <commit-sha>:refs/heads/release/beta

# Create the GitHub Release with --prerelease so the tarball updater
# picks it up only for beta channel users.
gh release create vN.M.K-beta.X --prerelease \
  --title "vN.M.K beta X" --notes-file BETA_NOTES.md
```

**(b) Untagged beta advance** — when you just want git-clone beta users to see
recent main changes without minting a prerelease version:

```sh
# Fast-forward release/beta to whatever main commit you want beta users on.
git push origin main:refs/heads/release/beta
```

Tarball beta users are unaffected by (b) since they're driven by the GitHub
Releases API.

### What the channel branches point at today

`release/stable` tracks the most recent stable tag; `release/beta` tracks the
most recent prerelease, which is normally at or ahead of stable. If you ever
find beta *behind* stable, a cut skipped step 3 — see the note above.

When this work first shipped, both branches were created at the then-current
`main` HEAD rather than at the most recent stable tag, because of the GitHub
quirk below. That bootstrapping state is gone: since v0.14.0 both branches
follow the procedure above.

### The GitHub quirk — read this once

There is an **undocumented** GitHub platform behavior we hit while
bootstrapping the channel branches:

> **You cannot create or move a branch to point at a commit that is the
> target of a published Release.** The push is silently rejected with
> `remote rejected ... (failed)` and the REST API returns
> `422: Reference update failed` with no detail. The behavior persists even
> after deleting the Release — the platform-level marker doesn't go away.

We confirmed this by:

1. Pushing a branch from the parent of `v0.13.4` — worked.
2. Pushing a branch from `v0.13.4` itself — rejected.
3. Pushing a branch from a freshly-created tag we'd just made on a non-tagged
   commit — worked.
4. Pushing a branch from `v0.13.3` (also a Release commit) — rejected.
5. Deleting the `v0.13.4` Release and trying again — still rejected.

The implication is the ordering rule above: **fast-forward `release/*` to the
release commit BEFORE creating the GitHub Release object**. Doing it in the
other order locks you out.

`gh ruleset check <branch>` reports zero rules, so this isn't surfaceable
through normal policy inspection. It's worth filing with GitHub Support if it
ever materially blocks something.

### Helper script (optional)

Save the ordering and the gotcha in one place so future-you doesn't have to
remember:

```sh
#!/bin/bash
# scripts/cut-release.sh — usage: ./scripts/cut-release.sh vN.M.K [--prerelease]
set -euo pipefail
TAG="$1"
PRERELEASE=""
[ "${2:-}" = "--prerelease" ] && PRERELEASE="--prerelease"

# Which channels this release moves. A stable cut advances both -- leaving beta
# behind hands beta users older content than stable users get. A prerelease
# advances only beta; release/stable must stay on the last stable tag.
if [ -n "$PRERELEASE" ]; then
  CHANNELS="release/beta"
else
  CHANNELS="release/stable release/beta"
fi

# 1. Tag. Reuse an existing one only when it points at the commit this run is
#    releasing; a mismatch means the tag was cut somewhere else, and moving a
#    published tag is not something a script should decide to do. Without the
#    reuse, any retry after a later step failed dies here on "tag already
#    exists" with the remote tag already pushed.
INTENDED=$(git rev-parse HEAD)
if git rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
  EXISTING=$(git rev-parse "$TAG^{commit}")
  if [ "$EXISTING" != "$INTENDED" ]; then
    echo "error: $TAG exists at ${EXISTING:0:8} but HEAD is ${INTENDED:0:8}" >&2
    echo "       Resolve that by hand before rerunning." >&2
    exit 1
  fi
  echo "note: $TAG already exists at ${EXISTING:0:8}, reusing it"
else
  git tag -a "$TAG" -m "$TAG"
fi
git push origin "$TAG"

# 2. Fast-forward the channel branches — MUST happen before step 4.
#    From the tag, not from main: the tag is what was published, and a
#    prerelease is often cut somewhere other than main HEAD.
for ref in $CHANNELS; do
  git push origin "$TAG:refs/heads/$ref"
done

# 3. Verify — the rejection above is silent, so this has to fail loudly.
git fetch origin
expected=$(git rev-parse "$TAG^{commit}")
for ref in $CHANNELS; do
  actual=$(git rev-parse "origin/$ref")
  if [ "$actual" != "$expected" ]; then
    echo "error: origin/$ref is at ${actual:0:8}, expected ${expected:0:8}" >&2
    echo "       The push above was rejected. Do NOT create the Release." >&2
    exit 1
  fi
done

# 4. Now create the Release
gh release create "$TAG" $PRERELEASE --title "$TAG" --notes "see CHANGELOG.md"
```

The verification compares against the tag rather than `origin/main` on purpose.
Only the branches this release actually moves are checked, so a prerelease is
not failed for leaving `release/stable` where it belongs.

**If it stops partway.** Every step before the Release is repeatable: the tag
is reused when it already points at the intended commit, and pushing a channel
branch to the same tag twice is a no-op. So the fix for a failed run is
normally to resolve the cause and run it again.

The exception is the one the ordering exists for. Once the Release is created,
GitHub silently refuses to point any branch at that commit, so a channel branch
left behind at that moment cannot be fixed by rerunning — it has to wait for
the next cut. That is why step 3 refuses to continue rather than warning.

---

## How the code knows about channels

- `~/.quern/config.json` field `update_channel`: `"stable"` (default) or
  `"beta"`. Persisted by `quern set-channel`, `server.config.set_update_channel`,
  and the `PUT /api/v1/system/channel` endpoint.
- `server.lifecycle.updater._get_release_branch()` resolves the configured
  channel to `release/<channel>`.
- `_update_via_git()` compares HEAD against `origin/<release_branch>` and
  warns when the user isn't on a release branch (no auto-switch).
- `_update_via_tarball()` calls `_fetch_latest_release(channel)`:
  - `stable` → `/releases/latest` (GitHub-defined as latest non-prerelease).
  - `beta` → `/releases`, picks the first non-draft prerelease; falls through
    to the latest stable when no prereleases exist.

## Related

- Issue #41 — original channels proposal
- Issue #40 — the upstream-tracking bug that motivated the `_get_release_branch`
  refactor
- PR #43 — channels implementation
