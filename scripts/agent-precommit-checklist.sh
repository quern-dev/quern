#!/usr/bin/env bash
#
# Pre-commit checklist hook for Claude Code agents working on apps that
# use Quern. Installed globally by `quern setup` (or `quern install-
# precommit-hook`) into ~/.claude/settings.json so the hook fires in any
# Claude Code session, but emits the checklist only when the agent is
# committing in a project that actually uses Quern.
#
# Trigger: PreToolUse:Bash with a command containing `git commit`.
# Gate: the project's working directory (or any ancestor) must contain
#       a .quern/ directory — that's the signal the user has initialized
#       Quern app knowledge for this project.
# Output: the contents of agent-precommit-checklist.md sitting next to
#         this script. Becomes a system reminder for the agent.
# Exit: always 0 — informational, never a gate.

set -u

input=$(cat 2>/dev/null || true)

# Match `git commit` in the tool call. Covers `git commit -m "..."`,
# `git commit --amend`, `cd … && git commit`, etc. Avoids matching
# `git log --grep="commit"` and other strings that contain the word.
echo "$input" | grep -qE '"command"[[:space:]]*:[[:space:]]*"[^"]*git[[:space:]]+commit' || exit 0

# Stay silent in projects that don't use Quern. Walk up from cwd to /
# looking for `.quern/knowledge/` — the canonical per-project signal
# that the user has initialized app knowledge here (screen docs,
# landmarks, flows). Stops at $HOME so we don't match the user-level
# data dir at ~/.quern/ which exists for any Quern installation.
dir="$(pwd)"
found_quern=""
while [ "$dir" != "/" ] && [ "$dir" != "$HOME" ]; do
    if [ -d "$dir/.quern/knowledge" ]; then
        found_quern="$dir/.quern"
        break
    fi
    dir="$(dirname "$dir")"
done

if [ -z "$found_quern" ]; then
    exit 0
fi

# Emit the checklist. Sits next to this script in the install location
# (~/.quern/bin/agent-precommit-checklist.sh + ~/.quern/agent-precommit-
# checklist.md), so we resolve the install dir from $0 and look one
# level up.
script_dir="$(cd "$(dirname "$0")" && pwd)"
checklist="$script_dir/../agent-precommit-checklist.md"

if [ -f "$checklist" ]; then
    cat "$checklist"
fi

exit 0
