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
        monkeypatch.setattr(update_check, "read_user_config", dict)
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

    def test_force_does_not_override_the_opt_out(self, monkeypatch, tmp_path):
        """A different thing entirely. Someone who turned checking off did not
        ask, whoever is calling."""
        update_check = self._patched(monkeypatch, tmp_path, checked_recently=False)
        monkeypatch.setattr(update_check, "read_user_config",
                            lambda: {"update_check": False})
        looked = []
        monkeypatch.setattr(update_check, "_get_local_version",
                            lambda: looked.append(True) or "0.16.1")

        assert update_check.check_for_updates(force=True) is None
        assert not looked


# --- Failure discrimination -------------------------------------------------
#
# One message per failure is the point. "Could not check for updates, see the
# log" named no cause and no action, and the log it pointed at said no more.
# What each case asserts is therefore not the wording but the two properties
# the wording exists for: the raw error survives into the text a person reads,
# and the instruction is the one that actually fixes *that* failure.


def _describe(exc):
    from server.lifecycle.update_check import describe_failure
    return describe_failure(exc)


def test_dns_failure_blames_the_connection_and_keeps_the_raw_error():
    failure = _describe(urllib.error.URLError(socket.gaierror(8, "nodename nor servname")))
    assert "Could not resolve quern.dev" in failure.detail
    assert "nodename nor servname" in failure.detail
    assert "internet connection" in failure.remedy


def test_timeout_names_the_limit_it_actually_waited():
    from server.lifecycle.update_check import TIMEOUT
    failure = _describe(urllib.error.URLError(TimeoutError("timed out")))
    # Derived, not written down. A literal here would keep saying "5s" after
    # someone changed TIMEOUT, which is a lie about what just happened.
    assert f"within {TIMEOUT}s" in failure.detail


def test_http_error_tells_the_reader_it_is_not_theirs_to_fix():
    failure = _describe(
        urllib.error.HTTPError("https://quern.dev/x", 503, "Service Unavailable", {}, None)
    )
    assert "503" in failure.detail
    assert "Service Unavailable" in failure.detail
    assert "Nothing to fix locally" in failure.remedy


def test_certificate_failure_points_at_querns_own_proxy():
    import ssl
    inner = ssl.SSLCertVerificationError(
        1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed (_ssl.c:1010)"
    )
    failure = _describe(urllib.error.URLError(inner))
    # The inner error, not urllib's "<urlopen error (...)>" wrapper. The
    # sentence saying which check failed is inside the wrapper, and printing
    # the wrapper buries it in a parenthesised tuple.
    assert "CERTIFICATE_VERIFY_FAILED" in failure.detail
    assert "<urlopen error" not in failure.detail
    assert "quern stop" in failure.remedy


def test_connection_refused_blames_a_firewall_not_the_connection():
    failure = _describe(urllib.error.URLError(ConnectionRefusedError(61, "Connection refused")))
    assert "refused the connection" in failure.detail
    assert "firewall" in failure.remedy


def test_unreachable_network_falls_back_to_the_connection_advice():
    failure = _describe(urllib.error.URLError(OSError(51, "Network is unreachable")))
    assert "Could not reach quern.dev" in failure.detail
    assert "internet connection" in failure.remedy


def test_unreadable_response_is_not_the_readers_problem():
    import json as _json
    failure = _describe(_json.JSONDecodeError("Expecting value", "<html>", 0))
    assert "could not read" in failure.detail
    assert "Nothing to fix locally" in failure.remedy


def test_unrecognised_failure_still_names_itself_and_says_what_to_do():
    # The fallback is the one that has to hold up, because it is the branch
    # that runs for every failure nobody anticipated.
    failure = _describe(RuntimeError("something odd"))
    assert "RuntimeError" in failure.detail
    assert "something odd" in failure.detail
    assert "capture-env" in failure.remedy


def test_every_failure_carries_an_instruction():
    import ssl
    cases = [
        urllib.error.HTTPError("https://quern.dev/x", 500, "Boom", {}, None),
        urllib.error.URLError(ssl.SSLError("bad")),
        urllib.error.URLError(socket.gaierror(8, "no")),
        urllib.error.URLError(TimeoutError("timed out")),
        urllib.error.URLError(ConnectionRefusedError(61, "refused")),
        urllib.error.URLError(OSError(51, "unreachable")),
        ValueError("not json"),
        RuntimeError("unknown"),
    ]
    for exc in cases:
        failure = _describe(exc)
        # The pet peeve this feature exists to satisfy: no error without an
        # instruction. A remedy that is blank, or that only restates the
        # error, is the dead end all over again.
        assert failure.remedy.strip(), f"no remedy for {type(exc).__name__}"
        assert failure.detail.strip(), f"no detail for {type(exc).__name__}"


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
    assert "Could not resolve" in seen[0].detail


def test_check_for_updates_survives_a_caller_with_no_error_handler(isolated_update_files):
    # on_error is optional, and the callers that predate it pass nothing. A
    # failure must still be swallowed rather than propagating into a server
    # start path that has no business dying over an update check.
    from server.lifecycle.update_check import check_for_updates
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("boom")):
        assert check_for_updates(force=True) is None


# --- The check-updates command ----------------------------------------------
#
# The command is where the ordering matters. read_update_info() returns
# whatever the last *successful* check left behind, so consulting it first lets
# a stale "update available" answer a question the network never got to.


@pytest.fixture
def check_updates_cmd(isolated_update_files, monkeypatch):
    """The command, with server.main's view of the cache redirected too.

    server.main imports check_for_updates and read_update_info from the module
    at call time, so patching update_check's own constants is enough -- but
    only because the import is inside the function. Asserted below rather than
    assumed.
    """
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
    # The regression this ordering exists for. A cache left saying "update
    # available" by a check that succeeded yesterday must not be printed as
    # today's answer when today's check could not leave the machine.
    _cache(isolated_update_files, update_available=True, latest_version="0.17.0")
    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError(socket.gaierror(8, "nodename nor servname")),
    ):
        code = check_updates_cmd()
    captured = capsys.readouterr()
    assert code == 1
    assert "0.17.0" not in captured.out
    assert "Could not resolve quern.dev" in captured.err
    assert "internet connection" in captured.err


def test_command_sends_failures_to_stderr_not_stdout(
    check_updates_cmd, isolated_update_files, capsys
):
    # So a caller that captures stdout to read the version does not get an
    # error message where it expects an answer.
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("boom")):
        check_updates_cmd()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip()


def test_command_reports_an_update_when_the_check_succeeds(
    check_updates_cmd, isolated_update_files, capsys
):
    fake_resp = MagicMock()
    fake_resp.read.return_value = json.dumps(
        {"latest_version": "0.17.0", "update_available": True}
    ).encode()
    fake_resp.__enter__ = lambda self: self
    fake_resp.__exit__ = lambda self, *a: False
    with patch("urllib.request.urlopen", return_value=fake_resp):
        code = check_updates_cmd()
    captured = capsys.readouterr()
    assert code == 0
    assert "Update available" in captured.out
    assert captured.err == ""


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
    captured = capsys.readouterr()
    assert code == 0
    assert "Up to date" in captured.out


def test_command_does_not_call_a_silent_no_result_up_to_date(
    check_updates_cmd, isolated_update_files, capsys
):
    # No exception, no cache written. Nothing raised, so there is no failure to
    # describe -- and still nothing was learned, so "up to date" would be a
    # false all-clear.
    with patch(
        "server.lifecycle.update_check.check_for_updates", return_value=None
    ), patch("server.lifecycle.update_check.read_update_info", return_value=None):
        code = check_updates_cmd()
    captured = capsys.readouterr()
    assert code == 1
    assert "Up to date" not in captured.out
    assert "no result" in captured.err
    assert "capture-env" in captured.err
