"""`cr-findings.sh` must not pair one comment's tally with another's rows.

The pre-merge checks table is the third place CodeRabbit findings live, after
inline threads and review bodies, and `scripts/cr-findings.sh` reads it. The
first version of that reading took the tally from the newest comment and the
rows from *every* comment concatenated, so an older walkthrough's failures were
printed underneath a current, passing tally — reporting a failure that had
already been fixed, in the tool whose whole purpose is to stop findings being
misreported. Found on review of #310.

Only one comment per PR carries a table today, because CodeRabbit edits the
walkthrough in place rather than posting a new one, so no real PR reproduces
this. That is exactly why it wants a test: the guard would otherwise rest on a
condition nobody can currently observe, and would break silently the first time
a second walkthrough appeared.

`gh` is stubbed on `PATH`, so this reaches no network.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "cr-findings.sh"

# Two walkthroughs. The older one failed three checks; the newer passed all
# five. Marker-separated, which is the shape the script's own --jq produces.
OLDER = """\U0001f6a5 Pre-merge checks | ✅ 2 | ❌ 3

### ❌ Failed checks (3)

| Check name | Status | Explanation | Resolution |
| :---: | :--- | :--- | :--- |
| Out of Scope Changes check | ⚠️ Warning | STALE-ALREADY-FIXED | do a thing |
| Title check | ❓ Inconclusive | STALE-ALREADY-FIXED | do a thing |
| Docstring Coverage | ⚠️ Warning | 12.00% insufficient | write docstrings |
"""

NEWER = """\U0001f6a5 Pre-merge checks | ✅ 5

### ✅ Passed checks (5 passed)

| Check name | Status | Explanation |
| :---: | :--- | :--- |
| Title check | ✅ Passed | fine now |
"""

# A comment that *names* the checks without carrying a table. Selecting on the
# phrase rather than on a parseable tally made this an IndexError, which
# `set -euo pipefail` turned into a non-zero exit for the whole script.
PROSE_ONLY = """CodeRabbit here. I could not evaluate the Pre-merge checks for this run.

No table is included in this comment.
"""

# A custom check name with a digit and a hyphen, which a `[A-Za-z ]` name
# pattern silently dropped -- and the report still looked tidy, because
# Docstring Coverage parsed. CodeRabbit allows custom names up to 50 chars.
CUSTOM_NAME = """\U0001f6a5 Pre-merge checks | \u2705 3 | \u274c 2

### \u274c Failed checks (2)

| Check name | Status | Explanation | Resolution |
| :---: | :--- | :--- | :--- |
| CI-1 | \u26a0\ufe0f Warning | a custom gate failed | look at CI |
| Docstring Coverage | \u26a0\ufe0f Warning | 12.00% insufficient | write docstrings |
"""

# The walkthrough comment carries tables that are not the checks table. A
# cell-shaped pattern run over the whole comment reports their header rows as
# failing checks, which is the false positive that widening the name cell
# introduced before the region was scoped.
OTHER_TABLE = """\U0001f4dd Walkthrough

| Layer / File(s) | Summary |
| :--- | :--- |
| server/api | changed a thing |

\U0001f6a5 Pre-merge checks | \u2705 4 | \u274c 1

### \u274c Failed checks (1)

| Check name | Status | Explanation | Resolution |
| :---: | :--- | :--- | :--- |
| Docstring Coverage | \u26a0\ufe0f Warning | 12.00% insufficient | write docstrings |
"""

GH_STUB = """#!/bin/bash
# Only the issue-comments call returns anything; everything else is silent, so
# the threads and review-body sections render empty and this test is about the
# checks table alone.
for a in "$@"; do case "$a" in *issues/*comments*) COMMENTS=1;; esac; done
if [ -n "${COMMENTS:-}" ]; then
  printf '\\000CR-COMMENT\\000%s' "$OLDER_BODY"
  printf '\\000CR-COMMENT\\000%s' "$NEWER_BODY"
fi
exit 0
"""


@pytest.fixture
def run_script(tmp_path):
    """Run cr-findings.sh with `gh` stubbed to return two walkthrough comments."""
    if shutil.which("bash") is None:  # pragma: no cover - bash is assumed
        pytest.skip("bash not available")

    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "gh"
    stub.write_text(GH_STUB)
    stub.chmod(0o755)

    def run(older: str = OLDER, newer: str = NEWER) -> str:
        env = dict(os.environ)
        env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"
        env["OLDER_BODY"] = older
        env["NEWER_BODY"] = newer
        proc = subprocess.run(
            ["bash", str(SCRIPT), "999"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert proc.returncode == 0, proc.stderr[-2000:]
        return proc.stdout

    return run


class TestTheTallyAndItsRowsComeFromOneComment:
    def test_the_newest_tally_is_the_one_reported(self, run_script):
        assert "tally: ✅ 5" in run_script()

    def test_an_older_comments_failures_are_not_reported_under_it(self, run_script):
        """The defect. Asserting on the tally alone passes against it."""
        out = run_script()

        assert "STALE-ALREADY-FIXED" not in out, (
            "a resolved failure from an older walkthrough was reported beside "
            f"the current passing tally:\n{out}"
        )
        assert "Out of Scope Changes check" not in out, out
        assert "FAILED -- look at these" not in out, out

    def test_it_says_when_more_than_one_comment_has_a_table(self, run_script):
        """Silently picking one of several is the same class of problem; say so."""
        assert "2 comments carry a table" in run_script()

    def test_no_row_leaks_across_the_comment_boundary(self, run_script):
        """The pre-fix version emitted a row whose name was scraped out of the
        marker between two comments, which is how the bleed announced itself."""
        assert "CR-COMMENT" not in run_script()


class TestACommentThatNamesTheChecksWithoutCarryingATable:
    """Selecting comments on the phrase rather than on a parseable tally made
    this an `IndexError`, and `set -euo pipefail` turned that into a non-zero
    exit for the whole script -- so a PR could break the tool by mentioning
    the checks in prose."""

    def test_it_does_not_crash(self, run_script):
        out = run_script(older=PROSE_ONLY, newer=PROSE_ONLY)

        assert "Traceback" not in out, out
        assert "IndexError" not in out, out

    def test_it_says_the_phrase_appeared_without_a_table(self, run_script):
        """Distinguishing this from "never processed" is the point: one means
        read the page yourself, the other means there is nothing to read."""
        assert "no table does" in run_script(older=PROSE_ONLY, newer=PROSE_ONLY)

    def test_a_real_table_still_wins_over_prose(self, run_script):
        """The prose comment must not shadow a comment that does have one."""
        out = run_script(older=PROSE_ONLY, newer=NEWER)

        assert "tally: \u2705 5" in out
        assert "no table does" not in out


class TestTheCheckNameCellIsNotAssumedToBeLettersOnly:
    """Check names are user-defined, so a character class is the wrong tool.
    A `[A-Za-z ]` pattern dropped `CI-1` while Docstring Coverage still parsed,
    so the output looked orderly with a real failure missing from it."""

    def test_a_name_with_a_digit_and_hyphen_is_reported(self, run_script):
        out = run_script(older=CUSTOM_NAME, newer=CUSTOM_NAME)

        assert "CI-1" in out, f"a custom check name was dropped:\n{out}"
        assert "FAILED -- look at these" in out, out

    def test_it_is_not_filed_under_the_noise_heading(self, run_script):
        """Only Docstring Coverage is noise; a custom gate is not."""
        out = run_script(older=CUSTOM_NAME, newer=CUSTOM_NAME)
        failed_block = out.split("FAILED -- look at these")[1].split("known-noisy")[0]

        assert "CI-1" in failed_block, out


class TestTheParsedCountIsCheckedAgainstTheTally:
    """The general guard. Widening a pattern fixes the case you thought of;
    comparing parsed rows against the declared count makes the *next* gap loud
    instead of silent -- and it is what caught the false positive below."""

    def test_a_mismatch_is_reported(self, run_script):
        """Tally declares 2, only one row is present, so one was dropped."""
        one_row_short = CUSTOM_NAME.replace(
            "| CI-1 | \u26a0\ufe0f Warning | a custom gate failed | look at CI |\n", "",
        )

        out = run_script(older=one_row_short, newer=one_row_short)

        assert "WARNING: tally declares 2 failing but 1 row(s) parsed" in out, out

    def test_no_warning_when_they_agree(self, run_script):
        out = run_script(older=CUSTOM_NAME, newer=CUSTOM_NAME)

        assert "something was dropped" not in out, out


class TestOnlyTheChecksTableIsParsed:
    def test_another_tables_header_is_not_reported_as_a_check(self, run_script):
        """`Layer / File(s) | Summary` is the walkthrough's own table. Reporting
        its header as a failing check is the false positive that widening the
        name cell introduced."""
        out = run_script(older=OTHER_TABLE, newer=OTHER_TABLE)

        assert "Layer / File(s)" not in out, out
        assert "FAILED -- look at these" not in out, out
        assert "something was dropped" not in out, out
