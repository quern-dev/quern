"""Setup's Python handling on a machine with no supported Python (#455).

A first install on a Mac whose only `python3` was outside the supported range
went: setup installed Python via Homebrew, printed "restart your shell, then
re-run", and returned 0 with no venv. install.sh trusted the 0 and died on
`.venv/bin/python`, and a re-run said "already installed". Setup now builds the
venv with the Python it installed, by path, and carries on.

The supported range was also stuck at 3.11–3.13 for eight months while CI tested
3.14, so it is now tied to the CI matrix.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from server.lifecycle import setup
from server.lifecycle.setup import CheckResult, CheckStatus
from tests.test_setup import _stub_the_checks_before_the_venv

ROOT = Path(__file__).resolve().parent.parent
BREW_PY = "/opt/homebrew/opt/python@3.13/bin/python3.13"


def _ci_matrix() -> list[tuple[int, int]]:
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    m = re.search(r'python-version:\s*\[([^\]]+)\]', text)
    assert m, "ci.yml has no python-version matrix to compare against"
    return sorted(tuple(int(p) for p in v.strip().strip("\"'").split("."))
                  for v in m.group(1).split(","))


class TestTheRangeIsTheCiMatrix:
    def test_the_supported_range_is_what_ci_tests(self):
        matrix = _ci_matrix()
        assert (setup.PYTHON_MIN, setup.PYTHON_MAX) == (matrix[0], matrix[-1]), (
            "setup's supported range has drifted from the CI matrix; PYTHON_MAX "
            "sat at 3.13 for months while CI tested 3.14, so setup called a "
            "tested Python 'untested' and offered to replace it"
        )

    def test_every_supported_version_is_tested(self):
        assert setup._supported_versions() == [f"{a}.{b}" for a, b in reversed(_ci_matrix())]

    def test_the_recommended_version_is_one_ci_tests(self):
        assert setup.RECOMMENDED_PYTHON in _ci_matrix()


@pytest.fixture
def no_supported_python(monkeypatch, tmp_path):
    """run_setup on an interpreter outside the range, with no venv yet."""
    _stub_the_checks_before_the_venv(monkeypatch)
    (tmp_path / "pyproject.toml").write_text("")
    monkeypatch.setattr(setup, "_find_project_root", lambda *a, **k: tmp_path)
    monkeypatch.setattr(setup.sys, "prefix", "/usr/local", raising=False)
    monkeypatch.setattr(setup.sys, "base_prefix", "/usr/local", raising=False)
    monkeypatch.setattr(setup, "_find_best_python", lambda: sys.executable)
    calls = {"created_with": [], "reexec": 0, "installed": []}

    def create_venv(root, python=None):
        calls["created_with"].append(python)
        return True

    def reexec(*a, **k):
        calls["reexec"] += 1
        return 0

    monkeypatch.setattr(setup, "create_venv", create_venv)
    monkeypatch.setattr(setup, "_reexec_in_venv", reexec)
    monkeypatch.setattr(setup, "_brew_install",
                        lambda formula: calls["installed"].append(formula) or True)
    return calls


def _python_check(monkeypatch, status):
    monkeypatch.setattr(setup, "check_python", lambda: CheckResult(
        name="Python", status=status, message="3.15.0", fixable=True))


class TestAfterInstallingPython:
    def test_setup_carries_on_into_the_venv_with_the_new_python(
        self, monkeypatch, no_supported_python, capsys,
    ):
        """The fix: no "restart your shell", and the venv is built with the
        installed interpreter by path."""
        _python_check(monkeypatch, CheckStatus.WARNING)
        monkeypatch.setattr(setup, "_prompt_yn", lambda *a, **k: True)
        monkeypatch.setattr(setup, "_brew_python", lambda formula, ver: BREW_PY)

        setup.run_setup()

        assert no_supported_python["installed"] == [f"python@{setup._recommended()}"]
        assert no_supported_python["created_with"] == [BREW_PY], (
            "setup did not build the venv with the Python it had just installed; "
            "it used to stop here and return 0 with no venv at all"
        )
        assert no_supported_python["reexec"] == 1
        assert "Restart your shell" not in capsys.readouterr().out

    def test_it_installs_the_recommended_version(self, monkeypatch, no_supported_python):
        _python_check(monkeypatch, CheckStatus.ERROR)
        monkeypatch.setattr(setup, "_prompt_yn", lambda *a, **k: True)
        monkeypatch.setattr(setup, "_brew_python", lambda formula, ver: BREW_PY)
        monkeypatch.setattr(setup, "RECOMMENDED_PYTHON", (3, 12))

        setup.run_setup()

        assert no_supported_python["installed"] == ["python@3.12"], (
            "the formula is not derived from RECOMMENDED_PYTHON"
        )

    def test_an_install_it_cannot_find_stops_with_a_failure(
        self, monkeypatch, no_supported_python, capsys,
    ):
        _python_check(monkeypatch, CheckStatus.ERROR)
        monkeypatch.setattr(setup, "_prompt_yn", lambda *a, **k: True)
        monkeypatch.setattr(setup, "_brew_python", lambda formula, ver: None)

        rc = setup.run_setup()

        assert rc == 1, "an unfinished setup reported success"
        assert no_supported_python["created_with"] == []
        out = capsys.readouterr().out
        assert "could not find" in out
        assert "none was found" not in out, (
            "it told the user to install the Python Homebrew had just installed")


class TestWithoutASupportedPython:
    def test_declining_stops_with_a_failure_that_says_what_to_do(
        self, monkeypatch, no_supported_python, capsys,
    ):
        """A venv from a too-old interpreter fails minutes later in pip, with an
        error naming neither Python nor setup."""
        _python_check(monkeypatch, CheckStatus.ERROR)
        monkeypatch.setattr(setup, "_prompt_yn", lambda *a, **k: False)

        rc = setup.run_setup()

        assert rc == 1
        assert no_supported_python["created_with"] == [], "it built a venv it knew would fail"
        out = capsys.readouterr().out
        assert setup._version_range() in out
        assert "setup" in out.split("none was found")[-1]

    def test_a_too_old_interpreter_is_fine_when_a_supported_one_exists(
        self, monkeypatch, no_supported_python,
    ):
        """Running setup under the system 3.9 with Homebrew's 3.12 on PATH has
        always worked; the new stop must not catch it."""
        _python_check(monkeypatch, CheckStatus.ERROR)
        monkeypatch.setattr(setup, "_prompt_yn", lambda *a, **k: False)
        monkeypatch.setattr(setup, "_find_best_python", lambda: "/opt/homebrew/bin/python3.12")

        setup.run_setup()

        assert no_supported_python["created_with"] == [None], (
            "setup stopped although a supported Python was available"
        )


class TestBrewPython:
    def test_it_finds_the_interpreter_by_path(self, monkeypatch, tmp_path):
        exe = tmp_path / "bin" / "python3.13"
        exe.parent.mkdir()
        exe.write_text("")
        exe.chmod(0o755)
        monkeypatch.setattr(setup, "_run", lambda cmd, **k: (0, f"{tmp_path}\n", ""))
        assert setup._brew_python("python@3.13", "3.13") == str(exe)

    def test_a_missing_interpreter_is_none(self, monkeypatch, tmp_path):
        monkeypatch.setattr(setup, "_run", lambda cmd, **k: (0, f"{tmp_path}\n", ""))
        assert setup._brew_python("python@3.13", "3.13") is None

    def test_brew_failing_is_none(self, monkeypatch):
        monkeypatch.setattr(setup, "_run", lambda cmd, **k: (1, "", "no such formula"))
        assert setup._brew_python("python@3.13", "3.13") is None


class TestAnExistingVenvIsCheckedBeforeItIsUsed:
    """A half-built venv -- pip killed or timed out, or its Python uninstalled --
    was re-execed into and died on ModuleNotFoundError or exited 255 silently.
    install.sh's repair kept it, so no retry could get past it (#455 review)."""

    def _with_venv(self, monkeypatch, tmp_path, usable):
        venv = tmp_path / ".venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "python").write_text("")
        _stub_the_checks_before_the_venv(monkeypatch)
        (tmp_path / "pyproject.toml").write_text("")
        monkeypatch.setattr(setup, "_find_project_root", lambda *a, **k: tmp_path)
        monkeypatch.setattr(setup.sys, "prefix", "/usr/local", raising=False)
        monkeypatch.setattr(setup.sys, "base_prefix", "/usr/local", raising=False)
        monkeypatch.setattr(setup, "_venv_is_usable", lambda path: usable)
        monkeypatch.setattr(setup, "_run", lambda cmd, **k: (0, "Python 3.12.0", ""))
        seen = {"created": 0, "reexec": 0}

        def created(*a, **k):
            seen["created"] += 1
            return True

        def reexeced(*a, **k):
            seen["reexec"] += 1
            return 0

        monkeypatch.setattr(setup, "create_venv", created)
        monkeypatch.setattr(setup, "_reexec_in_venv", reexeced)
        return venv, seen

    def test_a_broken_venv_is_rebuilt(self, monkeypatch, tmp_path):
        venv, seen = self._with_venv(monkeypatch, tmp_path, usable=False)
        setup.run_setup()
        assert seen["created"] == 1, "a broken venv was re-entered instead of rebuilt"
        assert seen["reexec"] == 1

    def test_a_working_venv_is_kept(self, monkeypatch, tmp_path):
        venv, seen = self._with_venv(monkeypatch, tmp_path, usable=True)
        setup.run_setup()
        assert seen["created"] == 0, "a working venv was thrown away"
        assert venv.exists()

    def test_usable_means_its_python_runs_and_imports_the_deps(self, monkeypatch, tmp_path):
        venv = tmp_path / ".venv"
        assert setup._venv_is_usable(venv) is False, "a venv with no python was usable"
        (venv / "bin").mkdir(parents=True)
        py = venv / "bin" / "python"
        py.symlink_to(tmp_path / "uninstalled-python")
        assert setup._venv_is_usable(venv) is False, "a dangling interpreter was usable"


class TestTheInstalledPythonReachesEveryVenvBuild:
    def test_a_too_new_venv_is_rebuilt_with_the_python_just_installed(
        self, monkeypatch, tmp_path,
    ):
        """The recreate branch: an old venv on 3.15, nothing supported on PATH,
        3.13 just installed. Without the pass-through it was rebuilt on 3.15
        again (review mutations M9, M10)."""
        venv = tmp_path / ".venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "python").write_text("")
        _stub_the_checks_before_the_venv(monkeypatch)
        _python_check(monkeypatch, CheckStatus.WARNING)
        (tmp_path / "pyproject.toml").write_text("")
        monkeypatch.setattr(setup, "_find_project_root", lambda *a, **k: tmp_path)
        monkeypatch.setattr(setup.sys, "prefix", "/usr/local", raising=False)
        monkeypatch.setattr(setup.sys, "base_prefix", "/usr/local", raising=False)
        monkeypatch.setattr(setup, "_venv_is_usable", lambda path: True)
        monkeypatch.setattr(setup, "_prompt_yn", lambda *a, **k: True)
        monkeypatch.setattr(setup, "_brew_install", lambda formula: True)
        monkeypatch.setattr(setup, "_brew_python", lambda formula, ver: BREW_PY)
        monkeypatch.setattr(setup, "_find_best_python", lambda: sys.executable)
        too_new = f"{setup.PYTHON_MAX[0]}.{setup.PYTHON_MAX[1] + 1}.0"
        monkeypatch.setattr(setup, "_run", lambda cmd, **k: (
            0, f"Python {too_new}" if str(cmd[0]).endswith(".venv/bin/python")
            else "Python 3.13.0", ""))
        created_with = []
        monkeypatch.setattr(setup, "create_venv",
                            lambda root, python=None: created_with.append(python) or True)
        monkeypatch.setattr(setup, "_reexec_in_venv", lambda *a, **k: 0)

        setup.run_setup()

        assert created_with == [BREW_PY]


class TestTheDefaultAnswer:
    def test_enter_installs_a_supported_python_for_a_too_new_one(
        self, monkeypatch, no_supported_python,
    ):
        """Declining built the venv on the too-new Python, which pip then fails
        on wherever a wheel is missing. Enter means yes now (#455 review)."""
        _python_check(monkeypatch, CheckStatus.WARNING)
        defaults = []
        monkeypatch.setattr(setup, "_prompt_yn",
                            lambda q, default=False, **k: defaults.append(default) or default)
        monkeypatch.setattr(setup, "_brew_python", lambda formula, ver: BREW_PY)

        setup.run_setup()

        assert defaults and defaults[0] is True
        assert no_supported_python["created_with"] == [BREW_PY]


class TestTheBootstrapDoesNotHandSetupABrokenVenv:
    """`python -m server setup` re-execs into `.venv` before setup runs. Into a
    broken one, setup never got to check it: found by a clean-VM repair test
    after every unit test here passed (#455)."""

    def _bootstrap(self, monkeypatch, tmp_path, argv, usable):
        from server import __main__ as main_mod
        venv_python = tmp_path / ".venv" / "bin" / "python"
        venv_python.parent.mkdir(parents=True)
        venv_python.write_text("")
        monkeypatch.setattr(main_mod.sys, "prefix", "/usr/local", raising=False)
        monkeypatch.setattr(main_mod.sys, "base_prefix", "/usr/local", raising=False)
        monkeypatch.setattr(main_mod, "_find_project_root", lambda: tmp_path)
        monkeypatch.setattr(main_mod.sys, "argv", ["server", *argv])
        monkeypatch.setattr(setup, "_venv_is_usable", lambda path: usable)
        execs = []
        monkeypatch.setattr(main_mod.os, "execv", lambda *a: execs.append(a))
        main_mod._maybe_reexec_in_venv()
        return execs

    def test_setup_is_not_sent_into_a_broken_venv(self, monkeypatch, tmp_path):
        assert self._bootstrap(monkeypatch, tmp_path, ["setup"], usable=False) == []

    def test_setup_still_uses_a_working_venv(self, monkeypatch, tmp_path):
        assert len(self._bootstrap(monkeypatch, tmp_path, ["setup"], usable=True)) == 1

    def test_other_commands_are_unchanged(self, monkeypatch, tmp_path):
        """Only setup can rebuild a venv; anything else still goes into it."""
        assert len(self._bootstrap(monkeypatch, tmp_path, ["start"], usable=False)) == 1


class TestSetupInsideABrokenVenv:
    def test_it_says_how_to_rebuild_instead_of_failing_on_an_import(
        self, monkeypatch, tmp_path, capsys,
    ):
        _stub_the_checks_before_the_venv(monkeypatch)
        (tmp_path / "pyproject.toml").write_text("")
        monkeypatch.setattr(setup, "_find_project_root", lambda *a, **k: tmp_path)
        monkeypatch.setattr(setup.sys, "prefix", str(tmp_path / ".venv"), raising=False)
        monkeypatch.setattr(setup.sys, "base_prefix", "/usr/local", raising=False)
        monkeypatch.setattr(setup, "_venv_is_usable", lambda path: False)
        reached = []
        monkeypatch.setattr(setup, "check_mitmdump", lambda *a, **k: reached.append(True))

        rc = setup.run_setup()

        assert rc == 1
        assert reached == [], "setup carried on in an environment it knew was broken"
        out = capsys.readouterr().out
        assert "incomplete" in out and "install.sh" in out


class TestTheUsabilityProbeItself:
    """`_venv_is_usable` is mocked everywhere else, so these run it for real
    against stand-in interpreters (#455 review: N10, N12)."""

    def _python(self, tmp_path, script):
        py = tmp_path / ".venv" / "bin" / "python"
        py.parent.mkdir(parents=True)
        py.write_text("#!/bin/sh\n" + script)
        py.chmod(0o755)
        return tmp_path / ".venv"

    def test_a_python_that_imports_the_deps_is_usable(self, tmp_path):
        assert setup._venv_is_usable(self._python(tmp_path, "exit 0\n")) is True

    def test_a_python_missing_them_is_not(self, tmp_path):
        assert setup._venv_is_usable(self._python(tmp_path, "exit 1\n")) is False

    def test_the_probe_is_isolated_from_the_callers_directory(self, tmp_path):
        """Without -I, cwd is on sys.path, and a directory holding something
        named httpx made a working venv read as broken."""
        args = tmp_path / "args"
        venv = self._python(tmp_path, f'echo "$@" > {args}\nexit 0\n')
        setup._venv_is_usable(venv)
        assert args.read_text().split()[0] == "-I"


class TestPipIsStoppedWithEverythingItStarted:
    def test_it_runs_in_its_own_session_and_the_whole_group_is_killed(
        self, tmp_path, monkeypatch,
    ):
        from tests.test_setup import _FakePip, _venv_ok
        monkeypatch.setattr(setup, "PIP_INSTALL_TIMEOUT", setup.PIP_HEARTBEAT)
        pip = _FakePip(never_exits=True)
        pip.pid = 4242
        popen_kwargs, killed_groups = {}, []

        def popen(cmd, **kw):
            popen_kwargs.update(kw)
            return pip(cmd, **kw)

        monkeypatch.setattr(setup.subprocess, "run", _venv_ok)
        monkeypatch.setattr(setup.subprocess, "Popen", popen)
        monkeypatch.setattr(setup.os, "killpg", lambda pid, sig: killed_groups.append(pid))
        pip.kill = lambda: setattr(pip, "killed", True)
        monkeypatch.setattr(pip, "wait", lambda timeout=None: (
            -9 if killed_groups else (_ for _ in ()).throw(
                setup.subprocess.TimeoutExpired("pip", timeout))))

        assert setup.create_venv(tmp_path, python="/usr/bin/python3") is False
        assert popen_kwargs.get("start_new_session") is True, (
            "pip shares setup's session, so Ctrl-C and the kill reach pip alone")
        assert killed_groups == [4242], "only pip was killed, not what it started"


class TestTheBrokenVenvAdviceIsOnlyForQuernsOwn:
    def test_a_developers_own_venv_is_not_called_broken(self, monkeypatch, tmp_path, capsys):
        _stub_the_checks_before_the_venv(monkeypatch)
        (tmp_path / "pyproject.toml").write_text("")
        monkeypatch.setattr(setup, "_find_project_root", lambda *a, **k: tmp_path)
        monkeypatch.setattr(setup.sys, "prefix", str(tmp_path / "otherenv"), raising=False)
        monkeypatch.setattr(setup.sys, "base_prefix", "/usr/local", raising=False)
        monkeypatch.setattr(setup, "_venv_is_usable", lambda path: False)

        class PastTheVenvBlock(Exception):
            pass

        def first_check_after_it(*a, **k):
            raise PastTheVenvBlock  # stop before checks that touch the machine

        monkeypatch.setattr(setup, "check_mitmdump", first_check_after_it)

        with pytest.raises(PastTheVenvBlock):
            setup.run_setup()  # reaching it is the assertion: setup did not stop

        assert "rm -rf" not in capsys.readouterr().out, "it advised deleting someone else's venv"
