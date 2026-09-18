#!/usr/bin/env bash
# Rehearse the *move* to a candidate, before it is tagged (quern#219).
#
#   scripts/release-rehearsal.sh [candidate-ref] [previous-tag]
#
# release-verify.sh checks a release after it is published. This checks the
# thing that has actually broken: updating into it. Every release so far has
# shipped a defect in the move rather than in the code -- a channel branch left
# behind (0.14.0), a downgrade offer (0.18.1), a wrapper with no dependencies
# (0.18.3), and an update that crashed for everyone (#212) -- and #212 in
# particular could only ever have been caught by running the *previous
# release's* updater against the candidate, which is what this does.
#
# The old code is the point. The candidate's own updater is not exercised here
# at all: users run the one they already have.
#
# Each case runs in its own sandbox -- its own HOME and QUERN_STATE_DIR, with
# osascript/open/sudo/launchctl/pkill/killall stubbed ahead of the real ones on
# PATH. Nothing outside the sandbox is written. That is not a nicety: this
# project's own test suite twice deleted the developer's `quern` command and
# twice rewrote a Claude hook, because a path was not redirected.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CANDIDATE="${1:-HEAD}"

# The newest *published* release, not the newest tag. A release that was cut
# and then pulled back to a draft leaves its tag behind -- 0.18.3 is exactly
# this -- and no user is on it, so rehearsing from it rehearses a move nobody
# will make. `releases/latest` excludes drafts and prereleases by definition.
latest_published() {
  gh api repos/quern-dev/quern/releases/latest -q .tag_name 2>/dev/null \
    || git -C "$ROOT" tag --list 'v*' --sort=-v:refname | head -1
}
PREV="${2:-$(latest_published)}"

failures=0
skips=0
ok()   { printf '  \033[0;32m✓\033[0m %s\n' "$1"; }
bad()  { printf '  \033[0;31m✗\033[0m %s\n' "$1"; failures=$((failures + 1)); }
skip() { printf '  – %s\n' "$1"; skips=$((skips + 1)); }
step() { printf '\n==> %s\n' "$1"; }

candidate_version="$(git -C "$ROOT" show "$CANDIDATE:pyproject.toml" \
  | sed -n 's/^version = "\(.*\)"/\1/p' | head -1)"
prev_version="${PREV#v}"

printf 'Rehearsing %s (%s) from %s\n' "$CANDIDATE" "${candidate_version:-?}" "$PREV"
[[ -n "$candidate_version" ]] || { echo "error: no version in $CANDIDATE:pyproject.toml" >&2; exit 2; }
if [[ "$candidate_version" == "$prev_version" ]]; then
  echo "error: candidate and previous are both $candidate_version — bump first" >&2
  exit 2
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Caches from the real home, so a case is not a network test. Deliberately the
# only two things a sandbox borrows from outside it, and both are read-mostly
# stores that pip and npm are built to share.
export PIP_CACHE_DIR="${PIP_CACHE_DIR:-$HOME/.cache/pip}"
export npm_config_cache="${npm_config_cache:-$HOME/.npm}"

REAL_HOME="$HOME"

# --------------------------------------------------------------------------
# Sandbox
# --------------------------------------------------------------------------

# Stub anything that would reach out of the sandbox and touch the machine.
# `open` and `osascript` drive the menu-bar app, `launchctl` the login item,
# `pkill`/`killall` would find the developer's own processes, and `sudo` is
# never acceptable unattended. Each records that it was called, so a case can
# assert on what was attempted.
make_stubs() {
  local bin="$1"
  mkdir -p "$bin"
  local tool
  for tool in osascript open sudo launchctl pkill killall; do
    cat > "$bin/$tool" <<EOF
#!/bin/sh
echo "$tool \$*" >> "$bin/../calls.log"
exit 0
EOF
    chmod +x "$bin/$tool"
  done
}

# A repository whose history is two commits: the previous release's tree, then
# the candidate's. Synthetic on purpose -- the shas are unknown to quern.dev,
# which answers "update available" for anything it does not recognise, so the
# update proceeds on the git path rather than being told it is current. Real
# shas would have the previous *release* answered as up to date, and the case
# would exit 2 having tested nothing.
build_origin() {
  local origin="$1" build="$2"
  git init -q --bare "$origin"
  git init -q -b release/stable "$build"
  git -C "$build" config user.email rehearsal@localhost
  git -C "$build" config user.name Rehearsal

  git -C "$ROOT" archive --format=tar "$PREV" | tar -x -C "$build"
  git -C "$build" add -A
  git -C "$build" commit -qm "$PREV"

  # Everything tracked goes, so files the candidate deletes are really deleted
  # rather than left behind by an overlay.
  git -C "$build" rm -rq --cached . >/dev/null
  find "$build" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
  git -C "$ROOT" archive --format=tar "$CANDIDATE" | tar -x -C "$build"
  git -C "$build" add -A
  git -C "$build" commit -qm "candidate $candidate_version"

  git -C "$build" push -q "$origin" release/stable
}

# --------------------------------------------------------------------------
step "A git install updating from $PREV to $candidate_version"
# --------------------------------------------------------------------------
# The case #212 would have failed: `quern update` on a clone, run by the code
# the user already has, ending in a tree that is the candidate and a server
# that can still start.
#
# Run in a subshell, because a case exports HOME and PATH and must not leak
# them into the next one -- which means its `failures` are a copy, and the
# subshell's exit status is the only way back. The first version of this script
# did not do that, printed a ✗ and summarised "the rehearsal passed": the exact
# defect it exists to catch, inside itself.
case_git_update() {
  sb="$WORK/git-update"
  mkdir -p "$sb/home" "$sb/state" "$sb/bin"
  make_stubs "$sb/bin"
  build_origin "$sb/origin.git" "$sb/build"

  git clone -q -b release/stable "$sb/origin.git" "$sb/install"
  git -C "$sb/install" reset -q --hard HEAD~1     # the user is on the previous release

  export HOME="$sb/home" QUERN_STATE_DIR="$sb/state" PATH="$sb/bin:$PATH"
  export QUERN_PORT=9187

  # The venv the user would already have. Built from the *previous* release's
  # metadata, because that is what they installed.
  python3 -m venv -q "$sb/install/.venv" 2>/dev/null || python3 -m venv "$sb/install/.venv"
  "$sb/install/.venv/bin/pip" install -q -e "$sb/install" >"$sb/pip.log" 2>&1 || {
    echo "    (pip install failed; see $sb/pip.log)" >&2
  }

  set +e
  ( cd "$sb/install" && ./quern update ) > "$sb/update.log" 2>&1
  rc=$?
  set -e

  if [[ $rc -eq 0 ]]; then
    ok "the previous release's updater exits 0"
  else
    bad "the previous release's updater exited $rc — see $sb/update.log"
    sed -n '1,40p' "$sb/update.log" | sed 's/^/      /'
  fi

  if grep -q "Traceback (most recent call last)" "$sb/update.log"; then
    # #212 was exactly this: a traceback at the end of an update that had
    # already replaced the source.
    bad "the update printed a traceback"
    grep -A 6 "Traceback (most recent call last)" "$sb/update.log" | sed 's/^/      /'
  else
    ok "no traceback"
  fi

  landed="$(sed -n 's/^version = "\(.*\)"/\1/p' "$sb/install/pyproject.toml" | head -1)"
  [[ "$landed" == "$candidate_version" ]] \
    && ok "the tree is $candidate_version" \
    || bad "the tree is ${landed:-nothing}, expected $candidate_version"

  # The record every surface reads -- the CLI, the menu bar, the result file.
  # An update that says it worked and writes nothing here is the state #212
  # left behind.
  result="$sb/state/last-update.json"
  if [[ -f "$result" ]]; then
    # The field is `outcome`, and it is what every surface reads: the CLI, the
    # menu bar's isNoOp, and the alert after a failed update.
    if grep -q '"outcome" *: *"updated"' "$result" \
       && grep -q "\"version\" *: *\"$candidate_version\"" "$result"; then
      ok "last-update.json records updated → $candidate_version"
    else
      bad "last-update.json says $(tr -d '\n' < "$result")"
    fi
  else
    bad "no last-update.json was written"
  fi

  [[ -f "$sb/install/mcp/dist/launcher.cjs" ]] \
    && ok "the MCP wrapper was rebuilt" \
    || bad "mcp/dist/launcher.cjs is missing — the wrapper would need npm at start"

  # Setup runs as part of the update, and its whole job is writing outside the
  # project. It must have written inside the sandbox.
  [[ -x "$sb/home/.local/bin/quern" ]] \
    && ok "setup wrote the wrapper into the sandbox home" \
    || bad "no wrapper at \$HOME/.local/bin/quern — did setup run?"

  return "$failures"
}

set +e
( case_git_update )
failures=$((failures + $?))
set -e

# --------------------------------------------------------------------------
step "A tarball install updating from $PREV to $candidate_version"
# --------------------------------------------------------------------------
# Needs the *previous* release to honour QUERN_RELEASES_URL, since that is the
# code doing the fetching, and only 0.18.5 and later do. Until then the old
# updater would resolve against GitHub and download the published release --
# rehearsing nothing, while printing the same lines as a real run.
prev_has_override=0
git -C "$ROOT" cat-file -e "$PREV:server/lifecycle/releases.py" 2>/dev/null && prev_has_override=1
if (( prev_has_override )); then
  skip "tarball update: not implemented yet (the previous release can host it now)"
else
  skip "tarball update: $PREV predates QUERN_RELEASES_URL, so its updater would fetch the published release instead of the candidate"
fi

# --------------------------------------------------------------------------
step "Nothing outside the sandbox was touched"
# --------------------------------------------------------------------------
# The backstop, because the cost of getting this wrong is measured in this
# repo's own history rather than in theory.
[[ "$HOME" == "$REAL_HOME" ]] && ok "the real HOME was restored" || bad "HOME is $HOME"
if [[ -e "$REAL_HOME/.local/bin/quern" ]]; then
  ok "the real ~/.local/bin/quern is still there"
else
  skip "no ~/.local/bin/quern to protect on this machine"
fi

printf '\n'
if (( failures )); then
  printf '\033[0;31m%d check(s) failed.\033[0m\n' "$failures"
  exit 1
fi
if (( skips )); then
  printf '\033[0;32mThe rehearsal passed\033[0m — %d skipped, listed above.\n' "$skips"
else
  printf '\033[0;32mThe rehearsal passed.\033[0m\n'
fi
