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

# 3. Fast-forward release/stable to the same commit.
#    *** Do this BEFORE creating the GitHub Release. ***
#    Once a commit has a Release attached, GitHub silently refuses pushes
#    that point any branch at that exact commit (see "GitHub quirk" below).
git push origin main:refs/heads/release/stable

# 4. Now create the GitHub Release.
gh release create vN.M.K --title "vN.M.K — short release headline" --notes-file RELEASE_NOTES.md
```

**Why the ordering matters:** see the *GitHub quirk* section. Once step 4 has
happened, you cannot retroactively move `release/stable` to that commit. The
fast-forward in step 3 has to happen first.

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

### Why are both `release/*` branches currently at main HEAD?

When this channels work first shipped, GitHub's `release/stable` and
`release/beta` branches were created at the then-current `main` HEAD, not at
the most recent stable tag. The reason is the *GitHub quirk* below.

The practical effect: until you cut the next stable release using the
procedure above, "stable channel" effectively means "the post-release main
HEAD at the moment release/stable was last advanced." That's a slightly looser
interpretation than "the latest tagged commit," but it's functionally fine —
every commit on main has passed CI, and once you cut the next release the
strict semantics kick in.

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
# scripts/cut-stable-release.sh — usage: ./scripts/cut-stable-release.sh vN.M.K
set -euo pipefail
TAG="$1"

# 1. Tag
git tag -a "$TAG" -m "$TAG"
git push origin "$TAG"

# 2. Fast-forward release/stable — MUST happen before step 3
git push origin main:refs/heads/release/stable

# 3. Now create the Release
gh release create "$TAG" --title "$TAG" --notes "see CHANGELOG.md"

echo "release/stable now at $(git rev-parse "$TAG")"
```

(Same shape works for `release/beta`; swap `--prerelease` in for step 3.)

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
