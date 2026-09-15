"""Starting the server must not depend on reaching npm.

A tarball install at 0.17.0 updated to 0.18.2, and the server would not start:

    File "server/main.py", line 767, in _cmd_start
      if not _ensure_mcp_built(quiet=True):
    FileNotFoundError: [Errno 2] No such file or directory: 'npm'

`quern start` from a terminal on the same machine worked, which is the whole
diagnosis: the menubar app launches the server from a GUI context, inheriting
launchd's minimal PATH rather than a shell's. A node installed by fnm or nvm is
unreachable from there, and unreachable in a way no static PATH list can fix --
fnm's directory is named for the pid of the shell that asked for it, which is
why `quern setup` records node's path as None on such a machine.

So the fix is not to find npm. It is to stop needing it (ship a built `dist/`),
and to survive not having it (return False rather than raise). See #193.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from server import __main__ as entry


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project tree with mcp/src, and no node_modules -- the tarball shape."""
    (tmp_path / "mcp" / "src").mkdir(parents=True)
    (tmp_path / "mcp" / "src" / "index.ts").write_text("// source\n")
    (tmp_path / "mcp" / "package.json").write_text('{"version": "0.0.0"}\n')
    monkeypatch.setattr(entry, "_find_project_root", lambda: tmp_path)
    return tmp_path


def _ship_dist(project):
    """What a release tarball now contains: a built dist/, newer than src/."""
    import os
    import time

    dist = project / "mcp" / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "index.js").write_text("// built\n")
    (dist / "launcher.cjs").write_text("#!/usr/bin/env node\n")
    later = time.time() + 10
    for f in dist.iterdir():
        os.utime(f, (later, later))
    return dist


class TestAShippedBuildNeedsNoNpm:
    """The fix that matters: a tarball install never reaches for npm at all."""

    def test_a_current_dist_skips_npm_entirely(self, project):
        _ship_dist(project)
        with patch("subprocess.run", side_effect=AssertionError("npm was invoked")) as run:
            assert entry._ensure_mcp_built(quiet=True) is True
        assert run.call_count == 0, (
            "start shelled out despite a current dist/, so shipping one buys "
            "nothing on the machines that cannot reach npm"
        )

    def test_the_decision_comes_before_the_install_check(self, project):
        """`node_modules` is absent in a tarball, and it used to be checked
        first -- so a shipped dist/ would still have triggered `npm install`
        and still have crashed. The order is the fix, not just the shipping."""
        _ship_dist(project)
        assert not (project / "mcp" / "node_modules").exists()
        with patch("subprocess.run", side_effect=AssertionError("npm was invoked")):
            assert entry._ensure_mcp_built(quiet=True) is True

    def test_a_stale_dist_still_rebuilds(self, project):
        """It is still a cache. A src/ newer than dist/ has to win."""
        import os
        import time

        _ship_dist(project)
        later = time.time() + 100
        os.utime(project / "mcp" / "src" / "index.ts", (later, later))

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0)
            entry._ensure_mcp_built(quiet=True)
        assert run.call_count > 0, "a stale dist/ was served as current"


class TestAMissingNpmIsReportedNotRaised:
    """Every caller already treats a failed build as survivable. That intent
    only worked for the failures the function anticipated."""

    @pytest.mark.parametrize("failure", [
        FileNotFoundError(2, "No such file or directory", "npm"),
        PermissionError(13, "Permission denied", "npm"),
        subprocess.TimeoutExpired(cmd="npm install", timeout=120),
    ])
    def test_a_build_that_cannot_run_returns_false(self, project, failure):
        with patch("subprocess.run", side_effect=failure):
            result = entry._ensure_mcp_built(quiet=True)
        assert result is False, (
            f"{type(failure).__name__} escaped instead of being reported, so "
            "`quern start` dies rather than warning that MCP tools are stale"
        )

    def test_the_failure_names_the_thing_a_gui_launch_gets_wrong(self, project, capsys):
        """A user seeing this has a working node one terminal away, and no
        reason to guess that is the difference."""
        with patch("subprocess.run", side_effect=FileNotFoundError(2, "nope", "npm")):
            entry._ensure_mcp_built(quiet=True)
        out = capsys.readouterr().out
        assert "terminal" in out and ("fnm" in out or "nvm" in out), (
            f"the advice does not mention the actual cause: {out!r}"
        )

    def test_a_build_that_runs_and_fails_still_returns_false(self, project):
        """The case that always worked, which must keep working."""
        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 1)
            assert entry._ensure_mcp_built(quiet=True) is False


class TestClientsAreRegisteredOnTheVersionGate:
    """`launcher.cjs` is CommonJS so it parses on ancient Node, checks the
    major version, and says which binary it is running under. Registering
    `index.js` bypassed it -- a too-old Node got a raw ESM syntax error.

    Survivable while everyone built dist/ locally, since having built it proved
    a working Node. Tarballs ship it prebuilt now, so the first Node to meet
    that file may be the wrong one.
    """

    def _configs(self, tmp_path, monkeypatch):
        root = tmp_path / "proj"
        (root / "mcp" / "dist").mkdir(parents=True)
        (root / "mcp" / "src").mkdir(parents=True)
        (root / "mcp" / "dist" / "launcher.cjs").write_text("#!/usr/bin/env node\n")
        monkeypatch.setattr(entry, "_find_project_root", lambda: root)
        monkeypatch.setattr(entry, "_ensure_mcp_built", lambda **_kw: True)
        return root

    @pytest.mark.parametrize("client,config", [
        ("claude-code", ".claude.json"),
        ("cursor", ".cursor/mcp.json"),
    ])
    def test_the_registered_entry_point_is_the_launcher(
        self, tmp_path, monkeypatch, client, config,
    ):
        import json

        root = self._configs(tmp_path, monkeypatch)
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr("pathlib.Path.home", lambda: home)

        monkeypatch.setattr("sys.argv", ["quern", "mcp-install", client])
        entry._cmd_mcp_install()

        written = json.loads((home / config).read_text())
        args = written["mcpServers"]["quern-debug"]["args"]
        assert any(a.endswith("launcher.cjs") for a in args), (
            f"{client} was pointed at {args}, bypassing the Node version gate"
        )
        assert not any(a.endswith("dist/index.js") for a in args)
        assert str(root) in args[0]
