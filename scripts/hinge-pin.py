#!/usr/bin/env python3
"""The upstream commit tools/hinge-control was last reviewed against (#537).

    scripts/hinge-pin.py --check          exit 0 if no upstream commit since the
                                          pin touches the helper, 1 if some do,
                                          2 if GitHub could not be asked
    scripts/hinge-pin.py --check --json   the same, as {"pinned", "head",
                                          "behind", "commits", "compare"}
    scripts/hinge-pin.py --bump [COMMIT]  record COMMIT (default: the newest
                                          that touches the helper) as reviewed

The helper is a copy of tddworks/baguette's Injected/HingeControl, adapted. The
hard part is the knowledge -- Apple's private pose events and the HID setup --
and upstream keeps learning it: the helper changed 8 times in its first month,
once for an Xcode 27.1 fix. So this says when there is something to look at.

A bump records a *review*, not a merge: much of what changes upstream (the key
service, the frontmost query) is left out of this copy, and "reviewed, nothing
to take" has to be recordable or the tracking issue never closes. Before
bumping, diff the copy against upstream, take what matters, run
scripts/hinge-selftest.sh, and live-test on an iPhone Duo if anything was
taken. Standard library only: the weekly workflow runs this on a bare runner.
GITHUB_TOKEN, when set, is used to ask GitHub.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

PIN_FILE = Path(__file__).resolve().parent.parent / "tools" / "hinge-control" / "hinge-pin.json"
API = "https://api.github.com"
#: Pages of the path's history to look through for the pin before giving up:
#: 500 commits to one directory is far beyond anything that would go unread.
MAX_PAGES = 5

Getter = Callable[[str], object]


class CouldNotAsk(Exception):
    """GitHub could not be asked, or did not answer in a usable way."""


def _get(url: str) -> object:
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": "quern-hinge-pin"})
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise CouldNotAsk(f"{url}: {e}") from e


def _pin() -> dict:
    return json.loads(PIN_FILE.read_text())


def commits_since(pin: dict, get: Getter = _get, stop_at: str | None = None) -> list[dict]:
    """Upstream commits touching the path, newest first, back to `stop_at`
    (default: the pinned commit), which is not included.

    Raises CouldNotAsk when that commit is not found in the path's history: a
    rewritten upstream or a typo in the pin must not read as "nothing new".
    """
    stop_at = stop_at or pin["commit"]
    found: list[dict] = []
    path = urllib.parse.quote(pin["path"])
    for page in range(1, MAX_PAGES + 1):
        batch = get(f"{API}/repos/{pin['repo']}/commits?path={path}&per_page=100&page={page}")
        if not isinstance(batch, list):
            raise CouldNotAsk(f"GitHub answered something not a commit list: {batch!r:.200}")
        for c in batch:
            try:
                sha, commit = c["sha"], c["commit"]
                date = (commit.get("committer") or {}).get("date") or ""
                message = (commit.get("message") or "").splitlines()
            except (KeyError, TypeError, AttributeError) as e:
                raise CouldNotAsk(f"GitHub answered a commit this cannot read: {c!r:.200}") from e
            if sha == stop_at:
                return found
            found.append({"sha": sha, "date": date,
                          "message": message[0][:120] if message else ""})
        if len(batch) < 100:
            break
    raise CouldNotAsk(f"{stop_at[:10]} is not in the history of {pin['repo']}:{pin['path']} "
                      f"(looked through {len(found)} commits)")


def check(as_json: bool, get: Getter = _get) -> int:
    pin = _pin()
    try:
        new = commits_since(pin, get)
    except CouldNotAsk as e:
        print(f"could not check {pin['repo']}: {e}", file=sys.stderr)
        return 2
    head = new[0]["sha"] if new else pin["commit"]
    compare = f"https://github.com/{pin['repo']}/compare/{pin['commit']}...{head}"
    if as_json:
        print(json.dumps({"pinned": pin["commit"], "head": head, "behind": bool(new),
                          "commits": new, "compare": compare}))
    elif new:
        print(f"{len(new)} upstream commit(s) touch {pin['path']} since the reviewed "
              f"{pin['commit'][:10]}:")
        for c in new:
            print(f"  {c['sha'][:10]} {c['date'][:10]} {c['message']}")
        print(f"  {compare}\nReview them, then: scripts/hinge-pin.py --bump {head[:10]}")
    else:
        print(f"{pin['path']} has not changed upstream since the reviewed {pin['commit'][:10]}")
    return 1 if new else 0


def bump(commit: str | None, get: Getter = _get, today: str | None = None) -> int:
    pin = _pin()
    try:
        if commit is None:
            new = commits_since(pin, get)
            if not new:
                print(f"Nothing upstream since {pin['commit'][:10]}; the pin is unchanged.")
                return 0
            commit = new[0]["sha"]
        meta = get(f"{API}/repos/{pin['repo']}/commits/{urllib.parse.quote(commit)}")
        if not isinstance(meta, dict) or not isinstance(meta.get("sha"), str):
            raise CouldNotAsk(f"{commit} is not a commit of {pin['repo']}")
        # Only a commit in the path's own history can be checked from later:
        # pinning any other would make every --check after it exit 2.
        commits_since(pin, get, stop_at=meta["sha"])
    except CouldNotAsk as e:
        print(f"could not bump: {e}", file=sys.stderr)
        return 2
    updated = {**pin, "commit": meta["sha"],
               "reviewed": today or datetime.date.today().isoformat()}
    PIN_FILE.write_text(json.dumps(updated, indent=2) + "\n")
    print(f"hinge-control reviewed against {pin['repo']}@{meta['sha'][:10]} "
          f"(was {pin['commit'][:10]}). If anything was taken: scripts/hinge-selftest.sh, "
          "then a live test on an iPhone Duo.")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--bump", nargs="?", const="", metavar="COMMIT")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.check:
        return check(args.json)
    return bump(args.bump or None)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
