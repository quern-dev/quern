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


class TestCurrentMeansEveryArtifactAndEveryInput:
    """"Current" was decided from one output file and one input directory."""

    def test_a_dist_missing_the_launcher_is_not_current(self, project):
        """Clients are registered on `launcher.cjs`, so a dist/ without it is
        not usable -- and `mcp-install` would write a path to a missing file."""
        dist = _ship_dist(project)
        (dist / "launcher.cjs").unlink()

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0)
            entry._ensure_mcp_built(quiet=True)
        assert run.call_count > 0, (
            "a dist/ with no launcher.cjs reported as current, so registration "
            "points at a file that is not there"
        )

    @pytest.mark.parametrize("name", ["package.json", "tsconfig.json", "package-lock.json"])
    def test_a_changed_build_input_rebuilds(self, project, name):
        """These change the output without any source file changing -- a new
        dependency, a different compile target."""
        import os
        import time

        _ship_dist(project)
        f = project / "mcp" / name
        f.write_text("{}\n")
        later = time.time() + 100
        os.utime(f, (later, later))

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0)
            entry._ensure_mcp_built(quiet=True)
        assert run.call_count > 0, f"a newer {name} did not trigger a rebuild"

    def test_an_input_written_in_the_same_second_rebuilds(self, project):
        """Filesystem timestamps are coarse. An input stamped equal to the
        output is a real outcome of a fast checkout, and calling that current is
        how a cache serves a stale build."""
        import os

        dist = _ship_dist(project)
        stamp = (dist / "index.js").stat().st_mtime
        os.utime(project / "mcp" / "src" / "index.ts", (stamp, stamp))

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0)
            entry._ensure_mcp_built(quiet=True)
        assert run.call_count > 0, (
            "an input with the same mtime as the output was treated as older"
        )


class TestTheFreshnessComparisonIsNotAccidentallyRight:
    """Three mutations to the comparison itself survived the whole suite."""

    def test_the_oldest_output_decides_not_the_newest(self, project):
        """`min` over the outputs, not `max`. With `max`, a launcher rebuilt
        long after a stale index.js reads as current, and the stale one ships."""
        import os

        dist = _ship_dist(project)
        src = project / "mcp" / "src" / "index.ts"
        stamp = src.stat().st_mtime
        # index.js older than src; launcher much newer. `max` would pass this.
        os.utime(dist / "index.js", (stamp - 100, stamp - 100))
        os.utime(dist / "launcher.cjs", (stamp + 1000, stamp + 1000))

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0)
            entry._ensure_mcp_built(quiet=True)
        assert run.call_count > 0, (
            "freshness was judged from the newest artifact, so a stale "
            "index.js beside a fresh launcher reads as current"
        )

    def test_a_directory_mtime_does_not_force_a_rebuild(self, project):
        """`rglob` yields directories too. Without the is_file() filter a
        directory's mtime — which changes whenever anything inside it is
        touched — would count as a build input and rebuild forever."""
        _ship_dist(project)
        subdir = project / "mcp" / "src" / "tools"
        subdir.mkdir()
        (subdir / "a.ts").write_text("// x\n")
        import os
        old = (project / "mcp" / "dist" / "index.js").stat().st_mtime - 500
        os.utime(subdir / "a.ts", (old, old))
        os.utime(subdir, (old, old))

        with patch("subprocess.run", side_effect=AssertionError("npm was invoked")):
            assert entry._ensure_mcp_built(quiet=True) is True


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

    @pytest.mark.parametrize("stage", ["install", "build"])
    def test_a_failing_npm_command_returns_false(self, project, stage):
        """Both npm calls, and the second was unreachable in the first attempt.

        Without a `node_modules`, `needs_install` is True and the *install* call
        short-circuits, so a test that only asserted the return value never
        exercised `npm run build` at all -- deleting either `return False` left
        the whole suite green. The install stamp is what makes the build branch
        reachable.
        """
        if stage == "build":
            nm = project / "mcp" / "node_modules"
            nm.mkdir(parents=True)
            (nm / ".install-stamp").touch()

        seen = []

        def fake_run(cmd, **_kw):
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, 1)

        with patch("subprocess.run", side_effect=fake_run):
            assert entry._ensure_mcp_built(quiet=True) is False

        reached = seen[-1][:2]
        expected = ["npm", "install"] if stage == "install" else ["npm", "run"]
        assert reached == expected, f"expected to fail at {expected}, failed at {reached}"


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
        ("claude-desktop", "Library/Application Support/Claude/claude_desktop_config.json"),
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


class TestSetupUsesTheSameDecision:
    """`_build_mcp` was a near-copy of `_ensure_mcp_built` with the same three
    defects, and `run_setup` calls it unguarded -- so `quern update` had a
    second route into the crash that took down `quern start`."""

    def test_setup_does_not_crash_when_npm_is_missing(self, project):
        from server.lifecycle.setup import CheckStatus, _build_mcp

        with patch("subprocess.run", side_effect=FileNotFoundError(2, "nope", "npm")):
            result = _build_mcp(project)

        assert result.status is CheckStatus.ERROR, (
            "a missing npm escaped _build_mcp, so `quern setup` and "
            "`quern update` end in a traceback"
        )
        assert "terminal" in (result.detail or "")

    def test_setup_skips_npm_when_the_build_is_current(self, project):
        from server.lifecycle.setup import CheckStatus, _build_mcp

        _ship_dist(project)
        with patch("subprocess.run", side_effect=AssertionError("npm was invoked")):
            result = _build_mcp(project)
        assert result.status is CheckStatus.OK

    def test_setup_requires_the_launcher_too(self, project):
        """The narrow check, in the second place it lived."""
        dist = _ship_dist(project)
        (dist / "launcher.cjs").unlink()

        from server.lifecycle.setup import _build_mcp

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0)
            _build_mcp(project)
        assert run.call_count > 0, "setup reported a dist/ with no launcher as current"


class TestEveryRegistrationShapePointsAtTheLauncher:
    """Three writers, five targets. Two were covered; re-pointing opencode,
    codex or claude-desktop at `index.js` survived the whole suite.

    The codex one matters most: it registers a bare path and relies on the
    shebang and exec bit, so it is the shape most sensitive to what the tarball
    actually preserves.
    """

    def _prepare(self, tmp_path, monkeypatch):
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

    def test_opencode_is_registered_on_the_launcher(self, tmp_path, monkeypatch):
        import json

        home = self._prepare(tmp_path, monkeypatch)
        monkeypatch.setattr("sys.argv", ["quern", "mcp-install", "opencode"])
        entry._cmd_mcp_install()

        cfg = json.loads((home / ".config" / "opencode" / "opencode.json").read_text())
        command = cfg["mcp"]["quern"]["command"]
        assert any(str(c).endswith("launcher.cjs") for c in command), command

    def test_codex_is_registered_on_the_launcher(self, tmp_path, monkeypatch):
        home = self._prepare(tmp_path, monkeypatch)
        monkeypatch.setattr("sys.argv", ["quern", "mcp-install", "codex"])
        entry._cmd_mcp_install()

        text = (home / ".codex" / "config.toml").read_text()
        assert "launcher.cjs" in text, text
        assert "dist/index.js" not in text, (
            "codex runs this path directly via its shebang, so it must be the "
            "launcher -- the version gate is the only thing standing between a "
            "wrong node and a raw syntax error"
        )
