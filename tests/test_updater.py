"""Tests for server.lifecycle.updater — `quern update` flow.

Focuses on the branch-vs-release semantics fixed in #40: the check
should always compare against ``origin/<RELEASE_BRANCH>``, and the pull
step must be skipped when the user isn't actually on that branch.
"""

from __future__ import annotations

import contextlib
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


def test_fetch_latest_release_beta_picks_the_newest_prerelease(monkeypatch):
    """Beta scans /releases and takes the newest prerelease when it really is
    the newest thing.

    The docstring here used to say stable entries newer than the topmost
    prerelease were "deliberately NOT chosen — that's how beta diverges from
    stable". That was the bug written down as intent, and the fixture below
    never tested it: its prerelease (0.13.6-beta.1) is newer than its stable
    (0.13.5), so the claim was never exercised. See
    `test_fetch_latest_release_beta_never_goes_older_than_stable`."""
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


class TestUpdateRefreshesTheCachedCheck:
    """An update left the cached check saying the old version was current.

    `update-info.json` holds a version and `update_available` computed before
    the run; `last-update-check` suppresses a fresh check for 24 hours. Nothing
    in the update path touched either, so after 0.16.1 -> 0.17.0 the menu bar
    kept offering an update already applied. Three update runs left both
    untouched; one `check-updates` fixed it.

    The fix asks rather than infers. Two earlier attempts did infer, and a
    review showed both were worse than the bug:

    - writing `update_available: false` on `rc == 2` is false whenever
      `_update_via_git` returns 2 *because you are on a feature branch and the
      release branch is ahead* -- it prints "switch and rerun" and returns 2.
      That hides a real update, where the stale cache at least over-offered.
    - deleting the files is also a false all-clear: `UpdateInfo` in the menu bar
      defaults `updateAvailable` to false, so a missing file renders as "Up to
      date".

    So these tests drive the *real* `_update_via_git` decision rather than
    stubbing its return code -- stubbing it is what made the first defect
    invisible.
    """

    def _stale_cache(self):
        import json

        from server.lifecycle import update_check as uc

        uc.UPDATE_INFO_FILE.write_text(json.dumps({
            "current_version": "0.16.1", "latest_version": "0.17.0",
            "update_available": True, "message": "Update available",
        }))
        uc.LAST_CHECK_FILE.touch()
        return uc

    def _run(self, monkeypatch, git_rc, tools_ok=True, checked=None, failures=()):
        from pathlib import Path

        from server.lifecycle import updater

        monkeypatch.setattr(updater, "_find_project_root", lambda: Path("/tmp"))
        monkeypatch.setattr(updater, "_is_git_install", lambda _p: True)
        monkeypatch.setattr(updater, "_update_via_git", lambda _p: git_rc)
        monkeypatch.setattr(updater, "_report_tool_updates", lambda *_a: tools_ok)
        monkeypatch.setattr(updater, "_installed_version", lambda: "0.17.0")
        monkeypatch.setattr(updater, "_rebuild_and_restart", lambda _p: list(failures))

        def fake_check(force=False, on_error=None):
            (checked if checked is not None else []).append(force)
            return None

        monkeypatch.setattr(
            "server.lifecycle.update_check.check_for_updates", fake_check
        )
        return updater.run_update()

    def test_the_no_op_branch_asks(self, monkeypatch):
        """"Already up to date" is the branch that kept being hit, and the one
        a per-exit fix would forget."""
        calls = []
        self._stale_cache()
        assert self._run(monkeypatch, git_rc=2, checked=calls) == 0
        assert calls == [True], "no fresh check after a no-op update"

    def test_a_successful_update_asks(self, monkeypatch):
        calls = []
        self._stale_cache()
        assert self._run(monkeypatch, git_rc=0, checked=calls) == 0
        assert calls == [True]

    def test_it_forces_past_the_rate_limit_and_the_opt_out(self, monkeypatch):
        """Both gates are what kept the wrong answer on screen: the stamp for 24
        hours, and the opt-out permanently for anyone who turned it off.

        `force=True` is consistent with `check-updates` -- the user ran an
        update command, which is an explicit request, and the setting governs
        the automatic check.
        """
        calls = []
        self._stale_cache()
        self._run(monkeypatch, git_rc=2, checked=calls)
        assert calls == [True], f"the check was not forced: {calls}"

    def test_a_failed_update_leaves_the_record_alone(self, monkeypatch):
        """Nothing was applied, so the previous answer is no worse than before
        -- and removing it would render as "Up to date" in the menu bar."""
        import json

        calls = []
        uc = self._stale_cache()
        assert self._run(monkeypatch, git_rc=1, checked=calls) == 1
        assert calls == [], "a failed update refreshed the check anyway"
        info = json.loads(uc.UPDATE_INFO_FILE.read_text())
        assert info["update_available"] is True, "a failed update erased a true answer"

    def test_a_failed_rebuild_still_refreshes(self, monkeypatch):
        """The source moved, so the cached answer is about the old version.

        A failed rebuild or a failed tool upgrade does not undo the pull, and
        `_write_result` on those paths already reports the new version -- so
        skipping the refresh left the menu bar offering a version that was
        already installed.
        """
        calls = []
        self._stale_cache()
        assert self._run(monkeypatch, git_rc=0, checked=calls, failures=["venv"]) == 1
        assert calls == [True], "the source moved and nothing refreshed the record"

    def test_a_failed_tool_upgrade_after_an_update_still_refreshes(self, monkeypatch):
        calls = []
        self._stale_cache()
        assert self._run(monkeypatch, git_rc=0, tools_ok=False, checked=calls) == 1
        assert calls == [True]

    def test_nothing_applied_means_no_refresh(self, monkeypatch):
        """rc 2 with a failed tool upgrade: quern was not updated, so the
        previous record is no worse than before and removing or replacing it
        would be a guess."""
        calls = []
        self._stale_cache()
        assert self._run(monkeypatch, git_rc=2, tools_ok=False, checked=calls) == 1
        assert calls == []

    def test_refreshing_failing_does_not_fail_the_update(self, monkeypatch):
        # Bookkeeping must not turn a good update into a reported failure.
        from server.lifecycle import updater

        self._stale_cache()
        from pathlib import Path

        monkeypatch.setattr(updater, "_find_project_root", lambda: Path("/tmp"))
        monkeypatch.setattr(updater, "_is_git_install", lambda _p: True)
        monkeypatch.setattr(updater, "_update_via_git", lambda _p: 2)
        monkeypatch.setattr(updater, "_report_tool_updates", lambda *_a: True)
        monkeypatch.setattr(updater, "_installed_version", lambda: "0.17.0")
        monkeypatch.setattr(
            "server.lifecycle.update_check.check_for_updates",
            lambda **_k: (_ for _ in ()).throw(OSError("offline")),
        )
        assert updater.run_update() == 0

    def test_run_update_never_writes_the_record_itself(self, monkeypatch):
        """The invariant that makes the meaning of `rc` irrelevant here.

        `_update_via_git` returns 2 from three places and only one means "at
        the channel tip" -- `test_update_via_git_on_feature_branch_with_updates
        _warns_and_skips_pull` pins the one that does not. The first fix read
        `rc == 2` as "up to date" and wrote that, hiding a real update.

        So: `run_update` must never write `update-info.json` on any path. Only
        `check_for_updates` writes it, and only after asking. With the check
        stubbed out, the file must be exactly as it was.
        """
        import json


        cases = [
            ("rc=0", dict(git_rc=0)),
            ("rc=1", dict(git_rc=1)),
            ("rc=2", dict(git_rc=2)),
            ("rc=2 + failed tools", dict(git_rc=2, tools_ok=False)),
            ("rc=0 + failed tools", dict(git_rc=0, tools_ok=False)),
        ]
        for _label, kwargs in cases:
            uc = self._stale_cache()
            before = uc.UPDATE_INFO_FILE.read_text()
            self._run(monkeypatch, **kwargs)
            assert uc.UPDATE_INFO_FILE.read_text() == before, (
                f"run_update wrote the record itself on {_label}; "
                "that is an inference about what rc means"
            )
            assert json.loads(before)["update_available"] is True

        uc = self._stale_cache()
        before = uc.UPDATE_INFO_FILE.read_text()
        self._run(monkeypatch, git_rc=0, failures=["venv"])
        assert uc.UPDATE_INFO_FILE.read_text() == before, (
            "run_update wrote the record itself on the failed-rebuild exit"
        )

    def test_the_channel_reaches_the_sha_check(self, monkeypatch):
        """quern.dev compares the SHA against the channel's pointer branch.

        Omitting `channel` makes it assume stable, so a beta user at the stable
        pointer is told there is nothing to update to while beta is ahead --
        and this is the answer `run_update` acts on.
        """
        from server.lifecycle import updater

        seen = {}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"update_available": false}'

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            return _Resp()

        monkeypatch.setattr("server.config.get_update_channel", lambda: "beta")
        monkeypatch.setattr(updater.urllib.request, "urlopen", fake_urlopen)
        updater._check_via_quern_dev("abc123")

        assert "channel=beta" in seen["url"], seen["url"]
        # And the sha, which the name promised and the assertion did not.
        # Without it the endpoint has nothing to compare and answers
        # `update_available: false` unconditionally, so `quern update` would
        # report "Already up to date" forever.
        assert "sha=abc123" in seen["url"], seen["url"]

    def test_a_failed_refresh_does_not_buy_24_hours_of_silence(self, monkeypatch):
        """`check_for_updates` stamps the rate-limit file *before* the network
        call, so failures do not retry rapidly. Right for the automatic check,
        wrong here.

        A refresh that cannot reach the endpoint would otherwise leave the
        stale record in place *and* a fresh stamp suppressing the automatic
        check that would have corrected it -- turning an hour of staleness into
        a day. Before this function existed `quern update` never touched that
        stamp, so that window would be this change making the bug worse.
        """
        import os
        import time

        from server.lifecycle import update_check as uc
        from server.lifecycle import updater

        uc.UPDATE_INFO_FILE.write_text('{"update_available": true}')
        uc.LAST_CHECK_FILE.touch()
        old = time.time() - 23 * 3600
        os.utime(uc.LAST_CHECK_FILE, (old, old))

        def failing_check(force=False, on_error=None):
            uc.LAST_CHECK_FILE.touch()      # what the real one does first
            return None                      # ...and then fails to write a record

        monkeypatch.setattr(
            "server.lifecycle.update_check.check_for_updates", failing_check
        )
        updater._refresh_update_check()

        age_hours = (time.time() - uc.LAST_CHECK_FILE.stat().st_mtime) / 3600
        assert age_hours > 22, (
            f"a failed refresh reset the rate-limit stamp (age now {age_hours:.1f}h), "
            "suppressing the automatic check that would have corrected the record"
        )

    def test_a_successful_refresh_leaves_the_new_stamp(self, monkeypatch):
        # The converse, so the restore cannot become "always put it back".
        import os
        import time

        from server.lifecycle import update_check as uc
        from server.lifecycle import updater

        uc.UPDATE_INFO_FILE.write_text('{"update_available": true}')
        uc.LAST_CHECK_FILE.touch()
        old = time.time() - 23 * 3600
        os.utime(uc.LAST_CHECK_FILE, (old, old))

        def good_check(force=False, on_error=None):
            uc.LAST_CHECK_FILE.touch()
            uc.UPDATE_INFO_FILE.write_text('{"update_available": false}')
            return None

        monkeypatch.setattr(
            "server.lifecycle.update_check.check_for_updates", good_check
        )
        updater._refresh_update_check()

        age_hours = (time.time() - uc.LAST_CHECK_FILE.stat().st_mtime) / 3600
        assert age_hours < 1, "a real answer should reset the rate limit"


class TestBetaNeverOffersOlderContentThanStable:
    """The channel exists so beta users see *more* recent content, never less.

    Found live after cutting 0.18.0: with 0.15.0-beta.1 the newest prerelease
    in the repo, a beta tarball user on 0.18.0 was told
    `Updating v0.18.0 -> v0.15.0-beta.1` and downgraded three minor versions
    onto a source-only tarball with no menu-bar app -- then pinned there, since
    the next check found current == latest and said "already up to date".

    That is the *normal* state of this repo between beta cycles, not an edge
    case: stable moves on every release and prereleases only appear when a beta
    is being prepared.
    """

    def _releases(self, monkeypatch, entries):
        from server.lifecycle import updater

        monkeypatch.setattr(
            updater.urllib.request, "urlopen", lambda req, **kw: _FakeUrlResp(entries),
        )
        return updater

    def test_a_stale_prerelease_does_not_beat_a_newer_stable(self, monkeypatch):
        updater = self._releases(monkeypatch, [
            {"tag_name": "v0.18.0", "tarball_url": "stable.tgz", "prerelease": False},
            {"tag_name": "v0.15.0-beta.1", "tarball_url": "beta.tgz", "prerelease": True},
        ])
        assert updater._fetch_latest_release("beta") == ("0.18.0", "stable.tgz")

    def test_a_genuine_newer_prerelease_still_wins(self, monkeypatch):
        """The fix must not collapse beta into stable."""
        updater = self._releases(monkeypatch, [
            {"tag_name": "v0.19.0-beta.1", "tarball_url": "beta.tgz", "prerelease": True},
            {"tag_name": "v0.18.0", "tarball_url": "stable.tgz", "prerelease": False},
        ])
        assert updater._fetch_latest_release("beta") == ("0.19.0-beta.1", "beta.tgz")

    def test_a_prerelease_loses_to_its_own_final(self, monkeypatch):
        """PEP 440 puts 0.18.0-beta.1 before 0.18.0, which is what we want and
        the reason for not hand-rolling the comparison."""
        updater = self._releases(monkeypatch, [
            {"tag_name": "v0.18.0", "tarball_url": "final.tgz", "prerelease": False},
            {"tag_name": "v0.18.0-beta.1", "tarball_url": "beta.tgz", "prerelease": True},
        ])
        assert updater._fetch_latest_release("beta") == ("0.18.0", "final.tgz")

    def test_release_order_does_not_decide_it(self, monkeypatch):
        """`/releases` is ordered by `created_at`, so a hotfix published after a
        newer release sits above it. Taking the topmost of each category would
        offer 0.17.1 over 0.18.0."""
        updater = self._releases(monkeypatch, [
            {"tag_name": "v0.17.1", "tarball_url": "hotfix.tgz", "prerelease": False},
            {"tag_name": "v0.18.0", "tarball_url": "newest.tgz", "prerelease": False},
        ])
        assert updater._fetch_latest_release("beta") == ("0.18.0", "newest.tgz")

    def test_a_null_tag_does_not_fail_the_whole_check(self, monkeypatch):
        """A JSON `"tag_name": null` used to raise AttributeError out of the
        comparison, which the broad handler upstream reported as "could not
        fetch release info" -- failing the update rather than falling back to
        the candidate that parsed."""
        updater = self._releases(monkeypatch, [
            {"tag_name": None, "tarball_url": "junk.tgz", "prerelease": True},
            {"tag_name": "v0.18.0", "tarball_url": "stable.tgz", "prerelease": False},
        ])
        assert updater._fetch_latest_release("beta") == ("0.18.0", "stable.tgz")

    def test_a_repo_of_only_malformed_tags_offers_nothing(self, monkeypatch):
        """Selection is not comparison. `_newer_release` treats an unparseable
        tag as the lesser, which is right when ranking two -- but with a single
        candidate there is nothing to lose to, so `nightly` was returned as the
        answer. `_update_via_tarball` then catches `InvalidVersion` around its
        own guard and carries on, so this really would have downloaded and
        installed it."""
        updater = self._releases(monkeypatch, [
            {"tag_name": "nightly", "tarball_url": "junk.tgz", "prerelease": True},
            {"tag_name": "rolling", "tarball_url": "junk2.tgz", "prerelease": False},
        ])
        assert updater._fetch_latest_release("beta") is None

    def test_a_malformed_latest_stable_offers_nothing(self, monkeypatch):
        """`/releases/latest` returns one object and never passes through the
        selection above, so it needs validating on its own."""
        from server.lifecycle import updater

        monkeypatch.setattr(
            updater.urllib.request, "urlopen",
            lambda req, **kw: _FakeUrlResp(
                {"tag_name": "nightly", "tarball_url": "junk.tgz", "prerelease": False},
            ),
        )
        assert updater._fetch_latest_release("stable") is None

    def test_an_unparseable_tag_cannot_win(self, monkeypatch):
        """A malformed tag must not offer itself as an update by accident."""
        updater = self._releases(monkeypatch, [
            {"tag_name": "v0.18.0", "tarball_url": "stable.tgz", "prerelease": False},
            {"tag_name": "nightly", "tarball_url": "junk.tgz", "prerelease": True},
        ])
        assert updater._fetch_latest_release("beta") == ("0.18.0", "stable.tgz")

    def test_an_unparseable_tag_cannot_win_from_either_side(self, monkeypatch):
        """The fixture above always puts the malformed tag in the same argument
        position, so only one of the two comparisons was pinned. Two stable
        releases put it in the other one -- and unpinned, the mutant really does
        offer `nightly` as an update."""
        updater = self._releases(monkeypatch, [
            {"tag_name": "v0.18.0", "tarball_url": "stable.tgz", "prerelease": False},
            {"tag_name": "nightly", "tarball_url": "junk.tgz", "prerelease": False},
        ])
        assert updater._fetch_latest_release("beta") == ("0.18.0", "stable.tgz")


class TestAnUpdateNeverMovesBackwards:
    """The guard that makes a wrong answer from the resolver a refusal rather
    than a downgrade -- without blocking the one backwards move that is
    deliberate.

    `current == latest` was the only check, so any wrong answer was acted on.
    But `quern set-channel stable && quern update` from a beta build genuinely
    moves backwards mid beta cycle, and that flow is documented in
    docs/guides/update-channels.md. A first version of this guard refused it,
    and -- because rc 2 is mapped to "already up to date" -- told the user the
    move had succeeded. That is this release's own defect class, reintroduced
    by the fix for it.
    """

    def _run(self, monkeypatch, tmp_path, current, offered, channel="beta"):
        from server.lifecycle import updater

        monkeypatch.setattr(updater, "_read_local_version", lambda root: current)
        monkeypatch.setattr(
            updater, "_fetch_latest_release", lambda ch: (offered, "x.tgz"),
        )
        monkeypatch.setattr("server.config.get_update_channel", lambda: channel)
        return updater._update_via_tarball(tmp_path)

    def test_an_older_offer_on_the_same_channel_is_refused(
        self, monkeypatch, tmp_path, capsys
    ):
        rc = self._run(monkeypatch, tmp_path, "0.18.0", "0.15.0-beta.1")
        assert rc == 1, "it went ahead and downgraded"
        assert "Not downgrading" in capsys.readouterr().out

    def test_a_refusal_is_not_reported_as_being_up_to_date(
        self, monkeypatch, tmp_path
    ):
        """rc 2 is mapped to NO_OP / "already up to date" and exit 0. A refusal
        is the opposite: an update was wanted and did not happen. Returning 2
        here would tell every surface -- the CLI, the menu bar's `isNoOp`, the
        result file -- that nothing needed doing."""
        rc = self._run(monkeypatch, tmp_path, "0.18.0", "0.15.0-beta.1")
        assert rc != 2, (
            "a refusal was mapped to 'already up to date', which is how someone "
            "ends up on a stale build believing they are current"
        )

    def test_leaving_beta_for_stable_is_allowed(self, monkeypatch, tmp_path, capsys):
        """The documented flow: `quern set-channel stable && quern update` from
        a beta build. Mid beta cycle the newest stable really is older than what
        is installed, and refusing it strands tarball users on beta."""
        with contextlib.suppress(Exception):
            self._run(
                monkeypatch, tmp_path, "0.19.0-beta.1", "0.18.0", channel="stable",
            )
        out = capsys.readouterr().out
        assert "Not downgrading" not in out, (
            "a user switching back to stable was refused and left on beta"
        )
        assert "→ v0.18.0" in out

    def test_a_beta_build_is_still_protected_on_the_beta_channel(
        self, monkeypatch, tmp_path, capsys
    ):
        """Leaving beta is the exception, not "prereleases may go backwards"."""
        rc = self._run(monkeypatch, tmp_path, "0.19.0-beta.1", "0.15.0-beta.1")
        assert rc == 1
        assert "Not downgrading" in capsys.readouterr().out

    def test_a_stable_user_is_protected_too(self, monkeypatch, tmp_path, capsys):
        """Every other test here drives the beta channel, and the one that uses
        stable has a *prerelease* installed -- so `leaving_beta` could be
        rewritten as plain `channel == "stable"`, disabling the guard for every
        user on the default channel, with the suite green.

        Stable can offer something older: a release yanked after the fact, or a
        locally built version ahead of it. The guard is the only thing there."""
        rc = self._run(monkeypatch, tmp_path, "0.18.0", "0.17.1", channel="stable")
        assert rc == 1
        assert "Not downgrading" in capsys.readouterr().out

    def _got_past_the_guard(self, monkeypatch, tmp_path, capsys, current, offered):
        """Did it reach the update, rather than refuse at the version check?

        Asserted on the decision it printed, not on a return code: past the
        guard the run proceeds to a download that fails on this fixture's fake
        URL, and a test keyed to that failure would be testing the fixture.
        """
        with contextlib.suppress(Exception):
            self._run(monkeypatch, tmp_path, current, offered)
        out = capsys.readouterr().out
        assert "Not downgrading" not in out, "a legitimate update was refused"
        return out

    def test_a_newer_offer_is_not_blocked(self, monkeypatch, tmp_path, capsys):
        out = self._got_past_the_guard(
            monkeypatch, tmp_path, capsys, "0.17.0", "0.18.0",
        )
        assert "Updating v0.17.0 → v0.18.0" in out

    def test_an_unparseable_current_version_does_not_block(
        self, monkeypatch, tmp_path, capsys,
    ):
        """An unreadable local version must not wedge updates shut."""
        out = self._got_past_the_guard(
            monkeypatch, tmp_path, capsys, "not-a-version", "0.18.0",
        )
        assert "→ v0.18.0" in out
