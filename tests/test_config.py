"""Tests for server.config — user-config persistence helpers.

Focused on the channel helpers added in #41. Other config helpers in
this module are exercised indirectly by the routes that use them.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Redirect ~/.quern/config.json to a temp dir for every test."""
    monkeypatch.setattr("server.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr(
        "server.config.USER_CONFIG_FILE", tmp_path / "config.json",
    )
    return tmp_path


def test_get_update_channel_defaults_to_stable():
    from server.config import get_update_channel
    assert get_update_channel() == "stable"


def test_set_update_channel_round_trip():
    from server.config import get_update_channel, set_update_channel
    set_update_channel("beta")
    assert get_update_channel() == "beta"


def test_set_update_channel_rejects_unknown_name():
    from server.config import set_update_channel
    with pytest.raises(ValueError, match="Unknown update channel"):
        set_update_channel("nightly")


def test_unknown_channel_in_config_falls_back_to_default(isolated_config):
    """A typo in config.json should not break the daemon — falls back to
    the safe default rather than throwing."""
    import json
    (isolated_config / "config.json").write_text(
        json.dumps({"update_channel": "very_unstable"})
    )
    from server.config import get_update_channel
    assert get_update_channel() == "stable"


def test_channel_to_release_branch():
    from server.config import channel_to_release_branch
    assert channel_to_release_branch("stable") == "release/stable"
    assert channel_to_release_branch("beta") == "release/beta"
