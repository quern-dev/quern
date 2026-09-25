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
# There is a third place, and it is not a review at all: the **pre-merge checks**
# table in the walkthrough *issue comment*. A PR can have zero reviews and zero
# review comments and still be carrying findings there -- #309 did, and this
# script printed two empty sections for it. So the checks table is read below,
# separately, and it is read from the issue comments rather than the reviews.
#
# Do not read a failing table as a wall, though; most of it is noise. Measured
# over the 28 most recently merged PRs (2026-09-25): 23 merged with a failing
# tally, and Docstring Coverage was failing in *all 23* -- an 80% threshold
# nothing here meets. Only two carried anything else: #277 (Out of Scope
# Changes) and #275 (Title check, Inconclusive). That ratio is exactly why a
# real finding goes unseen: the table has been red by default for months.
# Docstring Coverage is therefore reported under a separate heading, not hidden
# -- hiding it would just be a fourth blind spot.
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

echo
echo "── pre-merge checks (walkthrough comment, not a review) ──"
# Deliberately re-fetched from the issue comments: with zero reviews there is no
# review body to carry this, which is the case the section exists for.
gh api "repos/$OWNER/$NAME/issues/$N/comments?per_page=100" --paginate \
  --jq '.[] | select(.user.login=="coderabbitai[bot]") | .body' \
| python3 -c '
import re, sys
text = sys.stdin.read()

tallies = re.findall(r"Pre-merge checks \| ([^<\n]*)", text)
if not tallies:
    print("  (no checks table -- CodeRabbit may not have processed this PR)")
    raise SystemExit(0)

tally = tallies[-1].strip()
print(f"  tally: {tally}")

# Rows look like:  | Check name | <status> | explanation | resolution |
# The status column is NOT always "⚠️ Warning": "❓ Inconclusive" exists too
# (#275 had one, and a tally of ❌ 2 with a single Warning row is the tell), and
# matching one spelling is how a failing check goes unreported by the very tool
# meant to surface it. So match any status and treat everything that is not a
# pass as worth showing.
rows = re.findall(r"\|\s*([A-Za-z][A-Za-z ]{3,40}?)\s*\|\s*([^|]*?)\s*\|([^|]*)", text)
NOISE = "docstring coverage"
real, noise = [], []
for name, status, why in rows:
    name, status = name.strip(), status.strip()
    if not status or "Passed" in status or status.startswith("✅"):
        continue
    if "Status" in status or set(status) <= set(": -"):   # header / separator rows
        continue
    entry = (f"{name} [{status}]", " ".join(why.split())[:160])
    (noise if name.lower() == NOISE else real).append(entry)

def dedupe(xs):
    seen, out = set(), []
    for n, w in xs:
        if n not in seen:
            seen.add(n); out.append((n, w))
    return out

real, noise = dedupe(real), dedupe(noise)

if real:
    print("  FAILED -- look at these:")
    for n, w in real:
        print(f"    - {n}")
        if w: print(f"        {w}")
if noise:
    print("  known-noisy (fails on ~4 of every 5 merged PRs here; judge, do not reflex-fix):")
    for n, _ in noise:
        print(f"    - {n}")
if not real and not noise and "❌" in tally:
    print("  tally shows failures but no warning rows parsed -- read the comment by hand")
'
