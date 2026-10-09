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
        # Tolerant of spacing: this is JS source, and reformatting it should
        # not fail a check about the *number*.
        assert re.search(rf"REQUIRED_MAJOR\s*=\s*{node_env.MIN_NODE_MAJOR}\b", launcher)
        engines = json.loads((ROOT / "mcp" / "package.json").read_text())["engines"]["node"]
        assert re.fullmatch(rf">=\s*{node_env.MIN_NODE_MAJOR}", engines), engines


class TestEachPlaceIsAskedSeparately:
    def test_every_place_is_reported(self):
        node = "/usr/bin/node"
        world = World(on_path={"/caller/bin": node, "/usr/bin": node},
                      versions={node: "v22.1.0"},
                      login=_line(node, "v22.1.0"), script=_line(node, "v22.1.0"))
        sites = world.probe()
        assert [s.place for s in sites] == [
            "this command", "login shell", "non-interactive shell",
            "GUI apps", "the Quern app",
        ]
        assert all(s.ok for s in sites)

    def test_a_homebrew_node_is_invisible_to_dock_apps_but_not_to_the_quern_app(self):
        """These were one row, and the merged row was flattering: a Homebrew
        node made "GUI apps" green while a Dock-launched MCP client still could
        not see it, and the advice then "fixed" a row that was never broken."""
        brew = "/opt/homebrew/bin/node"
        world = World(on_path={"/opt/homebrew/bin": brew}, versions={brew: "v22.1.0"})
        sites = _by_place(world.probe())
        assert sites["GUI apps"].status == node_env.MISSING
        assert sites["the Quern app"].ok
        fix = node_env.fix_for(sites["GUI apps"], world.probe())
        assert "brew install node" not in fix, "advice that would change nothing here"
        assert "absolute path" in fix

    def test_the_quern_app_sees_its_own_extra_directories(self):
        """`~/.local/bin` is templated with the home directory; nothing
        asserted the expansion, so it could break silently."""
        local = f"{HOME}/.local/bin/node"
        world = World(on_path={f"{HOME}/.local/bin": local}, versions={local: "v22.1.0"})
        sites = _by_place(world.probe())
        assert sites["the Quern app"].ok and sites["the Quern app"].path == local
        assert sites["GUI apps"].status == node_env.MISSING

    @pytest.mark.parametrize("fnm_dir", [".local/share/fnm", "Library/Application Support/fnm",
                                         ".fnm"])
    def test_the_quern_app_sees_fnms_default_node(self, fnm_dir):
        """The machine #447 was found on: fnm's default alias is where every
        MCP client's node is, and the app's search path did not include it."""
        node = f"{HOME}/{fnm_dir}/aliases/default/bin/node"
        world = World(on_path={f"{HOME}/{fnm_dir}/aliases/default/bin": node},
                      versions={node: "v22.23.2"})
        sites = _by_place(world.probe())
        assert sites["the Quern app"].ok and sites["the Quern app"].path == node
        assert sites["GUI apps"].status == node_env.MISSING, "launchd's PATH has no fnm"

    def test_gui_apps_do_not_see_the_callers_path(self):
        """The field shape: fine in the terminal, nothing for a GUI client."""
        fnm = f"{HOME}/.local/state/fnm_multishells/1_2/bin/node"
        world = World(on_path={"/caller/bin": fnm}, versions={fnm: "v22.1.0"},
                      login=_line(fnm, "v22.1.0"), script=_line(fnm, "v22.1.0"))
        gui = _by_place(world.probe())["GUI apps"]
        assert gui.status == node_env.MISSING

    def test_a_node_in_launchds_own_path_is_seen_by_dock_apps(self):
        system = "/usr/local/bin/node"
        world = World(on_path={"/usr/bin": system}, versions={system: "v23.0.0"})
        gui = _by_place(world.probe())["GUI apps"]
        assert gui.ok and gui.path == system

    def test_the_shells_start_clean(self):
        """A shell given the caller's PATH would report the caller's node,
        which is the one question already answered."""
        world = World(login=_line("", ""), script=_line("", ""))
        world.probe(path="/caller/bin:/secret/bin")
        shells = [(argv, kw) for argv, kw in world.calls if argv[-1] != "--version"]
        # Order-free: the places are probed concurrently, and CI caught the
        # first version of this assertion depending on which thread won.
        assert sorted(argv[1] for argv, _ in shells) == ["-c", "-lic"]
        for _argv, kw in shells:
            assert kw["env"]["PATH"] == ":".join(node_env.GUI_PATH)
            assert kw["env"]["HOME"] == HOME
            assert kw["stdin"] is subprocess.DEVNULL
            assert kw["timeout"] == node_env.PROBE_TIMEOUT

    def test_zdotdir_and_the_locale_travel_with_the_shell(self):
        """Without ZDOTDIR a user whose config lives in ~/.config/zsh gets a
        shell that reads nothing, is reported as having no node, and is told to
        edit a file their shell never opens."""
        world = World(login=_line("", ""), script=_line("", ""))
        world.probe(ZDOTDIR=f"{HOME}/.config/zsh", LANG="en_GB.UTF-8")
        for _argv, kw in [c for c in world.calls if c[0][-1] != "--version"]:
            assert kw["env"]["ZDOTDIR"] == f"{HOME}/.config/zsh"
            assert kw["env"]["LANG"] == "en_GB.UTF-8"

    def test_the_version_call_is_bounded_and_cannot_be_asked_a_question(self):
        node = "/caller/bin/node"
        world = World(on_path={"/caller/bin": node}, versions={node: "v22.1.0"})
        world.probe()
        version_calls = [kw for argv, kw in world.calls if argv[-1] == "--version"]
        assert version_calls, "no version was read"
        for kw in version_calls:
            assert kw["stdin"] is subprocess.DEVNULL
            assert kw["timeout"] == node_env.PROBE_TIMEOUT

    def test_the_timeout_is_long_enough_to_be_a_timeout(self):
        """Asserting `timeout == PROBE_TIMEOUT` passes for 0.001 too, which
        would make every probe on a slow machine read as "could not ask"."""
        assert 5 <= node_env.PROBE_TIMEOUT <= 30

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
        here = _by_place(world.probe())["this command"]
        # Its own status: "too old" would advise upgrading a node that does
        # not run at all. Found in review.
        assert here.status == node_env.UNUSABLE
        fix = node_env.fix_for(here, [here])
        assert "did not run" in fix and "below" not in fix


class TestTheProbesRunTogether:
    def test_four_slow_places_take_the_time_of_one(self):
        """Up to 10s each, one after another, was a 40s worst case in front of
        setup and doctor. Found in review."""
        import threading
        import time

        node = "/opt/homebrew/bin/node"
        world = World(on_path={"/caller/bin": node, "/opt/homebrew/bin": node},
                      versions={node: "v22.1.0"},
                      login=_line(node, "v22.1.0"), script=_line(node, "v22.1.0"))
        inner = world.run
        active = {"now": 0, "most": 0}
        lock = threading.Lock()

        def slow(argv, **kw):
            with lock:
                active["now"] += 1
                active["most"] = max(active["most"], active["now"])
            time.sleep(0.2)
            with lock:
                active["now"] -= 1
            return inner(argv, **kw)

        world.run = slow
        started = time.monotonic()
        sites = world.probe()
        assert [s.place for s in sites] == [
            "this command", "login shell", "non-interactive shell",
            "GUI apps", "the Quern app",
        ], "the order is part of the report"
        assert active["most"] >= 3, f"only {active['most']} ran at once"
        # Generous: sequential would be ~1.2s for the shells alone. A tight
        # bound here is a CI flake, not a stronger assertion.
        assert time.monotonic() - started < 1.0


class TestTheRealRunner:
    """`run_bounded` is the default; the fakes above never exercise it."""

    def test_a_timeout_takes_the_whole_process_group_with_it(self, tmp_path):
        """subprocess's own timeout kills the direct child only, and real
        startup files spawn daemons (gitstatusd, atuin, direnv)."""
        import os
        import signal
        import subprocess as sp
        import time

        marker = tmp_path / "grandchild-alive"
        script = (f"sh -c 'while :; do touch {marker}; sleep 0.05; done' & "
                  "sleep 30")
        with pytest.raises(sp.TimeoutExpired):
            node_env.run_bounded(["/bin/sh", "-c", script], timeout=0.6)
        time.sleep(0.4)
        marker.unlink(missing_ok=True)
        time.sleep(0.4)
        assert not marker.exists(), "a grandchild outlived the probe"
        _ = os, signal

    def test_a_daemon_that_outlives_its_shell_is_killed_too(self, tmp_path):
        """The shape that made `getpgid` fail: the shell exits at once, a
        background descendant keeps the pipe open, and `communicate` waits."""
        import subprocess as sp
        import time

        marker = tmp_path / "daemon-alive"
        script = (f"sh -c 'while :; do touch {marker}; sleep 0.05; done' & exit 0")
        with pytest.raises(sp.TimeoutExpired):
            node_env.run_bounded(["/bin/sh", "-c", script], timeout=0.6)
        time.sleep(0.3)
        marker.unlink(missing_ok=True)
        time.sleep(0.4)
        assert not marker.exists(), "a daemon outlived the probe"

    def test_output_that_is_not_utf8_is_read_rather_than_raising(self):
        result = node_env.run_bounded(
            ["/bin/sh", "-c", "printf 'a\\377b\\n'"], timeout=5)
        assert result.returncode == 0
        assert "a" in result.stdout and "b" in result.stdout


    def test_it_runs_in_the_directory_it_is_given(self, tmp_path):
        """What a directory-following version manager answers depends on it."""
        result = node_env.run_bounded(["/bin/sh", "-c", "pwd -P"], timeout=5, cwd=str(tmp_path))
        assert result.stdout.strip() == str(tmp_path.resolve())


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

    def test_a_startup_line_without_a_newline_cannot_swallow_the_answer(self):
        """p10k's instant prompt, `echo -n`, a spinner: the marker was appended
        to their line and a working Node 22 read as "could not ask".

        A real /bin/sh, because the fix is in the probe script rather than in
        the parser -- faking the output here would be testing the fake.
        """
        site = node_env._in_shell(
            "login shell", "x",
            ["/bin/sh", "-c", "printf 'instant prompt'; " + node_env._SHELL_PROBE],
            {"PATH": "/usr/bin:/bin", "HOME": HOME}, node_env.run_bounded,
        )
        assert site.status != node_env.UNKNOWN, site.detail
    def test_a_shell_that_writes_undecodable_bytes_does_not_raise(self):
        """`text=True` decodes strictly, so one stray byte raised
        UnicodeDecodeError out of probe, through check_node, out of run_setup
        -- which `quern update` calls after the pull."""
        def boom(argv, **kw):
            if argv[-1] == "--version":
                return _done("v22.1.0")
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

        world = World(login=_line("", ""), script=_line("", ""))
        world.run = boom
        sites = _by_place(world.probe())
        assert sites["login shell"].status == node_env.UNKNOWN
        assert "could not start the shell" in sites["login shell"].detail

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
    def test_an_unsupported_shell_is_skipped_and_not_run(self, shell):
        """Skipped, not unknown: it is a fact about the machine, and doctor
        fails on unknown."""
        world = World()
        sites = _by_place(world.probe(shell=shell))
        assert sites["login shell"].status == node_env.SKIPPED
        assert sites["non-interactive shell"].status == node_env.SKIPPED
        assert "unsupported shell" in sites["login shell"].detail
        assert not [argv for argv, _ in world.calls if argv[-1] != "--version"]

    def test_the_shell_places_record_which_shell(self):
        world = World(login=_line("", ""), script=_line("", ""))
        sites = _by_place(world.probe(shell="/bin/bash"))
        assert sites["login shell"].shell == "bash"
        assert sites["non-interactive shell"].shell == "bash"


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

    def test_the_quern_app_advice_does_not_suggest_a_keg_only_formula(self):
        """`brew install node@22` is not linked onto PATH, so even the app's
        own extra directories would not find it."""
        site = self._site("the Quern app", node_env.MISSING)
        fix = node_env.fix_for(site, [site])
        assert "`brew install node`" in fix
        assert "brew install node@" not in fix

    def test_with_fnm_the_quern_app_advice_is_a_default_not_a_second_node(self):
        """fnm's default alias is on the app's search path (#447), so an fnm
        user missing there has no default set; installing Homebrew's node
        alongside would be the wrong fix."""
        app = self._site("the Quern app", node_env.MISSING)
        shell = self._site("login shell", node_env.OK,
                           f"{HOME}/.local/state/fnm_multishells/9_9/bin/node", "v22.23.2")
        fix = node_env.fix_for(app, [app, shell])
        # Installed first: `fnm default 22` alone fails when 22 is not.
        assert "fnm install 22 && fnm default 22" in fix
        assert "brew install node" not in fix
        assert "FNM_DIR" in fix, "says when the default is somewhere it cannot look"

    def test_the_quern_app_keeps_a_current_homebrew_node_over_an_old_fnm_default(self):
        """fnm's directories come after Homebrew's: ahead, an fnm default of
        20 turned a working Homebrew node into a too-old one (#447 review)."""
        brew = "/opt/homebrew/bin/node"
        fnm = f"{HOME}/.local/share/fnm/aliases/default/bin/node"
        world = World(on_path={"/opt/homebrew/bin": brew,
                               f"{HOME}/.local/share/fnm/aliases/default/bin": fnm},
                      versions={brew: "v25.0.0", fnm: "v20.19.4"})
        app = _by_place(world.probe())["the Quern app"]
        assert app.ok and app.path == brew

    def test_the_menu_bar_search_path_matches_the_app(self):
        """Doctor's "the Quern app" row is only true if it searches what the
        app searches. `QuernCLI.searchPath` and `MENUBAR_EXTRA_PATH` are two
        copies of one list, and nothing kept them in step until fnm's default
        alias was added to both (#447)."""
        src = (ROOT / "macos/QuernMenuBar/Sources/QuernCLI.swift").read_text()
        expr = re.search(r"static func searchPath\(home: String\) -> \[String\] \{\s*"
                         r"let extra = (.*?)\n\s*let current", src, re.S).group(1)
        fnm_dirs = re.findall(r'"([^"]*)"',
                              re.search(r"static let fnmDataDirs = \[(.*?)\]", src).group(1))
        assert fnm_dirs, "fnmDataDirs not found"
        swift: list[str] = []
        for part in re.split(r"\n\s*\+ ", expr.strip()):
            if part.startswith("fnmDataDirs.map"):
                template = re.search(r'"(.*?)"', part).group(1)
                swift += [template.replace(r"\($0)", d) for d in fnm_dirs]
            else:
                # Only a literal list is read. Anything else -- a named
                # constant, a function call -- would contribute no strings and
                # pass while the lists differed, so it fails here instead.
                assert re.fullmatch(r'\[("[^"]*"(,\s*)?)+\]', part), \
                    f"cannot read this part of searchPath; teach the test: {part}"
                swift += re.findall(r'"([^"]*)"', part)
        # Doctor assumes the app's own directories come before launchd's PATH.
        assert re.search(r"return extra \+ current", src), "extra must come first"
        swift = [d.replace(r"\(home)", "{home}") for d in swift]
        # The app also names /usr/bin and /bin, which doctor takes from GUI_PATH.
        tail = [d for d in swift if d in node_env.GUI_PATH]
        assert swift[:len(swift) - len(tail)] == list(node_env.MENUBAR_EXTRA_PATH)
        assert tail and swift[-len(tail):] == tail, "launchd's directories come last"

    def test_dock_advice_names_only_fixes_that_work_there(self):
        """launchd's PATH has no Homebrew in it, so `brew install node`
        changes nothing for a Dock-launched client."""
        site = self._site("GUI apps", node_env.MISSING)
        fix = node_env.fix_for(site, [site])
        assert "absolute path" in fix and "launchctl config user path" in fix
        assert "brew install" not in fix

    def test_with_no_manager_recognised_the_default_is_linked_node(self):
        site = self._site("this command", node_env.MISSING)
        fix = node_env.fix_for(site, [site])
        assert "brew install node" in fix
        assert "brew install node@" not in fix

    def test_scripts_are_pointed_at_zshenv_for_fnm(self):
        fnm = self._site("login shell", node_env.OK,
                         f"{HOME}/.local/state/fnm_multishells/1/bin/node", "v22.0.0")
        script = self._site("non-interactive shell", node_env.MISSING)
        assert ".zshenv" in node_env.fix_for(script, [fnm, script])

    def test_bash_scripts_are_pointed_at_bash_env(self):
        """zsh's file would be read by nothing. Found in review."""
        fnm = self._site("login shell", node_env.OK,
                         f"{HOME}/.local/state/fnm_multishells/1/bin/node", "v22.0.0")
        script = node_env.NodeSite("non-interactive shell", "x", node_env.MISSING, shell="bash")
        fix = node_env.fix_for(script, [fnm, script])
        assert "BASH_ENV" in fix and ".zshenv" not in fix

    def test_bash_mise_is_activated_for_bash(self):
        mise = self._site("login shell", node_env.OK,
                          f"{HOME}/.local/share/mise/shims/node", "v22.0.0")
        script = node_env.NodeSite("non-interactive shell", "x", node_env.MISSING, shell="bash")
        assert "mise activate bash --shims" in node_env.fix_for(script, [mise, script])

    @pytest.mark.parametrize("shell, file", [("zsh", "~/.zshrc"), ("bash", "~/.bash_profile")])
    def test_a_login_shell_missing_an_installed_node_is_told_to_load_it(self, shell, file):
        nvm = self._site("this command", node_env.OK,
                         f"{HOME}/.nvm/versions/node/v22.0.0/bin/node", "v22.0.0")
        login = node_env.NodeSite("login shell", "x", node_env.MISSING, shell=shell)
        fix = node_env.fix_for(login, [nvm, login])
        assert file in fix and "nvm" in fix and "No node found" not in fix

    def test_unknown_explains_itself(self):
        site = node_env.NodeSite("login shell", "x", node_env.UNKNOWN, detail="it hung")
        assert node_env.fix_for(site, [site]) == "it hung"


class TestInstallers:
    """#219's matrix, as far as recognising and advising goes."""

    @pytest.mark.parametrize("path, real, n_dir, manager", [
        (f"{HOME}/.local/state/fnm_multishells/1_2/bin/node",
         f"{HOME}/.local/share/fnm/node-versions/v22.1.0/installation/bin/node", False, "fnm"),
        (f"{HOME}/.nvm/versions/node/v22.1.0/bin/node", None, False, "nvm"),
        (f"{HOME}/.volta/bin/node", None, False, "volta"),
        (f"{HOME}/.asdf/shims/node", None, False, "asdf"),
        (f"{HOME}/.local/share/mise/shims/node", None, False, "mise"),
        (f"{HOME}/.local/share/mise/installs/node/22.1.0/bin/node", None, False, "mise"),
        (f"{HOME}/.nodenv/shims/node", None, False, "nodenv"),
        (f"{HOME}/Library/pnpm/node", None, False, "pnpm"),
        (f"{HOME}/.nix-profile/bin/node", "/nix/store/abc-nodejs-22/bin/node", False, "nix"),
        ("/run/current-system/sw/bin/node", None, False, "nix"),
        ("/opt/local/bin/node", None, False, "macports"),
        ("/opt/homebrew/bin/node", "/opt/homebrew/Cellar/node/23.0.0/bin/node", False, "brew"),
        # Intel Homebrew: only the resolved path says so.
        ("/usr/local/bin/node", "/usr/local/Cellar/node/23.0.0/bin/node", False, "brew"),
        ("/opt/homebrew/opt/node@18/bin/node", None, False, "brew-keg"),
        # The same path, from two other installers.
        ("/usr/local/bin/node", None, True, "n"),
        ("/usr/local/bin/node", None, False, "installer"),
        ("/some/where/else/node", None, False, None),
    ])
    def test_who_installed_it(self, path, real, n_dir, manager):
        got = node_env.manager_of(
            path, resolve=lambda p: real or p,
            is_dir=lambda d: n_dir and d == node_env.N_PREFIX,
        )
        assert got == manager

    @pytest.mark.parametrize("manager, fragment", [
        ("fnm", "fnm install 22"), ("nvm", "nvm install 22"), ("volta", "volta install node@22"),
        ("mise", "mise use -g node@22"), ("asdf", "asdf install nodejs latest:22"),
        ("nodenv", "nodenv install"), ("n", "n 22"), ("installer", "nodejs.org"),
        ("pnpm", "pnpm env use --global 22"), ("macports", "port install nodejs22"),
        ("nix", "nodejs_22"), ("brew", "brew upgrade node"), ("brew-keg", "brew install node"),
        (None, "brew install node"),
    ])
    def test_each_gets_its_own_upgrade(self, manager, fragment):
        assert fragment in node_env.upgrade_command(manager)

    def test_a_too_old_node_is_upgraded_with_its_own_installer(self, monkeypatch):
        """Not whichever place happens to be listed first."""
        monkeypatch.setattr(node_env, "manager_of", lambda p, **_k: {
            "/fnm/node": "fnm", "/usr/local/bin/node": "installer"}.get(p))
        fnm = node_env.NodeSite("this command", "x", node_env.OK, "/fnm/node", "v22.0.0")
        old = node_env.NodeSite("GUI apps", "x", node_env.TOO_OLD,
                                "/usr/local/bin/node", "v18.0.0")
        fix = node_env.fix_for(old, [fnm, old])
        assert "nodejs.org" in fix and "fnm" not in fix

    def test_mise_scripts_are_told_about_shims(self):
        mise = node_env.NodeSite("login shell", "x", node_env.OK,
                                 f"{HOME}/.local/share/mise/shims/node", "v22.0.0")
        script = node_env.NodeSite("non-interactive shell", "x", node_env.MISSING)
        assert "--shims" in node_env.fix_for(script, [mise, script])


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

    def test_a_place_that_could_not_be_checked_fails_doctor(self, monkeypatch):
        """Doctor's exit code says when a check could not be made. Found in
        review."""
        from server import main

        monkeypatch.setattr(node_env, "probe", lambda: [node_env.NodeSite(
            "login shell", "x", node_env.UNKNOWN, detail="the shell did not answer")])
        assert main._report_node() is False

    def test_an_unsupported_shell_does_not_fail_doctor(self, monkeypatch, capsys):
        from server import main

        monkeypatch.setattr(node_env, "probe", lambda: [node_env.NodeSite(
            "login shell", "x", node_env.SKIPPED, detail="unsupported shell fish")])
        assert main._report_node() is True
        assert "note: unsupported shell fish" in capsys.readouterr().out

    def test_a_probe_that_throws_is_reported_as_unchecked(self, monkeypatch, capsys):
        from server import main

        def boom():
            raise RuntimeError("nope")

        monkeypatch.setattr(node_env, "probe", boom)
        assert main._report_node() is False
        assert "could not be checked" in capsys.readouterr().out


class TestNothingHereStopsAnUpdate:
    """`check_node` runs inside `run_setup`, which `quern update` calls after
    the pull. A raise there leaves the install pulled but not rebuilt."""

    def test_a_probe_that_raises_becomes_a_warning(self, monkeypatch):
        from server.lifecycle import setup as setup_mod

        def boom(**_kw):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

        monkeypatch.setattr(node_env, "probe", boom)
        result = setup_mod.check_node()
        assert result.status == setup_mod.CheckStatus.WARNING
        assert "could not be checked" in result.message

    def test_the_update_warning_survives_a_raising_probe(self, monkeypatch, tmp_path, capsys):
        from server.lifecycle import updater

        monkeypatch.setattr(updater, "RESULT_FILE", tmp_path / "r.json")
        monkeypatch.setattr(updater, "_find_project_root", lambda: tmp_path)
        monkeypatch.setattr(updater, "_is_git_install", lambda _r: True)
        pulled = []
        monkeypatch.setattr(updater, "_update_via_git", lambda _r: pulled.append(1) or 2)
        monkeypatch.setattr(updater, "_report_tool_updates", lambda _a: True)
        monkeypatch.setattr(updater, "_refresh_update_check", lambda: None)

        def boom(**_kw):
            raise RuntimeError("the shell exploded")

        monkeypatch.setattr(node_env, "here", boom)
        assert updater.run_update() == 0
        assert pulled, "the update was stopped by a node check"
        assert "could not check" in capsys.readouterr().out


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


# ── the node MCP clients are registered with ─────────────────────────────────


FNM = f"{HOME}/.local/share/fnm"
FNM_V22 = f"{FNM}/node-versions/v22.22.2/installation/bin/node"
FNM_ALIAS = f"{FNM}/aliases/default/bin/node"
SHELL_LINK = f"{HOME}/.local/state/fnm_multishells/123_456/bin/node"


class Machine:
    """What each node binary answers, and how it behaves with launchd's PATH."""

    def __init__(self, *, versions=None, exec_paths=None, needs_shell=(), links=None):
        self.versions = versions or {}         # path -> "vNN" (any env)
        self.exec_paths = exec_paths or {}     # path -> process.execPath
        self.needs_shell = set(needs_shell)    # paths that fail under launchd's PATH
        self.links = links or {}               # symlink -> target
        self.calls: list[tuple[list[str], dict]] = []

    def run(self, argv, **kw):
        self.calls.append((argv, kw))
        path = argv[0]
        bare = kw.get("env", {}).get("PATH") == ":".join(node_env.GUI_PATH)
        if path in self.needs_shell and bare:
            return _done("", 127)
        if argv[1:] == ["-p", "process.execPath"]:
            real = self.exec_paths.get(path)
            return _done(real + "\n") if real else _done("", 1)
        v = self.versions.get(path)
        return _done(v + "\n") if v else _done("", 1)

    def resolve(self, path):
        return self.links.get(path, path)

    def exists(self, path):
        return path in self.versions or path in self.links

    def choose(self, sites):
        return node_env._real_node_for_clients(sites, run=self.run, env={"HOME": HOME},
                                               home=HOME, resolve=self.resolve,
                                               exists=self.exists)


def _site(place, path, version="v22.22.2"):
    return node_env.NodeSite(place, "test", node_env.OK, path, version)


class TestTheNodeClientsAreRegisteredWith:
    def test_homebrews_stable_link_is_kept_over_the_cellar(self):
        """`brew upgrade` removes the Cellar path the link points at."""
        cellar = "/opt/homebrew/Cellar/node/22.22.2/bin/node"
        m = Machine(versions={"/opt/homebrew/bin/node": "v22.22.2", cellar: "v22.22.2"},
                    exec_paths={"/opt/homebrew/bin/node": cellar})
        chosen = m.choose([_site("login shell", "/opt/homebrew/bin/node")])
        assert chosen == node_env.ClientNode("/opt/homebrew/bin/node", "v22.22.2",
                                             "login shell")

    def test_fnms_per_shell_link_becomes_its_default_alias(self):
        """The per-shell link disappears with the shell; the alias follows
        `fnm default` from then on."""
        m = Machine(versions={SHELL_LINK: "v22.22.2", FNM_V22: "v22.22.2",
                              FNM_ALIAS: "v22.22.2"},
                    exec_paths={SHELL_LINK: FNM_V22}, links={FNM_ALIAS: FNM_V22})
        chosen = m.choose([_site("login shell", SHELL_LINK)])
        assert chosen.path == FNM_ALIAS
        assert all(argv[0] != SHELL_LINK or argv[1] != "--version" for argv, _ in m.calls), \
            "the per-shell link was considered for registration"

    def test_an_alias_to_another_version_is_not_used(self):
        other = f"{FNM}/node-versions/v24.1.0/installation/bin/node"
        m = Machine(versions={SHELL_LINK: "v22.22.2", FNM_V22: "v22.22.2",
                              FNM_ALIAS: "v24.1.0", other: "v24.1.0"},
                    exec_paths={SHELL_LINK: FNM_V22}, links={FNM_ALIAS: other})
        assert m.choose([_site("login shell", SHELL_LINK)]).path == FNM_V22

    def test_a_shim_that_needs_its_shell_gives_way_to_its_binary(self):
        """A GUI client runs it with launchd's PATH and nothing else."""
        shim = f"{HOME}/.asdf/shims/node"
        real = f"{HOME}/.asdf/installs/nodejs/22.22.2/bin/node"
        m = Machine(versions={shim: "v22.22.2", real: "v22.22.2"},
                    exec_paths={shim: real}, needs_shell={shim})
        assert m.choose([_site("login shell", shim)]).path == real

    def test_every_candidate_is_run_with_launchds_path(self):
        m = Machine(versions={"/usr/local/bin/node": "v22.22.2"})
        m.choose([_site("login shell", "/usr/local/bin/node")])
        [(argv, kw)] = [c for c in m.calls if c[0][-1] == "--version"]
        assert kw["env"] == {"HOME": HOME, "PATH": ":".join(node_env.GUI_PATH)}

    def test_the_login_shells_node_is_preferred(self):
        m = Machine(versions={"/caller/node": "v22.1.0", "/login/node": "v24.0.0"})
        chosen = m.choose([_site("this command", "/caller/node"),
                           _site("login shell", "/login/node")])
        assert (chosen.path, chosen.found_in) == ("/login/node", "login shell")

    def test_a_node_too_old_outside_a_shell_is_passed_over(self):
        """The site said 22 in its own shell; launchd's PATH is what counts."""
        m = Machine(versions={"/login/node": "v20.20.2", "/opt/homebrew/bin/node": "v22.22.2"})
        chosen = m.choose([_site("login shell", "/login/node"),
                           _site("the Quern app", "/opt/homebrew/bin/node")])
        assert chosen.path == "/opt/homebrew/bin/node"

    def test_nothing_usable_is_none(self):
        m = Machine(versions={"/login/node": "v20.20.2"})
        assert m.choose([_site("login shell", "/login/node"),
                         node_env.NodeSite("GUI apps", "test", node_env.MISSING)]) is None

    def test_a_node_that_will_not_say_where_it_is_is_not_fatal(self):
        def run(argv, **kw):
            raise OSError("exec format error")
        assert node_env._real_node_for_clients(
            [_site("login shell", SHELL_LINK)], run=run, env={}, home=HOME) is None

    def test_every_run_is_from_home_not_the_callers_project(self):
        """Inside a project pinned to Node 20, fnm's `--use-on-cd` and mise
        answer 20 everywhere, and the default 22 was never seen (measured)."""
        m = Machine(versions={SHELL_LINK: "v22.22.2", FNM_V22: "v22.22.2"},
                    exec_paths={SHELL_LINK: FNM_V22})
        m.choose([_site("login shell", SHELL_LINK)])
        assert m.calls and all(kw.get("cwd") == HOME for _, kw in m.calls)

    def test_the_probe_it_runs_itself_is_from_home(self, monkeypatch):
        seen = {}

        def probe(**kw):
            seen.update(kw)
            return []

        monkeypatch.setattr(node_env, "probe", probe)
        node_env._real_node_for_clients(env={"HOME": HOME}, home=HOME, run=Machine().run)
        assert seen.get("cwd") == HOME

    def test_a_relative_exec_path_is_not_taken(self):
        m = Machine(versions={SHELL_LINK: "v22.22.2", "node": "v22.22.2"},
                    exec_paths={SHELL_LINK: "node"})
        assert m.choose([_site("login shell", SHELL_LINK)]) is None


class TestCheckOutsideAShell:
    def _check(self, run):
        return node_env._real_check_outside_a_shell("/n/node", run=run, home=HOME)

    def test_the_four_answers(self):
        assert self._check(lambda *a, **k: _done("v22.1.0\n")) == (node_env.OK, "v22.1.0")
        assert self._check(lambda *a, **k: _done("v20.20.2\n")) == (node_env.TOO_OLD, "v20.20.2")
        assert self._check(lambda *a, **k: _done("usage: sh\n", 2)) == (node_env.UNUSABLE, None)

        def timeout(*a, **k):
            raise subprocess.TimeoutExpired("node", 10)
        assert self._check(timeout) == (node_env.UNKNOWN, None)
