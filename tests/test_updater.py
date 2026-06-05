"""Tests for server.lifecycle.updater — `quern update` flow.

Focuses on the branch-vs-release semantics fixed in #40: the check
should always compare against ``origin/<RELEASE_BRANCH>``, and the pull
step must be skipped when the user isn't actually on that branch.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _make_run(returncode: int = 0, stdout: str = "", stderr: str = ""):
    """Build a MagicMock subprocess CompletedProcess-ish object."""
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


def _make_subprocess_dispatcher(responses: dict):
    """Build a side_effect that dispatches on the command tuple.

    ``responses`` is keyed by the tuple of command tokens. Defaults are
    provided for the common quern.dev / git ops; tests override only the
    interesting ones.
    """
    def fake_run(cmd, *args, **kwargs):
        key = tuple(cmd)
        for prefix, response in responses.items():
            if key[: len(prefix)] == prefix:
                return response
        # Default to a successful no-op so unmocked git invocations
        # don't crash the test — they'd surface as the wrong answer
        # rather than an exception, which is what we want when
        # diagnosing a missed mock.
        return _make_run(returncode=0, stdout="")
    return fake_run


# ---------------------------------------------------------------------------
# _check_via_git — compares against origin/<RELEASE_BRANCH>, not the
# current branch's upstream
# ---------------------------------------------------------------------------


def test_check_via_git_compares_to_release_branch_not_current_branch():
    """Bug #40: on a non-main branch the check used to look at
    `origin/<current-branch>` and miss new commits on main."""
    from server.lifecycle import updater

    responses = {
        ("git", "fetch", "origin"): _make_run(0),
        ("git", "rev-parse", "--abbrev-ref", "HEAD"): _make_run(
            0, stdout="feat/something\n",
        ),
        ("git", "rev-list", "HEAD..origin/main", "--count"): _make_run(
            0, stdout="3\n",
        ),
    }
    with patch(
        "server.lifecycle.updater.subprocess.run",
        side_effect=_make_subprocess_dispatcher(responses),
    ) as run_mock:
        result = updater._check_via_git(Path("/fake"))

    assert result == (True, "feat/something", 3)
    # Crucial assertion: the rev-list command must reference origin/main,
    # not origin/feat/something.
    rev_list_calls = [
        c for c in run_mock.call_args_list
        if c.args[0][:2] == ["git", "rev-list"]
    ]
    assert len(rev_list_calls) == 1
    assert rev_list_calls[0].args[0] == [
        "git", "rev-list", "HEAD..origin/main", "--count",
    ]


def test_check_via_git_returns_zero_when_no_new_commits_on_release_branch():
    from server.lifecycle import updater

    responses = {
        ("git", "fetch", "origin"): _make_run(0),
        ("git", "rev-parse", "--abbrev-ref", "HEAD"): _make_run(
            0, stdout="main\n",
        ),
        ("git", "rev-list", "HEAD..origin/main", "--count"): _make_run(
            0, stdout="0\n",
        ),
    }
    with patch(
        "server.lifecycle.updater.subprocess.run",
        side_effect=_make_subprocess_dispatcher(responses),
    ):
        result = updater._check_via_git(Path("/fake"))

    assert result == (False, "main", 0)


def test_check_via_git_returns_none_on_fetch_failure():
    from server.lifecycle import updater

    responses = {
        ("git", "fetch", "origin"): _make_run(
            1, stderr="fatal: unable to access ...\n",
        ),
    }
    with patch(
        "server.lifecycle.updater.subprocess.run",
        side_effect=_make_subprocess_dispatcher(responses),
    ):
        assert updater._check_via_git(Path("/fake")) is None


# ---------------------------------------------------------------------------
# _update_via_git — non-release-branch handling per approach (A) of #40
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_quern_dev_no_signal(monkeypatch):
    """Make _check_via_quern_dev return None ("don't know") so the test
    falls through to the git-side logic we actually want to exercise."""
    monkeypatch.setattr(
        "server.lifecycle.updater._check_via_quern_dev", lambda sha: None,
    )


def test_update_via_git_on_feature_branch_with_updates_warns_and_skips_pull(
    capsys, stub_quern_dev_no_signal,
):
    """The key #40 scenario: user is on a feature branch, main has new
    commits. We must tell the user what's available without pulling
    (because `git pull --ff-only` would operate on the wrong upstream),
    and skip the rebuild (rc=2)."""
    from server.lifecycle import updater

    responses = {
        ("git", "rev-parse", "HEAD"): _make_run(0, stdout="deadbeef\n"),
        ("git", "fetch", "origin"): _make_run(0),
        ("git", "rev-parse", "--abbrev-ref", "HEAD"): _make_run(
            0, stdout="feat/foo\n",
        ),
        ("git", "rev-list", "HEAD..origin/main", "--count"): _make_run(
            0, stdout="3\n",
        ),
    }
    with patch(
        "server.lifecycle.updater.subprocess.run",
        side_effect=_make_subprocess_dispatcher(responses),
    ) as run_mock:
        rc = updater._update_via_git(Path("/fake"))

    assert rc == 2  # Skip rebuild
    captured = capsys.readouterr()
    assert "feat/foo" in captured.out
    assert "`origin/main` is 3 commits ahead" in captured.out
    assert "git checkout main" in captured.out

    # Critically: `git pull` must NOT have been invoked.
    pull_calls = [
        c for c in run_mock.call_args_list
        if c.args[0][:2] == ["git", "pull"]
    ]
    assert pull_calls == []


def test_update_via_git_on_feature_branch_with_no_updates_notes_and_skips(
    capsys, stub_quern_dev_no_signal,
):
    """When the feature branch is in sync with main, we should still
    point out that the user is on a non-release branch — silent success
    is misleading because the check semantics aren't obvious."""
    from server.lifecycle import updater

    responses = {
        ("git", "rev-parse", "HEAD"): _make_run(0, stdout="deadbeef\n"),
        ("git", "fetch", "origin"): _make_run(0),
        ("git", "rev-parse", "--abbrev-ref", "HEAD"): _make_run(
            0, stdout="feat/foo\n",
        ),
        ("git", "rev-list", "HEAD..origin/main", "--count"): _make_run(
            0, stdout="0\n",
        ),
    }
    with patch(
        "server.lifecycle.updater.subprocess.run",
        side_effect=_make_subprocess_dispatcher(responses),
    ):
        rc = updater._update_via_git(Path("/fake"))

    assert rc == 2
    captured = capsys.readouterr()
    assert "feat/foo" in captured.out
    assert "release branch `main`" in captured.out
    assert "No new commits" in captured.out


def test_update_via_git_on_main_with_updates_still_pulls(
    capsys, stub_quern_dev_no_signal, monkeypatch,
):
    """Regression guard: the happy path (on main, behind, pull-and-rebuild)
    must still work after the #40 rewrite."""
    from server.lifecycle import updater

    monkeypatch.setattr(updater, "_read_local_version", lambda root: "0.13.5")

    responses = {
        ("git", "rev-parse", "HEAD"): _make_run(0, stdout="deadbeef\n"),
        ("git", "fetch", "origin"): _make_run(0),
        ("git", "rev-parse", "--abbrev-ref", "HEAD"): _make_run(
            0, stdout="main\n",
        ),
        ("git", "rev-list", "HEAD..origin/main", "--count"): _make_run(
            0, stdout="2\n",
        ),
        ("git", "pull", "--ff-only"): _make_run(0, stdout="Updating ...\n"),
    }
    with patch(
        "server.lifecycle.updater.subprocess.run",
        side_effect=_make_subprocess_dispatcher(responses),
    ) as run_mock:
        rc = updater._update_via_git(Path("/fake"))

    assert rc == 0  # Will trigger rebuild
    pull_calls = [
        c for c in run_mock.call_args_list
        if c.args[0][:2] == ["git", "pull"]
    ]
    assert len(pull_calls) == 1
    captured = capsys.readouterr()
    assert "0.13.5" in captured.out
