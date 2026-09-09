"""Tests for server.lifecycle.update_check — persistence + read API.

Doesn't exercise the network round-trip (quern.dev). Patches the urllib
call so test runs are deterministic.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def isolated_update_files(tmp_path, monkeypatch):
    """Redirect CONFIG_DIR / LAST_CHECK_FILE / UPDATE_INFO_FILE to tmp_path.

    The update check writes files in CONFIG_DIR; without redirection the
    test would mutate the real ~/.quern.
    """
    from server.lifecycle import update_check
    monkeypatch.setattr(update_check, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(update_check, "LAST_CHECK_FILE", tmp_path / "last-update-check")
    monkeypatch.setattr(update_check, "UPDATE_INFO_FILE", tmp_path / "update-info.json")
    return tmp_path


def test_read_update_info_returns_none_when_missing(isolated_update_files):
    from server.lifecycle.update_check import read_update_info
    assert read_update_info() is None


def test_read_update_info_returns_persisted_record(isolated_update_files):
    from server.lifecycle.update_check import read_update_info
    payload = {
        "checked_at": "2026-06-05T19:00:00+00:00",
        "current_version": "0.13.4",
        "latest_version": "0.13.5",
        "update_available": True,
        "message": "Update available — run \"quern update\" ...",
    }
    (isolated_update_files / "update-info.json").write_text(json.dumps(payload))
    assert read_update_info() == payload


def test_read_update_info_returns_none_on_invalid_json(isolated_update_files):
    """Defensive: corrupt sidecar shouldn't crash the system endpoint."""
    from server.lifecycle.update_check import read_update_info
    (isolated_update_files / "update-info.json").write_text("not json at all")
    assert read_update_info() is None


def test_check_for_updates_persists_when_update_available(
    isolated_update_files, monkeypatch,
):
    from server.lifecycle import update_check

    monkeypatch.setattr(update_check, "read_user_config", lambda: {})
    monkeypatch.setattr(update_check, "_get_local_version", lambda: "0.13.4")
    monkeypatch.setattr(update_check, "_get_head_sha", lambda: None)

    fake_resp = MagicMock()
    fake_resp.__enter__ = MagicMock(return_value=fake_resp)
    fake_resp.__exit__ = MagicMock(return_value=False)
    fake_resp.read = MagicMock(
        return_value=json.dumps(
            {"update_available": True, "latest_version": "0.13.5"}
        ).encode(),
    )
    with patch("urllib.request.urlopen", return_value=fake_resp):
        msg = update_check.check_for_updates()

    assert msg and "Update available" in msg
    info = update_check.read_update_info()
    assert info is not None
    assert info["update_available"] is True
    assert info["current_version"] == "0.13.4"
    assert info["latest_version"] == "0.13.5"
    assert info["checked_at"]  # ISO timestamp present


def test_check_for_updates_persists_when_no_update(
    isolated_update_files, monkeypatch,
):
    """Even when nothing is available, persist the "checked" record so
    the system API can report when the last check ran."""
    from server.lifecycle import update_check

    monkeypatch.setattr(update_check, "read_user_config", lambda: {})
    monkeypatch.setattr(update_check, "_get_local_version", lambda: "0.13.4")
    monkeypatch.setattr(update_check, "_get_head_sha", lambda: None)

    fake_resp = MagicMock()
    fake_resp.__enter__ = MagicMock(return_value=fake_resp)
    fake_resp.__exit__ = MagicMock(return_value=False)
    fake_resp.read = MagicMock(
        return_value=json.dumps({"update_available": False}).encode(),
    )
    with patch("urllib.request.urlopen", return_value=fake_resp):
        msg = update_check.check_for_updates()

    assert msg is None
    info = update_check.read_update_info()
    assert info is not None
    assert info["update_available"] is False
    assert info["message"] is None


def _fake_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    resp.read = MagicMock(return_value=json.dumps(payload).encode())
    return resp


@pytest.mark.parametrize("channel", ["stable", "beta"])
def test_check_for_updates_sends_the_configured_channel(
    isolated_update_files, monkeypatch, channel,
):
    """quern.dev compares the SHA against that channel's pointer branch.

    Omitting the channel makes the endpoint assume stable, which reports a
    spurious update to every beta user — and, before the endpoint became
    channel-aware, reported one to stable users too as soon as any commit
    landed on main.
    """
    from server.lifecycle import update_check

    monkeypatch.setattr(update_check, "read_user_config", lambda: {})
    monkeypatch.setattr(update_check, "_get_local_version", lambda: "0.14.0")
    monkeypatch.setattr(update_check, "_get_head_sha", lambda: "abc123")
    monkeypatch.setattr("server.config.get_update_channel", lambda: channel)

    with patch(
        "urllib.request.urlopen",
        return_value=_fake_response({"update_available": False}),
    ) as urlopen:
        update_check.check_for_updates()

    requested_url = urlopen.call_args[0][0].full_url
    assert f"channel={channel}" in requested_url
    assert "sha=abc123" in requested_url


def test_message_names_the_version_when_the_endpoint_supplies_one(
    isolated_update_files, monkeypatch,
):
    from server.lifecycle import update_check

    monkeypatch.setattr(update_check, "read_user_config", lambda: {})
    monkeypatch.setattr(update_check, "_get_local_version", lambda: "0.14.0")
    monkeypatch.setattr(update_check, "_get_head_sha", lambda: None)
    monkeypatch.setattr("server.config.get_update_channel", lambda: "beta")

    with patch(
        "urllib.request.urlopen",
        return_value=_fake_response(
            {"update_available": True, "latest_version": "0.14.1-beta.2"}
        ),
    ):
        msg = update_check.check_for_updates()

    assert msg is not None
    assert "0.14.1-beta.2" in msg


def test_message_falls_back_when_endpoint_omits_the_version(
    isolated_update_files, monkeypatch,
):
    """Older quern.dev deployments returned only update_available."""
    from server.lifecycle import update_check

    monkeypatch.setattr(update_check, "read_user_config", lambda: {})
    monkeypatch.setattr(update_check, "_get_local_version", lambda: "0.14.0")
    monkeypatch.setattr(update_check, "_get_head_sha", lambda: None)
    monkeypatch.setattr("server.config.get_update_channel", lambda: "stable")

    with patch(
        "urllib.request.urlopen",
        return_value=_fake_response({"update_available": True}),
    ):
        msg = update_check.check_for_updates()

    assert msg is not None
    assert "Update available" in msg
    assert update_check.read_update_info()["latest_version"] is None


# --------------------------------------------------------------------------
# A git install that is ahead of its channel is not "out of date" (#123)
# --------------------------------------------------------------------------
#
# quern.dev answers the sha question with string equality, because a Cloudflare
# Worker holding only branch refs has no commit graph and cannot distinguish
# "ahead" from "behind". A git install working on main is ahead of its channel
# pointer, so the endpoint reported an update available to an *ancestor* of the
# local HEAD.
#
# `_update_via_git` never believed it — it counts HEAD..origin/<ref> and returns
# "no update needed" at zero — so the update was always refused and only the
# notification was wrong. These pin that the notification now agrees with the
# action.
#
# Tarball installs never send a sha and never reach this path; their behaviour
# is unchanged.


def _fake_git(monkeypatch, tmp_path, *, is_ancestor: bool = True,
              has_object: bool = True, has_repo: bool = True, fail: bool = False):
    """Stand in for the git calls _is_ahead_of makes.

    Uses a real temp directory rather than patching `Path.exists` on the class —
    doing that globally broke an autouse fixture's teardown.
    """
    from server.lifecycle import update_check as uc

    if has_repo:
        (tmp_path / ".git").mkdir()
    monkeypatch.setattr(uc, "_find_project_root", lambda: tmp_path)

    def run(cmd, **_kw):
        if fail:
            raise OSError("git exploded")
        if "cat-file" in cmd:
            return SimpleNamespace(returncode=0 if has_object else 128, stdout="", stderr="")
        if "merge-base" in cmd:
            return SimpleNamespace(returncode=0 if is_ancestor else 1, stdout="", stderr="")
        raise AssertionError(f"unexpected git call: {cmd}")

    monkeypatch.setattr(uc.subprocess, "run", run)


SHA = "d9710814f46fcdd27190f683f24921ecf8658142"


def test_already_containing_the_latest_sha_reads_as_ahead(monkeypatch, tmp_path):
    from server.lifecycle.update_check import _is_ahead_of

    _fake_git(monkeypatch, tmp_path, is_ancestor=True)
    assert _is_ahead_of(SHA) is True


def test_genuinely_behind_is_not_suppressed(monkeypatch, tmp_path):
    """The guard must never swallow a real update."""
    from server.lifecycle.update_check import _is_ahead_of

    _fake_git(monkeypatch, tmp_path, is_ancestor=False)
    assert _is_ahead_of(SHA) is False


def test_an_unknown_commit_is_not_treated_as_behind(monkeypatch, tmp_path):
    """`merge-base` errors on a sha we do not have, and that must not be read as
    a negative answer to a question we never got to ask — a shallow clone would
    otherwise report itself ahead of everything."""
    from server.lifecycle.update_check import _is_ahead_of

    _fake_git(monkeypatch, tmp_path, has_object=False)
    assert _is_ahead_of(SHA) is False


def test_a_non_git_install_never_claims_to_be_ahead(monkeypatch, tmp_path):
    """Tarball installs have no repository. True here would suppress every
    update they are ever offered."""
    from server.lifecycle.update_check import _is_ahead_of

    _fake_git(monkeypatch, tmp_path, has_repo=False)
    assert _is_ahead_of(SHA) is False


def test_a_git_failure_falls_back_to_the_endpoint(monkeypatch, tmp_path):
    from server.lifecycle.update_check import _is_ahead_of

    _fake_git(monkeypatch, tmp_path, fail=True)
    assert _is_ahead_of(SHA) is False


def test_a_missing_latest_sha_is_unanswerable(monkeypatch, tmp_path):
    """An older quern.dev deployment may omit it."""
    from server.lifecycle.update_check import _is_ahead_of

    _fake_git(monkeypatch, tmp_path)
    assert _is_ahead_of(None) is False
    assert _is_ahead_of("") is False


def test_the_guard_never_fetches(monkeypatch, tmp_path):
    """check_for_updates() runs synchronously on the foreground startup path
    (server/main.py), so this must not add blocking network git. An earlier
    version fetched and could stall startup for up to 25s."""
    from server.lifecycle import update_check as uc

    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(uc, "_find_project_root", lambda: tmp_path)
    seen = []

    def run(cmd, **_kw):
        seen.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(uc.subprocess, "run", run)
    uc._is_ahead_of(SHA)
    assert not any("fetch" in c for c in seen), f"guard fetched: {seen}"
