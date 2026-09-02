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
  # Only 2 is worth a review request. Reacting to any nonzero code would ask
  # for a re-review of a PR whose findings nobody has read yet, and again when
  # gh cannot answer -- spending capped review runs on the two cases a review
  # cannot help. Automatic incremental reviews are off (.coderabbit.yaml), so
  # this is the request that gets made, and it should be the useful one.
  python3 scripts/pr-review-status.py "$PR" || STATE=$?
  STATE="${STATE:-0}"

  if [ "$STATE" = "2" ]; then
    echo "Requesting a review of #$PR..."
    gh pr comment "$PR" --repo jerimiah797/quern --body "@coderabbitai review" >/dev/null
    python3 scripts/pr-review-status.py "$PR" --wait || STATE=$?
  fi

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
