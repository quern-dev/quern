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
import pathlib
import subprocess
import sys
import time
from datetime import datetime


def detect_repo() -> str:
    """The `owner/name` this clone actually pushes to.

    Hardcoded as `jerimiah797/quern` until the repo moved to the `quern-dev`
    org. That kept working only because GitHub serves a permanent 301 from the
    old owner path -- so the scripts were reaching the right repo by redirect,
    not by knowing where it was. A repo later created at the old path would
    supersede the redirect and silently point these at somebody else's PRs.

    Reading the remote also makes a fork work without editing the script, which
    the hardcoded value never did.
    """
    try:
        url = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        sys.exit(f"cannot determine the repo: git remote get-url origin failed ({exc})")
    parsed = parse_remote(url)
    if parsed is None:
        # Never echo the URL: a remote can carry credentials in its userinfo
        # (https://user:token@host/...), and this runs in CI where stderr is
        # captured and kept.
        sys.exit(f"cannot determine the repo from the origin url ({redact(url)})")
    return parsed


def redact(url: str) -> str:
    """A remote URL safe to print: userinfo removed, host and path kept."""
    if "@" in url and "://" in url:
        scheme, _, rest = url.partition("://")
        return f"{scheme}://***@{rest.split('@', 1)[1]}"
    return url


def parse_remote(url: str) -> tuple[str, str] | None:
    """`(host, "owner/name")` from any spelling of a git remote, or None.

    The host is kept, not discarded. `gh --repo owner/name` resolves against the
    default host, so dropping it means a remote on a self-hosted forge selects a
    same-named repository on github.com instead -- which is the exact class of
    bug this whole change exists to remove, reintroduced one layer down.

    Covers scp-style (`git@host:owner/name.git`), https, ssh:// and git://.
    Returning None rather than a guess matters: a wrong answer here sends a
    merge at somebody else's repository.
    """
    url = url.strip()
    if not url:
        return None
    if url.endswith(".git"):
        url = url[:-4]

    host = ""
    # scp-style has no scheme and separates host from path with a colon.
    if "://" not in url and ":" in url:
        host, _, url = url.partition(":")
    else:
        for scheme in ("https://", "http://", "ssh://", "git://"):
            if url.startswith(scheme):
                url = url[len(scheme):]
                host, _, url = url.partition("/")
                break
        else:
            return None
    host = host.rsplit("@", 1)[-1]          # strip any user@ prefix
    parts = [seg for seg in url.strip("/").split("/") if seg]
    if not host or len(parts) < 2:
        return None
    return host, "/".join(parts[-2:])


# Detected once, at startup, and only when run as a script. Importing this
# module -- which the tests do, to exercise parse_remote -- must not shell out to
# git or sys.exit on a machine that has no origin remote.
HOST, REPO = detect_repo() if __name__ == "__main__" else ("unknown", "unknown/unknown")


def repo_arg() -> str:
    """What to pass to `gh --repo`. Host-qualified, which gh accepts for
    github.com too, so there is one form rather than a conditional."""
    return f"{HOST}/{REPO}"

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

# The head each PR was verified against, so a caller can bind its merge to the
# same commit this check looked at. Re-reading the head afterwards would let a
# push land in between and protect the new, unreviewed one instead.
_VERIFIED_HEAD: dict[int, str] = {}


class GhError(RuntimeError):
    """A gh invocation failed. Raised rather than returned, because the caller
    treating an empty result as data is precisely how this gate failed open:
    a failed query produced no commits, no commits produced a push time of 0,
    and any prior review then looked newer than the latest push."""


def gh(*args: str) -> str:
    # `gh api` resolves paths against whichever host is configured as default,
    # not against the one this clone points at, so the hostname travels with
    # every call rather than being assumed.
    if args and args[0] == "api" and "--hostname" not in args:
        args = ("api", "--hostname", HOST, *args[1:])
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
    raw = gh("pr", "list", "--repo", repo_arg(), "--state", "open", "--json", "number")
    return [p["number"] for p in json.loads(raw or "[]")]



# CodeRabbit's replies to "@coderabbitai review", observed. It answers whether
# the current head has been reviewed, which nothing else in the API does.
_ALREADY_REVIEWED = "already reviewed the last commit"
_FINISHED = "review finished"
_TRIGGERED = "review triggered"


def _ask_for_review(number: int) -> str:
    """Post the request and return GitHub's timestamp for it.

    Posted through the API rather than `gh pr comment` so the created_at comes
    back: anchoring the reply search on a local clock invites skew against
    GitHub's, and the anchor decides which replies count.
    """
    created = json.loads(gh("api", f"repos/{REPO}/issues/{number}/comments",
                            "-f", "body=@coderabbitai review"))
    return created.get("created_at", "")


def _reply_after(number: int, since: str) -> str:
    """CodeRabbit's newest reply to a review command, lowercased.

    Only replies to the review command count. Its summary comment also contains
    the word "action", so a looser match picks the summary up whenever one is
    newer than the completion reply — and then this waits out the full timeout
    while the answer sits one comment away.
    """
    args = ["api", "--paginate", "--slurp",
            f"repos/{REPO}/issues/{number}/comments"]
    if since:
        args += ["-X", "GET", "-f", f"since={since}"]
    pages = json.loads(gh(*args))

    best, body = since, ""
    for c in [c for page in pages for c in page]:
        user = c.get("user") or {}
        if user.get("id") != CODERABBIT_ID or user.get("login") != CODERABBIT_LOGIN:
            continue
        text = c.get("body", "")
        if "review command invocation" not in text.lower():
            continue  # a summary comment, not an answer to the request
        when = c.get("created_at", "")
        if when > best:
            best, body = when, text
    return body.lower()


# Two at most per merge attempt: one to trigger, one to confirm afterwards.
# Re-asking on every poll would stack requests against a review that is simply
# still running, and each one is a comment on the PR.
_MAX_ASKS = 2


def _reviewed_by_asking(number: int, timeout: float = 600.0) -> bool:
    """Ask whether the head commit is reviewed, and wait if a review starts.

    Returns True once CodeRabbit reports the current head as reviewed.
    """
    since = _ask_for_review(number)
    asks = 1
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        time.sleep(20)
        reply = _reply_after(number, since)
        if _ALREADY_REVIEWED in reply or _FINISHED in reply:
            return True
        if _TRIGGERED in reply and asks < _MAX_ASKS:
            # A review is running. It leaves a review object only if it finds
            # something, so the way to learn it finished is to ask again — once.
            time.sleep(60)
            since = _ask_for_review(number)
            asks += 1
    return False


def status(number: int, ask: bool = False) -> tuple[str, str]:
    """Returns (state, detail) where state is ok | pending | findings | unknown.

    Every failure path returns "unknown", never "ok". A gate that cannot see
    the data must refuse, not approve — rate limiting and network trouble are
    exactly when it matters, and both are common here.
    """
    try:
        return _status(number, ask)
    except (GhError, OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
        return "unknown", f"#{number} — could not determine review state: {exc}"


def _status(number: int, ask: bool = False) -> tuple[str, str]:
    raw = gh("pr", "view", str(number), "--repo", repo_arg(),
             "--json", "title,headRefName,headRefOid")
    pr = json.loads(raw)

    # GitHub does not register a push instantly, and this check tends to run
    # right after one. Reading commits inside that window returns the *previous*
    # head, whose review looks current — which merged an unreviewed commit six
    # seconds after it was pushed. Comparing the API's head against the local
    # ref makes the staleness visible instead of invisible.
    head = pr.get("headRefOid") or ""
    branch = pr.get("headRefName") or ""
    if not head or not branch:
        raise ValueError("the PR did not report a head ref")

    try:
        local = subprocess.run(["git", "rev-parse", f"origin/{branch}"],
                               capture_output=True, text=True)
    except OSError as exc:  # git missing, not executable, cwd gone
        raise ValueError(f"could not run git: {exc}") from exc
    local_sha = local.stdout.strip()
    if local.returncode != 0 or not local_sha:
        # Unresolvable rather than verified. Skipping the comparison here would
        # approve on the strength of a check that never ran — which is how the
        # rest of this file kept failing open. A fork PR lands here, correctly:
        # origin/<branch> does not exist locally, so freshness is unprovable.
        raise ValueError(
            f"could not resolve origin/{branch}; head freshness is unverifiable "
            f"(a fork PR needs its head repository fetched first)"
        )
    if local_sha != head:
        raise ValueError(
            f"the API still reports head {head[:8]} while origin/{branch} is "
            f"{local_sha[:8]} — it has not caught up with the push"
        )
    _VERIFIED_HEAD[number] = head

    commits = json.loads(gh("pr", "view", str(number), "--repo", repo_arg(), "--json", "commits"))
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
    ap.add_argument("--emit-head", metavar="FILE",
                    help="write the verified head SHA here, for a caller that "
                         "needs to bind a merge to the commit this check saw")
    ap.add_argument("--ask", action="store_true",
                    help="ask CodeRabbit whether the head is reviewed. Posts a "
                         "comment, so it is off by default: a status check "
                         "should not change the thing it reports on.")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--repo-slug", action="store_true",
                    help="print the detected owner/name and exit. merge-pr.sh "
                         "uses this so the gate and the merge cannot disagree "
                         "about which repository they are acting on.")
    args = ap.parse_args()

    if args.repo_slug:
        print(repo_arg())
        return 0

    numbers = args.prs or open_prs()
    if not numbers:
        print("  no open PRs")
        return 0

    deadline = time.monotonic() + args.timeout
    while True:
        print(f"— review status @ {datetime.now().strftime('%H:%M:%S')}")
        code = report(numbers, args.ask)
        if code == EXIT_OK:
            if args.emit_head and len(numbers) == 1:
                pathlib.Path(args.emit_head).write_text(_VERIFIED_HEAD.get(numbers[0], ""))
            return EXIT_OK
        # Only pending resolves by waiting. Findings need a human, and unknown
        # means the check itself could not see the data.
        if not args.wait or code != EXIT_PENDING or time.monotonic() > deadline:
            return code
        time.sleep(30)


if __name__ == "__main__":
    sys.exit(main())
