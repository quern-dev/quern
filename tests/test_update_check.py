"""Tests for server.lifecycle.update_check — persistence + read API.

Doesn't exercise the network round-trip (quern.dev). Patches the urllib
call so test runs are deterministic.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def isolated_update_files(tmp_path, monkeypatch):
    """Redirect CONFIG_DIR / LAST_CHECK_FILE / UPDATE_INFO_FILE to tmp_path.

    The update check writes files in CONFIG_DIR; without redirection the
    test would mutate the real ~/.quern. CHANNEL_LOCK_FILE is included for a
    second reason as well as tidiness: left pointing at the real path, a test
    would contend for the same lock as the developer's running server.
    """
    from server.lifecycle import update_check
    monkeypatch.setattr(update_check, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(update_check, "LAST_CHECK_FILE", tmp_path / "last-update-check")
    monkeypatch.setattr(update_check, "UPDATE_INFO_FILE", tmp_path / "update-info.json")
    monkeypatch.setattr(update_check, "CHANNEL_LOCK_FILE", tmp_path / "channel.lock")
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
    # The persisted answer records which channel produced it. Without this a
    # reader cannot tell a "you are up to date" cached under stable from one
    # cached under beta, and the two mean different things.
    persisted = json.loads((isolated_update_files / "update-info.json").read_text())
    assert persisted["channel"] == channel


def test_a_channel_switch_mid_check_discards_the_result(
    isolated_update_files, monkeypatch,
):
    """The network call can block for seconds, long enough for the user to
    switch channels. That switch deletes the cache precisely because the
    in-flight answer no longer applies, so writing it would undo the
    invalidation and leave the old channel's verdict reading as current.
    """
    from server.lifecycle import update_check

    monkeypatch.setattr(update_check, "read_user_config", lambda: {})
    monkeypatch.setattr(update_check, "_get_local_version", lambda: "0.14.0")
    monkeypatch.setattr(update_check, "_get_head_sha", lambda: None)

    # "stable" when the request is built, "beta" by the time it returns.
    channels = iter(["stable", "beta"])
    monkeypatch.setattr("server.config.get_update_channel", lambda: next(channels))

    with patch(
        "urllib.request.urlopen",
        return_value=_fake_response({"update_available": True, "latest_version": "9.9.9"}),
    ):
        message = update_check.check_for_updates()

    assert message is None
    assert not (isolated_update_files / "update-info.json").exists()


def test_an_unchanged_channel_still_writes_the_result(
    isolated_update_files, monkeypatch,
):
    """The guard must only fire on an actual switch. Discarding every result
    would silently disable update notifications altogether."""
    from server.lifecycle import update_check

    monkeypatch.setattr(update_check, "read_user_config", lambda: {})
    monkeypatch.setattr(update_check, "_get_local_version", lambda: "0.14.0")
    monkeypatch.setattr(update_check, "_get_head_sha", lambda: None)
    monkeypatch.setattr("server.config.get_update_channel", lambda: "beta")

    with patch(
        "urllib.request.urlopen",
        return_value=_fake_response({"update_available": True, "latest_version": "9.9.9"}),
    ):
        message = update_check.check_for_updates()

    assert message is not None and "9.9.9" in message
    persisted = json.loads((isolated_update_files / "update-info.json").read_text())
    assert persisted["channel"] == "beta"
    assert persisted["update_available"] is True


def test_a_switch_during_the_commit_cannot_leave_a_stale_verdict(
    isolated_update_files, monkeypatch,
):
    """The interleaving a bare comparison cannot cover.

    The check passes its channel comparison, and only *then* does the switch
    land. Without a shared lock the check writes its stable answer after the
    invalidation removed it, leaving config saying beta and the cache saying
    stable. The invariant: whatever the ordering, the cached verdict either is
    absent or names the channel that config now holds.
    """
    import threading

    from server.lifecycle import update_check

    config: dict = {}
    monkeypatch.setattr(update_check, "read_user_config", lambda: config)
    monkeypatch.setattr("server.config.read_user_config", lambda: config)
    monkeypatch.setattr("server.config.USER_CONFIG_FILE",
                        isolated_update_files / "config.json")
    monkeypatch.setattr("server.config.CONFIG_DIR", isolated_update_files)
    monkeypatch.setattr(update_check, "_get_local_version", lambda: "0.14.0")
    monkeypatch.setattr(update_check, "_get_head_sha", lambda: None)

    switched = threading.Event()
    real_write = update_check._write_update_info

    def slow_write(info):
        # Inside the lock, past the comparison. Kick off the switch and give it
        # time to block, so the commit really is the one holding the lock.
        switched.set()
        time.sleep(0.25)
        real_write(info)

    monkeypatch.setattr(update_check, "_write_update_info", slow_write)

    def switcher():
        switched.wait(timeout=5)
        update_check.switch_channel("beta")

    t = threading.Thread(target=switcher)
    t.start()
    with patch(
        "urllib.request.urlopen",
        return_value=_fake_response({"update_available": True, "latest_version": "9.9.9"}),
    ):
        update_check.check_for_updates()
    t.join(timeout=5)
    assert not t.is_alive()

    from server.config import get_update_channel

    assert get_update_channel() == "beta"
    persisted = update_check.read_update_info()
    assert persisted is None or persisted["channel"] == "beta", (
        f"stale verdict survived the switch: {persisted}"
    )


def test_switch_channel_reports_a_bad_name_before_invalidating(
    isolated_update_files, monkeypatch,
):
    """The cache must survive a typo. Raising after invalidating would cost a
    valid update notification for nothing."""
    from server.lifecycle import update_check

    monkeypatch.setattr("server.config.USER_CONFIG_FILE",
                        isolated_update_files / "config.json")
    monkeypatch.setattr("server.config.CONFIG_DIR", isolated_update_files)
    info = isolated_update_files / "update-info.json"
    info.write_text('{"update_available": true, "channel": "stable"}')

    with pytest.raises(ValueError):
        update_check.switch_channel("nightly")

    assert info.exists()


def test_invalidate_clears_both_the_stamp_and_the_answer(isolated_update_files):
    """Switching channel must not leave the old channel's answer in place.

    The rate-limit stamp alone would suppress a fresh check for 24 hours, so
    clearing only the answer would leave no answer at all for a day.
    """
    from server.lifecycle import update_check

    stamp = isolated_update_files / "last-update-check"
    info = isolated_update_files / "update-info.json"
    stamp.touch()
    info.write_text(json.dumps({"update_available": True, "channel": "stable"}))

    update_check.invalidate_update_check()

    assert not stamp.exists()
    assert not info.exists()
    assert update_check.read_update_info() is None


def test_invalidate_is_a_no_op_when_nothing_is_cached(isolated_update_files):
    """Called on a fresh install, before any check has ever run."""
    from server.lifecycle import update_check

    update_check.invalidate_update_check()  # must not raise
    assert update_check.read_update_info() is None


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


class TestForcingACheck:
    """The cache is refreshed at most once a day, so a release landing this
    afternoon is not offered until tomorrow. The CLI never had that problem --
    `quern update` checks when you run it -- but the menu bar reads the cache
    and had no way to ask."""

    def _patched(self, monkeypatch, tmp_path, *, checked_recently: bool):
        from server.lifecycle import update_check

        last = tmp_path / "last-check"
        last.touch()
        if not checked_recently:
            old = time.time() - update_check.CHECK_INTERVAL - 60
            import os

            os.utime(last, (old, old))
        monkeypatch.setattr(update_check, "LAST_CHECK_FILE", last)
        monkeypatch.setattr(update_check, "CONFIG_DIR", tmp_path)
        monkeypatch.setattr(update_check, "UPDATE_INFO_FILE", tmp_path / "update-info.json")
        monkeypatch.setattr(update_check, "CHANNEL_LOCK_FILE", tmp_path / "channel.lock")
        monkeypatch.setattr(update_check, "read_user_config", dict)
        # The request itself, which was reaching quern.dev for real. It passed
        # either way -- check_for_updates swallows the failure -- so offline it
        # went green after burning the timeout, and online it made a network
        # call from the test suite. CHANNEL_LOCK_FILE above is the same class
        # of leak: harmless only while the request failed before the lock was
        # taken, and now that it succeeds it would contend with a running
        # server's real ~/.quern/channel.lock.
        monkeypatch.setattr(
            "urllib.request.urlopen",
            MagicMock(side_effect=AssertionError(
                "a test reached the network; inject the response instead"
            )),
        )
        return update_check

    def test_the_rate_limit_holds_for_an_ordinary_check(self, monkeypatch, tmp_path):
        update_check = self._patched(monkeypatch, tmp_path, checked_recently=True)
        looked = []
        monkeypatch.setattr(update_check, "_get_local_version",
                            lambda: looked.append(True) or "0.16.1")

        update_check.check_for_updates()

        assert not looked, "an ordinary check must not hit the network every few minutes"

    def test_force_goes_past_the_rate_limit(self, monkeypatch, tmp_path):
        update_check = self._patched(monkeypatch, tmp_path, checked_recently=True)
        looked = []
        monkeypatch.setattr(update_check, "_get_local_version",
                            lambda: looked.append(True) or "0.16.1")
        monkeypatch.setattr(update_check, "_get_head_sha", lambda: None)

        update_check.check_for_updates(force=True)

        assert looked, "someone who just asked should not be told tomorrow"

    def test_the_automatic_check_respects_the_opt_out(self, monkeypatch, tmp_path):
        """What the setting is actually for: no unattended calls."""
        update_check = self._patched(monkeypatch, tmp_path, checked_recently=False)
        monkeypatch.setattr(update_check, "read_user_config",
                            lambda: {"update_check": False})
        looked = []
        monkeypatch.setattr(update_check, "_get_local_version",
                            lambda: looked.append(True) or "0.16.1")

        assert update_check.check_for_updates() is None
        assert not looked, "the automatic check ignored the opt-out"

    def test_asking_directly_still_works_with_the_opt_out_set(
        self, monkeypatch, tmp_path
    ):
        """"update_check": false turns off the *automatic* check.

        It is the checkbox every other updater has, and every one of them
        leaves Check Now working. Refusing an explicit request answers a
        question the setting was never asked -- and clicking Check for Updates
        is not an unattended call, so the privacy reading of the setting is
        satisfied too.
        """
        update_check = self._patched(monkeypatch, tmp_path, checked_recently=False)
        monkeypatch.setattr(update_check, "read_user_config",
                            lambda: {"update_check": False})
        looked = []
        monkeypatch.setattr(update_check, "_get_local_version",
                            lambda: looked.append(True) or "0.16.1")
        monkeypatch.setattr(update_check, "_get_head_sha", lambda: None)

        update_check.check_for_updates(force=True)

        assert looked, "someone who asked was refused on the strength of a setting"


# --- What a failure tells the reader ------------------------------------
#
# Three outcomes on screen, the same three every updater has: an update is
# available, you are up to date, or the check could not be made. The third gets
# one reason and one instruction. The raw exception goes to the log, because
# when the short answer is not enough it is the only thing that is.
#
# So these tests assert the *action*, not the wording. There are three things a
# person can do -- fix their connection, wait, or report it -- and the only
# thing that matters is that each failure is sorted into the right one.


def _describe(exc):
    from server.lifecycle.update_check import describe_failure
    return describe_failure(exc)


def _remedy_for(exc):
    return _describe(exc).remedy


def test_network_failures_all_say_check_your_connection():
    import ssl
    # One bucket, keyed on the OS error family rather than a list of the
    # failures we happened to think of. The last two are the ones a
    # branch-per-exception version got wrong: a connection dropped while
    # reading the response arrives raw from http.client rather than wrapped in
    # URLError, and a handshake reset is a network fault, not a certificate
    # one.
    for exc in [
        urllib.error.URLError(socket.gaierror(8, "nodename nor servname")),
        urllib.error.URLError(TimeoutError("timed out")),
        urllib.error.URLError(ConnectionRefusedError(61, "refused")),
        urllib.error.URLError(OSError(51, "Network is unreachable")),
        ConnectionResetError(54, "Connection reset by peer"),
        ssl.SSLEOFError(8, "EOF occurred in violation of protocol"),
    ]:
        assert "internet connection" in _remedy_for(exc), type(exc).__name__


def test_service_failures_say_wait_rather_than_asking_the_reader_to_act():
    import json as _json
    # The distinction worth keeping. "Check your connection" and "wait" are
    # different instructions to a person: one says act, the other says don't.
    for exc in [
        urllib.error.HTTPError("https://quern.dev/x", 503, "Busy", {}, None),
        urllib.error.HTTPError("https://quern.dev/x", 500, "Boom", {}, None),
        _json.JSONDecodeError("Expecting value", "<html>", 0),
    ]:
        remedy = _remedy_for(exc)
        assert "Nothing to fix here" in remedy, type(exc).__name__
        assert "internet connection" not in remedy


def test_a_rejected_certificate_says_the_network_is_intercepting_https():
    import ssl
    inner = ssl.SSLCertVerificationError(
        1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed (_ssl.c:1010)"
    )
    failure = _describe(urllib.error.URLError(inner))
    assert "intercepting HTTPS" in failure.summary
    # Not "turn off quern's proxy" any more. quern cannot be the cause: it
    # never intercepts its own update traffic -- see ALWAYS_BYPASS in
    # server/proxy/addon.py -- so that advice would send the reader to a
    # setting that is already correct.
    assert "quern stop" not in failure.remedy


def test_an_unrecognised_failure_is_reported_rather_than_guessed_at():
    failure = _describe(RuntimeError("something odd"))
    assert "capture-env" in failure.remedy
    assert "RuntimeError" in failure.detail
    assert "something odd" in failure.detail


def test_the_raw_error_is_kept_but_never_shown():
    # The whole point of the collapse. The detail survives for the log; the
    # lines a reader sees carry the summary and the instruction instead.
    failure = _describe(urllib.error.URLError(socket.gaierror(8, "nodename")))
    assert "gaierror" in failure.detail
    shown = "\n".join(failure.lines())
    assert "gaierror" not in shown
    assert "Could not check for updates" in shown


def test_every_failure_carries_one_of_the_three_instructions():
    import ssl

    from server.lifecycle.update_check import (
        _CHECK_CONNECTION,
        _REPORT,
        _WAIT,
    )
    cases = [
        urllib.error.HTTPError("https://quern.dev/x", 500, "Boom", {}, None),
        urllib.error.URLError(ssl.SSLCertVerificationError(1, "bad cert")),
        urllib.error.URLError(socket.gaierror(8, "no")),
        urllib.error.URLError(TimeoutError("timed out")),
        urllib.error.URLError(ConnectionRefusedError(61, "refused")),
        ConnectionResetError(54, "reset"),
        ValueError("not json"),
        RuntimeError("unknown"),
    ]
    for exc in cases:
        failure = _describe(exc)
        # The pet peeve this exists to satisfy: no error without an
        # instruction, and only instructions someone can actually follow.
        assert failure.remedy in {_CHECK_CONNECTION, _WAIT, _REPORT}, (
            f"{type(exc).__name__} invented a fourth instruction"
        )
        assert failure.summary.strip()
        assert failure.detail.strip()


def test_check_for_updates_reports_the_failure_to_its_caller(isolated_update_files):
    from server.lifecycle.update_check import check_for_updates
    seen = []
    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError(socket.gaierror(8, "nodename nor servname")),
    ):
        result = check_for_updates(force=True, on_error=seen.append)
    assert result is None
    assert len(seen) == 1
    assert "internet connection" in seen[0].remedy


def test_check_for_updates_logs_the_raw_error(isolated_update_files, caplog):
    # The log is now the only place the exception survives, so it is load
    # bearing rather than incidental.
    from server.lifecycle.update_check import check_for_updates
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("boom")):
        with caplog.at_level("WARNING"):
            check_for_updates(force=True)
    assert any("boom" in r.message for r in caplog.records)


def test_check_for_updates_survives_a_caller_with_no_error_handler(isolated_update_files):
    from server.lifecycle.update_check import check_for_updates
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("boom")):
        assert check_for_updates(force=True) is None


def test_a_raising_error_handler_does_not_escape(isolated_update_files):
    # The docstring promises this never raises into a server start path, and a
    # caller's reporting bug must not become quern's.
    from server.lifecycle.update_check import check_for_updates

    def explode(_failure):
        raise RuntimeError("handler is broken")

    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("boom")):
        assert check_for_updates(force=True, on_error=explode) is None


# --- The check-updates command ----------------------------------------------


@pytest.fixture
def check_updates_cmd(isolated_update_files, monkeypatch):
    """The command, with every external lookup redirected or injected."""
    from server.lifecycle import update_check
    # Not the developer's real ~/.quern/config.json: a machine with checking
    # turned off would otherwise take every one of these down a path the test
    # is not about.
    monkeypatch.setattr(update_check, "read_user_config", dict)
    from server.main import _cmd_check_updates
    return _cmd_check_updates


def _cache(path, **fields):
    payload = {
        "checked_at": "2026-09-12T05:01:35+00:00",
        "current_version": "0.16.1",
        "latest_version": "0.16.1",
        "update_available": False,
    }
    payload.update(fields)
    (path / "update-info.json").write_text(json.dumps(payload))


def test_command_reports_the_failure_rather_than_a_stale_update(
    check_updates_cmd, isolated_update_files, capsys
):
    # The regression the ordering exists for. A cache left saying "update
    # available" by a check that succeeded yesterday must not be printed as
    # today's answer when today's check never left the machine.
    _cache(isolated_update_files, update_available=True, latest_version="0.17.0")
    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError(socket.gaierror(8, "nodename")),
    ):
        code = check_updates_cmd()
    captured = capsys.readouterr()
    assert code == 1
    assert "0.17.0" not in captured.out
    assert "Could not check for updates" in captured.err
    assert "internet connection" in captured.err


def test_command_points_at_the_log_without_printing_the_raw_error(
    check_updates_cmd, isolated_update_files, capsys
):
    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError(socket.gaierror(8, "nodename nor servname")),
    ):
        check_updates_cmd()
    captured = capsys.readouterr()
    assert "server.log" in captured.err
    # The detail belongs in the log, not on screen -- otherwise the collapse
    # bought nothing and the pointer is redundant.
    assert "gaierror" not in captured.err


def test_the_command_still_checks_when_automatic_checking_is_off(
    check_updates_cmd, isolated_update_files, capsys, monkeypatch
):
    # Running `quern check-updates` is asking. The setting governs the
    # unattended check, so it must not turn this into a refusal -- and the
    # answer must come from the network, not from the cache the last check
    # before the opt-out happened to leave behind.
    from server.lifecycle import update_check
    monkeypatch.setattr(
        update_check, "read_user_config", lambda: {"update_check": False}
    )
    _cache(isolated_update_files, update_available=True, latest_version="0.17.0")
    fake_resp = MagicMock()
    fake_resp.read.return_value = json.dumps(
        {"latest_version": "0.18.0", "update_available": True}
    ).encode()
    fake_resp.__enter__ = lambda self: self
    fake_resp.__exit__ = lambda self, *a: False
    with patch("urllib.request.urlopen", return_value=fake_resp) as urlopen:
        code = check_updates_cmd()
    captured = capsys.readouterr()
    assert urlopen.called, "an explicit check was refused by the opt-out"
    assert code == 0
    assert "Update available" in captured.out


def test_command_sends_failures_to_stderr_not_stdout(
    check_updates_cmd, isolated_update_files, capsys
):
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("boom")):
        check_updates_cmd()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip()


def test_command_asks_even_though_the_rate_limit_is_current(
    check_updates_cmd, isolated_update_files, capsys
):
    # The feature's entire premise: the cached answer refreshes at most once a
    # day, and this command exists to ignore that. With the stamp fresh, a call
    # without force=True would return without ever reaching the network.
    (isolated_update_files / "last-update-check").write_text("")
    fake_resp = MagicMock()
    fake_resp.read.return_value = json.dumps(
        {"latest_version": "0.17.0", "update_available": True}
    ).encode()
    fake_resp.__enter__ = lambda self: self
    fake_resp.__exit__ = lambda self, *a: False
    with patch("urllib.request.urlopen", return_value=fake_resp) as urlopen:
        code = check_updates_cmd()
    assert urlopen.called, "the rate limit was allowed to skip the request"
    assert code == 0
    assert "Update available" in capsys.readouterr().out


def test_command_reports_up_to_date_without_claiming_a_check_it_did_not_make(
    check_updates_cmd, isolated_update_files, capsys
):
    fake_resp = MagicMock()
    fake_resp.read.return_value = json.dumps(
        {"latest_version": "0.16.1", "update_available": False}
    ).encode()
    fake_resp.__enter__ = lambda self: self
    fake_resp.__exit__ = lambda self, *a: False
    with patch("urllib.request.urlopen", return_value=fake_resp):
        code = check_updates_cmd()
    assert code == 0
    assert "Up to date" in capsys.readouterr().out


def test_command_does_not_call_a_silent_no_result_up_to_date(
    check_updates_cmd, isolated_update_files, capsys
):
    with patch(
        "server.lifecycle.update_check.check_for_updates", return_value=None
    ), patch("server.lifecycle.update_check.read_update_info", return_value=None):
        code = check_updates_cmd()
    captured = capsys.readouterr()
    assert code == 1
    assert "Up to date" not in captured.out
    assert "no result" in captured.err


# --- The command is actually reachable --------------------------------------


def test_check_updates_is_wired_into_the_cli_dispatch(monkeypatch, capsys):
    """Deleting the dispatch arm used to leave the whole suite green.

    Everything else here calls `_cmd_check_updates` directly, and the only
    other thing that mentioned the command was the README sync check, which
    reads the subparser registration. So `quern check-updates` could have
    stopped running the check entirely -- falling through to exit 0, doing
    nothing, looking exactly like "up to date" -- and nothing would have said
    so. That is the branch's whole user-facing deliverable.
    """
    import server.main as main

    called = []
    monkeypatch.setattr(main, "_cmd_check_updates", lambda: called.append(True) or 0)
    monkeypatch.setattr("sys.argv", ["quern", "check-updates"])

    with pytest.raises(SystemExit) as exit_info:
        main.cli()

    assert called, "`quern check-updates` did not reach the check"
    assert exit_info.value.code == 0


def test_the_commands_exit_code_reaches_the_shell(monkeypatch):
    # A failed check that exits 0 tells a script the opposite of what happened.
    import server.main as main

    monkeypatch.setattr(main, "_cmd_check_updates", lambda: 1)
    monkeypatch.setattr("sys.argv", ["quern", "check-updates"])

    with pytest.raises(SystemExit) as exit_info:
        main.cli()

    assert exit_info.value.code == 1


def test_a_check_that_learned_nothing_does_not_report_the_old_cache(
    check_updates_cmd, isolated_update_files, capsys, monkeypatch
):
    """The quiet sibling of the stale-cache bug.

    A raised exception is not the only way to learn nothing. check_for_updates
    returns None silently when it cannot read a local version or a HEAD sha,
    and _write_update_info swallows its own write errors -- so the check can
    come back having produced no result while a record from days ago sits on
    disk. Printing that as today's answer is the same false all-clear.
    """
    from server.lifecycle import update_check
    _cache(isolated_update_files, update_available=True, latest_version="0.17.0")
    monkeypatch.setattr(update_check, "_get_local_version", lambda: None)
    monkeypatch.setattr(update_check, "_get_head_sha", lambda: None)

    code = check_updates_cmd()
    captured = capsys.readouterr()

    assert code == 1
    assert "0.17.0" not in captured.out
    assert "no result" in captured.err


def test_a_cache_this_run_wrote_is_reported(
    check_updates_cmd, isolated_update_files, capsys
):
    # The other side: a record written by this very check must be trusted, or
    # the freshness test would reject every answer and the command could never
    # report anything.
    fake_resp = MagicMock()
    fake_resp.read.return_value = json.dumps(
        {"latest_version": "0.17.0", "update_available": True}
    ).encode()
    fake_resp.__enter__ = lambda self: self
    fake_resp.__exit__ = lambda self, *a: False
    with patch("urllib.request.urlopen", return_value=fake_resp):
        code = check_updates_cmd()

    assert code == 0
    assert "Update available" in capsys.readouterr().out


def test_a_record_with_no_timestamp_is_not_trusted(
    check_updates_cmd, isolated_update_files, capsys, monkeypatch
):
    # No way to tell which run it belongs to, and guessing optimistically is
    # what reports a stale "up to date".
    from server.lifecycle import update_check
    (isolated_update_files / "update-info.json").write_text(
        json.dumps({"current_version": "0.16.1", "update_available": False})
    )
    monkeypatch.setattr(update_check, "_get_local_version", lambda: None)
    monkeypatch.setattr(update_check, "_get_head_sha", lambda: None)

    assert check_updates_cmd() == 1
    assert "Up to date" not in capsys.readouterr().out
