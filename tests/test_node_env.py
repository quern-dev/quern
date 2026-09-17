"""Which `node` each place will run (#214).

Every external lookup is faked: these tests say nothing about the machine
running them. `conftest` replaces `node_env.probe` for the rest of the suite;
the real one is reached here as `node_env._real_probe`.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from server.lifecycle import node_env

ROOT = Path(__file__).resolve().parent.parent
HOME = "/Users/someone"


def _done(stdout="", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


class World:
    """A machine: which node each PATH finds, and what each shell prints."""

    def __init__(self, *, on_path=None, versions=None, login="", script="",
                 raise_for=None):
        self.on_path = on_path or {}       # dir -> node path
        self.versions = versions or {}     # node path -> "vNN..."
        self.shell_out = {"-lic": login, "-c": script}
        self.raise_for = raise_for or {}   # "-lic" -> exception
        self.calls: list[tuple[list[str], dict]] = []

    def which(self, name, path=""):
        assert name == "node"
        for d in path.split(":"):
            if d in self.on_path:
                return self.on_path[d]
        return None

    def run(self, argv, **kw):
        self.calls.append((argv, kw))
        if argv[-1] == "--version":
            v = self.versions.get(argv[0])
            return _done(v + "\n") if v else _done("", 1)
        flag = argv[1]
        if flag in self.raise_for:
            raise self.raise_for[flag]
        return _done(self.shell_out[flag])

    def probe(self, shell="/bin/zsh", path="/caller/bin", **extra):
        env = {"HOME": HOME, "SHELL": shell, "PATH": path, "USER": "someone", **extra}
        return node_env._real_probe(run=self.run, which=self.which, env=env, home=HOME)


def _line(path, version):
    return f"{node_env._MARKER}\t{path}\t{version}\n"


def _by_place(sites):
    return {s.place: s for s in sites}


class TestVersions:
    @pytest.mark.parametrize("text, major", [
        ("v22.22.2", 22), ("v20.20.2\n", 20), ("22.0.0", 22), ("v10.15.3", 10),
        ("", None), (None, None), ("node: bad option", None),
    ])
    def test_major_version(self, text, major):
        assert node_env.major_version(text) == major

    def test_the_floor_matches_the_wrapper(self):
        """Three copies of one number; the launcher's is the one that bites."""
        launcher = (ROOT / "mcp" / "src" / "launcher.cjs").read_text()
        assert f"REQUIRED_MAJOR = {node_env.MIN_NODE_MAJOR};" in launcher
        engines = json.loads((ROOT / "mcp" / "package.json").read_text())["engines"]["node"]
        assert re.fullmatch(rf">=\s*{node_env.MIN_NODE_MAJOR}", engines), engines


class TestEachPlaceIsAskedSeparately:
    def test_all_four_are_reported(self):
        node = "/opt/homebrew/bin/node"
        world = World(on_path={"/caller/bin": node, "/opt/homebrew/bin": node},
                      versions={node: "v22.1.0"},
                      login=_line(node, "v22.1.0"), script=_line(node, "v22.1.0"))
        sites = world.probe()
        assert [s.place for s in sites] == [
            "this command", "login shell", "non-interactive shell", "GUI apps",
        ]
        assert all(s.ok for s in sites)

    def test_gui_apps_do_not_see_the_callers_path(self):
        """The field shape: fine in the terminal, nothing for a GUI client."""
        fnm = f"{HOME}/.local/state/fnm_multishells/1_2/bin/node"
        world = World(on_path={"/caller/bin": fnm}, versions={fnm: "v22.1.0"},
                      login=_line(fnm, "v22.1.0"), script=_line(fnm, "v22.1.0"))
        gui = _by_place(world.probe())["GUI apps"]
        assert gui.status == node_env.MISSING

    def test_gui_apps_see_what_the_menu_bar_adds(self):
        brew = "/opt/homebrew/bin/node"
        world = World(on_path={"/opt/homebrew/bin": brew}, versions={brew: "v23.0.0"})
        gui = _by_place(world.probe())["GUI apps"]
        assert gui.ok and gui.path == brew

    def test_the_shells_start_clean(self):
        """A shell given the caller's PATH would report the caller's node,
        which is the one question already answered."""
        world = World(login=_line("", ""), script=_line("", ""))
        world.probe(path="/caller/bin:/secret/bin")
        shells = [(argv, kw) for argv, kw in world.calls if argv[-1] != "--version"]
        assert [argv[1] for argv, _ in shells] == ["-lic", "-c"]
        for _argv, kw in shells:
            assert kw["env"]["PATH"] == ":".join(node_env.GUI_PATH)
            assert kw["env"]["HOME"] == HOME
            assert kw["stdin"] is subprocess.DEVNULL
            assert kw["timeout"] == node_env.PROBE_TIMEOUT

    def test_bash_keeps_the_one_file_a_script_reads(self):
        world = World(login=_line("", ""), script=_line("", ""))
        world.probe(shell="/bin/bash", BASH_ENV=f"{HOME}/.bashenv")
        script = next(kw for argv, kw in world.calls if argv[1:2] == ["-c"])
        assert script["env"]["BASH_ENV"] == f"{HOME}/.bashenv"

    def test_too_old_is_not_ok(self):
        old = "/usr/local/bin/node"
        world = World(on_path={"/caller/bin": old}, versions={old: "v20.20.2"})
        here = _by_place(world.probe())["this command"]
        assert here.status == node_env.TOO_OLD and here.version == "v20.20.2"

    def test_a_node_that_will_not_say_its_version_is_not_ok(self):
        broken = "/caller/bin/node"
        world = World(on_path={"/caller/bin": broken}, versions={})
        assert _by_place(world.probe())["this command"].status == node_env.TOO_OLD


class TestShellOutput:
    def test_startup_noise_is_ignored(self):
        node = f"{HOME}/.nvm/versions/node/v22.3.0/bin/node"
        noisy = "Welcome!\nnvm: loaded\n" + _line(node, "v22.3.0") + "bye\n"
        world = World(login=noisy, script=_line("", ""))
        login = _by_place(world.probe())["login shell"]
        assert login.ok and login.path == node

    def test_no_node_in_a_shell_is_missing(self):
        world = World(login=_line("", ""), script=_line("", ""))
        assert _by_place(world.probe())["non-interactive shell"].status == node_env.MISSING

    def test_a_shell_that_never_answered_is_unknown_not_missing(self):
        """Reporting "no node" would send someone to install one."""
        world = World(login="zshrc: syntax error\n", script=_line("", ""))
        login = _by_place(world.probe())["login shell"]
        assert login.status == node_env.UNKNOWN
        assert "exited before answering" in login.detail

    def test_a_hung_shell_is_unknown(self):
        world = World(script=_line("", ""),
                      raise_for={"-lic": subprocess.TimeoutExpired("zsh", 10)})
        login = _by_place(world.probe())["login shell"]
        assert login.status == node_env.UNKNOWN and "did not answer" in login.detail

    def test_a_shell_that_cannot_start_is_unknown(self):
        world = World(login=_line("", ""), raise_for={"-c": OSError("no such file")})
        assert _by_place(world.probe())["non-interactive shell"].status == node_env.UNKNOWN

    @pytest.mark.parametrize("shell", ["/usr/local/bin/fish", ""])
    def test_an_unsupported_shell_is_unknown_and_not_run(self, shell):
        world = World()
        sites = _by_place(world.probe(shell=shell))
        assert sites["login shell"].status == node_env.UNKNOWN
        assert sites["non-interactive shell"].status == node_env.UNKNOWN
        assert not [argv for argv, _ in world.calls if argv[-1] != "--version"]


class TestFixes:
    def _site(self, place, status, path=None, version=None):
        return node_env.NodeSite(place, "someone", status, path, version)

    def test_an_fnm_node_that_is_too_old_gets_the_fnm_command(self):
        site = self._site("this command", node_env.TOO_OLD,
                          f"{HOME}/.local/state/fnm_multishells/9_9/bin/node", "v20.1.0")
        fix = node_env.fix_for(site, [site])
        assert "fnm install 22" in fix and "v20.1.0" in fix

    def test_a_brew_node_that_is_too_old_is_upgraded(self):
        site = self._site("GUI apps", node_env.TOO_OLD, "/opt/homebrew/bin/node", "v18.0.0")
        assert "brew upgrade node" in node_env.fix_for(site, [site])

    def test_gui_advice_does_not_suggest_a_keg_only_formula(self):
        """`brew install node@22` is not linked onto PATH, so a GUI app
        still would not find it."""
        site = self._site("GUI apps", node_env.MISSING)
        fix = node_env.fix_for(site, [site])
        assert "`brew install node`" in fix
        assert "brew install node@" not in fix
        assert "absolute path" in fix

    def test_scripts_are_pointed_at_zshenv_for_fnm(self):
        fnm = self._site("login shell", node_env.OK,
                         f"{HOME}/.local/state/fnm_multishells/1/bin/node", "v22.0.0")
        script = self._site("non-interactive shell", node_env.MISSING)
        assert ".zshenv" in node_env.fix_for(script, [fnm, script])

    def test_unknown_explains_itself(self):
        site = node_env.NodeSite("login shell", "x", node_env.UNKNOWN, detail="it hung")
        assert node_env.fix_for(site, [site]) == "it hung"


class TestDoctor:
    def test_every_place_is_listed_with_its_fix(self, monkeypatch, capsys):
        from server import main

        sites = [
            node_env.NodeSite("this command", "building", node_env.OK, "/n", "v22.0.0"),
            node_env.NodeSite("GUI apps", "the menu bar", node_env.MISSING),
        ]
        monkeypatch.setattr(node_env, "probe", lambda: sites)
        assert main._report_node() is True
        out = capsys.readouterr().out
        assert "✓ this command" in out and "✗ GUI apps" in out
        assert out.count("fix:") == 1

    def test_a_probe_that_throws_is_reported_as_unchecked(self, monkeypatch, capsys):
        from server import main

        def boom():
            raise RuntimeError("nope")

        monkeypatch.setattr(node_env, "probe", boom)
        assert main._report_node() is False
        assert "could not be checked" in capsys.readouterr().out


class TestUpdateWarnsAndContinues:
    def test_an_old_node_is_named_but_the_update_goes_ahead(self, monkeypatch, tmp_path, capsys):
        from server.lifecycle import updater

        monkeypatch.setattr(updater, "RESULT_FILE", tmp_path / "r.json")
        monkeypatch.setattr(updater, "_find_project_root", lambda: tmp_path)
        monkeypatch.setattr(updater, "_is_git_install", lambda _r: True)
        pulled = []
        monkeypatch.setattr(updater, "_update_via_git", lambda _r: pulled.append(1) or 2)
        monkeypatch.setattr(updater, "_report_tool_updates", lambda _a: True)
        monkeypatch.setattr(updater, "_refresh_update_check", lambda: None)
        monkeypatch.setattr(node_env, "here", lambda: node_env.NodeSite(
            "this command", "building", node_env.TOO_OLD, "/usr/local/bin/node", "v20.20.2"))

        assert updater.run_update() == 0
        assert pulled, "the update was refused over Node"
        out = capsys.readouterr().out
        assert "Warning" in out and "v20.20.2" in out

    def test_a_good_node_says_nothing(self, monkeypatch, tmp_path, capsys):
        from server.lifecycle import updater

        monkeypatch.setattr(updater, "RESULT_FILE", tmp_path / "r.json")
        monkeypatch.setattr(updater, "_find_project_root", lambda: tmp_path)
        monkeypatch.setattr(updater, "_is_git_install", lambda _r: True)
        monkeypatch.setattr(updater, "_update_via_git", lambda _r: 2)
        monkeypatch.setattr(updater, "_report_tool_updates", lambda _a: True)
        monkeypatch.setattr(updater, "_refresh_update_check", lambda: None)
        monkeypatch.setattr(node_env, "here", lambda: node_env.NodeSite(
            "this command", "building", node_env.OK, "/n", "v22.0.0"))

        updater.run_update()
        assert "Warning" not in capsys.readouterr().out
