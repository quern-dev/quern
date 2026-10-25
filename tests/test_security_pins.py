"""Packages installed past a dependency's cap: CVE-2026-111497's Brotli 1.2.1."""

from __future__ import annotations

import stat
from importlib import metadata

import pytest

from server.lifecycle import security_pins, setup
from server.lifecycle.setup import CheckStatus


def _versions(**installed):
    def version_of(name):
        if name not in installed:
            raise metadata.PackageNotFoundError(name)
        return installed[name]
    return version_of


def test_brotli_is_pinned_to_the_fixed_release():
    assert security_pins.requirements() == ["Brotli>=1.2.1"]


@pytest.mark.parametrize("installed, left", [
    ("1.2.0", True), ("1.1.0", True), ("1.0.9", True),
    ("1.2.1", False), ("1.2.10", False), ("1.3.0", False), ("2.0", False),
])
def test_unfixed_compares_versions_as_numbers(installed, left):
    assert bool(security_pins.unfixed(_versions(Brotli=installed))) is left


def test_a_missing_package_is_unfixed_not_safe():
    """It is a dependency of quern's; absent is a broken venv, not a safe one."""
    assert security_pins.unfixed(_versions()) == [
        ("Brotli", None, "1.2.1", security_pins.OVERRIDES[0][2])]


def _fake_pip(tmp_path, code):
    log = tmp_path / "args"
    pip = tmp_path / "pip"
    pip.write_text(f'#!/bin/sh\necho "$@" > "{log}"\necho "pip says no" >&2\nexit {code}\n')
    pip.chmod(pip.stat().st_mode | stat.S_IEXEC)
    return str(pip), log


def test_apply_installs_without_deps(tmp_path):
    pip, log = _fake_pip(tmp_path, 0)
    assert security_pins.apply(pip) is None
    assert log.read_text().split() == ["install", "--no-deps", "Brotli>=1.2.1"]


def test_apply_reports_pips_failure(tmp_path):
    pip, _ = _fake_pip(tmp_path, 1)
    assert security_pins.apply(pip) == "pip says no"


def test_apply_reports_a_pip_that_cannot_run(tmp_path):
    assert security_pins.apply(str(tmp_path / "no-such-pip"))


def test_setup_says_when_the_fix_is_missing():
    result = setup.check_security_pins(_versions(Brotli="1.2.0"))
    assert result.status == CheckStatus.WARNING
    assert "Brotli 1.2.0" in result.message and "1.2.1+" in result.message
    assert "CVE-2026-111497" in result.detail and "--no-deps" in result.detail


def test_setup_is_ok_when_the_fix_is_in():
    assert setup.check_security_pins(_versions(Brotli="1.2.1")).status == CheckStatus.OK


class TestDoctor:
    """A venv can match pyproject.toml and still be on Brotli 1.2.0: the
    override goes on after the install, not through it."""

    @pytest.fixture
    def in_sync(self, monkeypatch):
        monkeypatch.setattr("server.__main__.python_deps_state",
                            lambda *a: {"applicable": True, "in_sync": True, "reason": ""})

    def test_says_so_and_how_to_repair(self, in_sync, monkeypatch, capsys):
        from server.main import _report_python_deps
        monkeypatch.setattr(security_pins, "unfixed",
                            lambda *a: [("Brotli", "1.2.0", "1.2.1", "CVE-2026-111497")])
        monkeypatch.setattr(security_pins, "apply", lambda *a, **k: pytest.fail("read-only"))
        assert _report_python_deps(fix=False) is None
        out = capsys.readouterr().out
        assert "Brotli 1.2.0, needs 1.2.1+" in out and "doctor --fix" in out

    def test_fix_applies_and_checks_it_took(self, in_sync, monkeypatch):
        from server.main import _report_python_deps
        state = {"left": [("Brotli", "1.2.0", "1.2.1", "x")]}
        monkeypatch.setattr(security_pins, "unfixed", lambda *a: state["left"])

        def apply(*a, **k):
            state["left"] = []
        monkeypatch.setattr(security_pins, "apply", apply)
        assert _report_python_deps(fix=True) is True

    def test_fix_that_did_not_take_is_a_failure(self, in_sync, monkeypatch):
        """pip said yes and the version is still old: not repaired."""
        from server.main import _report_python_deps
        monkeypatch.setattr(security_pins, "unfixed",
                            lambda *a: [("Brotli", "1.2.0", "1.2.1", "x")])
        monkeypatch.setattr(security_pins, "apply", lambda *a, **k: None)
        assert _report_python_deps(fix=True) is False

    def test_nothing_to_say_when_fixed(self, in_sync, monkeypatch, capsys):
        from server.main import _report_python_deps
        monkeypatch.setattr(security_pins, "unfixed", lambda *a: [])
        assert _report_python_deps(fix=False) is None
        assert "Brotli" not in capsys.readouterr().out
