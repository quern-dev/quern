"""Which MCP clients cannot start the wrapper, for the menu bar (#214).

No node runs and no real config is read: registrations are built here, the
check and the GUI lookup are injected, and the state file goes to a temporary
directory.
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from server.lifecycle import mcp_clients, node_env, setup

ROOT = Path(__file__).resolve().parent.parent
NODE = "/Users/someone/.local/share/fnm/aliases/default/bin/node"
NOW = datetime(2026, 9, 30, 9, 0, tzinfo=UTC)


def _reg(client, node, **kw):
    return setup.McpRegistration(client, Path(f"/cfg/{client}"), node, **kw)


def _assess(regs, *, checks=None, gui=True, exists=None):
    asked = []

    def gui_has_node():
        asked.append(1)
        return gui

    out = mcp_clients.assess(
        regs, check=lambda path, **_: (checks or {}).get(path, (node_env.OK, "v22.23.2")),
        gui_has_node=gui_has_node, exists=exists or (lambda p: True))
    return out, asked


class TestAssess:
    def test_a_pinned_node_that_runs_is_fine(self):
        [a], _ = _assess([_reg("cursor", NODE)])
        assert (a.status, a.fails, a.version) == (mcp_clients.OK, False, "v22.23.2")

    def test_plain_node_fails_only_a_gui_client_with_no_gui_node(self):
        (desktop, code), asked = _assess([_reg("claude-desktop", "node"),
                                          _reg("claude-code", "node")], gui=False)
        assert desktop.fails and "apps opened from the Dock find none" in desktop.reason
        assert not code.fails, "a CLI client starts in a terminal, where node is found"
        assert asked == [1]

    def test_plain_node_on_a_gui_client_is_fine_when_the_dock_finds_one(self):
        [a], _ = _assess([_reg("cursor", "node")], gui=True)
        assert a.status == mcp_clients.PLAIN and not a.fails

    def test_the_gui_is_asked_only_when_it_matters(self):
        _, asked = _assess([_reg("claude-code", "node"), _reg("cursor", NODE)])
        assert asked == []

    def test_the_gui_lookup_failing_is_not_a_finding(self):
        def boom():
            raise OSError("no")
        [a] = mcp_clients.assess([_reg("cursor", "node")], gui_has_node=boom)
        assert not a.fails

    @pytest.mark.parametrize("node, exists, check, status, words", [
        ("/gone/node", False, None, mcp_clients.GONE, "no longer exists"),
        ("~/.nvm/node", True, None, mcp_clients.TILDE, "do not expand"),
        ("/old/node", True, (node_env.TOO_OLD, "v20.20.2"), mcp_clients.TOO_OLD,
         "Node v20.20.2; Quern needs 22"),
        ("/wrap.sh", True, (node_env.UNUSABLE, None), mcp_clients.UNUSABLE,
         "did not report a Node version"),
        (None, True, None, mcp_clients.NO_COMMAND, "has no command"),
    ])
    def test_what_fails_and_why(self, node, exists, check, status, words):
        [a], _ = _assess([_reg("claude-code", node)], exists=lambda p: exists,
                         checks={node: check} if check else None)
        assert a.status == status and a.fails and words in a.reason
        assert "mcp-install claude-code" in a.fix

    @pytest.mark.parametrize("reg, status", [
        (_reg("cursor", NODE), mcp_clients.UNKNOWN),
        (_reg("cursor", None, error="bad JSON"), mcp_clients.UNREADABLE),
    ])
    def test_could_not_tell_is_not_a_failure(self, reg, status):
        """A menu warning that is sometimes wrong is one people learn to ignore."""
        [a], _ = _assess([reg], checks={NODE: (node_env.UNKNOWN, None)})
        assert a.status == status and not a.fails

    def test_a_project_entry_is_told_to_edit_itself(self):
        [a], _ = _assess([_reg("claude-code", "/gone", project="/src/app")],
                         exists=lambda p: False)
        assert a.fails and "Claude Code (/src/app)" in a.reason
        assert 'projects["/src/app"]' in a.fix


class TestTheStateFile:
    def test_only_failures_and_the_clients_mcp_install_can_fix(self):
        assessments, _ = _assess([
            _reg("cursor", "/gone"), _reg("claude-desktop", NODE),
            _reg("claude-code", "/gone", project="/src/app"), _reg("claude-code", "/gone"),
        ], exists=lambda p: p != "/gone")
        data = mcp_clients.state(assessments, now=NOW)
        assert data["checked_at"] == "2026-09-30T09:00:00+00:00"
        assert [p["client"] for p in data["problems"]] == [
            "Cursor", "Claude Code (/src/app)", "Claude Code"]
        assert data["fix_clients"] == ["cursor", "claude-code"]
        assert all(p["reason"] and p["fix"] for p in data["problems"])
        assert [p["fixable"] for p in data["problems"]] == [True, False, True]

    def test_nothing_wrong_is_an_empty_list_not_no_file(self):
        """The file is rewritten, so a fixed client's warning goes away."""
        data = mcp_clients.state(_assess([_reg("cursor", NODE)])[0], now=NOW)
        assert data["problems"] == [] and data["fix_clients"] == []

    def test_written_whole(self, tmp_path):
        path = tmp_path / "mcp-clients.json"
        mcp_clients.write({"problems": []}, path)
        assert json.loads(path.read_text()) == {"problems": []}
        assert not list(tmp_path.glob("*.tmp"))

    def test_refresh_writes_what_it_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr(setup, "mcp_registrations", lambda: [_reg("cursor", "/gone/node")])
        path = tmp_path / "mcp-clients.json"
        mcp_clients.refresh(path)
        assert json.loads(path.read_text())["fix_clients"] == ["cursor"]

    def test_a_refresh_that_fails_leaves_the_last_answer(self, tmp_path, monkeypatch):
        """"Could not check" must not read as "all fine"."""
        path = tmp_path / "mcp-clients.json"
        path.write_text(json.dumps({"problems": [{"client": "Cursor"}]}))

        def boom():
            raise RuntimeError("unreadable home")
        monkeypatch.setattr(setup, "mcp_registrations", boom)
        assert mcp_clients.refresh(path) is None
        assert json.loads(path.read_text())["problems"] == [{"client": "Cursor"}]


class TestWhoRefreshes:
    def test_the_server_start_refreshes_it_in_a_thread(self):
        """Static: the lifespan does not run under test (see test_cert_preflight).
        Deleting the call would leave everything here green and the menu bar
        reading a file nobody writes."""
        tree = ast.parse((ROOT / "server" / "main.py").read_text())
        lifespan = next(n for n in ast.walk(tree)
                        if isinstance(n, ast.AsyncFunctionDef) and n.name == "lifespan")
        calls = [n for n in ast.walk(lifespan) if isinstance(n, ast.Call)
                 and getattr(n.func, "attr", None) == "to_thread"]
        assert any(isinstance(c.args[0], ast.Attribute) and c.args[0].attr == "refresh"
                   and getattr(c.args[0].value, "id", None) == "mcp_clients"
                   for c in calls), "the lifespan does not refresh mcp-clients.json"

    def test_mcp_install_refreshes_it(self, tmp_path, monkeypatch):
        import server.__main__ as entry

        root = tmp_path / "proj"
        (root / "mcp" / "dist").mkdir(parents=True)
        (root / "mcp" / "dist" / "launcher.cjs").write_text("")
        monkeypatch.setattr(entry, "_find_project_root", lambda: root)
        monkeypatch.setattr(entry, "_ensure_mcp_built", lambda **_kw: True)
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr("pathlib.Path.home", lambda: home)
        called = []
        monkeypatch.setattr(mcp_clients, "refresh", lambda *a, **k: called.append(1))
        monkeypatch.setattr("sys.argv", ["quern", "mcp-install", "cursor"])
        entry._cmd_mcp_install()
        assert called == [1]
