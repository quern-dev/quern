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
        assert "could not find" in capsys.readouterr().out


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
