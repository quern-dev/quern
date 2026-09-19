"""An update finishes in a process that has loaded only the new code.

Up to 0.18.3 `quern update` swapped the source tree and then carried on in the
same interpreter, importing the rest from it. Modules loaded before the swap
were the old release's and modules loaded after were the new one's; the new
`setup.py` asked the old `server.config` for `quern_cmd`, which 0.18.3 added,
and every update to 0.18.3 crashed after the pull -- setup and the restart
never ran (#212).

Two halves, tested separately:

- the updater hands off to `python -m server update --finish`, so an update
  *from* this release never mixes code;
- this release's files cope when an *older* updater imports them the old way,
  because that is how everyone on 0.18.3 or earlier will arrive here.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import pytest

from server.lifecycle import stale_modules, updater

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def swapped(monkeypatch, tmp_path):
    """`run_update` with the fetch done and a successful swap."""
    monkeypatch.setattr(updater, "RESULT_FILE", tmp_path / "last-update.json")
    monkeypatch.setattr(updater, "_find_project_root", lambda: tmp_path / "quern")
    monkeypatch.setattr(updater, "_is_git_install", lambda _r: True)
    monkeypatch.setattr(updater, "_update_via_git", lambda _r: 0)
    return tmp_path / "quern"


class TestTheUpdateHandsOff:
    def test_the_rest_runs_in_a_new_interpreter(self, swapped, monkeypatch):
        calls = []
        monkeypatch.setattr(updater, "_spawn_finish",
                            lambda cmd, root: calls.append((cmd, root)) or 0)
        # Nothing after the swap may run here. If it does, this is the bug.
        monkeypatch.setattr(updater, "finish_update",
                            lambda **_k: pytest.fail("finished in the old process"))
        monkeypatch.setattr(updater, "_rebuild_and_restart",
                            lambda _r: pytest.fail("rebuilt in the old process"))

        assert updater.run_update() == 0

        (cmd, root), = calls
        assert cmd[1:] == ["-m", "server", "update", updater.FINISH_FLAG]
        assert root == swapped, "the child must import the tree just installed"

    def test_the_venv_interpreter_is_preferred(self, swapped, monkeypatch):
        venv_python = swapped / ".venv" / "bin" / "python"
        venv_python.parent.mkdir(parents=True)
        venv_python.write_text("")
        calls = []
        monkeypatch.setattr(updater, "_spawn_finish",
                            lambda cmd, root: calls.append(cmd) or 0)
        updater.run_update()
        assert calls[0][0] == str(venv_python)

    def test_tools_travel_with_it(self, swapped, monkeypatch):
        calls = []
        monkeypatch.setattr(updater, "_spawn_finish",
                            lambda cmd, root: calls.append(cmd) or 0)
        updater.run_update(apply_tools=True)
        assert "--tools" in calls[0]

    def test_a_failed_finish_fails_the_update(self, swapped, monkeypatch):
        monkeypatch.setattr(updater, "_spawn_finish", lambda cmd, root: 3)
        assert updater.run_update() == 1

    def test_a_child_that_never_reported_is_recorded_as_failed(self, swapped, monkeypatch):
        """The menu bar reads the record. A child that died before writing one
        would otherwise leave nothing, which reads as still running."""
        monkeypatch.setattr(updater, "_spawn_finish", lambda cmd, root: 1)
        updater.run_update()
        assert '"failed"' in updater.RESULT_FILE.read_text()

    def test_a_child_that_reported_keeps_its_own_record(self, swapped, monkeypatch):
        def child(cmd, root):
            updater._write_result(updater.FAILED, "setup failed")
            return 1
        monkeypatch.setattr(updater, "_spawn_finish", child)
        updater.run_update()
        assert "setup failed" in updater.RESULT_FILE.read_text()

    def test_an_interpreter_that_will_not_start_is_reported(self, swapped, monkeypatch, capsys):
        def boom(cmd, root):
            raise OSError("no such file")
        monkeypatch.setattr(updater, "_spawn_finish", boom)
        assert updater.run_update() == 1
        out = capsys.readouterr().out
        assert "setup" in out and "restart" in out, out

    def test_what_was_printed_is_out_before_the_child_writes(self, swapped, monkeypatch):
        """Found live: through a pipe, the parent's "Updated successfully" came
        out after the child's entire setup summary."""
        events = []

        class Recording:
            def __init__(self, name):
                self.name = name

            def write(self, text):
                events.append((self.name, "write"))
                return len(text)

            def flush(self):
                events.append((self.name, "flush"))

        monkeypatch.setattr(sys, "stdout", Recording("stdout"))
        monkeypatch.setattr(sys, "stderr", Recording("stderr"))
        monkeypatch.setattr(updater, "_spawn_finish",
                            lambda cmd, root: events.append(("child", "run")) or 0)
        updater.run_update()

        child = events.index(("child", "run"))
        for stream in ("stdout", "stderr"):
            assert (stream, "flush") in events[:child], f"{stream} not flushed first: {events}"

    def test_nothing_to_do_does_not_hand_off(self, swapped, monkeypatch):
        monkeypatch.setattr(updater, "_update_via_git", lambda _r: 2)
        monkeypatch.setattr(updater, "_report_tool_updates", lambda _a: True)
        monkeypatch.setattr(updater, "_refresh_update_check", lambda: None)
        monkeypatch.setattr(updater, "_spawn_finish",
                            lambda cmd, root: pytest.fail("handed off with nothing to finish"))
        assert updater.run_update() == 0


class TestOldUpdatersAreRecognised:
    def test_a_current_updater_is_left_alone(self):
        assert hasattr(updater, stale_modules.HANDOFF_MARKER), (
            "the marker has to name something the updater really defines"
        )
        assert not stale_modules.running_under_old_updater()
        assert stale_modules.refresh_if_stale() == []

    def test_an_updater_without_the_hand_off_is_old(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "server.lifecycle.updater",
                            types.ModuleType("server.lifecycle.updater"))
        assert stale_modules.running_under_old_updater()

    def test_a_process_with_no_updater_is_left_alone(self, monkeypatch):
        monkeypatch.delitem(sys.modules, "server.lifecycle.updater", raising=False)
        assert stale_modules.refresh_if_stale() == []

    def test_the_stale_modules_are_reloaded(self, monkeypatch):
        """The #212 shape in miniature: a config module missing a name."""
        monkeypatch.setitem(sys.modules, "server.lifecycle.updater",
                            types.ModuleType("server.lifecycle.updater"))
        config = importlib.import_module("server.config")
        real = config.quern_cmd
        monkeypatch.delattr(config, "quern_cmd")
        try:
            assert "server.config" in stale_modules.refresh_if_stale()
            assert hasattr(config, "quern_cmd")
        finally:
            config.quern_cmd = real


def _tag_exists(tag: str) -> bool:
    return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "-q", "--verify",
                           f"refs/tags/{tag}"], capture_output=True).returncode == 0


@pytest.mark.parametrize("old", ["v0.18.2", "v0.18.3"])
def test_an_old_updater_can_import_this_release(old, tmp_path):
    """The real thing: load an old release's updater, swap in this tree, and
    import what that updater imports next.

    Needs the tags. CI fetches them (`fetch-depth: 0`); a shallow local
    checkout skips rather than fakes them, because the point is the *real* old
    modules.
    """
    if not _tag_exists(old):
        # CI fetches full history for exactly this test, so a missing tag there
        # is a broken checkout, not a shallow one. Skipping would pass the
        # suite without checking the thing #212 needed checked.
        if os.environ.get("CI"):
            pytest.fail(f"{old} is missing on CI; the checkout needs fetch-depth: 0")
        pytest.skip(f"{old} is not fetched in this checkout")

    tree = tmp_path / "quern"
    tree.mkdir()
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", old],
                             capture_output=True, check=True).stdout
    subprocess.run(["tar", "-x", "-C", str(tree)], input=archive, check=True)

    # The working tree, not HEAD: this is what is about to be released.
    current = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z", "--cached", "--others",
         "--exclude-standard", "server", "pyproject.toml"],
        capture_output=True, check=True,
    ).stdout.decode().split("\0")

    script = textwrap.dedent(f"""
        import shutil, sys
        from pathlib import Path
        sys.path.insert(0, {str(tree)!r})
        import server.lifecycle.updater                     # the old updater
        import server.config                                # ...and what it loaded
        # The swap: this release's files over the old ones, as a pull would.
        root, tree = Path({str(ROOT)!r}), Path({str(tree)!r})
        shutil.rmtree(tree / "server")
        for rel in {[f for f in current if f]!r}:
            dest = tree / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / rel, dest)
        # What `_rebuild_and_restart` imports after the swap, in its order.
        from server.__main__ import _ensure_mcp_built, _ensure_python_deps
        from server.lifecycle.setup import run_setup
        from server.lifecycle.state import is_server_healthy, read_state
        print("ok")
    """)
    result = subprocess.run(
        [sys.executable, "-B", "-c", script], cwd=str(tmp_path),
        capture_output=True, text=True, timeout=60,
        env={"PATH": "/usr/bin:/bin", "QUERN_STATE_DIR": str(tmp_path / "state"),
             "HOME": str(tmp_path / "home")},
    )
    assert result.stdout.strip().endswith("ok"), (
        f"an updater from {old} could not import this release:\n{result.stderr}"
    )
