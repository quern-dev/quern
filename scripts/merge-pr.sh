#!/usr/bin/env bash
# Merge a PR, but only if its review is current.
#
# The failure this prevents: CodeRabbit re-reviews on every push, so a
# "0 unresolved" reading taken before the latest push is stale and reads
# exactly like all-clear. Checking after every push does not fix that -- the
# risky moment is the merge, not the push -- so the check lives here, where
# acting on stale data actually costs something.
#
#   scripts/merge-pr.sh 85            # refuses if the review is not current
#   scripts/merge-pr.sh 85 --wait     # waits for the review, then merges
#   scripts/merge-pr.sh 85 --force    # merge anyway, deliberately
set -euo pipefail
cd "$(dirname "$0")/.."

PR="${1:?usage: merge-pr.sh <number> [--wait] [--force]}"; shift
WAIT=""; FORCE=""
for a in "$@"; do
  case "$a" in
    --wait) WAIT="--wait" ;;
    --force) FORCE=1 ;;
  esac
done

if [ -n "$FORCE" ]; then
  echo "Skipping the review gate deliberately (--force)."
elif ! python3 scripts/pr-review-status.py "$PR" $WAIT; then
  echo
  echo "Not merging #$PR: its review is not current, or it has unresolved findings."
  echo "  --wait   block until the review lands"
  echo "  --force  merge anyway"
  exit 1
fi

gh pr merge "$PR" --repo jerimiah797/quern --merge --delete-branch
