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
# Read-only: it fetches, unpacks into a temporary directory, and asserts. It
# installs nothing and changes no branch. Exits nonzero on the first failure,
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
latest_tag="$(curl -fsSL "$API/releases/latest" | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -1)"
if [[ "$latest_tag" == "$TAG" ]]; then
  ok "latest release is $TAG"
else
  # A draft, or a release created but never published, reads exactly like this.
  bad "latest release is ${latest_tag:-none}, expected $TAG"
fi

# --------------------------------------------------------------------------
step "What quern's own updater would offer"
# --------------------------------------------------------------------------
# The updater, not a re-implementation of it: 0.18.1's downgrade bug was in the
# resolving, and a check that resolved separately would have agreed with itself.
PYTHON="$ROOT/.venv/bin/python"
[[ -x "$PYTHON" ]] || PYTHON="python3"
for channel in stable beta; do
  offered="$("$PYTHON" -c '
import sys
sys.path.insert(0, sys.argv[1])
from server.lifecycle.updater import _fetch_latest_release
got = _fetch_latest_release(sys.argv[2])
print(got[0] if got else "")
' "$ROOT" "$channel" 2>/dev/null || true)"
  if [[ "$offered" == "$VERSION" ]]; then
    ok "channel $channel offers $VERSION"
  else
    bad "channel $channel offers ${offered:-nothing}, expected $VERSION"
  fi
done

# --------------------------------------------------------------------------
step "Where the refs point"
# --------------------------------------------------------------------------
# Git installs update by fast-forwarding a channel branch, so a branch left
# behind strands them silently -- 0.14.0 left release/beta 27 commits back.
tag_sha="$(git ls-remote "$REMOTE" "$TAG^{}" | awk '{print $1}' | head -1)"
if [[ -z "$tag_sha" ]]; then
  bad "$TAG is not on the remote"
else
  for branch in release/stable release/beta; do
    sha="$(git ls-remote "$REMOTE" "refs/heads/$branch" | awk '{print $1}' | head -1)"
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
  git -C "$ROOT" fetch -q origin "$TAG" 2>/dev/null || true
  if git -C "$ROOT" merge-base --is-ancestor "$tag_sha" "origin/main" 2>/dev/null; then
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

  stamped="$(sed -n 's/^version = "\(.*\)"/\1/p' "$tree/pyproject.toml" | head -1)"
  [[ "$stamped" == "$VERSION" ]] && ok "pyproject says $VERSION" \
    || bad "pyproject says ${stamped:-nothing}, expected $VERSION"

  # #193: a tarball whose wrapper needs npm cannot be started from the menu
  # bar, where a version-managed node is invisible.
  [[ -f "$tree/mcp/dist/launcher.cjs" ]] && ok "mcp/dist is built" \
    || bad "mcp/dist/launcher.cjs is missing — the wrapper would need npm"
  if [[ -d "$tree/mcp/node_modules" ]]; then
    ok "mcp/node_modules ships ($(find "$tree/mcp/node_modules" -maxdepth 1 -type d | wc -l | tr -d ' ') entries)"
  else
    # It loads and then fails on its first request, which reads as quern being
    # broken rather than as a packaging mistake.
    bad "mcp/node_modules is missing — the wrapper would fail on its first request"
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
  if printf '%s' "$answer" | grep -q "\"latest_version\": *\"$VERSION\""; then
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
