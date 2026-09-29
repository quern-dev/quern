"""`pr-review-status.py` replayed against review states that actually happened.

Each file in `tests/fixtures/review-state/` is one recorded state: every `gh`
call the check made, with its real stdout, plus what was *truly* going on when
it was recorded. `scripts/capture-review-state.py` takes them.

They exist because this check has told us a PR was "reviewed, clean" on a
commit no review had read -- three times in one session, on two PRs -- and each
time the mistake was invisible: the PR page was green, CodeRabbit's own CI
check said `pass`, and the only way to find out was to ask CodeRabbit directly
and be told "Review rate limited". A check that fails open is worse than no
check, so the states it gets wrong are kept here as evidence rather than
described in a comment.

`expected_verdict` is what the check *should* say. Where that differs from
`verdict_when_captured`, the fixture is an open bug and its test xfails, so the
day someone fixes the check the xfail turns into a failure that says so.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = sorted((REPO_ROOT / "tests" / "fixtures" / "review-state").glob("*.json"))


def _load_check():
    """Import the check. Its filename is not importable as a module name."""
    path = REPO_ROOT / "scripts" / "pr-review-status.py"
    spec = importlib.util.spec_from_file_location("pr_review_status", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Detection is skipped on import, and every `gh --repo` would otherwise be
    # built from the "unknown/unknown" placeholder. Replay never reaches the
    # network, but the argv has to match what was recorded.
    module.HOST, module.REPO = "github.com", "quern-dev/quern"
    return module


def _replay(monkeypatch, record: dict):
    """Answer the check's calls from the recording, matched on argv.

    Matched rather than returned in order: a fix is free to stop making a call,
    or to make a new one, and a positional replay would answer the wrong
    question rather than say so. An unrecorded call fails the test with the
    argv, which is the signal that a fixture needs re-capturing.
    """
    check = _load_check()
    answers = {tuple(c["args"]): c["stdout"] for c in record["gh_calls"]}
    unmatched: list[tuple] = []

    def fake_gh(*args: str) -> str:
        if tuple(args) in answers:
            return answers[tuple(args)]
        unmatched.append(tuple(args))
        raise AssertionError(
            f"the check made a call this fixture does not have: {' '.join(args)}\n"
            f"re-capture it with scripts/capture-review-state.py"
        )

    git = {tuple(c["args"]): c for c in record["git_calls"]}

    class _Result:
        def __init__(self, entry):
            self.stdout = entry["stdout"]
            self.returncode = entry["returncode"]
            self.stderr = ""

    def fake_run(cmd, *a, **k):
        if isinstance(cmd, list) and tuple(cmd) in git:
            return _Result(git[tuple(cmd)])
        raise AssertionError(f"unrecorded subprocess call: {cmd}")

    monkeypatch.setattr(check, "gh", fake_gh)
    monkeypatch.setattr(subprocess, "run", fake_run)
    return check, unmatched


def _record(path: Path) -> dict:
    return json.loads(path.read_text())


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_the_check_reaches_the_right_verdict(path, monkeypatch, request):
    record = _record(path)
    expected = record["expected_verdict"]
    captured = record["verdict_when_captured"]
    if expected != captured:
        request.node.add_marker(pytest.mark.xfail(
            reason=(
                f"known bug: the check says {captured!r} for a state that is "
                f"{expected!r} — {record['expected_why']}. Ground truth: "
                f"{record['ground_truth']}"
            ),
            strict=True,
        ))

    check, _ = _replay(monkeypatch, record)
    verdict, line = check.status(record["pr"])

    assert verdict == expected, (
        f"{path.stem}: {record['expected_why']}\n"
        f"  ground truth: {record['ground_truth']}\n"
        f"  check said:   {line}"
    )


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_the_recording_still_answers_every_call_the_check_makes(path, monkeypatch):
    """A fixture that no longer covers the check is not evidence of anything.

    Separate from the verdict test because it fails for a different reason and
    has a different fix: this one means re-capture, that one means the check
    is wrong.
    """
    record = _record(path)
    check, unmatched = _replay(monkeypatch, record)
    try:
        check.status(record["pr"])
    except AssertionError:
        pass
    assert not unmatched, (
        f"{path.stem} does not cover: "
        + ", ".join(" ".join(a) for a in unmatched)
    )


def test_there_is_a_case_where_clean_is_true_and_one_where_it_is_not():
    """The pair is the point.

    A check that answered "pending" always would pass every bug fixture here
    and be useless. `merged-after-genuine-review` is the control: same verdict,
    opposite ground truth, and the only thing separating them in the recorded
    data is whether the newest CodeRabbit review with a *non-empty body* is
    newer than the head commit.
    """
    truths = {p.stem: _record(p)["expected_verdict"] for p in FIXTURES}
    assert "ok" in truths.values(), "no fixture where the PR really is clean"
    assert "pending" in truths.values(), "no fixture where a clean reading is wrong"
