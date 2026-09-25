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

    def run() -> str:
        env = dict(os.environ)
        env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"
        env["OLDER_BODY"] = OLDER
        env["NEWER_BODY"] = NEWER
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
