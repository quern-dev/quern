"""`cr-findings.sh` says whether the head commit has been reviewed, by commit.

It used to compare the newest review *with a body* against the newest commit's
date, which was wrong three ways (2026-09-28): a clean re-review creates no
review, so #331's reviewed head read STALE; `committedDate` is when a commit
was made, not pushed, so one made before a review of the previous head landed
read as covered; and a person's review counted as much as CodeRabbit's. It now
compares the coverage marker's commit with the head's.

`gh` is stubbed on `PATH`, and the stub runs the script's own `--jq` filter
through `jq`, so the filter that picks CodeRabbit's comments is under test too.
No network.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "cr-findings.sh"

HEAD = "e186adf2719bc8ba1cebff80678d2dd76c3f0f5e"
OLDER = "adeb1d12fe747e817769ada8855c2ec7821368b2"
CODERABBIT = {"id": 136622811, "login": "coderabbitai[bot]"}


def summary(covered: str | None = HEAD, kind: str = "reviewed", raw: str | None = None) -> str:
    """A CodeRabbit summary comment, as the walkthrough carries its marker."""
    if raw is None:
        raw = json.dumps({"sourceCommitId": covered, "coveredCommitId": covered, "kind": kind})
    marker = f"<!-- final_review_risk_coverage:{raw} -->" if covered or raw else ""
    return ("<!-- This is an auto-generated comment: summarize by coderabbit.ai -->\n"
            f"{marker}\n## Walkthrough\nSomething changed.\n")


GH_STUB = """#!/bin/bash
# `pr view --json headRefOid`: the head. `api .../issues/N/comments`: the
# comments, filtered by the caller's own --jq. Everything else is silent, so
# the other sections render empty.
jq_filter=""; want_head=""; comments=""
while [ $# -gt 0 ]; do
  case "$1" in
    --jq) jq_filter="$2"; shift;;
    headRefOid) want_head=1;;
    *issues/*comments*) comments=1;;
  esac
  shift
done
if [ -n "$want_head" ]; then printf '%s\\n' "$HEAD_SHA"; exit 0; fi
if [ -n "$comments" ] && [ -n "$jq_filter" ]; then
  printf '%s' "$COMMENTS_JSON" | jq -r "$jq_filter"
fi
exit 0
"""


@pytest.fixture
def run_script(tmp_path):
    if shutil.which("bash") is None or shutil.which("jq") is None:  # pragma: no cover
        pytest.skip("bash and jq are needed to run the script's own filters")
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "gh"
    stub.write_text(GH_STUB)
    stub.chmod(0o755)

    def run(comments: list[tuple[dict, str]], head: str = HEAD) -> str:
        env = dict(os.environ)
        env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"
        env["HEAD_SHA"] = head
        env["COMMENTS_JSON"] = json.dumps([{"user": u, "body": b} for u, b in comments])
        proc = subprocess.run(["bash", str(SCRIPT), "331"],
                              capture_output=True, text=True, env=env, timeout=60)
        assert proc.returncode == 0, proc.stderr[-2000:]
        return proc.stdout.split("── review coverage ──", 1)[1]

    return run


class TestReviewCoverage:
    def test_a_clean_rereview_of_the_head_is_reviewed(self, run_script):
        """#331: the head reviewed and nothing found, so no review with a body."""
        out = run_script([(CODERABBIT, summary(HEAD))])
        assert "REVIEWED: the head commit has been reviewed" in out
        assert "NOT" not in out

    def test_a_review_of_an_earlier_commit_is_not_confirmed(self, run_script):
        """Whatever the clocks say: a commit made before that review landed and
        pushed after it read as covered."""
        out = run_script([(CODERABBIT, summary(OLDER))])
        assert "NOT CONFIRMED: the last recorded review covered an earlier commit" in out
        assert "adeb1d12fe" in out and "e186adf271" in out

    def test_someone_elses_comment_carrying_the_marker_does_not_count(self, run_script):
        person = {"id": 1, "login": "coderabbitai[bot]"}      # right name, wrong account
        out = run_script([(person, summary(HEAD)), (CODERABBIT, summary(OLDER))])
        assert "NOT CONFIRMED" in out

    def test_no_summary_is_not_reviewed(self, run_script):
        out = run_script([(CODERABBIT, "Review rate limited.")])
        assert "NOT REVIEWED: CodeRabbit has posted no summary" in out

    def test_a_summary_without_a_marker_is_not_confirmed(self, run_script):
        """A rate-limited walkthrough: summarised, never reviewed."""
        out = run_script([(CODERABBIT, summary(None, raw=""))])
        assert "NOT CONFIRMED: no completed review is recorded" in out

    @pytest.mark.parametrize("raw", ["{not json", json.dumps(["a list"]),
                                     json.dumps({"coveredCommitId": HEAD, "kind": "in_progress"})])
    def test_a_marker_that_does_not_say_reviewed_is_not_trusted(self, run_script, raw):
        out = run_script([(CODERABBIT, summary(raw=raw))])
        assert "REVIEWED: the head" not in out and "NOT CONFIRMED" in out

    def test_an_unreadable_head_confirms_nothing(self, run_script):
        out = run_script([(CODERABBIT, summary(HEAD))], head="")
        assert "UNKNOWN: the head commit could not be read" in out
