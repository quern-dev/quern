#!/usr/bin/env bash
# Merge a PR, but only if its review is current.
#
# The failure this prevents: CodeRabbit re-reviews on every push, so a
# "0 unresolved" reading taken before the latest push is stale and reads
# exactly like all-clear. Checking after every push does not fix that -- the
# risky moment is the merge, not the push -- so the check lives here, where
# acting on stale data actually costs something.
#
#   scripts/merge-pr.sh 85            # asks for a review if stale, waits, merges
#   scripts/merge-pr.sh 85 --force    # merge anyway, deliberately
#
# --wait is implied when a review has to be requested; pass it explicitly to
# wait on a review someone else already triggered.
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
else
  # 0 clean · 2 pending · 3 unresolved findings · 4 undeterminable.
  #
  # --ask lets the check put the question to CodeRabbit directly when the PR
  # looks stale. Nothing else in the API answers it: a clean review leaves no
  # review object, and the summary comment refreshes on every push whether or
  # not a review ran. Asking costs a comment, which is why the plain status
  # check stays read-only and only the merge path pays it.
  python3 scripts/pr-review-status.py "$PR" --ask $WAIT || STATE=$?

  case "${STATE:-0}" in
    0) ;;
    3) echo; echo "Not merging #$PR: it has unresolved findings. Read them first."; exit 1 ;;
    4) echo; echo "Not merging #$PR: could not determine its review state."; exit 1 ;;
    *) echo; echo "Not merging #$PR: still awaiting review."; exit 1 ;;
  esac
fi

# Bind the merge to the commit that was reviewed. Between the check above and
# the merge below, a push can land -- and merging then ships a commit nothing
# reviewed, which is the exact hole this script exists to close. GitHub refuses
# the merge if the head has moved.
HEAD_SHA=$(gh pr view "$PR" --repo jerimiah797/quern --json headRefOid -q .headRefOid)
gh pr merge "$PR" --repo jerimiah797/quern --merge --delete-branch \
  --match-head-commit "$HEAD_SHA"
