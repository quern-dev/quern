"""A CodeRabbit refusal no longer means no review is running.

With usage-based reviews enabled the "rate limited" banner tracks the
*included* allowance only, so a paid review can be under way while the banner
still quotes a wait. Measured 2026-09-29: the coverage marker moved about six
minutes after the ask while the monitor still reported 29 minutes.

The check used to return the moment it saw a refusal, abandoning a review that
was already running -- and the caller then backed off for a window that had
nothing to do with it, which on a merge gate reads as "not reviewed yet" for a
head that is about to be reviewed.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HEAD = "a" * 40


@pytest.fixture
def check(monkeypatch):
    path = REPO_ROOT / "scripts" / "pr-review-status.py"
    spec = importlib.util.spec_from_file_location("pr_review_status", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setenv("QUERN_PR_REPO", "quern-dev/quern")
    spec.loader.exec_module(module)
    # A fake clock, so the deadline is reached by *iterations* rather than by
    # elapsed time. With `sleep` merely no-op'd the two non-returning cases
    # below spin on real `monotonic` for the whole timeout -- 300s of busy
    # loop each, which is a hang rather than a test. Advancing the clock from
    # inside `sleep` makes every path terminate deterministically and fast.
    clock = {"t": 0.0}
    monkeypatch.setattr(module.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    monkeypatch.setattr(module.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(module, "_ask_for_review", lambda _n: 0.0)
    return module


def _drive(check, monkeypatch, replies, verdicts):
    """Run one `_reviewed_by_asking` against scripted poll results."""
    seen = {"replies": list(replies), "verdicts": list(verdicts)}

    def next_reply(_number, _since):
        return seen["replies"].pop(0) if seen["replies"] else ""

    def next_verdict(_number, _head):
        v = seen["verdicts"].pop(0) if seen["verdicts"] else "unknown"
        return v, None

    monkeypatch.setattr(check, "_reply_after", next_reply)
    monkeypatch.setattr(check, "_summary_verdict", next_verdict)
    return check._reviewed_by_asking(1, HEAD, timeout=80.0)


def test_a_refusal_followed_by_the_marker_moving_is_reviewed(check, monkeypatch):
    """The regression. The banner says rate limited; the review runs anyway."""
    outcome = _drive(
        check, monkeypatch,
        replies=[check._RATE_LIMITED, "", ""],
        verdicts=["unknown", "unknown", "reviewed"],
    )
    assert outcome == "reviewed"


def test_a_refusal_with_the_marker_never_moving_is_still_rate_limited(check, monkeypatch):
    """The other direction, so the fix cannot just always say 'reviewed'."""
    outcome = _drive(
        check, monkeypatch,
        replies=[check._RATE_LIMITED] * 3,
        verdicts=["unknown"] * 3,
    )
    assert outcome == "rate_limited"


def test_silence_without_a_refusal_is_a_timeout_not_a_refusal(check, monkeypatch):
    """A quiet PR must not be reported as rate limited; the two are different
    answers and the caller backs off differently for each."""
    outcome = _drive(
        check, monkeypatch, replies=["", "", ""], verdicts=["unknown"] * 3,
    )
    assert outcome == "timeout"


def test_the_marker_is_believed_without_any_reply_at_all(check, monkeypatch):
    """A clean review posts no body, so the reply can stay empty forever."""
    outcome = _drive(
        check, monkeypatch, replies=["", ""], verdicts=["unknown", "reviewed"],
    )
    assert outcome == "reviewed"
