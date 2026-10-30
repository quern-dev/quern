"""A client key may issue viewer links for its own recordings (#559).

A client can already start a recording with video and download its files; a
viewer link -- view-only, one recording, expiring after it stops -- exposes
less. So links for a client's own recordings are its to issue, unless the
owner turns that off with `remote.client_viewer_links: false`. Anyone else's
recording stays the owner's.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from server import config
from server.api import recordings as api
from server.recording.recorder import RecordingError

ALICE, BOB = "client:alice", "client:bob"


def _rec(principal=ALICE, video=True, state="recording"):
    return SimpleNamespace(id="rec-1", principal=principal, state=state,
                           filters=SimpleNamespace(video=video))


def _request(rec=None):
    manager = MagicMock()
    if rec is None:
        manager.get.side_effect = RecordingError("no recording 'rec-1'")
    else:
        manager.get.return_value = rec
    viewer = SimpleNamespace(base_url="http://mac:9120", error=None,
                             grants=MagicMock(), url_for=lambda token: f"http://mac/v/{token}")
    viewer.grants.issue.return_value = "tok"
    viewer.grants.revoke.return_value = 2
    req = MagicMock()
    req.app.state.recordings = manager
    req.app.state.viewer = viewer
    return req


@pytest.fixture
def as_caller(monkeypatch):
    def set_(principal=ALICE, *, blind=False):
        caller = SimpleNamespace(session="s1", principal=principal,
                                 is_owner=principal == "owner")
        monkeypatch.setattr(api.visibility, "current_caller", lambda: caller)
        monkeypatch.setattr(api.visibility, "blind_for", lambda c=None: blind)
        return caller
    return set_


@pytest.fixture
def links(monkeypatch):
    state = {"on": True}
    monkeypatch.setattr(config, "client_viewer_links", lambda: state["on"])
    return state


async def _issue(req):
    return await api.recording_viewer_link.__wrapped__(req, "rec-1")


async def _revoke(req):
    return await api.recording_viewer_revoke.__wrapped__(req, "rec-1")


async def test_a_client_issues_a_link_for_its_own_recording(as_caller, links):
    as_caller(ALICE)
    out = await _issue(_request(_rec()))
    assert out == {"recording_id": "rec-1", "viewer_url": "http://mac/v/tok"}


async def test_anothers_recording_is_403(as_caller, links):
    as_caller(BOB)
    with pytest.raises(HTTPException) as e:
        await _issue(_request(_rec(ALICE)))
    assert e.value.status_code == 403 and "not your recording" in e.value.detail


async def test_anothers_recording_does_not_exist_to_a_blind_client(as_caller, links):
    as_caller(BOB, blind=True)
    with pytest.raises(HTTPException) as e:
        await _issue(_request(_rec(ALICE)))
    assert e.value.status_code == 404


async def test_turned_off_the_client_is_told_why(as_caller, links):
    links["on"] = False
    as_caller(ALICE)
    req = _request(_rec())
    with pytest.raises(HTTPException) as e:
        await _issue(req)
    assert e.value.status_code == 403 and "client_viewer_links" in e.value.detail
    req.app.state.viewer.grants.issue.assert_not_called()


async def test_turned_off_a_client_may_still_revoke(as_caller, links):
    """Revoking only takes access away."""
    links["on"] = False
    as_caller(ALICE)
    assert (await _revoke(_request(_rec())))["revoked"] == 2


async def test_a_client_cannot_revoke_anothers(as_caller, links):
    as_caller(BOB)
    req = _request(_rec(ALICE))
    with pytest.raises(HTTPException) as e:
        await _revoke(req)
    assert e.value.status_code == 403
    req.app.state.viewer.grants.revoke.assert_not_called()


async def test_the_owner_issues_for_anyone_whatever_the_setting(as_caller, links):
    links["on"] = False
    as_caller("owner")
    assert (await _issue(_request(_rec(ALICE))))["viewer_url"]


async def test_an_unknown_recording_is_404(as_caller, links):
    as_caller(ALICE)
    with pytest.raises(HTTPException) as e:
        await _issue(_request(None))
    assert e.value.status_code == 404


@pytest.mark.parametrize("act", [_issue, _revoke])
async def test_a_blind_client_cannot_tell_anothers_from_none(as_caller, links, act):
    """Different words for "not yours" and "no such id" would say which ids
    exist (review). The real manager's message names the server."""
    as_caller(BOB, blind=True)
    with pytest.raises(HTTPException) as theirs:
        await act(_request(_rec(ALICE)))
    req = _request()
    req.app.state.recordings.get.side_effect = RecordingError(
        "no recording 'rec-1' on this server")
    with pytest.raises(HTTPException) as none:
        await act(req)
    assert (theirs.value.status_code, theirs.value.detail) == (
        none.value.status_code, none.value.detail)


async def test_start_with_a_link_is_refused_up_front_when_off(as_caller, links):
    """Before anything starts: a refusal after the start would leave a
    recording running that the caller asked for only with a link."""
    links["on"] = False
    as_caller(ALICE)
    req = _request()
    req.app.state.recordings.start = AsyncMock()
    body = api.RecordingStartRequest(udid="SIM-1", output_dir="/tmp/x", video=True,
                                     viewer=True)
    with pytest.raises(HTTPException) as e:
        await api.start_recording.__wrapped__(req, body)
    assert e.value.status_code == 403
    req.app.state.recordings.start.assert_not_called()


# -- the setting ---------------------------------------------------------------


@pytest.mark.parametrize("remote, on", [
    (None, True),                                   # no remote section: on
    ({}, True),
    ({"client_viewer_links": True}, True),
    ({"client_viewer_links": False}, False),
    ({"client_viewer_links": "false"}, False),      # what the owner meant
    ({"client_viewer_links": 0}, False),
    ({"client_viewer_links": "yes"}, False),        # anything but true is off
    ("on", False),                                  # a remote that is not an object
])
def test_the_setting_is_on_only_unset_or_true(monkeypatch, remote, on):
    cfg = {} if remote is None else {"remote": remote}
    monkeypatch.setattr(config, "read_user_config", lambda strict=False: cfg)
    assert config.client_viewer_links() is on


def test_an_unreadable_config_turns_it_off(monkeypatch):
    """An owner who set false and then broke the file elsewhere must not find
    client links back on (review)."""
    def unreadable(strict=False):
        if strict:
            raise config.ConfigUnreadable("config.json cannot be read")
        return {}
    monkeypatch.setattr(config, "read_user_config", unreadable)
    assert config.client_viewer_links() is False


def test_the_setting_is_a_known_remote_key(monkeypatch):
    monkeypatch.setattr(config, "read_user_config",
                        lambda strict=False: {"remote": {"client_viewer_links": False}})
    assert config.remote_settings()["key_warning"] is None
