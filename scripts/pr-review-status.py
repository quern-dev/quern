#!/usr/bin/env python3
"""Report whether each open PR has been reviewed since its last push.

The failure this exists to prevent: CodeRabbit re-reviews on every push, so a
"0 unresolved threads" reading taken before the latest push is stale — and
reads exactly like "all clear". Merging on it means merging unreviewed code.

    scripts/pr-review-status.py           # one shot, read-only
    scripts/pr-review-status.py --wait    # block until every PR is current
    scripts/pr-review-status.py 85        # a specific PR
    scripts/pr-review-status.py 85 --ask  # ask CodeRabbit directly (posts a comment)

Exit codes: 0 reviewed and clean, 2 pending a review, 3 has unresolved
findings, 4 could not be determined. Only 0 permits a merge, and only 2 is
worth waiting on.

Do not pipe it when you care about that exit code: `... | tail` reports tail's
status, not this script's, so a still-pending PR reads as success. merge-pr.sh
calls it directly for exactly this reason.

Note on cadence: CodeRabbit allows a limited number of review runs per hour
(10 at time of writing), so a push can sit queued rather than being reviewed
promptly. Several small pushes to the same branch spend that budget faster than
one considered push, and leave every intermediate state unreviewed.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime

REPO = "jerimiah797/quern"

# Distinct exit codes, so a caller can tell *why* a PR is not mergeable.
# merge-pr.sh needs that: requesting a review helps a pending PR and is pure
# waste for one with unresolved findings, which was spending capped review runs
# on PRs a review could not help.
# The exact bot, not a substring. A login merely *containing* "coderabbit" is
# something anyone can register, and this marker advances the reviewed
# timestamp -- so a lookalike commenter could make an unreviewed PR look
# reviewed and walk it through the gate. The numeric id is the stable identity;
# the login is checked too so a mismatch is obvious in a diff.
CODERABBIT_ID = 136622811
CODERABBIT_LOGIN = "coderabbitai[bot]"

EXIT_OK = 0
EXIT_PENDING = 2
EXIT_FINDINGS = 3
EXIT_UNKNOWN = 4
_EXIT = {"ok": EXIT_OK, "pending": EXIT_PENDING,
         "findings": EXIT_FINDINGS, "unknown": EXIT_UNKNOWN}


class GhError(RuntimeError):
    """A gh invocation failed. Raised rather than returned, because the caller
    treating an empty result as data is precisely how this gate failed open:
    a failed query produced no commits, no commits produced a push time of 0,
    and any prior review then looked newer than the latest push."""


def gh(*args: str) -> str:
    try:
        result = subprocess.run(["gh", *args], capture_output=True, text=True)
    except OSError as exc:  # gh not installed, not on PATH, not executable
        raise GhError(f"could not run gh: {exc}") from exc
    if result.returncode != 0:
        raise GhError(f"gh {' '.join(args[:3])}: {result.stderr.strip()[:160]}")
    return result.stdout.strip()


def ts(value: str | None) -> float:
    if not value:
        return 0.0
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def open_prs() -> list[int]:
    raw = gh("pr", "list", "--repo", REPO, "--state", "open", "--json", "number")
    return [p["number"] for p in json.loads(raw or "[]")]



# CodeRabbit's replies to "@coderabbitai review", observed. It answers whether
# the current head has been reviewed, which nothing else in the API does.
_ALREADY_REVIEWED = "already reviewed the last commit"
_FINISHED = "review finished"
_TRIGGERED = "review triggered"


def _ask_for_review(number: int) -> None:
    gh("pr", "comment", str(number), "--repo", REPO, "--body", "@coderabbitai review")


def _latest_reply(number: int, after: float) -> str:
    """The newest CodeRabbit reply to a review request, lowercased."""
    pages = json.loads(gh("api", "--paginate", "--slurp",
                          f"repos/{REPO}/issues/{number}/comments"))
    newest, body = after, ""
    for c in [c for page in pages for c in page]:
        user = c.get("user") or {}
        if user.get("id") != CODERABBIT_ID or user.get("login") != CODERABBIT_LOGIN:
            continue
        text = c.get("body", "")
        if "review command invocation" not in text.lower() and "action" not in text.lower():
            continue
        when = ts(c.get("created_at"))
        if when > newest:
            newest, body = when, text
    return body.lower()


def _reviewed_by_asking(number: int, timeout: float = 600.0) -> bool:
    """Ask whether the head commit is reviewed, waiting if a review starts.

    Returns True when CodeRabbit reports the current head as reviewed. A
    request costs a comment, so this runs only when the cheaper signals already
    say the PR looks stale.
    """
    asked_at = time.time()
    _ask_for_review(number)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(20)
        reply = _latest_reply(number, asked_at)
        if _ALREADY_REVIEWED in reply or _FINISHED in reply:
            return True
        if _TRIGGERED in reply:
            # A review is running. It leaves a review object only if it finds
            # something, so ask again rather than watching for one.
            time.sleep(40)
            asked_at = time.time()
            _ask_for_review(number)
    return False


def status(number: int, ask: bool = False) -> tuple[str, str]:
    """Returns (state, detail) where state is ok | pending | findings | unknown.

    Every failure path returns "unknown", never "ok". A gate that cannot see
    the data must refuse, not approve — rate limiting and network trouble are
    exactly when it matters, and both are common here.
    """
    try:
        return _status(number, ask)
    except (GhError, json.JSONDecodeError, KeyError, ValueError) as exc:
        return "unknown", f"#{number} — could not determine review state: {exc}"


def _status(number: int, ask: bool = False) -> tuple[str, str]:
    raw = gh("pr", "view", str(number), "--repo", REPO, "--json", "title")
    pr = json.loads(raw)

    commits = json.loads(gh("pr", "view", str(number), "--repo", REPO, "--json", "commits"))
    dates = [c["committedDate"] for c in commits["commits"]]
    if not dates:
        raise ValueError("the PR reported no commits")
    pushed = max(ts(d) for d in dates)

    review_pages = json.loads(gh("api", "--paginate", "--slurp",
                                 f"repos/{REPO}/pulls/{number}/reviews"))
    reviews = [r for page in review_pages for r in page]
    reviewed = max((ts(r.get("submitted_at")) for r in reviews
                    if (r.get("user") or {}).get("id") == CODERABBIT_ID), default=0.0)

    # Whether the newest commit has been reviewed is not inferable from the
    # API, and every proxy tried here was wrong in one direction or the other:
    #
    #   review objects        a clean review creates none, so a ready PR looks
    #                         unreviewed forever
    #   summary comment       its updated_at refreshes on every push whether or
    #                         not a review ran, so unreviewed code looks
    #                         reviewed — measured nine seconds after a push
    #
    # CodeRabbit will simply say, though, if asked. Its reply to a review
    # request is the authoritative signal, so ask instead of guessing.
    if reviewed < pushed and ask:
        reviewed = pushed if _reviewed_by_asking(number) else reviewed

    owner, name = REPO.split("/")
    query = (
        f'{{repository(owner:"{owner}", name:"{name}")'
        f"{{pullRequest(number:{number}){{"
        "reviewThreads(last:60){nodes{isResolved isOutdated}}}}}"
    )
    threads = json.loads(gh("api", "graphql", "-f", f"query={query}"))
    nodes = threads["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    unresolved = sum(1 for n in nodes if not n["isResolved"] and not n["isOutdated"])

    title = pr.get("title", "")[:44]
    if reviewed < pushed:
        return "pending", f"#{number} {title} — pushed since the last review"
    if unresolved:
        return "findings", f"#{number} {title} — {unresolved} unresolved"
    return "ok", f"#{number} {title} — reviewed, clean"


def report(numbers: list[int], ask: bool = False) -> int:
    """Returns the worst exit code across the PRs examined."""
    worst = EXIT_OK
    for n in numbers:
        state, detail = status(n, ask)
        mark = {"ok": "OK  ", "pending": "WAIT", "findings": "READ", "unknown": "FAIL"}[state]
        print(f"  {mark} {detail}")
        # Unknown outranks findings outranks pending: the least understood
        # state is the one a caller should act most cautiously on.
        worst = max(worst, _EXIT[state])
    return worst


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("prs", nargs="*", type=int)
    ap.add_argument("--wait", action="store_true",
                    help="poll until every PR is reviewed and clean")
    ap.add_argument("--ask", action="store_true",
                    help="ask CodeRabbit whether the head is reviewed. Posts a "
                         "comment, so it is off by default: a status check "
                         "should not change the thing it reports on.")
    ap.add_argument("--timeout", type=int, default=900)
    args = ap.parse_args()

    numbers = args.prs or open_prs()
    if not numbers:
        print("  no open PRs")
        return 0

    deadline = time.monotonic() + args.timeout
    while True:
        print(f"— review status @ {datetime.now().strftime('%H:%M:%S')}")
        code = report(numbers, args.ask)
        if code == EXIT_OK:
            return EXIT_OK
        # Only pending resolves by waiting. Findings need a human, and unknown
        # means the check itself could not see the data.
        if not args.wait or code != EXIT_PENDING or time.monotonic() > deadline:
            return code
        time.sleep(30)


if __name__ == "__main__":
    sys.exit(main())
