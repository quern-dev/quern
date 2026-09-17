"""The review gate and CodeRabbit's rate limit.

Merging #202 sat silent for ten minutes, twice. Every `@coderabbitai review` the
gate posted was answered within five seconds with "Review rate limited", a
reply the gate did not recognise, so it went on polling for an answer it
already had and would then have reported only "still awaiting review".

The head it was waiting on had in fact never been reviewed -- CodeRabbit's
auto-resolve pass closed every thread anyway -- so the reason was the part that
mattered. See #207.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import time
import types

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "pr-review-status.py"

HEAD = "7e2d8b924d97cdeef9c2d3c67541d4e153124724"
PREV = "a9916276fe43a9cf5e64022734fa90389f7739d5"
BOT = {"id": 136622811, "login": "coderabbitai[bot]"}


def _load():
    spec = importlib.util.spec_from_file_location("pr_review_status_rl", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _summary(covered: str = PREV, limited_range: tuple[str, str] | None = None,
             wait: str = "25 minutes", updated: str = "2026-09-17T04:30:24Z") -> dict:
    """CodeRabbit's summary comment, shaped like #202's."""
    body = ["<!-- This is an auto-generated comment: summarize by coderabbit.ai -->"]
    if limited_range:
        a, b = limited_range
        body += [
            "<!-- This is an auto-generated comment: rate limited by coderabbit.ai -->",
            "> ## Review limit reached",
            f"> **Next included review available in {wait}.**",
            f"> Reviewing files that changed from the base of the PR and between {a} and {b}.",
            "<!-- end of auto-generated comment: rate limited by coderabbit.ai -->",
        ]
    body += [
        "<!-- walkthrough_start -->",
        "<!-- final_review_risk_coverage:"
        + json.dumps({"sourceCommitId": covered, "coveredCommitId": covered,
                      "kind": "reviewed"}, separators=(",", ":"))
        + " -->",
    ]
    return {"user": BOT, "body": "\n".join(body),
            "created_at": "2026-09-16T20:26:05Z", "updated_at": updated}


def _reply(text: str, at: str) -> dict:
    return {"user": BOT, "created_at": at,
            "body": f"<!-- CodeRabbit review command invocation: v2:x -->\n{text}"}


class Fake:
    """GitHub as the gate sees it: one PR, a summary, and replies to requests."""

    def __init__(self, summary: dict, answer: str | None = None):
        self.summary = summary
        self.answer = answer          # what CodeRabbit replies to each request
        self.comments: list[dict] = [summary]
        self.asks = 0

    def gh(self, *args: str) -> str:
        if args[:2] == ("pr", "view") and "title,headRefName,headRefOid" in args:
            return json.dumps({"title": "t", "headRefName": "b", "headRefOid": HEAD})
        if args[:2] == ("pr", "view") and "commits" in args:
            return json.dumps({"commits": [{"committedDate": "2026-09-17T04:10:00Z"}]})
        if args[0] == "api" and args[-1].endswith("/reviews"):
            return json.dumps([[{"user": BOT, "submitted_at": "2026-09-16T21:01:34Z"}]])
        if args[0] == "api" and "graphql" in args:
            return json.dumps({"data": {"repository": {"pullRequest": {
                "reviewThreads": {"nodes": []}}}}})
        if args[0] == "api" and "body=@coderabbitai review" in args:
            self.asks += 1
            # Compared as strings by the script, so keep the reply strictly later.
            at = f"2026-09-17T05:00:{2 * self.asks:02d}Z"
            if self.answer:
                replied = f"2026-09-17T05:00:{2 * self.asks + 1:02d}Z"
                self.comments.append(_reply(self.answer, replied))
            return json.dumps({"created_at": at})
        if args[0] == "api" and any(a.endswith("/comments") for a in args):
            return json.dumps([self.comments])
        raise AssertionError(f"unexpected gh call: {args}")


@pytest.fixture
def gate(monkeypatch):
    module = _load()
    real_run = subprocess.run

    def fake_run(cmd, *a, **k):
        if cmd[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=HEAD + "\n", stderr="")
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    # A clock that moves only when the script sleeps. Stubbing `sleep` alone
    # turns the bug under test -- polling to a 600s deadline -- into a real
    # 600s hang per test, which reads as a stuck suite rather than a failure.
    clock = {"now": 1_000.0}

    def sleep(seconds):
        clock["now"] += seconds

    monkeypatch.setattr(module, "time", types.SimpleNamespace(
        sleep=sleep, monotonic=lambda: clock["now"], time=time.time,
    ))
    return module


def _install(gate, monkeypatch, fake: Fake):
    monkeypatch.setattr(gate, "gh", fake.gh)
    return fake


class TestARateLimitedReplyIsAnAnswer:
    def test_it_stops_at_the_first_reply(self, gate, monkeypatch):
        """The bug: this polled for the whole timeout."""
        fake = _install(gate, monkeypatch, Fake(_summary(), answer="Review rate limited."))
        started = gate.time.monotonic()
        assert gate._reviewed_by_asking(202, timeout=600) == "rate_limited"
        assert gate.time.monotonic() - started < 60, "it waited out the timeout"
        assert fake.asks == 1

    def test_the_reason_reaches_the_caller(self, gate, monkeypatch):
        _install(gate, monkeypatch,
                 Fake(_summary(), answer="⚠️ Action not completed\nReview rate limited."))

        state, detail = gate.status(202, ask=True)

        assert state == "pending"
        assert "rate limited" in detail, detail
        # Nothing here says when it lifts, so no time may be printed: the
        # retry delay is ours, and reads as CodeRabbit's reset time.
        assert "until" not in detail, detail

    def test_a_time_is_shown_only_when_one_was_stated(self, gate, monkeypatch):
        """Repeated checks inside the backoff must not start inventing one."""
        _install(gate, monkeypatch, Fake(_summary(), answer="Review rate limited."))
        gate.status(202, ask=True)
        _, detail = gate.status(202, ask=True)
        assert "rate limited" in detail and "until" not in detail, detail

    def test_a_finished_review_is_still_recognised(self, gate, monkeypatch):
        _install(gate, monkeypatch, Fake(_summary(), answer="Review finished."))
        assert gate._reviewed_by_asking(202, timeout=600) == "reviewed"


class TestThePageIsReadBeforeAsking:
    """Asking posts a comment. When the summary already answers, it is noise."""

    def test_coverage_of_the_head_counts_as_reviewed(self, gate, monkeypatch):
        """The clean-review case: no review object, so only the marker shows it."""
        fake = _install(gate, monkeypatch, Fake(_summary(covered=HEAD)))
        state, _ = gate.status(202, ask=True)
        assert state == "ok"
        assert fake.asks == 0

    def test_coverage_of_an_older_commit_proves_nothing(self, gate, monkeypatch):
        """The marker lags a real review (#191, #194), so a mismatch must still
        be asked about rather than read as unreviewed."""
        fake = _install(gate, monkeypatch, Fake(_summary(covered=PREV), answer="Review finished."))
        state, _ = gate.status(202, ask=True)
        assert fake.asks == 1
        assert state == "ok"

    def test_coverage_that_is_not_a_review_does_not_count(self, gate, monkeypatch):
        summary = _summary(covered=HEAD)
        summary["body"] = summary["body"].replace('"kind":"reviewed"', '"kind":"skipped"')
        fake = _install(gate, monkeypatch, Fake(summary, answer="Review finished."))
        gate.status(202, ask=True)
        assert fake.asks == 1

    def test_a_limit_over_the_head_is_reported_without_asking(self, gate, monkeypatch):
        fake = _install(gate, monkeypatch,
                        Fake(_summary(limited_range=(PREV, HEAD), updated=_soon())))
        state, detail = gate.status(202, ask=True)
        assert state == "pending"
        assert "rate limited until" in detail, detail
        assert fake.asks == 0

    def test_a_limit_over_an_older_push_is_not_about_this_one(self, gate, monkeypatch):
        older = "1" * 40
        fake = _install(gate, monkeypatch, Fake(
            _summary(limited_range=(older, PREV), updated=_soon()), answer="Review finished."))
        state, _ = gate.status(202, ask=True)
        assert fake.asks == 1
        assert state == "ok"

    def test_read_only_mode_still_names_the_limit(self, gate, monkeypatch):
        fake = _install(gate, monkeypatch,
                        Fake(_summary(limited_range=(PREV, HEAD), updated=_soon())))
        state, detail = gate.status(202, ask=False)
        assert state == "pending" and "rate limited" in detail
        assert fake.asks == 0


class TestTheNoteIsTrueWhenRead:
    def test_a_limit_in_the_past_is_not_reported_as_current(self, gate, monkeypatch):
        """Found live: #161 read "rate limited until 19:19" at 22:10, about a
        limit from an earlier day."""
        _install(gate, monkeypatch, Fake(
            _summary(limited_range=(PREV, HEAD), updated="2026-01-01T00:00:00Z")))
        _, detail = gate.status(202, ask=False)
        assert "until" not in detail, detail
        assert "lifted" in detail, detail


class TestWaitingDoesNotBecomeACommentStorm:
    def test_repeated_checks_ask_once_while_limited(self, gate, monkeypatch):
        """`--wait` re-runs the check every 30s. Once the answer comes back
        promptly, every pass would post again unless the limit is remembered."""
        fake = _install(gate, monkeypatch, Fake(_summary(), answer="Review rate limited."))
        for _ in range(5):
            state, _ = gate.status(202, ask=True)
            assert state == "pending"
        assert fake.asks == 1

    def test_a_limit_that_has_lifted_is_asked_about_again(self, gate, monkeypatch):
        """A lifted limit does not review the queued push by itself; the stated
        time postpones the question, it does not replace it."""
        fake = _install(gate, monkeypatch, Fake(
            _summary(limited_range=(PREV, HEAD), updated="2026-01-01T00:00:00Z"),
            answer="Review finished."))
        state, _ = gate.status(202, ask=True)
        assert fake.asks == 1
        assert state == "ok"


class TestTheStatedWaitIsParsed:
    @pytest.mark.parametrize("text, seconds", [
        ("25 minutes", 1500),
        ("1 minute", 60),
        ("1 hour and 5 minutes", 3900),
        ("2 hours", 7200),
        ("30 seconds", 30),
        ("a while", None),
    ])
    def test_duration(self, gate, text, seconds):
        assert gate._duration(text) == seconds

    def test_the_time_is_counted_from_the_comment_update(self, gate, monkeypatch):
        _install(gate, monkeypatch, Fake(_summary(
            limited_range=(PREV, HEAD), updated="2026-09-17T04:30:24Z")))
        verdict, lifts = gate._summary_verdict(202, HEAD)
        assert verdict == "rate_limited"
        assert lifts == gate.ts("2026-09-17T04:30:24Z") + 1500


def _soon() -> str:
    """An update time recent enough that a 25-minute limit is still in force."""
    from datetime import UTC, datetime
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
