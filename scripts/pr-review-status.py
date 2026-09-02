#!/usr/bin/env python3
"""Report whether each open PR has been reviewed since its last push.

The failure this exists to prevent: CodeRabbit re-reviews on every push, so a
"0 unresolved threads" reading taken before the latest push is stale — and
reads exactly like "all clear". Merging on it means merging unreviewed code.

    scripts/pr-review-status.py           # one shot
    scripts/pr-review-status.py --wait    # block until every PR is current
    scripts/pr-review-status.py 85        # a specific PR

Exit code is 0 only when every PR examined is reviewed-and-clean, so this can
gate a merge in a script.

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


def status(number: int) -> tuple[str, str]:
    """Returns (state, detail) where state is ok | pending | findings | unknown.

    Every failure path returns "unknown", never "ok". A gate that cannot see
    the data must refuse, not approve — rate limiting and network trouble are
    exactly when it matters, and both are common here.
    """
    try:
        return _status(number)
    except (GhError, json.JSONDecodeError, KeyError, ValueError) as exc:
        return "unknown", f"#{number} — could not determine review state: {exc}"


def _status(number: int) -> tuple[str, str]:
    raw = gh("pr", "view", str(number), "--repo", REPO, "--json", "title")
    pr = json.loads(raw)

    commits = json.loads(gh("pr", "view", str(number), "--repo", REPO, "--json", "commits"))
    dates = [c["committedDate"] for c in commits["commits"]]
    if not dates:
        raise ValueError("the PR reported no commits")
    pushed = max(ts(d) for d in dates)

    reviews = json.loads(gh("api", f"repos/{REPO}/pulls/{number}/reviews"))
    reviewed = max((ts(r.get("submitted_at")) for r in reviews), default=0.0)

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


def report(numbers: list[int]) -> bool:
    all_ok = True
    for n in numbers:
        state, detail = status(n)
        mark = {"ok": "OK  ", "pending": "WAIT", "findings": "READ", "unknown": "FAIL"}[state]
        print(f"  {mark} {detail}")
        all_ok &= state == "ok"
    return all_ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("prs", nargs="*", type=int)
    ap.add_argument("--wait", action="store_true",
                    help="poll until every PR is reviewed and clean")
    ap.add_argument("--timeout", type=int, default=900)
    args = ap.parse_args()

    numbers = args.prs or open_prs()
    if not numbers:
        print("  no open PRs")
        return 0

    deadline = time.monotonic() + args.timeout
    while True:
        print(f"— review status @ {datetime.now().strftime('%H:%M:%S')}")
        if report(numbers):
            return 0
        if not args.wait or time.monotonic() > deadline:
            return 1
        time.sleep(30)


if __name__ == "__main__":
    sys.exit(main())
