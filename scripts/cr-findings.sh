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
# One record per comment, marker-separated: the tally and the rows it describes
# have to come from the *same* comment. Concatenating every body and taking the
# last tally pairs a current tally with an older comment's rows, which reports a
# failure that may already be resolved -- the misreporting this file exists to
# stop, in the tool meant to stop it.
gh api "repos/$OWNER/$NAME/issues/$N/comments?per_page=100" --paginate \
  --jq '.[] | select(.user.login=="coderabbitai[bot]") | "\u0000CR-COMMENT\u0000" + .body' \
| python3 -c '
import re, sys

TALLY = re.compile(r"Pre-merge checks \| ([^<\n]*)")

comments = [c for c in sys.stdin.read().split("\x00CR-COMMENT\x00") if c.strip()]
# Select on "yields a tally", not "mentions the phrase". A comment can name
# "Pre-merge checks" in prose -- a reply about them, a quoted heading -- and
# then the tally regex finds nothing, so indexing [-1] raises IndexError and
# takes the whole script'"'"'s exit code with it.
withtable = [(c, m[-1].strip()) for c in comments if (m := TALLY.findall(c))]
if not withtable:
    if any("Pre-merge checks" in c for c in comments):
        print("  (the phrase appears but no table does -- read the comments by hand)")
    else:
        print("  (no checks table -- CodeRabbit may not have processed this PR)")
    raise SystemExit(0)
if len(withtable) > 1:
    print(f"  note: {len(withtable)} comments carry a table; reading the newest")

# The newest one, and everything below is parsed from it alone.
text, tally = withtable[-1]
print(f"  tally: {tally}")

# Rows look like:  | Check name | <status> | explanation | resolution |
#
# Neither column may be matched narrowly, and both mistakes were made here
# first. The status is not always "⚠️ Warning" -- "❓ Inconclusive" exists too,
# and #275 is the tell: a tally of ❌ 2 with a single Warning row. The *name*
# is worse, because check names are user-defined: CodeRabbit allows custom ones
# up to 50 characters, so a `[A-Za-z ]` name pattern silently drops `CI-1`,
# and the report then looks tidy because Docstring Coverage still parsed. A
# tool built to stop findings hiding must not hide one over a character class.
#
# So: take the whole cell, and treat anything that is not a pass as worth
# showing.
#
# But scope the search to the checks table. The walkthrough comment holds other
# tables -- the "Layer / File(s) | Summary" one among them -- and a cell-shaped
# pattern run over the whole comment reports their header rows as failing
# checks. Widening the cell without narrowing the region traded a false negative
# for a false positive; the count cross-check below is what caught it.
# End the region on a *structural* marker, never on bare label text. Check
# names are user-supplied, so a check actually named "Generate unit tests" would
# otherwise cut the region at its own name and drop its own row. A table cell
# cannot contain a `<summary>` tag or start a markdown heading, so anchoring on
# those makes the boundary independent of anything a name can say. This is the
# fourth time in this file that matching a bare substring against
# user-controlled text was wrong; the pattern is the lesson, not the label.
start = text.rfind("Pre-merge checks")
region = text[start:]
END = re.compile(
    r"(?:<summary>|^\s*#{1,6}\s*)[^<\n]*"
    r"(?:Finishing Touches|Generate docstrings|Generate unit tests)",
    re.MULTILINE,
)
end = END.search(region)
if end:
    region = region[:end.start()]
rows = re.findall(r"\|([^|\n]{1,60})\|([^|\n]{0,40})\|([^|\n]*)", region)
NOISE = "docstring coverage"
real, noise = [], []
for name, status, why in rows:
    name, status = name.strip(), status.strip()
    if not name or not status:
        continue
    if "Passed" in status or status.startswith("\u2705"):
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
# The count is the real guard. Widening a regex fixes the case you thought of;
# comparing what was parsed against what the tally claims catches the next one
# too, which is the whole complaint this script exists to make.
declared = re.search(r"\u274c\s*(\d+)", tally)
declared = int(declared.group(1)) if declared else 0
parsed = len(real) + len(noise)
if declared and parsed != declared:
    print(f"  WARNING: tally declares {declared} failing but {parsed} row(s) "
          f"parsed -- something was dropped; read the comment by hand")
elif declared and not real and not noise:
    print("  tally shows failures but no rows parsed -- read the comment by hand")
'
