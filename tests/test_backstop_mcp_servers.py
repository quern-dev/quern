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

from tests.conftest import _WATCH_EXACTLY, _WATCH_MCP_SERVERS, _describe, _mcp_servers

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
