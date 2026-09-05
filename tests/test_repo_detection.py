"""Which repository the review and merge scripts act on.

Both hardcoded `jerimiah797/quern`. That kept working after the repo moved to
the `quern-dev` org only because GitHub serves a permanent 301 from the old
owner path -- so the scripts were reaching the right repo by redirect rather
than by knowing where it was. A repo later created at `jerimiah797/quern` would
supersede that redirect, and `pr-review-status.py` would report on somebody
else's PRs while `merge-pr.sh` tried to merge into their repository.

Both now derive it from `git remote get-url origin`, and share one detection:
if the gate checked one repo and the merge ran against another, the check would
be meaningless in the most dangerous way available.

Returning None for anything unparseable is the load-bearing part -- a guess here
sends a merge somewhere nobody asked for.
"""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "pr-review-status.py"


def _load():
    spec = importlib.util.spec_from_file_location("pr_review_status", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script():
    return _load()


def test_importing_the_script_does_not_shell_out_to_git(script):
    """Detection runs only under __main__, so importing it on a machine with no
    origin remote must not exit."""
    assert script.REPO == "unknown/unknown"


@pytest.mark.parametrize(("url", "expected"), [
    ("git@github.com:quern-dev/quern.git", "quern-dev/quern"),
    ("git@github.com:quern-dev/quern", "quern-dev/quern"),
    ("https://github.com/quern-dev/quern.git", "quern-dev/quern"),
    ("https://github.com/quern-dev/quern", "quern-dev/quern"),
    ("ssh://git@github.com/quern-dev/quern.git", "quern-dev/quern"),
    ("git://github.com/quern-dev/quern.git", "quern-dev/quern"),
    # A fork must resolve to the fork, not to upstream -- that is the case the
    # hardcoded value could never serve.
    ("git@github.com:someone-else/quern.git", "someone-else/quern"),
    ("  https://github.com/quern-dev/quern.git  ", "quern-dev/quern"),
    # Self-hosted forge on a non-standard host.
    ("git@gitea.example.com:team/quern.git", "team/quern"),
])
def test_remote_urls_resolve_to_owner_and_name(script, url, expected):
    assert script.parse_remote(url) == expected


@pytest.mark.parametrize("url", [
    "", "   ", "not-a-url", "https://github.com/", "quern", "git@github.com:",
])
def test_unparseable_remotes_return_none_rather_than_guessing(script, url):
    assert script.parse_remote(url) is None


def test_detection_matches_this_clone(script):
    """The end-to-end check: whatever this clone pushes to is what the scripts
    will act on."""
    url = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        capture_output=True, text=True, timeout=10,
        cwd=_SCRIPT.parents[1],
    ).stdout.strip()
    assert script.parse_remote(url) == "quern-dev/quern"


def test_the_scripts_share_one_detection():
    """merge-pr.sh must not grow its own copy: a gate checking one repository
    while the merge runs against another is worse than no gate."""
    merge = (_SCRIPT.parent / "merge-pr.sh").read_text()
    assert "--repo-slug" in merge, "merge-pr.sh should ask the gate for the slug"
    assert 'gh pr merge "$PR" --repo "$REPO"' in merge


def test_no_hardcoded_owner_remains():
    for path in (_SCRIPT, _SCRIPT.parent / "merge-pr.sh"):
        for line in path.read_text().splitlines():
            if "jerimiah797" in line:
                # The docstring may explain the history; code may not.
                assert line.strip().startswith("#") or "Hardcoded as" in line, line
