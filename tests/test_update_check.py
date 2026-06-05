"""Tests for server.lifecycle.update_check — persistence + read API.

Doesn't exercise the network round-trip (quern.dev). Patches the urllib
call so test runs are deterministic.
"""

from __future__ import annotations

import json
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
