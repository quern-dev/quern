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

# `|| true` so a bad ref reaches the message below instead of aborting the
# script on this line with nothing said.
candidate_version="$(git -C "$ROOT" show "$CANDIDATE:pyproject.toml" 2>/dev/null \
  | sed -n 's/^version = "\(.*\)"/\1/p' | head -1 || true)"
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
  failures=0   # a subshell copy: a case reports only its own
  sb="$WORK/git-update"
  mkdir -p "$sb/home" "$sb/state" "$sb/bin"
  make_stubs "$sb/bin"
  build_origin "$sb/origin.git" "$sb/build"

  git clone -q -b release/stable "$sb/origin.git" "$sb/install"
  git -C "$sb/install" reset -q --hard HEAD~1     # the user is on the previous release

  export HOME="$sb/home" QUERN_STATE_DIR="$sb/state" PATH="$sb/bin:$PATH"

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

  landed="$(sed -n 's/^version = "\(.*\)"/\1/p' "$sb/install/pyproject.toml" 2>/dev/null | head -1 || true)"
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
step "Starting the candidate the way a GUI app would"
# --------------------------------------------------------------------------
# #193: the menu-bar app launches the server from a launchd context, which
# inherits a four-entry PATH and none of the user's shell startup files. A node
# installed by fnm, nvm, Volta, asdf or mise is not there. The server shells out
# to npm on every start, so a machine that could `quern start` in a terminal
# could not start the server from the menu bar at all.
#
# Runs against the tree the update case just produced, with `env -i` so the
# environment is built rather than inherited -- an exported variable leaking in
# from this shell is how a test like this passes for the wrong reason.
case_gui_start() {
  failures=0   # a subshell copy: a case reports only its own
  local sb="$WORK/git-update" install="$WORK/git-update/install"
  if [[ ! -x "$install/quern" ]]; then
    skip "GUI-style start: the update case did not leave an install to start"
    return 0
  fi

  set +e
  env -i \
    HOME="$sb/home" \
    QUERN_STATE_DIR="$sb/state" \
    PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
    "$install/quern" start > "$sb/gui-start.log" 2>&1
  local rc=$?
  set -e

  if [[ $rc -eq 0 ]]; then
    ok "the server starts with launchd's PATH and no shell startup files"
  else
    bad "start exited $rc under a GUI-style environment — see $sb/gui-start.log"
    sed -n '1,25p' "$sb/gui-start.log" | sed 's/^/      /'
  fi

  # Healthy, not merely launched: #193 presented as a process that came up and
  # then could not serve. The port is whatever the scan settled on and is
  # recorded in the sandbox's own state.json -- assuming one would test a
  # number rather than the server.
  # `server_port`, and read as JSON rather than matched with a regex: the
  # first version of this looked for "port", which is not a field -- and a
  # pattern that matches nothing reads exactly like a server that recorded
  # nothing.
  local port
  port="$(python3 -c 'import json,sys
try:
    print(json.load(open(sys.argv[1])).get("server_port", ""))
except Exception:
    print("")' "$sb/state/state.json" 2>/dev/null || true)"
  if [[ -z "$port" ]]; then
    bad "no port in the sandbox state.json — the server never recorded itself"
  elif curl -fsS --max-time 10 "http://127.0.0.1:$port/health" >/dev/null 2>&1; then
    ok "it answers /health on port $port"
  else
    bad "nothing answered http://127.0.0.1:$port/health"
  fi

  env -i HOME="$sb/home" QUERN_STATE_DIR="$sb/state" \
    PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
    "$install/quern" stop >/dev/null 2>&1 || true
  return "$failures"
}

set +e
( case_gui_start )
failures=$((failures + $?))
set -e

# --------------------------------------------------------------------------
step "The MCP wrapper the candidate ships"
# --------------------------------------------------------------------------
# What every agent client actually runs. A wrapper that cannot answer
# `initialize` is a quern that no agent can reach, and the tarball has shipped
# one: 0.18.3's had no dependencies at all.
case_mcp_handshake() {
  failures=0   # a subshell copy: a case reports only its own
  local sb="$WORK/git-update" install="$WORK/git-update/install"
  local launcher="$install/mcp/dist/launcher.cjs"
  if [[ ! -f "$launcher" ]]; then
    skip "MCP handshake: the candidate has no mcp/dist/launcher.cjs to run"
    return 0
  fi
  if ! command -v node >/dev/null 2>&1; then
    skip "MCP handshake: no node on PATH to run the wrapper with"
    return 0
  fi

  local req='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"rehearsal","version":"0"}}}'
  local out
  out="$(printf '%s\n' "$req" \
    | env HOME="$sb/home" QUERN_STATE_DIR="$sb/state" \
        timeout 30 node "$launcher" 2>&1 | head -c 4000 || true)"

  if printf '%s' "$out" | grep -q '"result"'; then
    ok "the wrapper answers initialize"
  else
    bad "the wrapper did not answer initialize"
    printf '%s\n' "$out" | sed -n '1,12p' | sed 's/^/      /'
  fi

  # A wrapper built for a newer Node than it runs on fails as a SyntaxError at
  # parse time, which reads as a corrupt file rather than as a version problem.
  if printf '%s' "$out" | grep -q "SyntaxError"; then
    bad "the wrapper raised a SyntaxError on this node ($(node --version))"
  else
    ok "no SyntaxError on $(node --version)"
  fi
  return "$failures"
}

set +e
( case_mcp_handshake )
failures=$((failures + $?))
set -e

# --------------------------------------------------------------------------
step "What each Node arrangement looks like from the candidate"
# --------------------------------------------------------------------------
# #214: four places choose a `node`, and a machine can be fine in every
# terminal while a GUI MCP client has none at all. The rows are the product --
# each carries a different fix -- so the rows are what this asserts, against
# arrangements built to order rather than against whatever this machine has.
#
# A fake `node` rather than a real one: what is under test is which place can
# *see* a node and what quern says about it, and a shim answers `--version`
# exactly as well as 90MB of V8. The version-specific behaviour that does need
# a real old Node -- the wrapper's own refusal to run on it -- is its own case.
make_fake_node() {
  mkdir -p "$1"
  cat > "$1/node" <<EOF
#!/bin/sh
[ "\$1" = "--version" ] && { echo "$2"; exit 0; }
exit 0
EOF
  chmod +x "$1/node"
}

# Just the Node block of a doctor run. Scoped because doctor prints `?` rows in
# other sections too -- the tool list alone has three on a bare machine -- and
# an assertion that greps the whole log is answered by the wrong section. The
# first version of this did exactly that and reported a failure that was not
# there.
node_section() {
  awk '/^Node\.js \(/{f=1} f&&/^$/{exit} f' "$1"
}

# The mark quern printed for one place: ✓, ✗, ? or –.
mark_for() {
  node_section "$1" | sed -n "s/^  \(.\) $2 —.*/\1/p" | head -1
}

expect_mark() {
  local log="$1" place="$2" want="$3" what="$4"
  local got
  got="$(mark_for "$log" "$place")"
  if [[ "$got" == "$want" ]]; then
    ok "$what: $place is $want"
  else
    bad "$what: $place is '${got:-nothing}', expected $want"
  fi
}

case_node_matrix() {
  failures=0   # a subshell copy: a case reports only its own
  local install="$WORK/git-update/install"
  if [[ ! -x "$install/quern" ]]; then
    skip "Node arrangements: the update case did not leave an install to ask"
    return 0
  fi

  # Each arrangement gets a home of its own: dotfiles are the whole point.
  local arrangements="$WORK/node"
  mkdir -p "$arrangements"

  run_doctor() {      # $1 = home, $2 = shell; prints nothing, writes $1/doctor.log
    ( cd "$install" && timeout 300 env -i \
        HOME="$1" QUERN_STATE_DIR="$1/state" SHELL="$2" \
        PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
        "$install/quern" doctor ) > "$1/doctor.log" 2>&1 || true
  }

  # A version manager set up in .zshrc: interactive shells only. The common
  # arrangement, and the one where the machine looks fine and an agent's tools
  # have no node.
  local h="$arrangements/zshrc-only"
  mkdir -p "$h/state"
  make_fake_node "$h/nodebin" "v22.9.0"
  : > "$h/.zshenv"
  echo 'export PATH="$HOME/nodebin:$PATH"' > "$h/.zshrc"
  run_doctor "$h" /bin/zsh
  expect_mark "$h/doctor.log" "login shell" "✓" "node in .zshrc"
  expect_mark "$h/doctor.log" "non-interactive shell" "✗" "node in .zshrc"
  expect_mark "$h/doctor.log" "GUI apps" "✗" "node in .zshrc"
  if node_section "$h/doctor.log" | grep -q "zshenv"; then
    ok "node in .zshrc: the non-interactive row names ~/.zshenv"
  else
    bad "node in .zshrc: no ~/.zshenv advice, which is the whole fix for that row"
  fi

  # The same node, moved to .zshenv: every shell sees it, GUI apps still do not.
  h="$arrangements/zshenv"
  mkdir -p "$h/state"
  make_fake_node "$h/nodebin" "v22.9.0"
  echo 'export PATH="$HOME/nodebin:$PATH"' > "$h/.zshenv"
  : > "$h/.zshrc"
  run_doctor "$h" /bin/zsh
  expect_mark "$h/doctor.log" "login shell" "✓" "node in .zshenv"
  expect_mark "$h/doctor.log" "non-interactive shell" "✓" "node in .zshenv"
  expect_mark "$h/doctor.log" "GUI apps" "✗" "node in .zshenv"

  # The field machine from #214: a real node, three majors too old, reported
  # with a green tick before that release.
  h="$arrangements/node20"
  mkdir -p "$h/state"
  make_fake_node "$h/nodebin" "v20.11.0"
  echo 'export PATH="$HOME/nodebin:$PATH"' > "$h/.zshenv"
  : > "$h/.zshrc"
  run_doctor "$h" /bin/zsh
  expect_mark "$h/doctor.log" "login shell" "✗" "node 20"
  if node_section "$h/doctor.log" | grep -q "v20.11.0"; then
    ok "node 20: the row names the version it found"
  else
    bad "node 20: the row does not say which version it found"
  fi

  # Nobody's node. Must read as missing rather than as an unanswered probe.
  h="$arrangements/none"
  mkdir -p "$h/state"
  : > "$h/.zshenv"; : > "$h/.zshrc"
  run_doctor "$h" /bin/zsh
  expect_mark "$h/doctor.log" "login shell" "✗" "no node"
  expect_mark "$h/doctor.log" "GUI apps" "✗" "no node"
  if node_section "$h/doctor.log" | grep -q "^  ? "; then
    bad "no node: a row reported itself unanswered, not missing"
  else
    ok "no node: every row is an answer, not a failed probe"
  fi

  # A shell quern cannot drive. Not a failure: it is a permanent fact about
  # the machine, and treating it as one would fail every doctor run there.
  h="$arrangements/fish"
  mkdir -p "$h/state"
  : > "$h/.zshenv"; : > "$h/.zshrc"
  run_doctor "$h" /usr/local/bin/fish
  local fish_mark
  fish_mark="$(mark_for "$h/doctor.log" "login shell")"
  if [[ "$fish_mark" == "–" || "$fish_mark" == "?" ]]; then
    ok "an unsupported shell: login shell is '$fish_mark', not a false missing"
  else
    bad "an unsupported shell: login shell is '${fish_mark:-nothing}', which reads as a real answer"
  fi
  return "$failures"
}

set +e
( case_node_matrix )
failures=$((failures + $?))
set -e

# --------------------------------------------------------------------------
step "A fresh install of the candidate"
# --------------------------------------------------------------------------
# The one-liner, against a candidate served locally. This is the path that has
# no previous version to fall back on: if the asset is wrong, a new user's
# first contact with quern is the failure.
#
# The installer lives in the site repo, so it is not here to run in CI, and
# that is said rather than hidden.
case_fresh_install() {
  failures=0   # a subshell copy: a case reports only its own
  local install="$WORK/git-update/install"
  local site_root install_sh
  site_root="$(git -C "$ROOT" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
  install_sh="${site_root:+$(dirname "$(dirname "$site_root")")/quern.dev/public/_install.sh}"
  if [[ -z "$install_sh" || ! -f "$install_sh" ]]; then
    skip "fresh install: quern.dev is not checked out beside this repo, so there is no installer to run"
    return 0
  fi
  if [[ ! -d "$install/mcp/dist" ]]; then
    skip "fresh install: the update case left no built tree to package"
    return 0
  fi

  # A terminal's PATH, not launchd's: the one-liner is pasted into a shell,
  # and Homebrew's python is how most machines have a 3.11+. Giving it the
  # four-entry GUI PATH tested nothing but Apple's system python.
  local sb="$WORK/fresh"
  mkdir -p "$sb/home" "$sb/srv/releases/download/v$candidate_version" "$sb/bin"
  make_stubs "$sb/bin"

  # The asset a release would carry, built from the tree the update case
  # produced: source, mcp/dist and its node_modules. Not a `git archive` --
  # that is the generated source tarball, which is exactly the thing whose
  # absence of dependencies broke 0.18.3.
  local staged="$sb/pkg/quern-$candidate_version"
  mkdir -p "$staged"
  ( cd "$install" && /usr/bin/tar --exclude .git --exclude .venv -cf - . ) \
    | ( cd "$staged" && /usr/bin/tar -xf - )
  ( cd "$sb/pkg" && /usr/bin/tar -czf \
      "$sb/srv/releases/download/v$candidate_version/quern-$candidate_version.tar.gz" \
      "quern-$candidate_version" )

  local port=8907
  cat > "$sb/srv/releases/latest" <<EOF
{"tag_name": "v$candidate_version", "prerelease": false,
 "assets": [{"name": "quern-$candidate_version.tar.gz",
             "browser_download_url": "http://127.0.0.1:$port/releases/download/v$candidate_version/quern-$candidate_version.tar.gz"}]}
EOF
  python3 -m http.server "$port" --directory "$sb/srv" >"$sb/srv.log" 2>&1 &
  local srv_pid=$!
  # Serve or fail loudly: a case that silently tests nothing is the thing this
  # whole script exists to stop.
  local waited=0
  until curl -fsS --max-time 2 "http://127.0.0.1:$port/releases/latest" >/dev/null 2>&1; do
    waited=$((waited + 1))
    if (( waited > 20 )); then
      { kill "$srv_pid" && wait "$srv_pid"; } 2>/dev/null || true
      bad "fresh install: the local release server never came up on $port"
      return "$failures"
    fi
    sleep 0.5
  done

  # Unattended. A rehearsal has no terminal to offer, and `script` cannot make
  # one where there is no controlling tty to begin with. So this is the
  # non-interactive contract: setup declines what it would have asked and says
  # so, and the case then finishes the install the way that message tells the
  # user to. Both halves are checked, because "installed the tree" and "left a
  # working command" are different claims.
  set +e
  env -i \
    HOME="$sb/home" \
    QUERN_STATE_DIR="$sb/home/.quern" \
    QUERN_RELEASES_URL="http://127.0.0.1:$port" \
    PATH="$sb/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
    PIP_CACHE_DIR="$PIP_CACHE_DIR" npm_config_cache="$npm_config_cache" \
    bash "$install_sh" > "$sb/install.log" 2>&1
  local rc=$?
  set -e
  # `wait` inside the same redirect, or bash reports "Terminated" on its own
  # line in the middle of the results.
  { kill "$srv_pid" && wait "$srv_pid"; } 2>/dev/null || true

  if [[ $rc -eq 0 ]]; then
    ok "install.sh exits 0 against a locally served candidate"
  elif grep -q "quern setup" "$sb/install.log"; then
    # Not a pass dressed up: exiting non-zero *and* naming the step is the
    # documented answer to having nothing to ask with. Exiting 0 here would be
    # the failure -- a caller told everything worked, with no venv.
    ok "install.sh declined what it could not ask and named the next step (exit $rc)"
  else
    bad "install.sh exited $rc without naming a next step — see $sb/install.log"
    tail -n 20 "$sb/install.log" | sed 's/^/      /'
  fi

  # It must have fetched *ours*. The override exists so a rehearsal tests the
  # candidate; an installer that quietly went to GitHub would pass every check
  # below while installing the published release.
  if grep -q "releases/download/v$candidate_version" "$sb/srv.log"; then
    ok "it downloaded the candidate from the local server"
  else
    bad "the local server was never asked for the asset — the installer went somewhere else"
  fi

  local installed="$sb/home/.local/share/quern"
  # `|| true`: under `set -e` a substitution whose command fails takes the
  # case down on its own line, and the case then reports fewer failures than
  # it found -- which is how this one first read as one failure when it had
  # two and had stopped early.
  local got
  got="$(sed -n 's/^version = "\(.*\)"/\1/p' "$installed/pyproject.toml" 2>/dev/null | head -1 || true)"
  [[ "$got" == "$candidate_version" ]] \
    && ok "it installed $candidate_version into ~/.local/share/quern" \
    || bad "the install says ${got:-nothing}, expected $candidate_version"

  # Finish it as its own message says to. A user who reads "Re-run ./quern
  # setup" and does so must end up with a working command; this is the half
  # that says whether the install is usable, not merely unpacked.
  if [[ -d "$installed" && ! -x "$sb/home/.local/bin/quern" ]]; then
    python3 -m venv "$installed/.venv" >"$sb/venv.log" 2>&1 || true
    if [[ -x "$installed/.venv/bin/python" ]]; then
      ok "a venv can be created in the installed tree"
    else
      bad "could not create a venv in the installed tree: $(tail -n 3 "$sb/venv.log" 2>/dev/null | tr '\n' ' ' || true)"
    fi
    "$installed/.venv/bin/pip" install -q -e "$installed" >"$sb/pip.log" 2>&1 \
      || bad "pip install -e failed in the installed tree: $(tail -n 3 "$sb/pip.log" 2>/dev/null | tr '\n' ' ' || true)"
    # From inside the install, which is where its own message tells the user
    # to run it -- and it matters more than it looks. `python -m server` puts
    # the caller's cwd first on sys.path, so running this from another quern
    # checkout inspects *that* tree: setup reported "venv not found" about a
    # directory it was never asked about. CONTRIBUTING names this hazard, and
    # the rehearsal walked straight into it.
    ( cd "$installed" && env -i HOME="$sb/home" QUERN_STATE_DIR="$sb/home/.quern" \
        PATH="$sb/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
        PIP_CACHE_DIR="$PIP_CACHE_DIR" npm_config_cache="$npm_config_cache" \
        "$installed/quern" setup ) > "$sb/setup.log" 2>&1 || true
  fi

  # From another directory, because the wrapper resolves its own location and
  # a cwd-dependent one works inside the tree and nowhere else.
  if [[ -x "$sb/home/.local/bin/quern" ]]; then
    set +e
    ( cd / && env -i HOME="$sb/home" QUERN_STATE_DIR="$sb/home/.quern" \
        PATH="$sb/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
        "$sb/home/.local/bin/quern" --version ) > "$sb/version.log" 2>&1
    local vrc=$?
    set -e
    if [[ $vrc -eq 0 ]] && grep -q "$candidate_version" "$sb/version.log"; then
      ok "the wrapper runs from another directory and reports $candidate_version"
    else
      bad "the wrapper failed from / (exit $vrc): $(tail -n 3 "$sb/version.log" 2>/dev/null | tr '\n' ' ' || true)"
    fi
  else
    bad "no wrapper at ~/.local/bin/quern after a fresh install and a setup run"
    tail -n 25 "$sb/setup.log" 2>/dev/null | sed 's/^/      /' || true
  fi
  return "$failures"
}

set +e
( case_fresh_install )
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
