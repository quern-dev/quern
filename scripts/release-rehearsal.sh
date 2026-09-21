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
# IT REHEARSES COMMITTED WORK. Both trees come from `git archive`, so anything
# still in the working directory is not in the candidate -- and the failure
# looks exactly like the fix not working, twice over: a result that disagrees
# with the unit tests you just watched pass is this, until proved otherwise.
# Commit, then rehearse.
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
  local tag
  tag="$(gh api repos/quern-dev/quern/releases/latest -q .tag_name 2>/dev/null || true)"
  if [[ -n "$tag" ]]; then
    printf '%s' "$tag"
    return 0
  fi
  # Not a silent fall back to the newest tag: this function exists *because*
  # the newest tag can be a release that was pulled back to a draft, and
  # quietly using one would rehearse a move nobody will make.
  echo "error: could not ask GitHub for the latest published release." >&2
  echo "       Pass the previous tag explicitly: $0 <candidate> <tag>" >&2
  return 1
}
PREV="${2:-$(latest_published)}"

failures=0
ok()   { printf '  \033[0;32m✓\033[0m %s\n' "$1"; }
bad()  { printf '  \033[0;31m✗\033[0m %s\n' "$1"; failures=$((failures + 1)); }
step() { printf '\n==> %s\n' "$1"; }

# Skips go to a file rather than a variable. A case runs in a subshell, so it
# can only hand back one number, and that is its failure count -- a skip
# incremented inside one was counted in a copy and thrown away. Four cases can
# skip everything they do and the summary said "the rehearsal passed" with no
# mention of a skip at all, which is the shape of the bug this whole script
# exists to catch.
SKIPS_FILE=""
skip() {
  printf '  – %s\n' "$1"
  [[ -n "$SKIPS_FILE" ]] && printf '%s\n' "$1" >> "$SKIPS_FILE"
  return 0
}
skip_count() {
  [[ -s "$SKIPS_FILE" ]] && wc -l < "$SKIPS_FILE" | tr -d ' ' || echo 0
}

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
SKIPS_FILE="$WORK/skips"
: > "$SKIPS_FILE"

# Caches from the real home, so a case is not a network test. Deliberately the
# only two things a sandbox borrows from outside it, and both are read-mostly
# stores that pip and npm are built to share.
export PIP_CACHE_DIR="${PIP_CACHE_DIR:-$HOME/.cache/pip}"
export npm_config_cache="${npm_config_cache:-$HOME/.npm}"

REAL_HOME="$HOME"

# Well clear of 9100/9101. See the note in case_gui_start: the default ports
# are the developer's, and quern reclaims a port by killing whatever quern-ish
# process holds it.
REHEARSAL_PORT=9187
REHEARSAL_PROXY_PORT=9188

# Everything outside the sandbox that a case could plausibly damage, recorded
# before anything runs and compared after. A snapshot, because the check this
# replaces asked whether ~/.local/bin/quern still *existed* -- so deleting it
# printed a skip, left `failures` at zero, and the run reported "the rehearsal
# passed". The one disaster this repo has actually had, reported as a shrug.
# Rewriting it in place passed too, and setup bakes a project path into it.
PROTECTED=(
  "$REAL_HOME/.local/bin/quern"
  "$REAL_HOME/.quern/api-key"
  "$REAL_HOME/.quern/config.json"
  "$REAL_HOME/.claude/settings.json"
  # A symlink into this checkout. Setup repoints it, and a review agent
  # repointed this one at a temporary directory earlier today by running a
  # real setup by accident -- so it is exactly the shape this guards.
  "$REAL_HOME/.claude/skills/quern-api"
)

# The MCP client configs are watched by *content that belongs to quern* rather
# than by file hash. Their owners rewrite them for their own reasons -- Claude
# Code stores session state in ~/.claude.json and changes it every few seconds
# -- so a whole-file hash reports a failure on every run that takes a minute.
# A backstop that cries wolf is one that gets ignored, which is how the thing
# it guards against ships. What matters here is quern's own registration: the
# entry setup rewrites, and the one this project has twice pointed at a
# temporary directory.
MCP_CONFIGS=(
  "$REAL_HOME/.claude.json"
  "$REAL_HOME/.cursor/mcp.json"
)

quern_entries() {
  python3 -c '
import json, sys
try:
    with open(sys.argv[1]) as fh:
        data = json.load(fh)
except Exception:
    print("unreadable")
    raise SystemExit
servers = data.get("mcpServers") or {}
quern = {k: v for k, v in servers.items() if "quern" in k.lower()}
print(json.dumps(quern, sort_keys=True))' "$1" 2>/dev/null || echo "unreadable"
}

# Contents *and* the metadata that decides what the contents mean. A hash
# alone misses a wrapper made non-executable, and misses a symlink repointed
# at a tree whose file happens to be identical -- and `shasum` follows the
# link, so it would report on the wrong file entirely without complaint.
snapshot_protected() {
  local path
  for path in "${PROTECTED[@]}"; do
    if [[ -L "$path" ]]; then
      printf '%s\tsymlink -> %s\n' "$path" "$(readlink "$path")"
    elif [[ -e "$path" ]]; then
      printf '%s\t%s mode=%s\n' "$path" \
        "$(shasum -a 256 "$path" | awk '{print $1}')" \
        "$(stat -f '%Lp' "$path" 2>/dev/null || echo '?')"
    else
      printf '%s\tabsent\n' "$path"
    fi
  done
  for path in "${MCP_CONFIGS[@]}"; do
    if [[ -e "$path" ]]; then
      printf '%s (quern entry)\t%s\n' "$path" "$(quern_entries "$path")"
    else
      printf '%s (quern entry)\tabsent\n' "$path"
    fi
  done
}

# The developer's server, if one is running. Killing it is the specific
# accident the sandbox ports exist to prevent, so it is also the one this
# checks rather than assumes.
real_server_pid() {
  python3 -c 'import json,sys
try:
    print(json.load(open(sys.argv[1])).get("pid", ""))
except Exception:
    print("")' "$REAL_HOME/.quern/state.json" 2>/dev/null || true
}

BEFORE="$(snapshot_protected)"
REAL_PID="$(real_server_pid)"

# --------------------------------------------------------------------------
# Sandbox
# --------------------------------------------------------------------------

# Stub anything that would reach out of the sandbox and touch the machine.
# `open` and `osascript` drive the menu-bar app, `launchctl` the login item,
# `pkill`/`killall` would find the developer's own processes, and `sudo` is
# never acceptable unattended. Each records that it was called, so a case can
# assert on what was attempted.
STUB_BIN=""

make_stubs() {
  local bin="$1"
  STUB_BIN="$bin"
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
  # The developer's ~/.gitconfig is not the rehearsal's: a global
  # `core.hooksPath` or `init.templateDir` would otherwise run their hooks
  # against this synthetic repository.
  export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
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

# The candidate, staged as the asset a release would carry and served on a
# free port. Two cases want this -- a fresh install and a tarball update --
# and a second copy of the staging is how the two end up testing different
# bytes while claiming to test the candidate.
#
# Built from the tree the git-update case produced: source, mcp/dist and its
# node_modules. Not a `git archive`, which is the generated source tarball --
# the one whose missing dependencies broke 0.18.3.
#
# Sets CANDIDATE_PORT and CANDIDATE_SRV_PID. Returns non-zero if it never
# came up, because a case that silently tests nothing is the thing this whole
# script exists to stop.
CANDIDATE_PORT=""
CANDIDATE_SRV_PID=""

serve_candidate() {
  local sb="$1" srv="$1/srv"
  local built="$WORK/git-update/install"
  mkdir -p "$srv/releases/download/v$candidate_version" "$sb/pkg"

  local staged="$sb/pkg/quern-$candidate_version"
  mkdir -p "$staged"
  ( cd "$built" && /usr/bin/tar --exclude .git --exclude .venv -cf - . ) \
    | ( cd "$staged" && /usr/bin/tar -xf - )
  ( cd "$sb/pkg" && /usr/bin/tar -czf \
      "$srv/releases/download/v$candidate_version/quern-$candidate_version.tar.gz" \
      "quern-$candidate_version" )

  # A port nobody else is on, so two rehearsals can run at once.
  CANDIDATE_PORT="$(python3 -c "
import socket
s = socket.socket(); s.bind(('127.0.0.1', 0))
print(s.getsockname()[1]); s.close()" 2>/dev/null || echo 8907)"

  cat > "$srv/releases/latest" <<EOF
{"tag_name": "v$candidate_version", "prerelease": false,
 "assets": [{"name": "quern-$candidate_version.tar.gz",
             "browser_download_url": "http://127.0.0.1:$CANDIDATE_PORT/releases/download/v$candidate_version/quern-$candidate_version.tar.gz"}],
 "tarball_url": "http://127.0.0.1:$CANDIDATE_PORT/releases/download/v$candidate_version/quern-$candidate_version.tar.gz"}
EOF
  python3 -m http.server "$CANDIDATE_PORT" --directory "$srv" >"$sb/srv.log" 2>&1 &
  CANDIDATE_SRV_PID=$!

  local waited=0
  until curl -fsS --max-time 2 \
      "http://127.0.0.1:$CANDIDATE_PORT/releases/latest" >/dev/null 2>&1; do
    waited=$((waited + 1))
    if (( waited > 20 )); then
      stop_candidate
      return 1
    fi
    sleep 0.5
  done
  return 0
}

stop_candidate() {
  [[ -n "$CANDIDATE_SRV_PID" ]] || return 0
  # `wait` inside the same redirect, or bash reports "Terminated" on its own
  # line in the middle of the results.
  { kill "$CANDIDATE_SRV_PID" && wait "$CANDIDATE_SRV_PID"; } 2>/dev/null || true
  CANDIDATE_SRV_PID=""
}

# Did the local server actually serve the asset? Without this a case passes
# while the fetcher quietly went to GitHub and installed the published
# release -- the substitution QUERN_RELEASES_URL exists to make visible.
served_the_candidate() {
  local log="$1" line
  line="$(grep -F \
    "releases/download/v$candidate_version/quern-$candidate_version.tar.gz" \
    "$log" 2>/dev/null | tail -1 || true)"
  [[ -n "$line" && "$line" == *'" 200 '* ]]
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
  # No `-q`: venv has no such flag, so the first branch always failed and the
  # fallback ran with its chatter in the middle of the results.
  python3 -m venv "$sb/install/.venv" > "$sb/venv.log" 2>&1 || true
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

  # Ports of its own, and this is not tidiness. `quern start` reclaims its
  # port: it looks up whatever holds it and, if the argv looks like a quern,
  # SIGTERMs and then SIGKILLs it (server/lifecycle/ports.py). A separate
  # QUERN_STATE_DIR does not enter into that decision, so a sandbox server
  # taking the default 9100 would kill the *developer's* running server, and
  # its proxy on 9101. No stub can cover it -- the kill is os.kill, in process.
  set +e
  env -i \
    HOME="$sb/home" \
    QUERN_STATE_DIR="$sb/state" \
    PATH="$sb/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
    "$install/quern" start --port "$REHEARSAL_PORT" \
      --proxy-port "$REHEARSAL_PROXY_PORT" > "$sb/gui-start.log" 2>&1
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
    PATH="$sb/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
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
  # Absolute paths, resolved out here: `env -i` with a built PATH loses
  # anything Homebrew provides, and `timeout` is one -- so the wrapper never
  # ran and the case reported an `env` error as the wrapper's answer.
  local node_bin timeout_bin
  node_bin="$(command -v node)"
  timeout_bin="$(command -v timeout || command -v gtimeout || true)"
  local out
  out="$(printf '%s\n' "$req" \
    | env -i HOME="$sb/home" QUERN_STATE_DIR="$sb/state" \
        PATH="$STUB_BIN:$(dirname "$node_bin"):/usr/bin:/bin:/usr/sbin:/sbin" \
        ${timeout_bin:+"$timeout_bin" 30} "$node_bin" "$launcher" 2>&1 \
    | head -c 4000 || true)"

  local answered=0
  if printf '%s' "$out" | grep -q '"result"'; then
    ok "the wrapper answers initialize"
    answered=1
  else
    bad "the wrapper did not answer initialize"
    printf '%s\n' "$out" | sed -n '1,12p' | sed 's/^/      /'
  fi

  # Only meaningful if the wrapper actually ran. Grepping the output of a
  # wrapper that never started finds no SyntaxError and reports a tick, which
  # is the same false pass as grepping an empty section for a `?` row -- and
  # it happened here, against `env: timeout: No such file or directory`.
  if (( ! answered )); then
    skip "SyntaxError check: the wrapper did not run, so there is nothing to judge"
  elif printf '%s' "$out" | grep -q "SyntaxError"; then
    bad "the wrapper raised a SyntaxError on this node ($("$node_bin" --version))"
  else
    ok "no SyntaxError on $("$node_bin" --version)"
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
  # `|| true`: under `pipefail`, `head` closing the pipe early can surface as
  # 141 and, in a bare assignment under `set -e`, take the case down mid-way.
  node_section "$1" | sed -n "s/^  \(.\) $2 —.*/\1/p" | head -1 || true
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
    # The stub directory first on PATH, like every other case. Nothing on
    # doctor's path calls the stubbed tools today, but `open` and `osascript`
    # are one refactor away in setup.py -- and the second quits the
    # developer's real menu-bar app, by name.
    ( cd "$install" && timeout 300 env -i \
        HOME="$1" QUERN_STATE_DIR="$1/state" SHELL="$2" \
        PATH="$STUB_BIN:/usr/bin:/bin:/usr/sbin:/sbin" \
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
  # The positive precondition first. A negative grep over an *empty* section
  # is satisfied by nothing at all, and that is not hypothetical: 0.18.3's
  # doctor has no Node section, so this printed a tick against a section that
  # did not exist. Same shape as the `port` field that was never there.
  local rows
  rows="$(node_section "$h/doctor.log" | grep -c "^  [✓✗?–] " || true)"
  if (( rows < 3 )); then
    bad "no node: the Node section has $rows rows — there is nothing to judge"
  elif node_section "$h/doctor.log" | grep -q "^  ? "; then
    bad "no node: a row reported itself unanswered, not missing"
  else
    ok "no node: all $rows rows are answers, not failed probes"
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
  mkdir -p "$sb/home" "$sb/bin"
  make_stubs "$sb/bin"

  if ! serve_candidate "$sb"; then
    bad "fresh install: the local release server never came up"
    return "$failures"
  fi
  local port="$CANDIDATE_PORT"

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
  stop_candidate

  if [[ $rc -eq 0 ]]; then
    ok "install.sh exits 0 against a locally served candidate"
  elif grep -q "declined without asking" "$sb/install.log"; then
    # Not a pass dressed up: exiting non-zero *and* reporting what it could
    # not ask is the documented answer to having no terminal. Exiting 0 here
    # would be the failure -- a caller told everything worked, with no venv.
    #
    # Matched on that report rather than on the string "quern setup", which
    # appears in the output of a genuinely failed setup too, so any non-zero
    # exit satisfied it.
    ok "install.sh declined what it could not ask and named the next step (exit $rc)"
  else
    bad "install.sh exited $rc without naming a next step — see $sb/install.log"
    tail -n 20 "$sb/install.log" | sed 's/^/      /'
  fi

  # It must have fetched *ours*. The override exists so a rehearsal tests the
  # candidate; an installer that quietly went to GitHub would pass every check
  # below while installing the published release.
  if served_the_candidate "$sb/srv.log"; then
    ok "it downloaded the candidate from the local server"
  else
    bad "the local server did not serve the asset — the installer went somewhere else"
    tail -n 3 "$sb/srv.log" 2>/dev/null | sed 's/^/      /' || true
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
    # The second half of the documented install. `install.sh` runs this as
    # its own step, and it is the only thing that points MCP clients at the
    # new install -- setup does not.
  fi

  # Unconditional, and it was not: this sat inside the "setup did not finish"
  # branch above, which stopped being taken once setup created the venv
  # without asking. The registration step then never ran, and the check for
  # it reported the absence of a file nobody had tried to write. The
  # documented install runs this every time.
  #
  # The caller's PATH, since `quern mcp-install` is run from the user's shell
  # and may rebuild the wrapper, which needs their node.
  ( cd "$installed" && env -i HOME="$sb/home" QUERN_STATE_DIR="$sb/home/.quern" \
      PATH="$sb/bin:$PATH" npm_config_cache="$npm_config_cache" \
      "$installed/quern" mcp-install ) > "$sb/mcp-install.log" 2>&1 || true

  # The MCP registration the installer writes, checked for *where it points*.
  # This project has twice written a path into another tool's config that was
  # wrong -- once a temporary directory -- and nothing notices, because the
  # client keeps launching whatever the entry says until someone wonders why
  # their tools are stale. `install.sh` runs `quern mcp-install` as its own
  # step; setup does not do this, which is why it is checked here.
  local claude_json="$sb/home/.claude.json"
  if [[ -f "$claude_json" ]]; then
    local entry
    entry="$(python3 -c '
import json, sys
try:
    servers = json.load(open(sys.argv[1])).get("mcpServers") or {}
except Exception:
    print(""); raise SystemExit
for name, spec in servers.items():
    if "quern" in name.lower():
        print(" ".join([spec.get("command", "")] + list(spec.get("args") or [])))
        break' "$claude_json" 2>/dev/null || true)"
    if [[ -z "$entry" ]]; then
      bad "install.sh wrote no quern entry into .claude.json"
    elif [[ "$entry" == *"$installed/mcp/dist/launcher.cjs"* ]]; then
      ok "the MCP registration points into the install it just made"
    else
      bad "the MCP registration points somewhere else: $entry"
    fi
  else
    bad "no .claude.json after mcp-install — the clients were never pointed anywhere"
    tail -n 6 "$sb/mcp-install.log" 2>/dev/null | sed 's/^/      /' || true
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
step "A git install updating while its server is running"
# --------------------------------------------------------------------------
# Every case above updates with the daemon stopped. Users do not: they run
# `quern update` with quern running, and the updater restarts it. That restart
# is where "the server did not come back" lives -- the condition the menu
# bar's recovery items (#225, #226) exist for -- and nothing exercised it.
#
# Safety first, because this one can reach outside the sandbox. `quern start`
# reclaims its port by SIGKILLing whatever quern-looking process holds it, and
# it does not consult QUERN_STATE_DIR. The restart inside an update asks for
# the *default* port -- `restart` takes no port and `_resolve_args` falls back
# to 9100 -- whatever port the server was actually on. So a rehearsal that let
# the restart run unimpeded would kill the developer's own server.
#
# The fix is a decoy: hold 9100 and 9101 with plain listeners that are not
# quern. `reclaim_port` then reports them busy rather than killing them, and
# the restarted server scans upward, exactly as it would beside any other
# application. If the ports cannot be held -- because a real quern already has
# them -- the case refuses to run rather than taking that server down.
hold_default_ports() {
  local sb="$1"
  python3 - "$sb/decoy.pid" <<'PY' > "$sb/decoy.log" 2>&1 &
import socket, sys, time

# Deliberately NOT SO_REUSEADDR. The bind failing is the whole signal: it is
# how this knows a real quern already has the port, so the case can refuse to
# run rather than let the restart reclaim it. With SO_REUSEADDR set, binding
# 127.0.0.1:9100 SUCCEEDS while a server holds 0.0.0.0:9100 -- so the guard
# reported the ports held, the case ran, and `reclaim_port` (which asks lsof,
# and finds the real listener) killed the developer's server. Measured: it
# did, twice.
held = []
for port in (9100, 9101):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
    except OSError:
        print(f"could not hold {port}", flush=True)
        raise SystemExit(1)
    s.listen(16)
    held.append(s)
print("held", flush=True)
time.sleep(900)
PY
  DECOY_PID=$!
  local waited=0
  until grep -q "held" "$sb/decoy.log" 2>/dev/null; do
    if ! kill -0 "$DECOY_PID" 2>/dev/null; then return 1; fi
    waited=$((waited + 1))
    (( waited > 20 )) && return 1
    sleep 0.25
  done
  return 0
}

release_default_ports() {
  [[ -n "${DECOY_PID:-}" ]] || return 0
  { kill "$DECOY_PID" && wait "$DECOY_PID"; } 2>/dev/null || true
  DECOY_PID=""
}

case_running_server_update() {
  failures=0   # a subshell copy: a case reports only its own
  local sb="$WORK/running-update"
  mkdir -p "$sb/home" "$sb/state" "$sb/bin"
  make_stubs "$sb/bin"

  if ! hold_default_ports "$sb"; then
    skip "running-server update: 9100/9101 are already taken, most likely by your own quern — this case would restart onto them and kill it. Stop your server and re-run to exercise it."
    return 0
  fi

  build_origin "$sb/origin.git" "$sb/build"
  git clone -q -b release/stable "$sb/origin.git" "$sb/install"
  git -C "$sb/install" reset -q --hard HEAD~1

  export HOME="$sb/home" QUERN_STATE_DIR="$sb/state" PATH="$sb/bin:$PATH"

  python3 -m venv "$sb/install/.venv" > "$sb/venv.log" 2>&1 || true
  "$sb/install/.venv/bin/pip" install -q -e "$sb/install" > "$sb/pip.log" 2>&1 || true

  # The previous release, running, on ports of its own.
  set +e
  ( cd "$sb/install" && "$sb/install/quern" start --port 9190 --proxy-port 9191 ) \
    > "$sb/start.log" 2>&1
  local started=$?
  set -e
  if [[ $started -ne 0 ]]; then
    bad "running-server update: $PREV would not start, so there was nothing to update under"
    tail -n 12 "$sb/start.log" | sed 's/^/      /'
    release_default_ports
    return "$failures"
  fi
  ok "$PREV's server is up before the update"

  set +e
  ( cd "$sb/install" && "$sb/install/quern" update ) > "$sb/update.log" 2>&1
  local rc=$?
  set -e

  if [[ $rc -eq 0 ]]; then
    ok "the update exits 0 with the server running"
  else
    bad "the update exited $rc — see $sb/update.log"
    tail -n 20 "$sb/update.log" | sed 's/^/      /'
  fi

  if grep -q "Traceback (most recent call last)" "$sb/update.log"; then
    bad "the update printed a traceback"
  else
    ok "no traceback"
  fi

  # The point of the case. A server that does not come back is the state the
  # menu bar grew a recovery item for, and it is silent from the CLI.
  local port
  port="$(python3 -c 'import json,sys
try:
    print(json.load(open(sys.argv[1])).get("server_port", ""))
except Exception:
    print("")' "$sb/state/state.json" 2>/dev/null || true)"
  if [[ -z "$port" ]]; then
    bad "no server_port in state.json — the server did not come back from the update"
  elif curl -fsS --max-time 15 "http://127.0.0.1:$port/health" >/dev/null 2>&1; then
    ok "the server came back after the update, on port $port"
  else
    bad "nothing answers /health on port $port — the server did not come back"
  fi

  # It has to come back as the *candidate*, not the version it was.
  local running
  running="$(curl -fsS --max-time 15 "http://127.0.0.1:${port:-0}/health" 2>/dev/null \
    | sed -n 's/.*"version" *: *"\([^"]*\)".*/\1/p' | head -1 || true)"
  if [[ -z "$running" ]]; then
    skip "running-server update: /health did not report a version to compare"
  elif [[ "$running" == "$candidate_version" ]]; then
    ok "it is serving $candidate_version"
  else
    bad "it came back on $running, expected $candidate_version — the restart picked up the old tree"
  fi

  ( cd "$sb/install" && "$sb/install/quern" stop ) >/dev/null 2>&1 || true
  release_default_ports
  return "$failures"
}

set +e
( case_running_server_update )
failures=$((failures + $?))
set -e

# --------------------------------------------------------------------------
step "A tarball install updating from $PREV to $candidate_version"
# --------------------------------------------------------------------------
# The other half of the #212 case, and the one that went untested longest:
# a release install fetching the candidate and swapping itself for it, driven
# by the *previous release's* updater. It needs that release to honour
# QUERN_RELEASES_URL, since it is the code doing the fetching -- only 0.19.0
# and later do, so before that this could only have pointed the old updater
# at GitHub and rehearsed nothing while printing the lines of a real run.
case_tarball_update() {
  failures=0   # a subshell copy: a case reports only its own
  if ! git -C "$ROOT" cat-file -e "$PREV:server/lifecycle/releases.py" 2>/dev/null; then
    skip "tarball update: $PREV predates QUERN_RELEASES_URL, so its updater would fetch the published release instead of the candidate"
    return 0
  fi
  if [[ ! -d "$WORK/git-update/install/mcp/dist" ]]; then
    skip "tarball update: the update case left no built tree to package"
    return 0
  fi

  local sb="$WORK/tarball-update"
  mkdir -p "$sb/home/.local/share" "$sb/bin" "$sb/state"
  make_stubs "$sb/bin"

  # The previous release as a user actually has it: the published asset,
  # unpacked where install.sh puts it. Not a `git archive` of the tag -- that
  # has no mcp/dist and no node_modules, so the update would be starting from
  # a tree no user is running.
  local prev_version="${PREV#v}"
  local installed="$sb/home/.local/share/quern"
  if ! curl -fsSL --max-time 300 -o "$sb/prev.tar.gz" \
      "https://github.com/quern-dev/quern/releases/download/$PREV/quern-$prev_version.tar.gz"
  then
    bad "tarball update: could not download $PREV's asset to update from"
    return "$failures"
  fi
  /usr/bin/tar -xzf "$sb/prev.tar.gz" -C "$sb"
  mv "$sb/quern-$prev_version" "$installed"

  python3 -m venv "$installed/.venv" > "$sb/venv.log" 2>&1 || true
  "$installed/.venv/bin/pip" install -q -e "$installed" > "$sb/pip.log" 2>&1 \
    || bad "tarball update: could not prepare $PREV's venv: $(tail -n 2 "$sb/pip.log" 2>/dev/null | tr '\n' ' ' || true)"

  if ! serve_candidate "$sb"; then
    bad "tarball update: the local release server never came up"
    return "$failures"
  fi

  # The caller's PATH, as the git-update case uses: `quern update` on a
  # release install is run from the user's shell, so their node is there. The
  # narrow terminal PATH borrowed from the fresh-install case has no node on a
  # machine whose node comes from a version manager, and setup then fails for
  # a reason that is about the sandbox rather than the update. Whether an
  # update survives a missing node is the Node matrix's question, and it asks
  # it properly.
  set +e
  ( cd "$installed" && env -i \
      HOME="$sb/home" \
      QUERN_STATE_DIR="$sb/state" \
      QUERN_RELEASES_URL="http://127.0.0.1:$CANDIDATE_PORT" \
      PATH="$sb/bin:$PATH" \
      PIP_CACHE_DIR="$PIP_CACHE_DIR" npm_config_cache="$npm_config_cache" \
      "$installed/quern" update ) > "$sb/update.log" 2>&1
  local rc=$?
  set -e
  stop_candidate

  if [[ $rc -eq 0 ]]; then
    ok "$PREV's updater exits 0 on a tarball install"
  else
    bad "$PREV's updater exited $rc — see $sb/update.log"
    tail -n 25 "$sb/update.log" | sed 's/^/      /'
  fi

  # It has to have fetched *ours*. Without this the case passes while the
  # updater quietly resolves against GitHub and installs the published
  # release, which is the whole reason the override exists.
  if served_the_candidate "$sb/srv.log"; then
    ok "it fetched the candidate from the local server, not GitHub"
  else
    bad "the local server did not serve the asset — the updater went somewhere else"
    tail -n 3 "$sb/srv.log" 2>/dev/null | sed 's/^/      /' || true
  fi

  if grep -q "Traceback (most recent call last)" "$sb/update.log"; then
    # #212 was exactly this: a traceback after the source had been replaced.
    bad "the update printed a traceback"
    grep -A 6 "Traceback (most recent call last)" "$sb/update.log" | sed 's/^/      /'
  else
    ok "no traceback"
  fi

  local landed
  landed="$(sed -n 's/^version = "\(.*\)"/\1/p' "$installed/pyproject.toml" 2>/dev/null | head -1 || true)"
  [[ "$landed" == "$candidate_version" ]] \
    && ok "the install is $candidate_version" \
    || bad "the install is ${landed:-nothing}, expected $candidate_version"

  # A release install must not need npm at start: the asset ships mcp/dist
  # already built, and the swap has to preserve that (#193).
  [[ -f "$installed/mcp/dist/launcher.cjs" ]] \
    && ok "the swapped tree still has mcp/dist built" \
    || bad "mcp/dist/launcher.cjs is missing after the swap — the wrapper would need npm"

  local result="$sb/state/last-update.json"
  if [[ -f "$result" ]]; then
    if grep -q '"outcome" *: *"updated"' "$result" \
       && grep -q "\"version\" *: *\"$candidate_version\"" "$result"; then
      ok "last-update.json records updated → $candidate_version"
    else
      bad "last-update.json says $(tr -d '\n' < "$result")"
    fi
  else
    bad "no last-update.json was written"
  fi

  return "$failures"
}

set +e
( case_tarball_update )
failures=$((failures + $?))
set -e

# --------------------------------------------------------------------------
step "Setup with the menu-bar app already running"
# --------------------------------------------------------------------------
# #215: every run of setup quit the running app and reopened it, even when
# there was nothing new to install -- which is every run on a git install.
# When the reopen failed (macOS error -600, seen during a live update) the app
# stayed quit, and the only sign was its absence from the menu bar.
#
# The app is faked, but the *detection* is not: setup finds it with
# `pgrep -f <bundle>/Contents/MacOS/QuernMenuBar`, so this puts a real process
# at that path with that argv. A stubbed pgrep would have tested the stub.
case_menubar_app_left_running() {
  failures=0   # a subshell copy: a case reports only its own
  local install="$WORK/git-update/install"
  if [[ ! -x "$install/quern" ]]; then
    skip "menu-bar app: the update case left no install to run setup from"
    return 0
  fi

  local sb="$WORK/menubar-running"
  local app="$sb/home/Applications/Quern.app"
  mkdir -p "$sb/home" "$sb/state" "$sb/bin" "$app/Contents/MacOS"
  make_stubs "$sb/bin"

  # Current, so there is nothing to install and nothing setup needs to
  # replace. A version behind would be a different case: then setup *should*
  # quit it, and putting both in one test would let either pass for the
  # other's reason.
  cat > "$app/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleShortVersionString</key>
  <string>$candidate_version</string>
  <key>CFBundleIdentifier</key>
  <string>dev.quern.QuernMenuBar</string>
</dict>
</plist>
EOF
  cat > "$app/Contents/MacOS/QuernMenuBar" <<'EOF'
#!/bin/sh
# Stands in for the app: what matters is the path and that it stays up.
sleep 900
EOF
  chmod +x "$app/Contents/MacOS/QuernMenuBar"

  "$app/Contents/MacOS/QuernMenuBar" &
  local app_pid=$!
  sleep 0.5
  if ! kill -0 "$app_pid" 2>/dev/null; then
    bad "menu-bar app: the stand-in would not stay running"
    return "$failures"
  fi
  # Confirm setup will actually see it, or the case proves nothing.
  if ! pgrep -f "$app/Contents/MacOS/QuernMenuBar" >/dev/null 2>&1; then
    kill "$app_pid" 2>/dev/null || true
    bad "menu-bar app: pgrep cannot see the stand-in, so setup would not either"
    return "$failures"
  fi
  ok "a running app of the current version is in place"

  set +e
  ( cd "$install" && env -i HOME="$sb/home" QUERN_STATE_DIR="$sb/state" \
      PATH="$sb/bin:$PATH" npm_config_cache="$npm_config_cache" \
      "$install/quern" setup ) > "$sb/setup.log" 2>&1
  set -e

  if kill -0 "$app_pid" 2>/dev/null; then
    ok "setup left it running"
  else
    bad "setup quit the app it had nothing to replace (#215)"
    grep -i "quern app\|menu.bar\|quit" "$sb/setup.log" | head -6 | sed 's/^/      /'
  fi

  # And it must not have decided to fetch one either: an app that is current
  # is not an app to reinstall, and a rehearsal that downloads here would be
  # testing the network.
  if grep -qi "Fetching the signed menu-bar app\|Downloading" "$sb/setup.log"; then
    bad "setup went to fetch an app it already had at $candidate_version"
  else
    ok "and did not go looking for another one"
  fi

  kill "$app_pid" 2>/dev/null || true
  wait "$app_pid" 2>/dev/null || true
  return "$failures"
}

set +e
( case_menubar_app_left_running )
failures=$((failures + $?))
set -e

# --------------------------------------------------------------------------
step "A clone that cannot be fast-forwarded"
# --------------------------------------------------------------------------
# Two states a real clone is often in, and in both the update must decline
# and say what to do -- not pull, not half-pull, and above all not discard
# work. They share the origin the first case built.
#
# `git pull --ff-only` is what runs, so the interesting part is the message:
# a raw git error tells the user nothing they can act on.
prepare_clone() {
  local into="$1"
  git clone -q -b release/stable "$WORK/git-update/origin.git" "$into"
  git -C "$into" reset -q --hard HEAD~1
  python3 -m venv "$into/.venv" > "$into/venv.log" 2>&1 || true
  "$into/.venv/bin/pip" install -q -e "$into" > "$into/pip.log" 2>&1 || true
}

case_clone_on_another_branch() {
  failures=0   # a subshell copy: a case reports only its own
  if [[ ! -d "$WORK/git-update/origin.git" ]]; then
    skip "branch clone: the update case left no origin to clone from"
    return 0
  fi
  local sb="$WORK/branch-clone"
  mkdir -p "$sb/home" "$sb/state" "$sb/bin"
  make_stubs "$sb/bin"
  prepare_clone "$sb/install"

  # The ordinary dev-clone state: working on something, not on the channel
  # branch. Pulling here would track the wrong upstream, so quern reports
  # what is available and leaves the decision alone (#40).
  git -C "$sb/install" checkout -q -b my-work

  set +e
  ( cd "$sb/install" && env -i HOME="$sb/home" QUERN_STATE_DIR="$sb/state" \
      PATH="$sb/bin:$PATH" "$sb/install/quern" update ) > "$sb/update.log" 2>&1
  local rc=$?
  set -e

  if grep -q "You're on branch" "$sb/update.log" \
     && grep -q "release/stable" "$sb/update.log"; then
    ok "it names the branch you are on and the one to switch to"
  else
    bad "the update did not explain why it would not pull"
    tail -n 15 "$sb/update.log" | sed 's/^/      /'
  fi

  local still
  still="$(sed -n 's/^version = "\(.*\)"/\1/p' "$sb/install/pyproject.toml" 2>/dev/null | head -1 || true)"
  [[ "$still" == "$prev_version" ]] \
    && ok "the clone is left where it was, on $prev_version" \
    || bad "the clone moved to ${still:-nothing} from a branch it should not have pulled"

  [[ "$(git -C "$sb/install" rev-parse --abbrev-ref HEAD)" == "my-work" ]] \
    && ok "and still on my-work" \
    || bad "the update changed the checked-out branch"
  (( rc == 0 || rc == 2 )) && ok "it exits without claiming failure (rc $rc)" \
    || ok "it exits $rc"
  return "$failures"
}

case_clone_with_local_changes() {
  failures=0   # a subshell copy: a case reports only its own
  if [[ ! -d "$WORK/git-update/origin.git" ]]; then
    skip "dirty clone: the update case left no origin to clone from"
    return 0
  fi
  local sb="$WORK/dirty-clone"
  mkdir -p "$sb/home" "$sb/state" "$sb/bin"
  make_stubs "$sb/bin"
  prepare_clone "$sb/install"

  # One stray edit is all it takes, and it is the user's work: the only
  # unacceptable outcome here is losing it.
  local marker="# a local edit the update must not discard"
  printf '%s\n' "$marker" >> "$sb/install/README.md"

  set +e
  ( cd "$sb/install" && env -i HOME="$sb/home" QUERN_STATE_DIR="$sb/state" \
      PATH="$sb/bin:$PATH" "$sb/install/quern" update ) > "$sb/update.log" 2>&1
  local rc=$?
  set -e

  if grep -qi "local changes" "$sb/update.log"; then
    ok "it says local changes are in the way"
  else
    bad "the update did not explain that local changes blocked it"
    tail -n 15 "$sb/update.log" | sed 's/^/      /'
  fi
  grep -qi "stash" "$sb/update.log" \
    && ok "and says what to do about them" \
    || bad "it does not say how to proceed"

  # The load-bearing one.
  if grep -qF "$marker" "$sb/install/README.md"; then
    ok "the local edit is still there"
  else
    bad "the update discarded uncommitted work"
  fi

  [[ $rc -ne 0 ]] \
    && ok "the refusal reaches the exit code (rc $rc)" \
    || bad "a blocked update exited 0"
  return "$failures"
}

set +e
( case_clone_on_another_branch )
failures=$((failures + $?))
( case_clone_with_local_changes )
failures=$((failures + $?))
set -e

# --------------------------------------------------------------------------
step "A channel offering an older release is refused"
# --------------------------------------------------------------------------
# 0.18.1's defect: the beta channel resolved to a release three minor
# versions old and every tarball user was offered it, then pinned there. The
# guard exists so a wrong answer from the resolver is refused rather than
# acted on -- and refusing has to be distinguishable from having nothing to
# do, because rc 2 means "already up to date" and maps to exit 0, which would
# tell every surface that nothing needed doing.
#
# Driven against the *candidate's* updater, unlike the cases above: this is a
# question about the code that is going out, not the code users are leaving.
case_downgrade_refused() {
  failures=0   # a subshell copy: a case reports only its own
  local installed="$WORK/tarball-update/home/.local/share/quern"
  if [[ ! -x "$installed/quern" ]]; then
    skip "downgrade refusal: the tarball-update case left no install to offer a downgrade to"
    return 0
  fi
  local at
  at="$(sed -n 's/^version = "\(.*\)"/\1/p' "$installed/pyproject.toml" 2>/dev/null | head -1 || true)"
  if [[ "$at" != "$candidate_version" ]]; then
    skip "downgrade refusal: that install is ${at:-nothing}, not the candidate"
    return 0
  fi

  local sb="$WORK/downgrade"
  mkdir -p "$sb/home" "$sb/state" "$sb/bin" "$sb/srv/releases"
  make_stubs "$sb/bin"
  # The state and home of the install being updated, so the run is the same
  # one the tarball case left behind rather than a fresh machine.
  cp -R "$WORK/tarball-update/home/." "$sb/home/" 2>/dev/null || true

  # No asset is staged on purpose. The refusal happens at the version
  # comparison, before anything is downloaded, so a served tarball here would
  # only be able to hide a guard that had stopped working.
  local port
  port="$(python3 -c "
import socket
s = socket.socket(); s.bind(('127.0.0.1', 0))
print(s.getsockname()[1]); s.close()" 2>/dev/null || echo 8908)"
  cat > "$sb/srv/releases/latest" <<EOF
{"tag_name": "$PREV", "prerelease": false,
 "assets": [{"name": "quern-$prev_version.tar.gz",
             "browser_download_url": "http://127.0.0.1:$port/releases/download/$PREV/quern-$prev_version.tar.gz"}]}
EOF
  python3 -m http.server "$port" --directory "$sb/srv" >"$sb/srv.log" 2>&1 &
  local srv_pid=$!
  local waited=0
  until curl -fsS --max-time 2 "http://127.0.0.1:$port/releases/latest" >/dev/null 2>&1; do
    waited=$((waited + 1))
    if (( waited > 20 )); then
      { kill "$srv_pid" && wait "$srv_pid"; } 2>/dev/null || true
      bad "downgrade refusal: the local release server never came up"
      return "$failures"
    fi
    sleep 0.5
  done

  set +e
  ( cd "$installed" && env -i \
      HOME="$sb/home" \
      QUERN_STATE_DIR="$sb/state" \
      QUERN_RELEASES_URL="http://127.0.0.1:$port" \
      PATH="$sb/bin:$PATH" \
      "$installed/quern" update ) > "$sb/update.log" 2>&1
  local rc=$?
  set -e
  { kill "$srv_pid" && wait "$srv_pid"; } 2>/dev/null || true

  if grep -q "Not downgrading" "$sb/update.log"; then
    ok "it refuses the older release, and says why"
  else
    bad "nothing refused $prev_version being offered to $candidate_version"
    tail -n 15 "$sb/update.log" | sed 's/^/      /'
  fi

  # Not rc 2. That is NO_OP -- "already up to date" -- and it reaches the
  # CLI, the menu bar's isNoOp and the result file alike. A refusal is the
  # opposite: an update was wanted and did not happen.
  if [[ $rc -eq 0 || $rc -eq 2 ]]; then
    bad "the refusal exited $rc, which reads as success or as nothing-to-do"
  else
    ok "the refusal reaches the exit code (rc $rc)"
  fi

  local still
  still="$(sed -n 's/^version = "\(.*\)"/\1/p' "$installed/pyproject.toml" 2>/dev/null | head -1 || true)"
  [[ "$still" == "$candidate_version" ]] \
    && ok "the install is untouched at $candidate_version" \
    || bad "the install is now ${still:-nothing} — it downgraded anyway"

  return "$failures"
}

set +e
( case_downgrade_refused )
failures=$((failures + $?))
set -e

# --------------------------------------------------------------------------
step "Nothing outside the sandbox was touched"
# --------------------------------------------------------------------------
# The backstop, because the cost of getting this wrong is measured in this
# repo's own history rather than in theory: the suite has twice deleted the
# developer's `quern` command and twice rewritten a Claude hook.
#
# A comparison, not an existence check, and `bad` rather than `skip` -- a
# file that is absent in both snapshots is fine, and one that changed is a
# failure however it changed.
AFTER="$(snapshot_protected)"
if [[ "$BEFORE" == "$AFTER" ]]; then
  ok "every protected path outside the sandbox is unchanged"
else
  bad "something outside the sandbox changed:"
  diff <(printf '%s\n' "$BEFORE") <(printf '%s\n' "$AFTER") | sed 's/^/      /' || true
fi

if [[ -z "$REAL_PID" ]]; then
  ok "no server of yours was running to disturb"
elif kill -0 "$REAL_PID" 2>/dev/null; then
  ok "your own server (pid $REAL_PID) is still running"
else
  # `quern start` reclaims a port by killing the quern-looking process that
  # holds it, and does not consult QUERN_STATE_DIR when deciding.
  bad "your own server (pid $REAL_PID) is gone — the rehearsal killed it"
fi

printf '\n'
skips="$(skip_count)"
if (( failures )); then
  printf '\033[0;31m%d check(s) failed.\033[0m\n' "$failures"
  (( skips )) && printf '%d also skipped, listed above.\n' "$skips"
  exit 1
fi
if (( skips )); then
  printf '\033[0;32mThe rehearsal passed\033[0m — %d skipped, listed above.\n' "$skips"
else
  printf '\033[0;32mThe rehearsal passed.\033[0m\n'
fi
