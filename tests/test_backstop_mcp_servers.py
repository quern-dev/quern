"""The install-path backstop watches Claude Desktop's config by its mcpServers section.

Claude Desktop rewrites the file while it runs -- its window and session state
live under `preferences` -- and the backstop compared mtime and size, so each
save failed whichever unrelated test happened to be running: four in one suite.
What a misbehaving test could do to the file is change quern's registration,
which is what is compared now.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.conftest import (
    _UNREADABLE,
    _WATCH_EXACTLY,
    _WATCH_MCP_SERVERS,
    _WATCHED,
    _describe,
    _mcp_servers,
)

DESKTOP = (Path.home() / "Library" / "Application Support" / "Claude"
           / "claude_desktop_config.json")


def _config(path: Path, servers: dict, preferences: dict) -> Path:
    path.write_text(json.dumps({"mcpServers": servers, "preferences": preferences}))
    return path


def test_the_app_saving_its_own_preferences_is_not_a_change(tmp_path):
    path = _config(tmp_path / "c.json", {"quern-debug": {"command": "q"}}, {"a": 1})
    before = _mcp_servers(path)
    _config(path, {"quern-debug": {"command": "q"}}, {"a": 2, "workspaces": ["x"]})
    assert _mcp_servers(path) == before


def test_a_changed_registration_is(tmp_path):
    path = _config(tmp_path / "c.json", {"quern-debug": {"command": "q"}}, {})
    before = _mcp_servers(path)
    _config(path, {}, {})
    assert _mcp_servers(path) != before, "removing quern's registration went unseen"
    _config(path, {"quern-debug": {"command": "elsewhere"}}, {})
    assert _mcp_servers(path) != before, "rewriting it went unseen"


def test_deleting_or_corrupting_the_file_is(tmp_path):
    path = _config(tmp_path / "c.json", {"quern-debug": {}}, {})
    before = _mcp_servers(path)
    path.write_text("{not json")
    assert _mcp_servers(path) != before
    path.unlink()
    assert _describe(before, _mcp_servers(path)) == "deleted"

    # A config with no registrations at all, corrupted: not the same reading
    # as the intact one, though neither has an mcpServers section.
    path.write_text(json.dumps({"preferences": {}}))
    intact = _mcp_servers(path)
    path.write_text("{not json")
    assert _mcp_servers(path) != intact


def test_key_order_is_not_a_change(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"mcpServers": {"a": {"x": 1, "y": 2}}}))
    before = _mcp_servers(path)
    path.write_text(json.dumps({"mcpServers": {"a": {"y": 2, "x": 1}}}))
    assert _mcp_servers(path) == before


def test_the_desktop_config_is_watched_this_way_and_no_other():
    """Left in the exact-match list as well, it would still fail on the app's
    own saves."""
    exact = [p for paths in _WATCH_EXACTLY.values() for p in paths]
    assert DESKTOP not in exact
    assert DESKTOP in [p for paths in _WATCH_MCP_SERVERS.values() for p in paths]
    # And wired: the fixture iterates _WATCHED, not the dicts, so a path the
    # dict lists but nothing reads -- or reads by mtime -- passed the above.
    assert {path: read for _, path, read in _WATCHED}[DESKTOP] is _mcp_servers


def test_corruption_reads_as_a_change_not_a_crash(tmp_path):
    """Not UTF-8, not an object, or unreadable: each a distinct reading, none
    an exception. An exception here errored every later test, naming none."""
    path = _config(tmp_path / "c.json", {}, {})
    intact = _mcp_servers(path)
    for damage in (b"\xff\xfe\x00 not utf-8", b"[]", b"null"):
        path.write_bytes(damage)
        reading = _mcp_servers(path)
        assert reading != intact, damage
        assert _describe(intact, reading) == "modified", damage


def test_an_unreadable_file_reads_as_unreadable(tmp_path):
    """Not "deleted": the file is there, it could not be read. (A directory,
    because patching Path.read_bytes would blind the backstop's own read of the
    real file too.)"""
    unreadable = tmp_path / "c.json"
    unreadable.mkdir()
    assert _mcp_servers(unreadable) == _UNREADABLE
