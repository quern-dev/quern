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
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime

REPO = "jerimiah797/quern"


def gh(*args: str) -> str:
    return subprocess.run(["gh", *args], capture_output=True, text=True).stdout.strip()


def ts(value: str | None) -> float:
    if not value:
        return 0.0
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def open_prs() -> list[int]:
    raw = gh("pr", "list", "--repo", REPO, "--state", "open", "--json", "number")
    return [p["number"] for p in json.loads(raw or "[]")]


def status(number: int) -> tuple[str, str]:
    """Returns (state, detail) where state is ok | pending | findings | unknown."""
    raw = gh("pr", "view", str(number), "--repo", REPO,
             "--json", "headRefOid,title,statusCheckRollup")
    if not raw:
        return "unknown", "could not read the PR"
    pr = json.loads(raw)

    commits = json.loads(gh("pr", "view", str(number), "--repo", REPO, "--json", "commits") or "{}")
    pushed = max((ts(c["committedDate"]) for c in commits.get("commits", [])), default=0.0)

    reviews = json.loads(gh("api", f"repos/{REPO}/pulls/{number}/reviews") or "[]")
    reviewed = max((ts(r.get("submitted_at")) for r in reviews), default=0.0)

    owner, name = REPO.split("/")
    query = (
        f'{{repository(owner:"{owner}", name:"{name}")'
        f"{{pullRequest(number:{number}){{"
        "reviewThreads(last:60){nodes{isResolved isOutdated}}}}}"
    )
    threads = json.loads(gh("api", "graphql", "-f", f"query={query}") or "{}")
    nodes = (threads.get("data", {}).get("repository", {}).get("pullRequest", {})
             .get("reviewThreads", {}).get("nodes", []))
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
        mark = {"ok": "OK  ", "pending": "WAIT", "findings": "READ", "unknown": "??  "}[state]
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
