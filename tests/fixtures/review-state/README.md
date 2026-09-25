# Recorded review states

Real states of real PRs, recorded with `scripts/capture-review-state.py`, for
testing `scripts/pr-review-status.py` against things that actually happened
rather than things someone imagined.

Each file holds every `gh` call the check made, with its exact stdout, plus:

| key | meaning |
|---|---|
| `ground_truth` | what was really going on, written by whoever captured it |
| `expected_verdict` | what the check *should* say |
| `verdict_when_captured` | what it *did* say |

Where the last two differ the fixture is an open bug, and
`tests/test_review_status_states.py` xfails it strictly — so a fix turns the
xfail into a failure that has to be acknowledged by updating the file.

## Taking a new one

```sh
scripts/capture-review-state.py <pr> <scenario-name> "what was really true"
```

It runs the check with `ask=False`, so it posts nothing and cannot change the
state it is recording. Then add `expected_verdict` and `expected_why` by hand —
deliberately, because deriving the expectation from the check's own answer is
how a fixture ends up asserting the bug.

Capture states as you meet them. The useful ones are the ones that surprised
you.

## What is here

| scenario | ground truth | expected |
|---|---|---|
| `false-clean-from-thread-resolution` | #224 at `51f8cc3`: no review had read the head; asked, CodeRabbit said "Review rate limited" | `pending` — read `ok` before the empty-body filter |
| `merged-after-genuine-review` | #204 at `bb59530`: a real review landed 65 minutes after the head | `ok` |
| `clean-pass-proved-by-coverage-marker` | #234: head newer than any bodied review, but the summary comment's coverage marker names it; four unresolved-but-outdated threads | `ok` |
| `clean-pass-leaves-no-review-body` | #228 at `308286b`: genuinely reviewed clean at 03:56Z, no review object posted; "Already reviewed the last commit" when asked | `pending` — unknowable without asking, so it fails closed |
| `never-reviewed-opened-while-rate-limited` | #228 earlier: opened during a rate limit, never reviewed | `pending` |

The first two are a matched pair and the reason this directory exists: same
verdict, opposite truth. A check that fixed the bug by answering `pending` more
often would pass the first and break the second, so any fix has to separate
them on evidence.

What separates them, in the recorded payloads:

```
#224  head committed 23:32:43Z
      coderabbitai reviews after it: 23:33:18Z (body 0 bytes), 23:33:22Z (body 0 bytes)
      newest with a body: 23:25:53Z — before the head

#204  head committed 21:20:11Z
      newest with a body: 22:25:17Z (2176 bytes) — after the head
```

The empty ones are review events GitHub creates when CodeRabbit *resolves
threads*. The check took the newest review by CodeRabbit's user id, so
resolving threads after a push marked that push as reviewed. It is worth being
clear that this was the check's own doing: replying to findings and resolving
them is the normal way to answer a review, and doing it is what made the next
reading lie. **Fixed** by counting only reviews with a non-empty body.

Dropping the empty ones does not make a clean pass look unreviewed, because a
clean pass posts no review object at all. Two other signals cover that, and
both are exercised here: the summary comment's
`final_review_risk_coverage.coveredCommitId`, which is the one trace a clean
review leaves (`clean-pass-proved-by-coverage-marker`), and asking CodeRabbit
directly, which is what `--ask` and `merge-pr.sh` do
(`clean-pass-leaves-no-review-body`, where the answer was "Already reviewed the
last commit").

## Known gap

Nothing here covers a genuine CodeRabbit review that legitimately has an empty
body — if one exists, "newest review with a body" would call a reviewed head
unreviewed, which fails closed rather than open, but would still be wrong. Every
real review observed so far opens with `**Actionable comments posted: N**`.
Capture one if you ever see otherwise.

States seen but not yet captured, all from `CONTRIBUTING.md`: a draft PR (no
review, and `cr-findings.sh` reads empty, indistinguishable from "found
nothing"), and a stacked PR whose base is not the default branch (auto review
disabled entirely).

One more, and this one the payloads here already contain. The check fetches
`issues/<n>/comments` for the coverage marker, so a capture also picks up the
walkthrough comment's **pre-merge checks** table — four of the five files here
hold one, `merged-after-genuine-review` (#204) being old enough to predate the
feature — and nothing reads it. #309
merged with a marker equal to head, zero reviews, zero review comments, an empty
`cr-findings.sh`, and `❌ 2` in that table, one of them a real out-of-scope
finding. #277 and #305 are merged in the same shape and still show it.

Deliberately *not* filed as a failing expectation, because the verdict was
right: this check answers "has a review read this head", not "are there
warnings", and gating on that table would be worse than the gap. Over the 28
most recently merged PRs, 23 had a failing tally and Docstring Coverage was
failing in all 23 — a gate on it would block almost every merge for a threshold
nothing here meets. The defect was that nobody *saw* the two real findings, which is a
reporting problem; `scripts/cr-findings.sh` now reads the table and sorts
Docstring Coverage under its own heading. Capture a state here if that judgement
ever needs revisiting — the evidence is already in these files.
