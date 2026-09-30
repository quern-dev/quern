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


def _assess(regs, *, checks=None, exists=None):
    out = mcp_clients.assess(
        regs, check=lambda path, **_: (checks or {}).get(path, (node_env.OK, "v22.23.2")),
        exists=exists or (lambda p: True))
    return out, None


class TestAssess:
    def test_a_pinned_node_that_runs_is_fine(self):
        [a], _ = _assess([_reg("cursor", NODE)])
        assert (a.status, a.fails, a.version) == (mcp_clients.OK, False, "v22.23.2")

    @pytest.mark.parametrize("client", ["claude-desktop", "cursor", "claude-code"])
    def test_plain_node_is_never_a_failure(self, client):
        """Each client resolves it its own way: Claude Desktop reads the shell's
        PATH and adds every nvm version (measured from its log), so flagging a
        Dock app on plain `node` was a false alarm on a machine where it worked."""
        [a], _ = _assess([_reg(client, "node")])
        assert a.status == mcp_clients.PLAIN and not a.fails and "mcp-install" in a.fix

    def test_a_launcher_that_is_gone_fails(self):
        """Quern moved, reinstalled, or the clone it was registered from is gone."""
        [a], _ = _assess([_reg("cursor", NODE, launcher="/old/quern/mcp/dist/launcher.cjs")],
                         exists=lambda p: p == NODE)
        assert a.status == mcp_clients.LAUNCHER_GONE and a.fails
        assert "start Quern from /old/quern/mcp/dist/launcher.cjs" in a.reason

    def test_a_relative_launcher_is_not_judged(self):
        """Relative to the client's directory, not ours: checking it from here
        would call a working registration broken."""
        [a], _ = _assess([_reg("cursor", NODE, launcher="mcp/dist/launcher.cjs")],
                         exists=lambda p: p == NODE)
        assert a.status == mcp_clients.OK and not a.fails

    def test_a_launcher_that_is_there_is_fine(self):
        [a], _ = _assess([_reg("cursor", NODE, launcher="/q/launcher.cjs")])
        assert a.status == mcp_clients.OK

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

    def test_a_project_entry_alone_has_nothing_for_mcp_install(self):
        assessments, _ = _assess([_reg("claude-code", "/gone", project="/src/app")],
                                 exists=lambda p: False)
        data = mcp_clients.state(assessments, now=NOW)
        assert len(data["problems"]) == 1 and data["fix_clients"] == []

    def test_nothing_wrong_is_an_empty_list_not_no_file(self):
        """The file is rewritten, so a fixed client's warning goes away."""
        data = mcp_clients.state(_assess([_reg("cursor", NODE)])[0], now=NOW)
        assert data["problems"] == [] and data["fix_clients"] == []

    def test_written_whole(self, tmp_path):
        path = tmp_path / "mcp-clients.json"
        mcp_clients.write({"problems": []}, path)
        assert json.loads(path.read_text()) == {"problems": []}
        assert not list(tmp_path.glob("*.tmp"))

    def test_a_failed_write_leaves_no_temp_file(self, tmp_path, monkeypatch):
        """Per-process names, and cleaned up: a shared one let the server and an
        mcp-install from Fix in Terminal truncate each other's file."""
        path = tmp_path / "mcp-clients.json"

        def fail(self, target):
            raise OSError(28, "No space left on device")
        monkeypatch.setattr(Path, "replace", fail)
        with pytest.raises(OSError):
            mcp_clients.write({"problems": []}, path)
        assert not list(tmp_path.iterdir())

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
        started = [n for n in ast.walk(lifespan) if isinstance(n, ast.Call)
                   and getattr(n.func, "id", None) == "_refresh_mcp_clients_periodically"]
        assert started, "the lifespan does not start the mcp-clients.json refresh"
        loop = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)
                    and n.name == "_refresh_mcp_clients_periodically")
        assert any(isinstance(c, ast.Call) and getattr(c.func, "attr", None) == "to_thread"
                   and getattr(c.args[0], "attr", None) == "refresh"
                   for c in ast.walk(loop)), "the loop does not refresh in a thread"

    def test_the_server_checks_again_periodically(self, monkeypatch):
        """A server runs for days; a node removed or a config fixed meanwhile
        should not wait for the next start."""
        import asyncio

        import server.main as server_main

        calls = []
        monkeypatch.setattr(mcp_clients, "refresh", lambda *a, **k: calls.append(1))

        async def sleep(seconds):
            assert seconds == server_main.MCP_CLIENTS_INTERVAL
            if len(calls) >= 2:
                raise asyncio.CancelledError
        monkeypatch.setattr(server_main.asyncio, "sleep", sleep)
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(server_main._refresh_mcp_clients_periodically())
        assert len(calls) == 2

    def test_an_update_refreshes_it_after_recording_its_result(self, monkeypatch):
        """The dialog opens only on an answer at least as new as the update, and
        the restarted server's own check runs before the result is written."""
        from server.lifecycle import updater

        src = (ROOT / "server" / "lifecycle" / "updater.py").read_text()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "finish_update")
        body = ast.get_source_segment(src, fn)
        written = body.index('_write_result(UPDATED, "update applied"')
        assert "mcp_clients.refresh()" in body[written:], \
            "finish_update does not refresh mcp-clients.json after writing its result"
        _ = updater

    def test_doctor_writes_what_it_found(self, tmp_path, monkeypatch):
        """Otherwise a registration fixed by hand stayed on the menu while doctor
        said it was fine."""
        from server import main as server_main

        written = []
        monkeypatch.setattr(setup, "mcp_registrations", lambda: [_reg("cursor", "/gone")])
        monkeypatch.setattr(mcp_clients, "write", lambda data, path=None: written.append(data))
        server_main._report_mcp_registrations()
        assert written and written[0]["fix_clients"] == ["cursor"]

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
