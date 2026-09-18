#!/usr/bin/env bash
# Check a published release from the outside, the way a user meets it.
#
#   scripts/release-verify.sh v0.18.5
#
# Every release so far has turned up a problem with the *move* to it rather
# than with the code in it, and each was found by a user: a channel branch left
# behind (0.14.0), a beta channel offering a downgrade (0.18.1), a tarball whose
# MCP wrapper had no dependencies (caught by hand during 0.18.3), an update
# that crashed for everyone (#212). These are the checks that would have caught
# them, run after step 6 of docs/release-channels.md.
#
# Read-only where it counts: it fetches, unpacks into a temporary directory,
# and asserts. It installs nothing, changes no branch and writes nothing to
# ~/.quern; it does `git fetch` the tag and main into the local object store,
# which is what lets it tell whether main contains the release. Exits nonzero on the first failure,
# naming what it expected.
#
# The rehearsal (quern#219) sets QUERN_RELEASES_URL to point the same checks at
# a candidate served locally; unset, everything below talks to GitHub.
set -euo pipefail

TAG="${1:-}"
[[ -n "$TAG" ]] || { echo "usage: $0 vX.Y.Z" >&2; exit 2; }
VERSION="${TAG#v}"

REPO="quern-dev/quern"
API="${QUERN_RELEASES_URL:-https://api.github.com/repos/$REPO}"
API="${API%/}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE="$(git -C "$ROOT" remote get-url origin)"
TEAM_ID="3QUH73KW5Q"

failures=0
ok()   { printf '  \033[0;32m✓\033[0m %s\n' "$1"; }
bad()  { printf '  \033[0;31m✗\033[0m %s\n' "$1"; failures=$((failures + 1)); }
step() { printf '\n==> %s\n' "$1"; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# --------------------------------------------------------------------------
step "What the release API says"
# --------------------------------------------------------------------------
# `|| true`, because `set -e` would otherwise end the run at the first failed
# request -- and unauthenticated api.github.com is 60 requests an hour. A
# verification that stops without saying so is the hazard this script exists to
# remove.
latest_json="$(curl -fsSL "$API/releases/latest" 2>/dev/null || true)"
latest_tag="$(printf '%s' "$latest_json" | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -1)"
if [[ -z "$latest_json" ]]; then
  # Rate limited, offline, or a network in the way. Distinct from an answer:
  # "none" here would read as an unpublished release and send the reader to
  # the wrong place.
  bad "could not ask GitHub for the latest release (rate limited or offline?)"
elif [[ "$latest_tag" == "$TAG" ]]; then
  ok "latest release is $TAG"
else
  # A draft, or a release created but never published, reads exactly like this.
  bad "latest release is ${latest_tag:-none}, expected $TAG"
fi

# --------------------------------------------------------------------------
step "Whether the updater checks can run at all"
# --------------------------------------------------------------------------
# The *released* updater, not this checkout's: 0.18.1's downgrade bug lived in
# the resolver users were running, and a check that ran the fixed local copy
# would have passed while every install was still wrong. The tarball is
# unpacked first, below, and this runs out of that tree.
#
# The interpreter is this checkout's venv, for its dependencies (`packaging`);
# the *code* comes from the release.
#
# Only the prerequisite is settled here. The resolver checks themselves need
# the unpacked tarball, so they run inside "What the tarball contains" below --
# and are skipped entirely when the asset cannot be downloaded, which is why
# that failure is reported as one `bad` covering everything it took with it.
PYTHON="$ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  bad "no venv interpreter at $PYTHON — the resolver checks need one; run setup"
fi

# --------------------------------------------------------------------------
step "Where the refs point"
# --------------------------------------------------------------------------
# Git installs update by fast-forwarding a channel branch, so a branch left
# behind strands them silently -- 0.14.0 left release/beta 27 commits back.
tag_sha="$(git ls-remote "$REMOTE" "$TAG^{}" 2>/dev/null | awk '{print $1}' | head -1 || true)"
if [[ -z "$tag_sha" ]]; then
  bad "$TAG is not on the remote"
else
  for branch in release/stable release/beta; do
    sha="$(git ls-remote "$REMOTE" "refs/heads/$branch" 2>/dev/null | awk '{print $1}' | head -1 || true)"
    if [[ "$sha" == "$tag_sha" ]]; then
      ok "$branch is at $TAG"
    else
      bad "$branch is at ${sha:-missing}, expected $TAG (${tag_sha:0:7})"
    fi
  done
  # Contains, not equals: main moves on the moment the next PR merges, and a
  # check that demanded equality would fail for every release ten minutes after
  # it was cut. What matters is that the release is an ancestor of main rather
  # than something cut from a branch that never landed.
  # Both refs fetched first: against a stale checkout this reported that main
  # did not contain a tag it has had for hours. This is the one thing here that
  # writes anything, and it writes only to the local object store.
  # Offline, or without a readable origin/main, the answer is "could not ask"
  # -- which must not print as "the release was cut from something unmerged".
  # That misreport already happened once here against a stale checkout, and it
  # sends the reader to audit a release process that is fine.
  if ! git -C "$ROOT" fetch -q origin "$TAG" main 2>/dev/null \
     || ! git -C "$ROOT" rev-parse -q --verify origin/main >/dev/null 2>&1; then
    bad "could not read origin/main, so nothing was checked about $TAG's ancestry"
  elif git -C "$ROOT" merge-base --is-ancestor "$tag_sha" "origin/main" 2>/dev/null; then
    ok "main contains $TAG"
  else
    bad "main does not contain $TAG — it was cut from something unmerged"
  fi
fi

# --------------------------------------------------------------------------
step "What the tarball contains"
# --------------------------------------------------------------------------
asset="quern-$VERSION.tar.gz"
if [[ -n "${QUERN_RELEASES_URL:-}" ]]; then
  asset_url="$API/releases/download/$TAG/$asset"
else
  asset_url="https://github.com/$REPO/releases/download/$TAG/$asset"
fi
if curl -fsSL --max-time 300 -o "$WORK/$asset" "$asset_url"; then
  ok "$asset downloaded"
  /usr/bin/tar -xzf "$WORK/$asset" -C "$WORK"
  tree="$WORK/quern-$VERSION"

  if [[ -x "$PYTHON" ]]; then
    for channel in stable beta; do
      # Both halves: the version *and* the URL. Dropping the URL was how the
      # first version of this script could not have caught its own branch
      # refusing GitHub's generated tarball.
      resolved="$("$PYTHON" -c '
import sys
sys.path.insert(0, sys.argv[1])
from server.lifecycle.updater import _fetch_latest_release
try:
    # Only releases from 0.18.5 have this; older ones follow any URL.
    from server.lifecycle.releases import asset_url_is_trusted
except ImportError:
    asset_url_is_trusted = lambda _url: True
got = _fetch_latest_release(sys.argv[2])
if got:
    print(got[0], got[1], "trusted" if asset_url_is_trusted(got[1]) else "REFUSED")
else:
    print("- - no-answer")
' "$tree" "$channel" 2>&1 | tail -1)"
      read -r offered offered_url trusted <<<"$resolved"
      if [[ "$offered" == "$VERSION" ]]; then
        ok "channel $channel offers $VERSION"
      else
        bad "channel $channel offers ${offered:-nothing}, expected $VERSION"
      fi
      if [[ "$trusted" == "trusted" ]]; then
        ok "channel $channel resolves to a URL the released code will follow"
      else
        bad "channel $channel resolved ${offered_url:-nothing} ($trusted) — the released code would refuse it"
      fi
    done
  fi

  stamped="$(sed -n 's/^version = "\(.*\)"/\1/p' "$tree/pyproject.toml" 2>/dev/null | head -1 || true)"
  [[ "$stamped" == "$VERSION" ]] && ok "pyproject says $VERSION" \
    || bad "pyproject says ${stamped:-nothing}, expected $VERSION"

  # #193: a tarball whose wrapper needs npm cannot be started from the menu
  # bar, where a version-managed node is invisible.
  [[ -f "$tree/mcp/dist/launcher.cjs" ]] && ok "mcp/dist is built" \
    || bad "mcp/dist/launcher.cjs is missing — the wrapper would need npm"
  # `|| true` on every one of these: under `set -euo pipefail` a command
  # substitution that fails takes the whole script down on its own line, so
  # the missing-node_modules case -- the one this check exists to catch --
  # killed the run before `bad` could name it, and skipped every check after
  # it. A verifier that dies where it should report is worse than no verifier:
  # the operator reads a truncated run with no failure in it.
  modules="$(find "$tree/mcp/node_modules" -maxdepth 1 -mindepth 1 2>/dev/null | wc -l | tr -d ' ' || true)"
  if [[ "${modules:-0}" -gt 10 ]]; then
    ok "mcp/node_modules ships ($modules entries)"
  else
    # It loads and then fails on its first request, which reads as quern being
    # broken rather than as a packaging mistake.
    bad "mcp/node_modules has ${modules:-0} entries — the wrapper would fail on its first request"
  fi

  app="$tree/Quern.app"
  if [[ -d "$app" ]]; then
    app_version="$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" \
      "$app/Contents/Info.plist" 2>/dev/null || true)"
    [[ "$app_version" == "$VERSION" ]] && ok "Quern.app is $VERSION" \
      || bad "Quern.app is ${app_version:-unknown}, expected $VERSION"
    req="anchor apple generic and certificate leaf[subject.OU] = \"$TEAM_ID\""
    codesign --verify --deep --strict -R="$req" "$app" 2>/dev/null \
      && ok "Quern.app is signed by $TEAM_ID" \
      || bad "Quern.app is not validly signed by $TEAM_ID"
    spctl -a -vvv "$app" >/dev/null 2>&1 \
      && ok "Gatekeeper accepts Quern.app" \
      || bad "Gatekeeper rejects Quern.app (notarization or stapling)"
  else
    bad "the tarball has no Quern.app — a release install would have no menu bar"
  fi
else
  bad "could not download $asset_url"
fi

# --------------------------------------------------------------------------
step "What quern.dev tells an older install"
# --------------------------------------------------------------------------
# The menu bar's "update available" hint comes from here, not from the API, so
# it can disagree with everything above.
if [[ -z "${QUERN_RELEASES_URL:-}" ]]; then
  answer="$(curl -fsSL "https://quern.dev/api/check-update?version=0.0.1&channel=stable" || true)"
  if printf '%s' "$answer" | grep -q '"update_available": *true'; then
    ok "quern.dev offers an update to an old install"
  else
    bad "quern.dev says no update is available: ${answer:-no answer}"
  fi
  if printf '%s' "$answer" | grep -qF "\"latest_version\":\"$VERSION\"" \
     || printf '%s' "$answer" | grep -qF "\"latest_version\": \"$VERSION\""; then
    ok "quern.dev names $VERSION"
  else
    bad "quern.dev does not name $VERSION: ${answer:-no answer}"
  fi
else
  printf '  – skipped (QUERN_RELEASES_URL is set; quern.dev reads the real repo)\n'
fi

printf '\n'
if (( failures )); then
  printf '\033[0;31m%d check(s) failed.\033[0m\n' "$failures"
  exit 1
fi
printf '\033[0;32mAll checks passed for %s.\033[0m\n' "$TAG"
