#!/usr/bin/env bash
# Run every quern CLI command for real, in a sandbox, and check what each one
# did -- not just its exit code (#406).
#
#   scripts/cli-live-test.sh [ref] [--keep]
#
# The suite covers the CLI unevenly: commands that only read are well tested,
# and the ones that change the machine are barely run at all -- `stop` 2%,
# `status` 3%, `run_setup` 25%, and the uninstall path that rewrites other
# tools' configs 2% (measured for #406). They act on processes, ~/.local/bin,
# MCP client configs and launchd, and "tests never touch the real machine"
# left them untested rather than redirected. This runs them against a machine
# that is allowed to be touched: a throwaway HOME and QUERN_STATE_DIR, with the
# tools that reach outside a HOME stubbed.
#
# IT TESTS COMMITTED WORK, like release-rehearsal.sh: the tree under test is a
# `git archive` of the ref, so an uncommitted fix is not in it.
#
# Every step appends `step | exit | outcome` to a transcript, with paths and
# pids normalised, so two runs can be diffed -- which is how a refactor of the
# CLI (#396 phase 4) shows it changed nothing a user can see.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REF="HEAD"
KEEP=0
for arg in "$@"; do
  case "$arg" in
    --keep) KEEP=1 ;;
    -h|--help) sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) REF="$arg" ;;
  esac
done

# Run from a copy. Bash reads a script as it goes, so editing this file during
# a run -- the normal thing to do while iterating on it -- shifts the offsets
# under the running shell, which then executes a fragment of the file as a
# command. It happened during this script's own development.
#
# The copy is deleted at exit, so it is recognised by more than a variable
# that could arrive from the environment: an inherited value once made the
# cleanup `rm -rf` the checkout it ran from. Only a directory this script
# created and marked is ever removed.
MARKER=".quern-cli-live-copy"
if [[ -z "${_QUERN_CLI_LIVE_COPY:-}" ]]; then
  # Physical paths on both sides of the check below: macOS's TMPDIR ends in a
  # slash, and a mktemp path with "//" in it never equals what `pwd` prints.
  copy="$(cd "$(mktemp -d "${TMPDIR:-/tmp}/quern-cli-live-script.XXXXXX")" && pwd -P)"
  cp -R "$ROOT/scripts" "$copy/"
  : > "$copy/$MARKER"
  _QUERN_CLI_LIVE_COPY="$copy" _QUERN_CLI_LIVE_ROOT="$ROOT" \
    exec bash "$copy/scripts/cli-live-test.sh" "$@"
fi
SCRIPT_COPY="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
ROOT="${_QUERN_CLI_LIVE_ROOT:-}"
if [[ "$SCRIPT_COPY" != "$_QUERN_CLI_LIVE_COPY" || "$SCRIPT_COPY" == "$ROOT" \
      || -z "$ROOT" || ! -f "$SCRIPT_COPY/$MARKER" ]]; then
  echo "error: not running from a copy this script made; refusing to go on" >&2
  echo "       (unset _QUERN_CLI_LIVE_COPY and _QUERN_CLI_LIVE_ROOT)" >&2
  exit 2
fi

# shellcheck source=lib/sandbox.sh
source "$SCRIPT_COPY/scripts/lib/sandbox.sh"

# Clear of the defaults (9100/9101, the developer's) and of the rehearsal's
# 9187/9188, so the two can run at once. Never the defaults: `quern start`
# kills a healthy quern on its port, whatever QUERN_STATE_DIR says (#405).
PORT=9197
PROXY_PORT=9198

SB=""
TREE=""

# Processes this run started, found by their command line containing the
# sandbox path -- never "whatever holds the port", which is #405's mistake.
sandbox_pids() {
  [[ -n "$SB" ]] || return 0
  local p pid
  for p in "$PORT" "$PROXY_PORT"; do
    for pid in $(lsof -nP -t -iTCP:"$p" -sTCP:LISTEN 2>/dev/null || true); do
      ps -o args= -p "$pid" 2>/dev/null | grep -qF "$SB" && echo "$pid"
    done
  done | sort -u
}

stop_sandbox_server() {
  [[ -n "$TREE" && -x "$TREE/quern" ]] && declare -F q >/dev/null \
    && q stop >/dev/null 2>&1 || true
  local pids i
  pids="$(sandbox_pids)"
  [[ -n "$pids" ]] && kill $pids 2>/dev/null || true
  for i in $(seq 1 20); do
    [[ -z "$(sandbox_pids)" ]] && return 0
    sleep 1
  done
  pids="$(sandbox_pids)"
  [[ -n "$pids" ]] && kill -9 $pids 2>/dev/null || true
}

cleanup() {
  local rc=$?
  stop_sandbox_server
  if [[ -n "$SB" ]]; then
    if [[ $KEEP -eq 1 ]]; then
      printf '\nsandbox kept (server stopped): %s\n' "$SB"
    else
      rm -rf "$SB"
    fi
  fi
  rm -rf "$SCRIPT_COPY"   # checked above to be a marked copy, not ROOT
  exit "$rc"
}
trap cleanup EXIT

for p in "$PORT" "$PROXY_PORT"; do
  if lsof -nP -iTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "error: port $p is in use; this test needs $PORT and $PROXY_PORT free" >&2
    exit 2
  fi
done

# A template, because macOS `mktemp -d` alone ignores TMPDIR. And the physical
# path: macOS's TMPDIR is under /var, a symlink to /private/var, and setup
# writes resolved paths into the wrapper and MCP entries -- so a check that
# they point at this tree must compare like with like.
SB="$(cd "$(mktemp -d "${TMPDIR:-/tmp}/quern-cli-live.XXXXXX")" && pwd -P)"
SKIPS_FILE="$SB/skips"
: > "$SKIPS_FILE"
TRANSCRIPT="$SB/transcript.txt"
: > "$TRANSCRIPT"

export PIP_CACHE_DIR="${PIP_CACHE_DIR:-$HOME/.cache/pip}"
export npm_config_cache="${npm_config_cache:-$HOME/.npm}"

# Node is not something setup installs, and the MCP server is built with it.
# Borrowed from the caller, the way the rehearsal's mcp-install case does.
NODE_DIR=""
if command -v node >/dev/null 2>&1; then
  NODE_DIR="$(dirname "$(command -v node)")"
fi

BEFORE="$(snapshot_protected)"
REAL_PID="$(real_server_pid)"

# --------------------------------------------------------------------------
# The sandbox
# --------------------------------------------------------------------------
HOME_SB="$SB/home"
STATE="$HOME_SB/.quern"
TREE="$SB/quern"
mkdir -p "$HOME_SB" "$TREE"
# setup --yes reaches past HOME: brew installs onto the machine, `defaults`
# writes the real user's preferences through cfprefsd whatever HOME says, and
# xcode-select and pipx are called directly. Stubbed, and logged -- so what
# setup *attempted* is checkable too.
#
# And no devices, deliberately: `xcrun` and `adb` are stubbed, so the sandbox
# server and setup see no simulators, phones or emulators. The real ones are
# the developer's, and setup would otherwise list them and, given a CA, offer
# to install it into their trust stores. This tests the CLI, not device
# control; the server it starts runs with device management unavailable.
make_stubs "$SB/bin" brew pipx defaults xcode-select xcrun adb
CALLS="$SB/calls.log"
: > "$CALLS"

git -C "$ROOT" archive "$REF" | tar -x -C "$TREE"
SANDBOX_PATH="$SB/bin:${NODE_DIR:+$NODE_DIR:}/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# Configs other tools own, seeded with things that are not quern's. Install
# must add quern beside them and uninstall must take only quern away again;
# `_remove_mcp_registrations` had 2% coverage when this was written.
mkdir -p "$HOME_SB/.cursor"
cat > "$HOME_SB/.claude.json" <<'EOF'
{"numStartups": 7, "mcpServers": {"someone-else": {"command": "/bin/true"}}}
EOF
cat > "$HOME_SB/.cursor/mcp.json" <<'EOF'
{"mcpServers": {"someone-else": {"command": "/bin/true"}}}
EOF

# Run the tree's own `quern` from inside it, with an environment that is built
# rather than inherited: an exported variable leaking in from this shell is how
# a test like this passes for the wrong reason.
#
# Detached from any terminal -- a new session and stdin from /dev/null -- so a
# person running this from a shell gets the same run an agent does. Without it
# setup's prompts reopened /dev/tty, waited on the person at the keyboard, and
# took their Enter as a yes.
#
# Offline once setup is done (it needs pip and npm): https traffic goes to a
# dead proxy, so `start` makes no real update check and `check-updates` takes
# its could-not-ask path. QUERN_RELEASES_URL covers the release API the same way.
DEAD_PROXY="http://127.0.0.1:9"
Q_OFFLINE=""
q() {
  local proxy="${Q_HTTPS_PROXY-$Q_OFFLINE}"
  ( cd "${QCWD:-$TREE}" && env -i \
      HOME="$HOME_SB" \
      QUERN_STATE_DIR="$STATE" \
      QUERN_RELEASES_URL="http://127.0.0.1:9/unreachable" \
      PATH="$SANDBOX_PATH" \
      PIP_CACHE_DIR="$PIP_CACHE_DIR" npm_config_cache="$npm_config_cache" \
      https_proxy="$proxy" HTTPS_PROXY="$proxy" \
      TERM=dumb \
      python3 -c 'import os, sys; os.setsid(); os.execv(sys.argv[1], sys.argv[1:])' \
      "$TREE/quern" "$@" ) < /dev/null
}

# The same, on a pseudo-terminal that answers prompts as they appear:
# `uninstall` has no --yes and declines when nobody can be asked, so without a
# tty it would test only the decline. Piping "y" into `script` does not do it --
# the EOF arrives first and the prompt reads that. Exits 99 if the prompt never
# came, so "not asked" cannot pass for "asked and answered"; 97 at a prompt it
# was not given an answer for, and 98 if the command outlives five minutes.
# A prompt written "?text" is optional: answered if it appears, not required.
q_answer() {   # q_answer <prompt> <answer> [<prompt> <answer> ...] -- <quern args...>
  local pairs=()
  while [[ "$1" != "--" ]]; do pairs+=("$1"); shift; done
  shift
  # Offline like q: the uninstall it drives must not reach the network either.
  local proxy="${Q_HTTPS_PROXY-$Q_OFFLINE}"
  ( cd "$TREE" && env -i \
      HOME="$HOME_SB" QUERN_STATE_DIR="$STATE" \
      QUERN_RELEASES_URL="http://127.0.0.1:9/unreachable" \
      https_proxy="$proxy" HTTPS_PROXY="$proxy" \
      PATH="$SANDBOX_PATH" TERM=dumb \
      python3 -c '
import os, pty, re, select, signal, sys, time
n = int(sys.argv[1])
pairs = list(zip(sys.argv[2:2 + n:2], sys.argv[3:2 + n:2]))
optional = [p.startswith("?") for p, _ in pairs]
pairs = [(p.lstrip("?"), a) for p, a in pairs]
argv = sys.argv[2 + n:]
pid, fd = pty.fork()
if pid == 0:
    os.execv(argv[0], argv)
seen, done, deadline = b"", set(), time.time() + 300
code = None
while time.time() < deadline:
    if not select.select([fd], [], [], 1)[0]:
        continue
    try:
        data = os.read(fd, 4096)
    except OSError:
        break
    if not data:
        break
    sys.stdout.buffer.write(data)
    sys.stdout.flush()
    seen += data
    hit = next((i for i, (p, _) in enumerate(pairs)
                if i not in done and p.encode() in seen), None)
    if hit is not None:
        os.write(fd, (pairs[hit][1] + "\n").encode())
        seen = b""
        done.add(hit)
    elif re.search(rb"\[[yY]/[nN]\] *$", seen):
        print("\n[cli-live-test] a prompt nobody expected", flush=True)
        code = 97
        break
else:
    print("\n[cli-live-test] timed out", flush=True)
    code = 98
if code is not None:
    os.kill(pid, signal.SIGKILL)
    os.waitpid(pid, 0)
    sys.exit(code)
status = os.waitpid(pid, 0)[1]
required = {i for i, opt in enumerate(optional) if not opt}
sys.exit(os.waitstatus_to_exitcode(status) if required <= done else 99)
' "${#pairs[@]}" "${pairs[@]}" "$TREE/quern" "$@" )
}

# Known failures, each with its issue. Remove a line when its issue is fixed;
# the check then reports as an ordinary pass or failure.
KNOWN_HELP="#408"

# A failure this run knows about and an issue tracks: still asserted, reported
# with its issue, and not counted -- so a baseline run is clean to diff. When a
# known failure starts passing, `fixed` says to take the marker off.
KNOWN=0
known() { printf '  \033[0;33m✗\033[0m %s (known: %s)\n' "$1" "$2"; KNOWN=$((KNOWN + 1)); }
fixed() { bad "$1 -- it passes now; remove the known-failure marker for $2"; }

# Run a step: its output to a log, its exit code into RC. Never aborts.
run() {
  local name="$1"; shift
  LOG="$SB/logs/$name.log"
  mkdir -p "$SB/logs"
  set +e
  "$@" > "$LOG" 2>&1
  RC=$?
  set -e
}

# Paths and pids differ between runs; nothing else should.
normalise() {
  SB="$SB" perl -pe 's/\Q$ENV{SB}\E/<SB>/g; s/pid[ =:]*\d+/pid <N>/gi;
    s/\d{4}-\d\d-\d\d[T ][\d:.]+Z?/<TIME>/g'
}

record() {   # record <step> <outcome>
  printf '%s | exit %s | %s\n' "$1" "$RC" "$2" | normalise >> "$TRANSCRIPT"
}

show_log() { sed -n '1,15p' "$LOG" | sed 's/^/      /'; }

expect_rc() {   # expect_rc <step> <code> <what it means>
  if [[ "$RC" -eq "$2" ]]; then
    ok "$1: exit $RC ($3)"
  else
    bad "$1: exit $RC, expected $2 ($3)"
    show_log
  fi
}

# The developer's server keeps its pid through every step. It is the specific
# accident the sandbox ports exist to prevent, so it is checked, not assumed.
real_server_ok() {
  [[ -z "$REAL_PID" ]] && return 0
  [[ "$(real_server_pid)" == "$REAL_PID" ]] && kill -0 "$REAL_PID" 2>/dev/null
}
check_real_server() {
  real_server_ok || bad "after $1: the developer's own server (pid $REAL_PID) is gone or replaced"
}

# A refusal is a message and an exit code, not a crash that also exits non-zero.
refused() {   # refused <what> <expected text>
  if [[ "$RC" -eq 0 ]]; then
    bad "$1 exited 0"
  elif grep -q "Traceback" "$LOG"; then
    bad "$1 exited $RC with a traceback, not a refusal"
    show_log
  elif ! grep -qi -- "$2" "$LOG"; then
    bad "$1 exited $RC without saying '$2'"
    show_log
  else
    ok "$1 is refused (exit $RC)"
  fi
}

json_get() {   # json_get <file> <python expression on d>
  python3 -c 'import json,sys
try:
    d = json.load(open(sys.argv[1]))
    print(eval(sys.argv[2]))
except Exception:
    print("")' "$1" "$2" 2>/dev/null || true
}

sandbox_server_pid() { json_get "$STATE/state.json" 'd.get("pid", "")'; }
sandbox_port() { json_get "$STATE/state.json" 'd.get("server_port", "")'; }

healthy() { curl -fsS --max-time 5 "http://127.0.0.1:$1/health" >/dev/null 2>&1; }

printf 'CLI live test of %s (%s)\n' "$REF" "$(git -C "$ROOT" rev-parse --short "$REF")"

# --------------------------------------------------------------------------
step "Before setup"
# --------------------------------------------------------------------------
run version q version
expect_rc "version" 0 "answers before setup has run"
want="$(sed -n 's/^version = "\(.*\)"/\1/p' "$TREE/pyproject.toml" | head -1)"
[[ -n "$want" ]] && grep -qF "$want" "$LOG" && ok "version prints $want" || bad "version does not print '${want}'"
record version "$(grep -c "$want" "$LOG" | sed 's/^0$/wrong version/; s/^[1-9].*/prints the tree version/')"

# Before setup there is no venv, so the wrapper runs the system python. `help`
# then imports the FastAPI app and dies on a missing uvicorn, though the README
# lists it beside `version`, which works.
run help q help
missing=""
for cmd in setup start stop restart status url env doctor update uninstall mcp-install; do
  grep -qw "$cmd" "$LOG" || missing="$missing $cmd"
done
if [[ "$RC" -eq 0 && -z "$missing" ]]; then
  [[ -n "${KNOWN_HELP:-}" ]] && fixed "help before setup" "$KNOWN_HELP" || ok "help before setup lists every core command"
else
  why="help before setup exits $RC: $(grep -m1 -o 'No module named [^ ]*' "$LOG" || head -1 "$LOG")"
  if [[ -n "${KNOWN_HELP:-}" ]]; then known "$why" "$KNOWN_HELP"; else bad "$why"; fi
fi
record help "missing:${missing:- none}"

# --------------------------------------------------------------------------
step "setup"
# --------------------------------------------------------------------------
# Unattended first: the contract is to decline what it cannot ask and say so.
run setup-no-tty q setup
grep -q "No terminal attached" "$LOG" \
  && ok "setup with no terminal says so, and declines rather than answering (exit $RC)" \
  || bad "setup with no terminal did not say it could not ask"
grep -q "Traceback" "$LOG" && bad "setup with no terminal raised a traceback" || true
record setup-no-tty "said-no-terminal:$(grep -q 'No terminal attached' "$LOG" && echo y || echo n)"
check_real_server "setup with no terminal"

run setup-yes q setup --yes
expect_rc "setup --yes" 0 "answers every non-deliberate question"
[[ -x "$TREE/.venv/bin/python" ]] && ok "it created a venv" || bad "no venv in the tree"
WRAPPER="$HOME_SB/.local/bin/quern"
if [[ -x "$WRAPPER" ]] && grep -q "$TREE" "$WRAPPER"; then
  ok "it wrote ~/.local/bin/quern, pointing at this tree"
else
  bad "the wrapper is missing, not executable, or points elsewhere"
fi
record setup-yes "venv:$([[ -x $TREE/.venv/bin/python ]] && echo y || echo n) wrapper:$([[ -x $WRAPPER ]] && echo y || echo n) attempted:$(cut -d' ' -f1,2 "$CALLS" | sort -u | tr '\n' ',')"
check_real_server "setup --yes"

if [[ ! -x "$TREE/.venv/bin/python" ]]; then
  bad "setup left no venv; nothing after this can run"
  sed -n '1,40p' "$LOG" | sed 's/^/      /'
  exit 1
fi
Q_OFFLINE="$DEAD_PROXY"

# --------------------------------------------------------------------------
step "mcp-install"
# --------------------------------------------------------------------------
run mcp-install q mcp-install all
expect_rc "mcp-install all" 0 "registers every client"
entry="$(json_get "$HOME_SB/.claude.json" '[k for k in d.get("mcpServers", {}) if "quern" in k.lower()]')"
[[ "$entry" == *quern* ]] && ok "Claude Code has a quern entry" || bad "no quern entry in ~/.claude.json"
grep -q "$TREE" "$HOME_SB/.claude.json" && ok "it points into this tree" || bad "the entry does not point at this tree"
[[ "$(json_get "$HOME_SB/.claude.json" 'd.get("numStartups")')" == "7" ]] \
  && ok "Claude Code's own keys are untouched" || bad "mcp-install changed a key that is not quern's"
[[ "$(json_get "$HOME_SB/.claude.json" '"someone-else" in d.get("mcpServers", {})')" == "True" ]] \
  && ok "Claude Code's other server is still there" || bad "mcp-install removed another server from Claude Code"
[[ "$(json_get "$HOME_SB/.cursor/mcp.json" '"someone-else" in d.get("mcpServers", {})')" == "True" ]] \
  && ok "Cursor's other server is still there" || bad "mcp-install removed another server from Cursor"
# Every client `all` names, each checked by its own key: opencode and codex
# register as "quern", the others as "quern-debug". By key, not by grepping
# for "quern" -- the sandbox path itself contains it.
CLIENT_CONFIGS=(
  "$HOME_SB/.claude.json"
  "$HOME_SB/Library/Application Support/Claude/claude_desktop_config.json"
  "$HOME_SB/.cursor/mcp.json"
  "$HOME_SB/.config/opencode/opencode.json"
  "$HOME_SB/.codex/config.toml"
)
client_has_quern() {
  case "$1" in
    *.toml) grep -q '^\[mcp_servers\.quern\]' "$1" 2>/dev/null ;;
    */opencode.json) [[ "$(json_get "$1" '"quern" in d.get("mcp", {})')" == "True" ]] ;;
    *) [[ "$(json_get "$1" '"quern-debug" in d.get("mcpServers", {})')" == "True" ]] ;;
  esac
}
registered() { local f n=0; for f in "${CLIENT_CONFIGS[@]}"; do client_has_quern "$f" && n=$((n + 1)); done; echo "$n"; }
[[ "$(registered)" -eq ${#CLIENT_CONFIGS[@]} ]] \
  && ok "all ${#CLIENT_CONFIGS[@]} clients have a quern entry" \
  || bad "only $(registered) of ${#CLIENT_CONFIGS[@]} clients have a quern entry"
record mcp-install "registered:$(registered)/${#CLIENT_CONFIGS[@]} others-kept:$(json_get "$HOME_SB/.claude.json" 'd.get("numStartups")')"

run grant-full-perms q grant-full-perms
expect_rc "grant-full-perms" 0 "writes Claude Code's permission list"
perms="$(json_get "$HOME_SB/.claude/settings.json" 'len([p for p in d.get("permissions", {}).get("allow", []) if "quern" in p])')"
[[ -n "$perms" && "$perms" != "0" ]] && ok "settings.json allows quern's tools ($perms rules)" || bad "no quern rules in ~/.claude/settings.json"
record grant-full-perms "quern-rules:${perms:-0}"
check_real_server "mcp-install"

# --------------------------------------------------------------------------
step "Settings"
# --------------------------------------------------------------------------
CONFIG="$STATE/config.json"
cfg() { json_get "$CONFIG" "$1"; }

run set-channel-show q set-channel
expect_rc "set-channel (show)" 0 "prints the channel"
record set-channel-show "$(tr -d '\n' < "$LOG" | cut -c1-60)"

run set-channel-beta q set-channel beta
expect_rc "set-channel beta" 0 "opts in"
[[ "$(cfg 'd.get("update_channel")')" == "beta" ]] && ok "config.json says beta" || bad "config.json does not say beta: $(cat "$CONFIG" 2>/dev/null)"
record set-channel-beta "channel:$(cfg 'd.get("update_channel")')"

cp "$CONFIG" "$SB/config.before-bogus" 2>/dev/null || true
run set-channel-bogus q set-channel nightly-ish
refused "an unknown channel" "channel"
cmp -s "$CONFIG" "$SB/config.before-bogus" && ok "and config.json is unchanged" || bad "a refused channel still changed config.json"
record set-channel-bogus "refused:$([[ $RC -ne 0 ]] && echo y || echo n)"

run set-channel-stable q set-channel stable
expect_rc "set-channel stable" 0 "switches back"
[[ "$(cfg 'd.get("update_channel")')" == "stable" ]] && ok "config.json says stable" || bad "config.json does not say stable"
record set-channel-stable "channel:$(cfg 'd.get("update_channel")')"

for v in off on; do
  want_v="$([[ $v == on ]] && echo True || echo False)"
  run "set-update-check-$v" q set-update-check "$v"
  expect_rc "set-update-check $v" 0 "accepted"
  [[ "$(cfg 'd.get("update_check")')" == "$want_v" ]] && ok "config.json has update_check $want_v" \
    || bad "config.json has update_check '$(cfg 'd.get("update_check")')', expected $want_v"
  record "set-update-check-$v" "value:$(cfg 'd.get("update_check")')"
done
for v in on off; do
  want_v="$([[ $v == on ]] && echo True || echo False)"
  run "set-auto-install-cert-$v" q set-auto-install-cert "$v"
  expect_rc "set-auto-install-cert $v" 0 "accepted"
  [[ "$(cfg 'd.get("auto_install_cert")')" == "$want_v" ]] && ok "config.json has auto_install_cert $want_v" \
    || bad "config.json has auto_install_cert '$(cfg 'd.get("auto_install_cert")')', expected $want_v"
  record "set-auto-install-cert-$v" "value:$(cfg 'd.get("auto_install_cert")')"
done
cp "$CONFIG" "$SB/config.before-bogus"
run set-auto-install-cert-bogus q set-auto-install-cert maybe
refused "set-auto-install-cert maybe" "on"
cmp -s "$CONFIG" "$SB/config.before-bogus" && ok "and config.json is unchanged" || bad "a refused value still changed config.json"
record set-auto-install-cert-bogus "refused:$([[ $RC -ne 0 ]] && echo y || echo n)"

# Local capture is enabled and disabled with no server running: a running one
# would route every simulator on this Mac through the sandbox proxy.
run enable-local-capture q enable-local-capture --skip-cert-check MyApp
expect_rc "enable-local-capture" 0 "writes the process list"
[[ "$(cfg '"MyApp" in d.get("local_capture", [])')" == "True" ]] && ok "config.json captures MyApp" \
  || bad "config.json does not list MyApp: $(cfg 'd.get("local_capture")')"
record enable-local-capture "config:$(cfg 'd.get("local_capture")')"
run disable-local-capture q disable-local-capture
expect_rc "disable-local-capture" 0 "never refused"
record disable-local-capture "config:$(cfg 'd.get("local_capture")')"
# Not just a check: a server started with local capture still configured runs
# mitmdump in local mode, macOS-wide, over the same process names the
# developer's own server may be capturing. Nothing starts until this is clear.
if [[ "$(cfg 'd.get("local_capture", [])')" == "[]" ]]; then
  ok "local capture is off again"
else
  bad "disable-local-capture left $(cfg 'd.get("local_capture")') -- not starting a server that would capture this Mac's traffic"
  exit 1
fi

key_before="$(cat "$STATE/api-key" 2>/dev/null || true)"
run regenerate-key q regenerate-key
expect_rc "regenerate-key" 0 "writes a new key"
key_after="$(cat "$STATE/api-key" 2>/dev/null || true)"
[[ -n "$key_after" && "$key_after" != "$key_before" ]] && ok "the key changed" || bad "the api key did not change"
record regenerate-key "changed:$([[ -n $key_after && $key_after != "$key_before" ]] && echo y || echo n)"
check_real_server "settings"

# --------------------------------------------------------------------------
step "The server: start, status, url, env, restart, stop"
# --------------------------------------------------------------------------
run url-stopped q url
refused "url with no server" "No server"
record url-stopped "$(head -1 "$LOG" | cut -c1-70)"

run start q start --port "$PORT" --proxy-port "$PROXY_PORT"
expect_rc "start" 0 "daemonises"
pid1="$(sandbox_server_pid)"
[[ "$(sandbox_port)" == "$PORT" ]] && ok "state.json records port $PORT" || bad "state.json records port '$(sandbox_port)'"
healthy "$PORT" && ok "it answers /health" || bad "nothing answered /health on $PORT"
record start "port:$(sandbox_port) healthy:$(healthy "$PORT" && echo y || echo n)"
check_real_server "start"

run status q status
expect_rc "status" 0 "a server is running"
[[ -n "$pid1" ]] && grep -qw "$pid1" "$LOG" && ok "status names the server's pid" || bad "status does not name pid '$pid1'"
grep -q "$PORT" "$LOG" && ok "status names the port" || bad "status does not name port $PORT"
record status "names-pid:$([[ -n $pid1 ]] && grep -qw "$pid1" "$LOG" && echo y || echo n) names-port:$(grep -q "$PORT" "$LOG" && echo y || echo n)"

run url q url
expect_rc "url" 0 "prints the base URL"
grep -q ":$PORT" "$LOG" && ok "url is on port $PORT" || bad "url printed $(head -1 "$LOG")"
record url "$(head -1 "$LOG")"

run env q env
expect_rc "env" 0 "prints exports"
grep -q "export" "$LOG" && grep -q "$PORT" "$LOG" && ok "env exports the URL" || bad "env printed no export with the port"
record env "lines:$(grep -c export "$LOG")"

run record-list q record list
record record-list "$(head -1 "$LOG" | cut -c1-60)"
ok "record list exits $RC (recorded)"

# A plain restart is what the updater runs, and it must keep the port. If it
# ever forgot it, it would start on 9100 -- and #405 means that kills the
# developer's daemon. So it is only run plainly when nothing holds 9100.
if lsof -nP -iTCP:9100 -sTCP:LISTEN >/dev/null 2>&1; then
  skip "plain restart: 9100 is in use, and a restart that forgot its port would kill it (#405); restarted with explicit ports instead"
  run restart q restart --port "$PORT" --proxy-port "$PROXY_PORT"
else
  run restart q restart
fi
expect_rc "restart" 0 "stops and starts"
pid2="$(sandbox_server_pid)"
[[ -n "$pid2" && "$pid2" != "$pid1" ]] && ok "the pid changed" || bad "the pid did not change ($pid1 -> $pid2)"
[[ "$(sandbox_port)" == "$PORT" ]] && ok "it kept port $PORT" || bad "restart moved to port '$(sandbox_port)'"
healthy "$PORT" && ok "it answers /health again" || bad "nothing answered /health after restart"
record restart "new-pid:$([[ $pid2 != "$pid1" ]] && echo y || echo n) port:$(sandbox_port)"
check_real_server "restart"

run stop q stop
expect_rc "stop" 0 "stops the daemon"
sleep 1
[[ -n "$pid2" ]] && ! kill -0 "$pid2" 2>/dev/null && ok "the process is gone" || bad "pid '$pid2' is still running"
lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 && bad "something still listens on $PORT" || ok "nothing listens on $PORT"
# The proxy goes with it, though mitmdump can take a few seconds.
for i in $(seq 1 15); do lsof -nP -iTCP:"$PROXY_PORT" -sTCP:LISTEN >/dev/null 2>&1 || break; sleep 1; done
lsof -nP -iTCP:"$PROXY_PORT" -sTCP:LISTEN >/dev/null 2>&1 && bad "the proxy still listens on $PROXY_PORT" || ok "nothing listens on $PROXY_PORT"
record stop "process-gone:$(kill -0 "$pid2" 2>/dev/null && echo n || echo y)"

run stop-again q stop
record stop-again "$(head -1 "$LOG" | cut -c1-60)"
ok "a second stop exits $RC (recorded)"
check_real_server "stop"

# --------------------------------------------------------------------------
step "Diagnostics"
# --------------------------------------------------------------------------
run doctor q doctor
record doctor "sections:$(grep -c '^[A-Z]' "$LOG")"
ok "doctor exits $RC (recorded)"

# The endpoint is hard-coded (update_check.ENDPOINT), so the network is cut
# rather than the feed moved -- q is offline by now -- and a check that could
# not ask must not read as "up to date".
run check-updates q check-updates
refused "check-updates with no network" "Could not check for updates"
grep -qi "up to date" "$LOG" && bad "and it also said up to date" || true
record check-updates "$(head -1 "$LOG" | cut -c1-70)"

run capture-env q capture-env "$SB/env-report.txt"
expect_rc "capture-env" 0 "writes a report"
[[ -s "$SB/env-report.txt" ]] && ok "the report is not empty" || bad "no report written"
record capture-env "written:$([[ -s $SB/env-report.txt ]] && echo y || echo n)"

run menubar-status q menubar status
record menubar-status "$(head -1 "$LOG" | cut -c1-60)"
ok "menubar status exits $RC (recorded)"

run tunneld-status q tunneld status
record tunneld-status "rc-only"
ok "tunneld status exits $RC (recorded)"

# A Claude Code hook, not a git one: an entry in ~/.claude/settings.json that
# runs a script setup copies to ~/.quern/bin.
SETTINGS="$HOME_SB/.claude/settings.json"
hook_entries() { grep -o 'agent-precommit-checklist[^"]*' "$SETTINGS" 2>/dev/null | wc -l | tr -d ' '; }
run install-precommit-hook q install-precommit-hook
expect_rc "install-precommit-hook" 0 "registers the hook"
[[ -x "$STATE/bin/agent-precommit-checklist.sh" ]] && ok "the checklist script is in place" || bad "no executable checklist script in ~/.quern/bin"
[[ "$(hook_entries)" -ge 1 ]] && ok "settings.json runs it" || bad "settings.json has no entry for the checklist"
run install-precommit-hook-again q install-precommit-hook
expect_rc "install-precommit-hook again" 0 "refreshes it"
[[ "$(hook_entries)" -eq 1 ]] && ok "running it again leaves one entry" || bad "running it again left $(hook_entries) entries"
record install-precommit-hook "entries:$(hook_entries)"
check_real_server "diagnostics"

# --------------------------------------------------------------------------
step "uninstall"
# --------------------------------------------------------------------------
run uninstall-no-tty q uninstall
[[ -x "$WRAPPER" && "$(registered)" -eq ${#CLIENT_CONFIGS[@]} ]] \
  && ok "uninstall with no terminal removed nothing (exit $RC)" \
  || bad "uninstall with no terminal removed something"
record uninstall-no-tty "wrapper-kept:$([[ -x $WRAPPER ]] && echo y || echo n)"

# "n" to the LaunchDaemon: that one is the real machine's, sudo stub or not.
# Optional, because it is only asked where the daemon is installed.
run uninstall q_answer "Proceed with uninstall?" y "?Remove tunneld LaunchDaemon" n -- uninstall
expect_rc "uninstall" 0 "confirmed through a terminal"
[[ -e "$WRAPPER" ]] && bad "the wrapper is still there" || ok "the wrapper is gone"
left="$(json_get "$HOME_SB/.claude.json" '[k for k in d.get("mcpServers", {}) if "quern" in k.lower()]')"
[[ "$left" == "[]" ]] && ok "Claude Code's quern entry is gone" || bad "Claude Code still has: $left"
[[ "$(json_get "$HOME_SB/.claude.json" 'd.get("numStartups")')" == "7" ]] \
  && ok "Claude Code's own keys survived" || bad "uninstall changed a key that is not quern's"
[[ "$(json_get "$HOME_SB/.claude.json" '"someone-else" in d.get("mcpServers", {})')" == "True" ]] \
  && ok "and so did its other server" || bad "uninstall removed another tool's server from Claude Code"
[[ "$(json_get "$HOME_SB/.cursor/mcp.json" 'sorted(d.get("mcpServers", {}))')" == "['someone-else']" ]] \
  && ok "Cursor is back to just its other server" || bad "Cursor's config is not as it was: $(cat "$HOME_SB/.cursor/mcp.json")"
[[ "$(registered)" -eq 0 ]] && ok "no client is left with a quern entry" \
  || bad "$(registered) of ${#CLIENT_CONFIGS[@]} clients still have a quern entry"
# What uninstall leaves in Claude Code's settings is recorded rather than
# judged: its summary does not mention the checklist hook or the
# grant-full-perms rules, and whether it should is a question for an issue.
record uninstall "wrapper-gone:$([[ -e $WRAPPER ]] && echo n || echo y) registered:$(registered) settings-hook:$(hook_entries) settings-quern-rules:$(json_get "$SETTINGS" 'len([p for p in d.get("permissions", {}).get("allow", []) if "quern" in p])')"
check_real_server "uninstall"

# --------------------------------------------------------------------------
step "Nothing outside the sandbox was touched"
# --------------------------------------------------------------------------
AFTER="$(snapshot_protected)"
if [[ "$BEFORE" == "$AFTER" ]]; then
  ok "every protected path outside the sandbox is unchanged"
else
  bad "a protected path outside the sandbox changed:"
  diff <(echo "$BEFORE") <(echo "$AFTER") | sed 's/^/      /' || true
fi
if [[ -n "$REAL_PID" ]]; then
  if real_server_ok; then
    ok "the developer's server (pid $REAL_PID) ran throughout"
  else
    bad "the developer's server (pid $REAL_PID) did not survive the run"
  fi
fi
printf '\nStubbed calls:\n'
sort "$CALLS" | uniq -c | sed 's/^/  /'

# Outside the tree, named by commit, so a before/after pair is two files to
# diff: `diff "$TMPDIR"/quern-cli-live-<a>.txt "$TMPDIR"/quern-cli-live-<b>.txt`.
tmp_dir="${TMPDIR:-/tmp}"
OUT="${CLI_LIVE_TRANSCRIPT:-${tmp_dir%/}/quern-cli-live-$(git -C "$ROOT" rev-parse --short "$REF").txt}"
cp "$TRANSCRIPT" "$OUT"
printf '\nTranscript: %s\n' "$OUT"

skips="$(skip_count)"
if [[ "$failures" -eq 0 ]]; then
  printf '\033[0;32mThe CLI live test passed\033[0m — %s skipped, %s known failures.\n' "$skips" "$KNOWN"
  exit 0
fi
printf '\033[0;31mThe CLI live test failed\033[0m — %d failures, %s skipped.\n' "$failures" "$skips"
exit 1
