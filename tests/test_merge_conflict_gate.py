"""merge-pr.sh must notice a conflicting branch before it asks for a review.

CodeRabbit allows one review an hour. The review gate in `merge-pr.sh` spends
that budget by asking for a review when the PR looks stale -- and a branch
that conflicts with the base cannot be merged afterwards no matter what the
review says. Asking first burns the hour on a merge that GitHub will refuse.

That is not hypothetical: on 2026-09-20 #250 went CONFLICTING the moment #247
merged, while a review request for it was already armed. It was caught by
hand with 25 minutes to spare.

These drive the real script with `gh` and `git` stubbed, and assert both that
it refuses and that it refused *cheaply* -- no review was requested.
"""

from __future__ import annotations

import pathlib
import stat
import subprocess

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "merge-pr.sh"


def _stub(path: pathlib.Path, name: str, body: str) -> None:
    f = path / name
    f.write_text("#!/usr/bin/env bash\n" + body)
    f.chmod(f.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def stubs(tmp_path):
    """A PATH where `gh` and `git` are recorded rather than real.

    `git` is stubbed so the test does not touch the network, and so
    `remote get-url` returns something the repo detection can parse.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"

    _stub(bin_dir, "git", f'''
echo "git $*" >> "{log}"
case "$1 $2" in
  "remote get-url") echo "https://github.com/quern-dev/quern.git" ;;
esac
exit 0
''')
    return bin_dir, log


def _run(script_cwd, bin_dir, mergeable, log, args=("999",)):
    _stub(bin_dir, "gh", f'''
echo "gh $*" >> "{log}"
for a in "$@"; do
  if [ "$a" = "mergeable" ] || [ "$a" = ".mergeable" ]; then
    echo "{mergeable}"; exit 0
  fi
done
echo "{mergeable}"
exit 0
''')
    return subprocess.run(
        ["bash", str(_SCRIPT), *args],
        cwd=script_cwd,
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HOME": str(bin_dir.parent),
            # The real gate waits 30s for GitHub to compute mergeability.
            # The test is about the decision, not the patience.
            "MERGE_PR_POLL_TRIES": "2",
            "MERGE_PR_POLL_SLEEP": "0",
        },
        capture_output=True, text=True, timeout=120,
    )


class TestAConflictingBranchIsRefusedBeforeAReviewIsSpent:
    def test_it_refuses(self, stubs, tmp_path):
        bin_dir, log = stubs
        result = _run(_SCRIPT.parent.parent, bin_dir, "CONFLICTING", log)

        assert result.returncode != 0
        assert "conflicts with the base branch" in result.stdout, result.stdout


class TestTheGateComesBeforeAnythingExpensive:
    """Refusing *after* spending the hour is no better than not refusing.

    Asserted structurally, on purpose. The behavioural version of this --
    stub everything, delete the gate, and check no review was requested --
    passes vacuously: with the gate gone the script dies in the review gate
    on stubbed input and never reaches the request either, so the test stays
    green while proving nothing. Measured, not assumed: that is exactly what
    three earlier tests here did.
    """

    def test_the_conflict_check_precedes_the_review_request(self):
        text = _SCRIPT.read_text()
        gate = text.index('case "$MERGEABLE" in')
        # The --ask invocation specifically. Plain `pr-review-status.py`
        # also appears earlier for --repo-slug, which is read-only and
        # legitimately runs first; matching that made this test fail on
        # correct code the first time it ran.
        ask = text.index("pr-review-status.py \"$PR\" --ask")

        assert gate < ask, (
            "the conflict gate must run before the review gate; after it, a "
            "conflicting PR still costs an hour of review budget"
        )

    def test_the_conflict_check_precedes_the_merge(self):
        text = _SCRIPT.read_text()
        gate = text.index('case "$MERGEABLE" in')
        merge = text.index("gh pr merge")

        assert gate < merge


class TestAnUndeterminedStateIsNotTreatedAsFine:
    def test_unknown_is_refused(self, stubs, tmp_path):
        """GitHub computes mergeability asynchronously, so a PR pushed seconds
        ago reads UNKNOWN. Treating that as mergeable would fail in exactly
        the case this gate exists for -- right after a push."""
        bin_dir, log = stubs
        result = _run(_SCRIPT.parent.parent, bin_dir, "UNKNOWN", log)

        assert result.returncode != 0
        assert "has not reported whether it merges cleanly" in result.stdout, result.stdout


class TestForceDoesNotOverrideAConflict:
    def test_force_still_refuses(self, stubs, tmp_path):
        """--force means "skip the gates I chose to put here", not "merge
        something GitHub will reject anyway"."""
        bin_dir, log = stubs
        result = _run(_SCRIPT.parent.parent, bin_dir, "CONFLICTING", log,
                      args=("999", "--force"))

        assert result.returncode != 0
        assert "conflicts with the base branch" in result.stdout, result.stdout
