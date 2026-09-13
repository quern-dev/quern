#!/bin/bash
# Every CodeRabbit finding on a PR, including the ones that are not threads.
#
# Inline findings are review threads, which `gh api graphql ... reviewThreads`
# returns. "Outside diff range", "Additional", "Nitpick" and "Duplicate"
# findings are not threads at all -- they live in the review *body*, inside
# collapsed <details> blocks -- so anything that walks reviewThreads alone
# silently skips them, with no count and no gap to notice.
#
# That is not hypothetical. On #145 a Major finding sat there unread while the
# threads showed a single item: a queued value equal to the one just written
# was written again, which for the update channel discards the cached update
# check a second time. It surfaced only because a human scrolled the page. It
# also turned out to be the finding that exposed a test asserting the bug as
# correct behaviour.
#
# Usage:  scripts/cr-findings.sh <pr-number> [owner/repo]
set -euo pipefail
N="${1:?usage: cr-findings.sh <pr-number>}"
REPO="${2:-quern-dev/quern}"
OWNER="${REPO%%/*}"; NAME="${REPO##*/}"

echo "── inline review threads ──"
gh api graphql -f query='query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){pullRequest(number:$n){reviewThreads(first:100){nodes{isResolved,path,line,comments(first:1){nodes{body}}}}}}}' \
  -f o="$OWNER" -f r="$NAME" -F n="$N" \
  --jq '.data.repository.pullRequest.reviewThreads.nodes[]
        | "[\(if .isResolved then "resolved" else "OPEN" end)] \(.path):\(.line // "?")  "
          + ((.comments.nodes[0].body | split("\n") | map(select(startswith("**")))[0]) // "(no headline)")'

echo
echo "── everything else (review bodies and comments) ──"
gh pr view "$N" --repo "$REPO" --json reviews,comments \
  --jq '(.reviews[]?.body // empty), (.comments[]?.body // empty)' \
| python3 -c '
import re, sys
text = sys.stdin.read()
# The body wraps these in <details><summary>Heading (N)</summary>…, with the
# finding lines quoted. Headlines are bold.
for section in ("Outside diff range comments", "Additional comments",
                "Nitpick comments", "Duplicate comments"):
    for m in re.finditer(re.escape(section) + r"[^(]*\((\d+)\)", text):
        chunk = text[m.start():m.start() + 4000]
        print(f"{section} ({m.group(1)}):")
        seen = set()
        for f in re.findall(r"\*\*(.+?)\*\*", chunk):
            if f not in seen and len(f) > 15:
                seen.add(f)
                print(f"   - {f}")
        for loc in re.findall(r"`(\d+-\d+)`", chunk)[:5]:
            print(f"     at lines {loc}")
        print()
'
