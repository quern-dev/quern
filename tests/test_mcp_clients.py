"""Which MCP clients cannot start the wrapper, for the menu bar (#214).

No node runs and no real config is read: registrations are built here, the
check is injected, and the state file goes to a temporary directory.
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime, timedelta
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

    def test_plain_node_with_its_launcher_gone_fails(self):
        """The pre-#214 shape, and the one most likely to point at an old clone."""
        [a], _ = _assess([_reg("cursor", "node", launcher="/old/mcp/dist/launcher.cjs")],
                         exists=lambda p: False)
        assert a.status == mcp_clients.LAUNCHER_GONE and a.fails

    def test_a_tilde_launcher_fails(self):
        [a], _ = _assess([_reg("cursor", NODE, launcher="~/quern/mcp/dist/launcher.cjs")])
        assert a.status == mcp_clients.TILDE and a.fails and "do not expand" in a.reason

    def test_a_shells_arguments_are_not_a_launcher(self):
        """`bash -c '…'` has `-c` where the launcher would be."""
        [a], _ = _assess([_reg("cursor", "/bin/bash", launcher="-c")], exists=lambda p: True)
        assert not a.fails

    @pytest.mark.parametrize("command", ["/bin/bash", "/usr/bin/env", "/opt/wrappers/start-quern"])
    def test_a_wrapper_is_neither_failed_nor_run(self, command):
        ran = []
        def check(path, **_):
            ran.append(path)
            return node_env.UNUSABLE, None
        [a] = mcp_clients.assess([_reg("cursor", command)], exists=lambda p: True, check=check)
        assert (a.status, a.fails, ran) == (mcp_clients.WRAPPER, False, [])

    @pytest.mark.parametrize("command", ["/x/bin/nodemon", "/x/bin/node-gyp"])
    def test_tools_named_like_node_are_not_judged(self, command):
        [a], _ = _assess([_reg("cursor", command)],
                         checks={command: (node_env.UNUSABLE, None)})
        assert a.status == mcp_clients.WRAPPER and not a.fails

    @pytest.mark.parametrize("command", ["npx", "bash"])
    def test_a_relative_wrapper_is_a_wrapper(self, command):
        [a], _ = _assess([_reg("cursor", command)])
        assert a.status == mcp_clients.WRAPPER and not a.fails

    def test_a_missing_wrapper_is_gone_whatever_its_name(self):
        [a], _ = _assess([_reg("cursor", "/bin/bash")], exists=lambda p: False)
        assert a.status == mcp_clients.GONE and a.fails

    @pytest.mark.parametrize("launcher", ["/usr/local/bin/start-quern", "~/bin/start-quern"])
    def test_an_argument_that_is_not_a_script_is_not_a_launcher(self, launcher):
        """A wrapper's own argument, which quern cannot judge."""
        [a], _ = _assess([_reg("cursor", NODE, launcher=launcher)], exists=lambda p: p == NODE)
        assert not a.fails

    @pytest.mark.parametrize("command", ["/x/bin/node", "/x/bin/nodejs", "/x/node22"])
    def test_a_node_binary_by_any_usual_name_is_judged(self, command):
        [a], _ = _assess([_reg("cursor", command)], checks={command: (node_env.TOO_OLD, "v20.0.0")})
        assert a.status == mcp_clients.TOO_OLD

    def test_a_launcher_that_is_there_is_fine(self):
        [a], _ = _assess([_reg("cursor", NODE, launcher="/q/launcher.cjs")])
        assert a.status == mcp_clients.OK

    @pytest.mark.parametrize("node, exists, check, status, words", [
        ("/gone/node", False, None, mcp_clients.GONE, "no longer exists"),
        ("~/.nvm/node", True, None, mcp_clients.TILDE, "do not expand"),
        ("/old/node", True, (node_env.TOO_OLD, "v20.20.2"), mcp_clients.TOO_OLD,
         "Node v20.20.2; Quern needs 22"),
        ("/broken/bin/node", True, (node_env.UNUSABLE, None), mcp_clients.UNUSABLE,
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

    def test_an_unreadable_config_keeps_its_last_problems(self):
        """Claude Code rewrites its 158 KB config constantly; a read mid-write
        erased a real problem, and the row vanished for ten minutes."""
        before, _ = _assess([_reg("claude-code", "/gone"),
                             _reg("claude-code", "/gone", project="/src/app"),
                             _reg("cursor", "/gone")], exists=lambda p: False)
        previous = mcp_clients.state(before, now=NOW)
        now, _ = _assess([_reg("claude-code", None, error="Expecting value"),
                          _reg("cursor", NODE)])
        data = mcp_clients.state(now, previous=previous, now=NOW)
        assert [p["client"] for p in data["problems"]] == ["Claude Code",
                                                           "Claude Code (/src/app)"]
        assert data["fix_clients"] == ["claude-code"]

    def test_a_node_that_did_not_answer_keeps_only_its_own_problem(self):
        before, _ = _assess([_reg("claude-code", NODE, project="/a"),
                             _reg("claude-code", NODE, project="/b")],
                            checks={NODE: (node_env.TOO_OLD, "v20.0.0")})
        previous = mcp_clients.state(before, now=NOW)
        now = [mcp_clients.Assessment(_reg("claude-code", NODE, project="/a"),
                                      mcp_clients.UNKNOWN),
               mcp_clients.Assessment(_reg("claude-code", NODE, project="/b"), mcp_clients.OK)]
        data = mcp_clients.state(now, previous=previous, now=NOW)
        assert [p["client"] for p in data["problems"]] == ["Claude Code (/a)"]

    def test_refresh_reads_the_last_answer_to_carry(self, tmp_path, monkeypatch):
        path = tmp_path / "mcp-clients.json"
        monkeypatch.setattr(setup, "mcp_registrations", lambda: [_reg("cursor", "/gone")])
        monkeypatch.setattr(mcp_clients.os.path, "exists", lambda p: False)
        mcp_clients.refresh(path)
        monkeypatch.setattr(setup, "mcp_registrations",
                            lambda: [_reg("cursor", None, error="mid-write")])
        mcp_clients.refresh(path)
        assert json.loads(path.read_text())["fix_clients"] == ["cursor"]

    def test_a_carried_problem_says_so_and_keeps_when_it_was_found(self):
        before, _ = _assess([_reg("cursor", "/gone")], exists=lambda p: False)
        previous = mcp_clients.state(before, now=NOW)
        now, _ = _assess([_reg("cursor", None, error="mid-write")])
        later = NOW.replace(minute=30)
        [p] = mcp_clients.state(now, previous=previous, now=later)["problems"]
        assert p["carried"] is True and p["found_at"] == NOW.isoformat()
        assert previous["problems"][0]["carried"] is False

    def test_a_carried_problem_expires(self):
        """A row must not go on describing a check nobody has made."""
        before, _ = _assess([_reg("cursor", "/gone")], exists=lambda p: False)
        previous = mcp_clients.state(before, now=NOW)
        now, _ = _assess([_reg("cursor", None, error="still unreadable")])
        data = mcp_clients.state(now, previous=previous, now=NOW + mcp_clients.CARRY_LIMIT
                                 + timedelta(seconds=1))
        assert data["problems"] == [] and data["fix_clients"] == []

    def test_a_timeout_on_a_new_node_does_not_keep_the_old_nodes_problem(self):
        """Fix in Terminal registers a new node; its first probe timing out must
        not keep "the old node no longer exists" up."""
        before, _ = _assess([_reg("cursor", "/old/node")], exists=lambda p: False)
        previous = mcp_clients.state(before, now=NOW)
        now = [mcp_clients.Assessment(_reg("cursor", "/new/node"), mcp_clients.UNKNOWN)]
        assert mcp_clients.state(now, previous=previous, now=NOW)["problems"] == []

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
        cancelled = {n.id for t in ast.walk(lifespan) if isinstance(t, ast.For)
                     and isinstance(t.iter, ast.Tuple) for n in t.iter.elts
                     if isinstance(n, ast.Name)}
        assert "mcp_clients_task" in cancelled, "the loop is not cancelled at shutdown"
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

    def test_an_update_refreshes_it_after_recording_its_result(self, monkeypatch, tmp_path):
        """The dialog opens only on an answer at least as new as the update, and
        the restarted server's own check runs before the result is written."""
        from server.lifecycle import updater

        order = []
        monkeypatch.setattr(updater, "_find_project_root", lambda: tmp_path)
        monkeypatch.setattr(updater, "_refresh_update_check", lambda: None)
        monkeypatch.setattr(updater, "_rebuild_and_restart", lambda root: [])
        monkeypatch.setattr(updater, "_report_tool_updates", lambda apply: True)
        monkeypatch.setattr(updater, "_installed_version", lambda: "0.23.0")
        monkeypatch.setattr(updater, "_write_result",
                            lambda outcome, *a, **k: order.append(("result", outcome)))
        monkeypatch.setattr(mcp_clients, "refresh", lambda *a, **k: order.append("refresh"))
        assert updater.finish_update() == 0
        assert order == [("result", updater.UPDATED), "refresh"]

    def test_doctor_writes_what_it_found(self, tmp_path, monkeypatch):
        """Otherwise a registration fixed by hand stayed on the menu while doctor
        said it was fine."""
        from server import main as server_main

        written = []
        monkeypatch.setattr(setup, "mcp_registrations", lambda: [_reg("cursor", "/gone")])
        monkeypatch.setattr(mcp_clients, "write", lambda data, path=None: written.append(data))
        server_main._report_mcp_registrations()
        assert written and written[0]["fix_clients"] == ["cursor"]

    def test_doctor_carries_a_problem_through_an_unreadable_config(self, monkeypatch):
        """Doctor writes too, and a run of it mid-rewrite erased the problem."""
        from server import main as server_main

        written = []
        found = datetime.now(UTC).isoformat()
        monkeypatch.setattr(mcp_clients, "read", lambda path=None: {"problems": [
            {"client": "Claude Code", "reason": "r", "fix": "f", "fixable": True,
             "id": "claude-code", "project": "", "found_at": found}]})
        monkeypatch.setattr(setup, "mcp_registrations",
                            lambda: [_reg("claude-code", None, error="mid-write")])
        monkeypatch.setattr(mcp_clients, "write", lambda data, path=None: written.append(data))
        server_main._report_mcp_registrations()
        assert written[0]["fix_clients"] == ["claude-code"]

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
