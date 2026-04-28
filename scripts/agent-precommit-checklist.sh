#!/usr/bin/env bash
#
# Pre-commit checklist hook for Claude Code agents working in this repo.
#
# Fires on PreToolUse:Bash. Reads the tool-call JSON from stdin and, if
# the command is a `git commit`, surfaces docs/agent-precommit-checklist.md
# as a system reminder. The agent reads the checklist before finalizing
# the commit.
#
# Always exits 0 — this is informational, never a gate.

set -u

input=$(cat 2>/dev/null || true)

# Match `git commit` in the tool input. Covers `git commit -m "..."`,
# `git commit --amend`, `cd … && git commit`, etc. Avoids matching
# `git log --grep="commit"` or other strings that contain the word.
if echo "$input" | grep -qE '"command"[[:space:]]*:[[:space:]]*"[^"]*git[[:space:]]+commit'; then
    repo_root="$(cd "$(dirname "$0")/.." && pwd)"
    checklist="$repo_root/docs/agent-precommit-checklist.md"
    if [ -f "$checklist" ]; then
        cat "$checklist"
    fi
fi

exit 0
