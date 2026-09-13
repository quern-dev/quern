"""Tests for server.lifecycle.updater — `quern update` flow.

Focuses on the branch-vs-release semantics fixed in #40: the check
should always compare against ``origin/<RELEASE_BRANCH>``, and the pull
step must be skipped when the user isn't actually on that branch.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def pinned_channel(monkeypatch):
    """Keep these tests off whatever channel the developer happens to be on.

    `_get_release_branch()` reads `update_channel` from `~/.quern/config.json`,
    so without this the command tables below -- which are keyed on the literal
    `origin/release/stable` -- miss every entry once someone has run
    `quern set-channel beta`, and the result of the suite depends on who runs
    it (#115). The dispatcher defaults a missed key to a successful no-op, by
    design, so the symptom is a wrong answer rather than an error, and it
    points at the updater rather than at the machine.

    Note this tracks the *setting*, not the installed version: the channel is
    written by `set-channel` before any beta code exists, so the tests can
    break on a checkout that has not moved.

    The two tests that are about the mapping itself override this.
    """
    monkeypatch.setattr("server.config.get_update_channel", lambda: "stable")


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
# Channel → release branch mapping
# ---------------------------------------------------------------------------


def test_get_release_branch_defaults_to_stable(monkeypatch):
    from server.lifecycle import updater
    monkeypatch.setattr("server.config.get_update_channel", lambda: "stable")
    assert updater._get_release_branch() == "release/stable"


def test_get_release_branch_respects_beta_channel(monkeypatch):
    from server.lifecycle import updater
    monkeypatch.setattr("server.config.get_update_channel", lambda: "beta")
    assert updater._get_release_branch() == "release/beta"


# ---------------------------------------------------------------------------
# _check_via_git — compares against origin/<release_branch>, not the
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
        ("git", "rev-list", "HEAD..origin/release/stable", "--count"): _make_run(
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
        "git", "rev-list", "HEAD..origin/release/stable", "--count",
    ]


def test_check_via_git_follows_the_configured_channel(monkeypatch):
    """The beta channel had no coverage at all: every table was keyed on
    `release/stable`, so the one behaviour the channel setting controls went
    untested while quietly breaking the suite for anyone who had opted in."""
    from server.lifecycle import updater

    monkeypatch.setattr("server.config.get_update_channel", lambda: "beta")
    responses = {
        ("git", "fetch", "origin"): _make_run(0),
        ("git", "rev-parse", "--abbrev-ref", "HEAD"): _make_run(
            0, stdout="release/beta\n",
        ),
        ("git", "rev-list", "HEAD..origin/release/beta", "--count"): _make_run(
            0, stdout="4\n",
        ),
    }
    with patch(
        "server.lifecycle.updater.subprocess.run",
        side_effect=_make_subprocess_dispatcher(responses),
    ) as run_mock:
        result = updater._check_via_git(Path("/fake"))

    assert result == (True, "release/beta", 4)
    rev_list_calls = [
        c for c in run_mock.call_args_list
        if c.args[0][:2] == ["git", "rev-list"]
    ]
    assert rev_list_calls[0].args[0] == [
        "git", "rev-list", "HEAD..origin/release/beta", "--count",
    ]


def test_check_via_git_returns_zero_when_no_new_commits_on_release_branch():
    from server.lifecycle import updater

    responses = {
        ("git", "fetch", "origin"): _make_run(0),
        ("git", "rev-parse", "--abbrev-ref", "HEAD"): _make_run(
            0, stdout="main\n",
        ),
        ("git", "rev-list", "HEAD..origin/release/stable", "--count"): _make_run(
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
        ("git", "rev-list", "HEAD..origin/release/stable", "--count"): _make_run(
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
    assert "`origin/release/stable` is 3 commits ahead" in captured.out
    assert "git checkout release/stable" in captured.out

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
        ("git", "rev-list", "HEAD..origin/release/stable", "--count"): _make_run(
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
    assert "release branch `release/stable`" in captured.out
    assert "No new commits" in captured.out


# ---------------------------------------------------------------------------
# Tarball updater — channel-aware release selection
# ---------------------------------------------------------------------------


class _FakeUrlResp:
    """Minimal context-manager wrapper for the urlopen patch."""
    def __init__(self, payload):
        self._payload = payload
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self):
        import json as _json
        return _json.dumps(self._payload).encode()


def test_fetch_latest_release_stable_hits_releases_latest(monkeypatch):
    """Stable channel must hit /releases/latest — GitHub's stable-only
    feed. The previous unconditional behavior is unchanged."""
    from server.lifecycle import updater

    captured_urls: list[str] = []

    def fake_urlopen(req, **kwargs):
        captured_urls.append(req.full_url)
        return _FakeUrlResp({
            "tag_name": "v0.13.4",
            "tarball_url": "https://example.com/v0.13.4.tar.gz",
            "prerelease": False,
        })

    monkeypatch.setattr(
        updater.urllib.request, "urlopen", fake_urlopen,
    )

    result = updater._fetch_latest_release("stable")
    assert result == ("0.13.4", "https://example.com/v0.13.4.tar.gz")
    assert captured_urls == [
        "https://api.github.com/repos/quern-dev/quern/releases/latest",
    ]


def test_fetch_latest_release_beta_picks_first_prerelease(monkeypatch):
    """Beta channel must scan /releases and pick the first prerelease.
    Stable entries newer than the topmost prerelease are deliberately
    NOT chosen — that's how beta diverges from stable."""
    from server.lifecycle import updater

    def fake_urlopen(req, **kwargs):
        return _FakeUrlResp([
            {"tag_name": "v0.13.5", "tarball_url": "stable.tgz", "prerelease": False},
            {"tag_name": "v0.13.6-beta.1", "tarball_url": "beta1.tgz", "prerelease": True},
            {"tag_name": "v0.13.4", "tarball_url": "stable-older.tgz", "prerelease": False},
        ])

    monkeypatch.setattr(
        updater.urllib.request, "urlopen", fake_urlopen,
    )

    result = updater._fetch_latest_release("beta")
    assert result == ("0.13.6-beta.1", "beta1.tgz")


def test_fetch_latest_release_beta_falls_back_to_stable_when_no_prereleases(
    monkeypatch,
):
    """If no prerelease exists yet, beta users must NOT regress to nothing.
    Fall through to the latest stable so they never see older content than
    a stable user."""
    from server.lifecycle import updater

    def fake_urlopen(req, **kwargs):
        return _FakeUrlResp([
            {"tag_name": "v0.13.5", "tarball_url": "stable.tgz", "prerelease": False},
            {"tag_name": "v0.13.4", "tarball_url": "older.tgz", "prerelease": False},
        ])

    monkeypatch.setattr(
        updater.urllib.request, "urlopen", fake_urlopen,
    )

    result = updater._fetch_latest_release("beta")
    assert result == ("0.13.5", "stable.tgz")


def test_fetch_latest_release_beta_skips_draft_prereleases(monkeypatch):
    """Drafts are not user-visible releases — beta channel must ignore
    them even when they're flagged prerelease."""
    from server.lifecycle import updater

    def fake_urlopen(req, **kwargs):
        return _FakeUrlResp([
            {
                "tag_name": "v0.13.6-beta.2",
                "tarball_url": "draft.tgz",
                "prerelease": True,
                "draft": True,
            },
            {
                "tag_name": "v0.13.6-beta.1",
                "tarball_url": "published.tgz",
                "prerelease": True,
                "draft": False,
            },
        ])

    monkeypatch.setattr(
        updater.urllib.request, "urlopen", fake_urlopen,
    )

    result = updater._fetch_latest_release("beta")
    assert result == ("0.13.6-beta.1", "published.tgz")


def test_update_via_git_on_release_branch_with_updates_still_pulls(
    capsys, stub_quern_dev_no_signal, monkeypatch,
):
    """Regression guard: the happy path (on the configured release
    branch, behind, pull-and-rebuild) must still work after the
    channels rewrite."""
    from server.lifecycle import updater

    monkeypatch.setattr(updater, "_read_local_version", lambda root: "0.13.5")

    responses = {
        ("git", "rev-parse", "HEAD"): _make_run(0, stdout="deadbeef\n"),
        ("git", "fetch", "origin"): _make_run(0),
        ("git", "rev-parse", "--abbrev-ref", "HEAD"): _make_run(
            0, stdout="release/stable\n",
        ),
        ("git", "rev-list", "HEAD..origin/release/stable", "--count"): _make_run(
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


# ---------------------------------------------------------------------------
# Release asset selection — the tarball must belong to the tag being installed
# ---------------------------------------------------------------------------


def test_asset_must_match_the_release_version():
    """A release can carry more than one ``quern-*.tar.gz``: a stale upload, a
    hand-built archive, a re-cut asset left behind. Taking the first match
    installs whichever GitHub happened to list first while the caller reports
    the current tag as the version -- an install that lies about what it is."""
    from server.lifecycle.updater import _select_asset_url

    assets = [
        {"name": "quern-0.14.0.tar.gz", "browser_download_url": "https://x/old"},
        {"name": "quern-0.15.0.tar.gz", "browser_download_url": "https://x/new"},
    ]

    assert _select_asset_url(assets, "0.15.0") == "https://x/new"
    assert _select_asset_url(assets, "0.14.0") == "https://x/old"


def test_asset_absent_falls_back_rather_than_guessing():
    """No asset for this tag means None, so the caller uses GitHub's generated
    source tarball. Releases cut before the asset existed still update."""
    from server.lifecycle.updater import _select_asset_url

    assets = [{"name": "quern-0.14.0.tar.gz", "browser_download_url": "https://x/old"}]

    assert _select_asset_url(assets, "0.15.0") is None
    assert _select_asset_url([], "0.15.0") is None
    assert _select_asset_url(assets, "") is None


def test_asset_ignores_near_miss_names():
    """Prefix matching also accepted names that merely start with the version,
    so quern-0.15.0-rc1 could be served to someone installing 0.15.0."""
    from server.lifecycle.updater import _select_asset_url

    assets = [
        {"name": "quern-0.15.0-rc1.tar.gz", "browser_download_url": "https://x/rc"},
        {"name": "quern-0.15.0.tar.gz.sha256", "browser_download_url": "https://x/sum"},
    ]

    assert _select_asset_url(assets, "0.15.0") is None


class TestAskingForAPassword:
    """`_can_ask_for_a_password`, including the half that had no coverage.

    Every existing test reached it with `isatty()` true or replaced the function
    wholesale, so the /dev/tty fallback was never exercised -- and that fallback
    *is* the menu-bar path: a GUI-launched process has no controlling terminal.
    The case the feature was written for was the untested one.
    """

    def test_a_terminal_on_stdin_is_enough(self, monkeypatch):
        from server.lifecycle import updater
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        assert updater._can_ask_for_a_password() is True

    def test_a_controlling_terminal_counts_even_without_one_on_stdin(
        self, monkeypatch
    ):
        # `quern update --tools | tee log` has no tty on stdin while the user
        # sits right in front of one, so stdin alone would refuse to prompt
        # someone who could answer.
        from server.lifecycle import updater
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        opened = []

        def fake_open(path, *args, **kwargs):
            opened.append(path)
            import io as _io
            return _io.StringIO()

        monkeypatch.setattr("builtins.open", fake_open)
        assert updater._can_ask_for_a_password() is True
        assert "/dev/tty" in opened

    def test_no_terminal_anywhere_means_do_not_prompt(self, monkeypatch):
        # The menu-bar case. sudo with nowhere to prompt either hangs or fails
        # with a message about a terminal, and neither tells the reader what to
        # do about it.
        from server.lifecycle import updater
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)

        def no_tty(path, *args, **kwargs):
            raise OSError(6, "Device not configured")

        monkeypatch.setattr("builtins.open", no_tty)
        assert updater._can_ask_for_a_password() is False


class TestTheUpdateRecord:
    """`quern update` writes down what it did, because the exit code cannot.

    "Already up to date" has to exit 0 -- the same as a real update -- or every
    script treating nonzero as failure breaks. So a caller seeing 0 could not
    tell an update from a no-op, and the menu bar, assuming it had updated,
    polled thirty seconds for a version that was never going to move and then
    announced that an update had finished.
    """

    @pytest.fixture
    def sandbox(self, tmp_path, monkeypatch):
        from server.lifecycle import updater
        monkeypatch.setattr(updater, "RESULT_FILE", tmp_path / "last-update.json")
        return updater

    def _read(self, updater):
        return json.loads(updater.RESULT_FILE.read_text())

    def test_nothing_to_do_is_recorded_as_such(self, sandbox, monkeypatch):
        updater = sandbox
        monkeypatch.setattr(updater, "_find_project_root", lambda: Path("/x"))
        monkeypatch.setattr(updater, "_is_git_install", lambda _r: True)
        monkeypatch.setattr(updater, "_update_via_git", lambda _r: 2)
        monkeypatch.setattr(updater, "_report_tool_updates", lambda _a: True)
        monkeypatch.setattr(updater, "_installed_version", lambda: "0.16.1")

        assert updater.run_update() == 0, "a no-op must still exit 0"
        assert self._read(updater)["outcome"] == updater.NO_OP

    def test_a_real_update_is_recorded_as_an_update(self, sandbox, monkeypatch):
        updater = sandbox
        monkeypatch.setattr(updater, "_find_project_root", lambda: Path("/x"))
        monkeypatch.setattr(updater, "_is_git_install", lambda _r: True)
        monkeypatch.setattr(updater, "_update_via_git", lambda _r: 0)
        monkeypatch.setattr(updater, "_rebuild_and_restart", lambda _r: [])
        monkeypatch.setattr(updater, "_report_tool_updates", lambda _a: True)
        monkeypatch.setattr(updater, "_installed_version", lambda: "0.17.0")

        assert updater.run_update() == 0
        record = self._read(updater)
        assert record["outcome"] == updater.UPDATED
        assert record["version"] == "0.17.0"

    def test_a_failure_is_recorded_as_a_failure(self, sandbox, monkeypatch):
        updater = sandbox
        monkeypatch.setattr(updater, "_find_project_root", lambda: Path("/x"))
        monkeypatch.setattr(updater, "_is_git_install", lambda _r: True)
        monkeypatch.setattr(updater, "_update_via_git", lambda _r: 1)

        assert updater.run_update() == 1
        assert self._read(updater)["outcome"] == updater.FAILED

    def test_a_partial_rebuild_is_a_failure_not_an_update(self, sandbox, monkeypatch):
        # The source moved but part of the rebuild did not. Recording this as
        # "updated" would have the menu bar relaunch into an install that is
        # half-built.
        updater = sandbox
        monkeypatch.setattr(updater, "_find_project_root", lambda: Path("/x"))
        monkeypatch.setattr(updater, "_is_git_install", lambda _r: True)
        monkeypatch.setattr(updater, "_update_via_git", lambda _r: 0)
        monkeypatch.setattr(updater, "_rebuild_and_restart", lambda _r: ["restart"])
        monkeypatch.setattr(updater, "_report_tool_updates", lambda _a: True)
        monkeypatch.setattr(updater, "_installed_version", lambda: "0.17.0")

        assert updater.run_update() == 1
        assert self._read(updater)["outcome"] == updater.FAILED

    def test_the_previous_run_is_cleared_even_if_this_one_crashes(
        self, sandbox, monkeypatch
    ):
        """A stale record is worse than none.

        Every ordinary path overwrites it, so clearing up front only matters
        when a run ends without writing at all -- an exception escaping
        mid-update. Then the previous run's record is still sitting there, and
        the menu bar reading a "no_op" from it would skip the relaunch after an
        update that genuinely happened. Asserting the ordinary paths instead
        would pass with the clear removed entirely, which is what the first
        version of this test did.
        """
        updater = sandbox
        updater.RESULT_FILE.write_text(json.dumps({"outcome": "no_op"}))
        monkeypatch.setattr(updater, "_find_project_root", lambda: Path("/x"))
        monkeypatch.setattr(updater, "_is_git_install", lambda _r: True)

        def explode(_root):
            raise RuntimeError("the update died half way")

        monkeypatch.setattr(updater, "_update_via_git", explode)

        with pytest.raises(RuntimeError):
            updater.run_update()

        assert not updater.RESULT_FILE.exists(), (
            "last run's record survived a run that wrote none"
        )

    def test_the_record_carries_a_timestamp_the_reader_can_parse(
        self, sandbox, monkeypatch
    ):
        # The menu bar compares this against when it started the run, so a
        # missing or unreadable one disables the whole mechanism.
        from datetime import datetime
        updater = sandbox
        monkeypatch.setattr(updater, "_find_project_root", lambda: Path("/x"))
        monkeypatch.setattr(updater, "_is_git_install", lambda _r: True)
        monkeypatch.setattr(updater, "_update_via_git", lambda _r: 2)
        monkeypatch.setattr(updater, "_report_tool_updates", lambda _a: True)
        monkeypatch.setattr(updater, "_installed_version", lambda: "0.16.1")

        updater.run_update()
        stamp = self._read(updater)["finished_at"]
        assert datetime.fromisoformat(stamp).tzinfo is not None, "must be aware"

    def test_an_unwritable_record_does_not_fail_the_update(
        self, sandbox, monkeypatch
    ):
        # Best-effort. Losing the record costs the caller its shortcut, and it
        # falls back to the version poll; failing the update would cost the
        # user the update.
        updater = sandbox

        def unwritable(*_a, **_k):
            raise OSError(30, "Read-only file system")

        monkeypatch.setattr(Path, "write_text", unwritable)
        monkeypatch.setattr(updater, "_find_project_root", lambda: Path("/x"))
        monkeypatch.setattr(updater, "_is_git_install", lambda _r: True)
        monkeypatch.setattr(updater, "_update_via_git", lambda _r: 2)
        monkeypatch.setattr(updater, "_report_tool_updates", lambda _a: True)
        monkeypatch.setattr(updater, "_installed_version", lambda: "0.16.1")

        assert updater.run_update() == 0

    def test_a_failed_tool_upgrade_is_not_recorded_as_success(
        self, sandbox, monkeypatch
    ):
        """The exit code and the record must not disagree.

        `_report_tool_updates` returning False makes `run_update` return 1.
        Recording NO_OP or UPDATED alongside that leaves the file -- the
        durable artefact, the one anybody reads afterwards -- saying the run
        went fine while the exit code says it did not.
        """
        updater = sandbox
        monkeypatch.setattr(updater, "_find_project_root", lambda: Path("/x"))
        monkeypatch.setattr(updater, "_is_git_install", lambda _r: True)
        monkeypatch.setattr(updater, "_update_via_git", lambda _r: 2)
        monkeypatch.setattr(updater, "_report_tool_updates", lambda _a: False)
        monkeypatch.setattr(updater, "_installed_version", lambda: "0.16.1")

        assert updater.run_update(apply_tools=True) == 1
        assert self._read(updater)["outcome"] == updater.FAILED

    def test_a_failed_tool_upgrade_after_a_real_update_is_also_recorded(
        self, sandbox, monkeypatch
    ):
        updater = sandbox
        monkeypatch.setattr(updater, "_find_project_root", lambda: Path("/x"))
        monkeypatch.setattr(updater, "_is_git_install", lambda _r: True)
        monkeypatch.setattr(updater, "_update_via_git", lambda _r: 0)
        monkeypatch.setattr(updater, "_rebuild_and_restart", lambda _r: [])
        monkeypatch.setattr(updater, "_report_tool_updates", lambda _a: False)
        monkeypatch.setattr(updater, "_installed_version", lambda: "0.17.0")

        assert updater.run_update(apply_tools=True) == 1
        assert self._read(updater)["outcome"] == updater.FAILED

    def test_the_record_is_swapped_in_never_written_over(
        self, sandbox, monkeypatch
    ):
        """The menu bar reads this file while the CLI writes it.

        `write_text` truncates first, so a read landing in that window gets a
        partial document. Asserted by watching how the file is produced rather
        than by racing a reader against it, which would be a flaky test of the
        same thing.
        """
        updater = sandbox
        monkeypatch.setattr(updater, "_find_project_root", lambda: Path("/x"))
        monkeypatch.setattr(updater, "_is_git_install", lambda _r: True)
        monkeypatch.setattr(updater, "_update_via_git", lambda _r: 2)
        monkeypatch.setattr(updater, "_report_tool_updates", lambda _a: True)
        monkeypatch.setattr(updater, "_installed_version", lambda: "0.16.1")

        replaced = []
        real_replace = os.replace
        monkeypatch.setattr(
            updater.os, "replace",
            lambda src, dst: replaced.append((str(src), str(dst))) or real_replace(src, dst),
        )

        updater.run_update()

        assert replaced, "the record was written in place rather than swapped in"
        src, dst = replaced[-1]
        assert dst == str(updater.RESULT_FILE)
        # Same directory, or the "move" is a copy and the window reopens.
        assert Path(src).parent == updater.RESULT_FILE.parent
        assert self._read(updater)["outcome"] == updater.NO_OP

    def test_a_failed_swap_leaves_no_temporary_file_behind(
        self, sandbox, monkeypatch
    ):
        updater = sandbox
        monkeypatch.setattr(updater, "_find_project_root", lambda: Path("/x"))
        monkeypatch.setattr(updater, "_is_git_install", lambda _r: True)
        monkeypatch.setattr(updater, "_update_via_git", lambda _r: 2)
        monkeypatch.setattr(updater, "_report_tool_updates", lambda _a: True)
        monkeypatch.setattr(updater, "_installed_version", lambda: "0.16.1")

        def boom(_src, _dst):
            raise OSError(18, "Invalid cross-device link")

        monkeypatch.setattr(updater.os, "replace", boom)

        assert updater.run_update() == 0, "a lost record must not fail the update"
        leftovers = list(updater.RESULT_FILE.parent.glob(".*tmp"))
        assert leftovers == [], f"left behind: {leftovers}"
