"""Tests for server.lifecycle.state — state.json management."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from server.lifecycle.state import (
    read_state,
    write_state,
    remove_state,
    update_state,
    is_server_healthy,
    STATE_FILE,
)


@pytest.fixture(autouse=True)
def tmp_state_dir(tmp_path, monkeypatch):
    """Redirect CONFIG_DIR and STATE_FILE to a temp directory."""
    state_file = tmp_path / "state.json"
    monkeypatch.setattr("server.lifecycle.state.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("server.lifecycle.state.STATE_FILE", state_file)
    return tmp_path


def test_write_then_read(tmp_state_dir):
    """write_state followed by read_state should round-trip."""
    state = {
        "pid": 12345,
        "server_port": 9100,
        "proxy_port": 9101,
        "proxy_enabled": True,
        "proxy_status": "running",
        "started_at": "2024-01-01T00:00:00Z",
        "api_key": "test-key-abc123",
        "active_devices": [],
    }
    write_state(state)
    result = read_state()
    assert result == state


def test_read_missing_file(tmp_state_dir):
    """read_state should return None when state.json doesn't exist."""
    result = read_state()
    assert result is None


def test_read_corrupt_json(tmp_state_dir):
    """read_state should return None for invalid JSON."""
    state_file = tmp_state_dir / "state.json"
    state_file.write_text("not valid json {{{")
    result = read_state()
    assert result is None


def test_read_empty_file(tmp_state_dir):
    """read_state should return None for empty file."""
    state_file = tmp_state_dir / "state.json"
    state_file.write_text("")
    result = read_state()
    assert result is None


def test_remove_state(tmp_state_dir):
    """remove_state should delete state.json."""
    write_state({"pid": 1, "server_port": 9100})
    state_file = tmp_state_dir / "state.json"
    assert state_file.exists()
    remove_state()
    assert not state_file.exists()


def test_remove_state_missing_file(tmp_state_dir):
    """remove_state should not raise when file doesn't exist."""
    remove_state()  # Should not raise


def test_update_state_merges(tmp_state_dir):
    """update_state should merge new keys into existing state."""
    write_state({"pid": 1, "server_port": 9100, "proxy_status": "running"})
    update_state(proxy_status="stopped")
    result = read_state()
    assert result["pid"] == 1
    assert result["server_port"] == 9100
    assert result["proxy_status"] == "stopped"


def test_update_state_adds_keys(tmp_state_dir):
    """update_state should add new keys to existing state."""
    write_state({"pid": 1, "server_port": 9100})
    update_state(proxy_status="running", proxy_port=9101)
    result = read_state()
    assert result["proxy_status"] == "running"
    assert result["proxy_port"] == 9101


def test_update_state_noop_when_no_file(tmp_state_dir):
    """update_state should be a no-op when state.json doesn't exist."""
    update_state(proxy_status="stopped")  # Should not raise
    assert read_state() is None


def test_is_server_healthy_success():
    """is_server_healthy should return True when health endpoint responds 200."""
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("server.lifecycle.state.urllib.request.urlopen", return_value=mock_resp):
        assert is_server_healthy(9100) is True


def test_is_server_healthy_failure():
    """is_server_healthy should return False when connection fails."""
    with patch(
        "server.lifecycle.state.urllib.request.urlopen",
        side_effect=ConnectionRefusedError,
    ):
        assert is_server_healthy(9100) is False


def test_is_server_healthy_timeout():
    """is_server_healthy should return False on timeout."""
    import urllib.error

    with patch(
        "server.lifecycle.state.urllib.request.urlopen",
        side_effect=urllib.error.URLError("timeout"),
    ):
        assert is_server_healthy(9100) is False


def test_write_creates_config_dir(tmp_path):
    """write_state should create CONFIG_DIR if it doesn't exist."""
    nested = tmp_path / "sub" / "dir"
    state_file = nested / "state.json"

    with patch("server.lifecycle.state.CONFIG_DIR", nested), \
         patch("server.lifecycle.state.STATE_FILE", state_file):
        write_state({"pid": 1, "server_port": 9100})

    assert nested.exists()
    assert state_file.exists()
