"""MCP clients are registered with an absolute node (#214).

`"command": "node"` let each client pick a node on its own PATH. A GUI client
finds none there, and an old session finds an old one: sessions started before
an nvm-to-fnm migration kept finding Node 20, and the wrapper's refusal reached
the user as `CONNECTION_CLOSED`, with the reason hidden.

No node runs here and no real config is read: `conftest` stubs the chooser and
the checks, and every test points `Path.home()` at a temporary directory.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

import server.__main__ as entry
from server import main as server_main
from server.lifecycle import node_env, setup

NODE = "/Users/someone/.local/share/fnm/aliases/default/bin/node"


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    (root / "mcp" / "dist").mkdir(parents=True)
    (root / "mcp" / "src").mkdir(parents=True)
    (root / "mcp" / "dist" / "launcher.cjs").write_text("#!/usr/bin/env node\n")
    monkeypatch.setattr(entry, "_find_project_root", lambda: root)
    monkeypatch.setattr(entry, "_ensure_mcp_built", lambda **_kw: True)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    return home


def _install(monkeypatch, *targets, chosen=node_env.ClientNode(NODE, "v22.22.2", "login shell")):
    monkeypatch.setattr(node_env, "node_for_clients", lambda *_a, **_kw: chosen)
    monkeypatch.setattr("sys.argv", ["quern", "mcp-install", *targets])
    return entry._cmd_mcp_install()


DESKTOP = "Library/Application Support/Claude/claude_desktop_config.json"


class TestMcpInstallPinsTheNode:
    @pytest.mark.parametrize("client,config", [
        ("claude-code", ".claude.json"), ("cursor", ".cursor/mcp.json"),
        ("claude-desktop", DESKTOP),
    ])
    def test_json_clients_get_the_absolute_node(self, home, monkeypatch, client, config):
        assert _install(monkeypatch, client) == 0
        written = json.loads((home / config).read_text())["mcpServers"]["quern-debug"]
        assert written["command"] == NODE
        assert written["args"][0].endswith("launcher.cjs")

    def test_opencode_gets_the_absolute_node(self, home, monkeypatch):
        assert _install(monkeypatch, "opencode") == 0
        cfg = json.loads((home / ".config/opencode/opencode.json").read_text())
        command = cfg["mcp"]["quern"]["command"]
        assert command[0] == NODE and command[1].endswith("launcher.cjs")

    def test_codex_runs_the_node_not_the_launchers_shebang(self, home, monkeypatch):
        """The shebang is `#!/usr/bin/env node`: a PATH lookup like the rest."""
        assert _install(monkeypatch, "codex") == 0
        cfg = tomllib.loads((home / ".codex/config.toml").read_text())["mcp_servers"]["quern"]
        assert cfg["command"] == NODE
        assert cfg["args"][0].endswith("launcher.cjs")

    def test_a_path_toml_must_escape_is_written_as_valid_toml(self, home, monkeypatch):
        odd = '/Users/some "one"/node'
        _install(monkeypatch, "codex", chosen=node_env.ClientNode(odd, "v22.0.0", "login shell"))
        cfg = tomllib.loads((home / ".codex/config.toml").read_text())
        assert cfg["mcp_servers"]["quern"]["command"] == odd

    def test_it_says_which_node_it_chose(self, home, monkeypatch, capsys):
        _install(monkeypatch, "claude-code")
        assert f"node: {NODE} (v22.22.2, from your login shell)" in capsys.readouterr().out

    def test_no_usable_node_still_registers_and_says_so_in_the_exit(
            self, home, monkeypatch, capsys):
        """Registered with plain `node` as before, but a nonzero exit: the
        likeliest outcome is a client that cannot start it."""
        assert _install(monkeypatch, "claude-code", chosen=None) == 1
        written = json.loads((home / ".claude.json").read_text())["mcpServers"]["quern-debug"]
        assert written["command"] == "node"
        assert "no Node 22+ was found" in capsys.readouterr().out

    def test_a_chooser_that_raises_does_not_stop_the_registration(self, home, monkeypatch):
        def boom(*_a, **_kw):
            raise RuntimeError("probe blew up")
        monkeypatch.setattr(node_env, "node_for_clients", boom)
        monkeypatch.setattr("sys.argv", ["quern", "mcp-install", "claude-code"])
        assert entry._cmd_mcp_install() == 1
        assert (home / ".claude.json").exists()


class TestReadingTheRegistrations:
    def _read(self):
        return setup._real_mcp_registrations()

    def test_each_client_and_the_node_it_runs(self, home, monkeypatch):
        _install(monkeypatch, "claude-code", "cursor", "opencode", "codex")
        found = {r.client: r.node for r in self._read()}
        assert found == {"claude-code": NODE, "cursor": NODE, "opencode": NODE, "codex": NODE}

    def test_nothing_registered_is_empty(self, home):
        (home / ".claude.json").write_text(json.dumps({"mcpServers": {"other": {}}}))
        assert self._read() == []

    def test_an_unreadable_config_is_not_an_absent_one(self, home):
        (home / ".claude.json").write_text("{not json")
        [reg] = self._read()
        assert reg.client == "claude-code" and reg.error and reg.node is None

    def test_codex_registered_before_214_runs_node_off_path(self, home):
        (home / ".codex").mkdir()
        (home / ".codex/config.toml").write_text(
            '[mcp_servers.quern]\ncommand = "/p/mcp/dist/launcher.cjs"\nargs = []\n')
        [reg] = self._read()
        assert (reg.client, reg.node) == ("codex", "node")


class TestDoctorReportsTheRegistrations:
    def _report(self, monkeypatch, registrations, versions=None):
        monkeypatch.setattr(setup, "mcp_registrations", lambda: registrations)
        monkeypatch.setattr(node_env, "version_outside_a_shell",
                            lambda path, **_kw: (versions or {}).get(path))
        return server_main._report_mcp_registrations()

    def test_a_pinned_node_that_runs_is_ok(self, monkeypatch, capsys, tmp_path):
        node = tmp_path / "node"
        node.write_text("")
        regs = [setup.McpRegistration("claude-code", Path("/c"), str(node))]
        assert self._report(monkeypatch, regs, {str(node): "v22.22.2"})
        assert f"✓ claude-code — v22.22.2  {node}" in capsys.readouterr().out

    def test_a_pinned_node_that_is_gone_is_said_with_the_fix(self, monkeypatch, capsys):
        regs = [setup.McpRegistration("cursor", Path("/c"), "/gone/node")]
        assert self._report(monkeypatch, regs)
        out = capsys.readouterr().out
        assert "✗ cursor — /gone/node no longer exists" in out
        assert "mcp-install cursor" in out

    def test_a_pinned_node_too_old_is_said(self, monkeypatch, capsys, tmp_path):
        node = tmp_path / "node"
        node.write_text("")
        regs = [setup.McpRegistration("claude-code", Path("/c"), str(node))]
        self._report(monkeypatch, regs)
        assert "is below Node 22" in capsys.readouterr().out

    def test_plain_node_is_flagged_as_left_to_each_client(self, monkeypatch, capsys):
        regs = [setup.McpRegistration("claude-desktop", Path("/c"), "node")]
        self._report(monkeypatch, regs)
        out = capsys.readouterr().out
        assert "! claude-desktop — `node`, found on each client's own PATH" in out

    def test_an_unreadable_config_fails_the_check(self, monkeypatch, capsys):
        """Doctor's exit says when a check could not be made."""
        regs = [setup.McpRegistration("claude-code", Path("/c"), error="bad JSON")]
        assert self._report(monkeypatch, regs) is False
        assert "could not be read" in capsys.readouterr().out
