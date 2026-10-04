# Sandbox helpers shared by release-rehearsal.sh and cli-live-test.sh (#406).
#
# Sourced, not run. Both scripts drive quern for real against a throwaway HOME
# and QUERN_STATE_DIR, and both must prove afterwards that nothing outside the
# sandbox moved -- so the snapshot, the stubs and the developer's-server check
# live here once, where a fix to one reaches the other.
#
# Expects the sourcing script to have set `set -euo pipefail`. Sets REAL_HOME
# from HOME at source time, so source it before anything exports a sandbox HOME.

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

REAL_HOME="$HOME"

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
  # Any further arguments are more tools to stub: the live test runs
  # `setup --yes`, which reaches brew, pipx, defaults and xcode-select.
  for tool in osascript open sudo launchctl pkill killall "${@:2}"; do
    cat > "$bin/$tool" <<EOF
#!/bin/sh
echo "$tool \$*" >> "$bin/../calls.log"
exit 0
EOF
    chmod +x "$bin/$tool"
  done
}

