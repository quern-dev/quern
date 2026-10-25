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
    ("1.2.1", False), ("1.2.10", False), ("1.10.0", False), ("1.3.0", False), ("2.0", False),
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
    assert "CVE-2026-111497" in result.detail and "doctor --fix" in result.detail


def test_setup_is_ok_when_the_fix_is_in():
    assert setup.check_security_pins(_versions(Brotli="1.2.1")).status == CheckStatus.OK


class TestApplyFailures:
    """Every way pip can fail is a message, never a crash on the start path."""

    def test_a_pip_that_is_not_executable(self, tmp_path):
        pip = tmp_path / "pip"
        pip.write_text("#!/bin/sh\nexit 0\n")      # no exec bit: PermissionError
        assert security_pins.apply(str(pip))

    def test_a_pip_that_hangs(self, tmp_path):
        pip = tmp_path / "pip"
        pip.write_text("#!/bin/sh\nsleep 5\n")
        pip.chmod(0o755)
        assert security_pins.apply(str(pip), timeout=1)

    def test_an_error_on_stdout_only_is_still_named(self, tmp_path):
        pip = tmp_path / "pip"
        pip.write_text("#!/bin/sh\necho 'no matching distribution'\nexit 1\n")
        pip.chmod(0o755)
        assert security_pins.apply(str(pip)) == "no matching distribution"


def _venv_with(tmp_path, *dists):
    site = tmp_path / ".venv" / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    for d in dists:
        (site / f"{d}.dist-info").mkdir()
    return tmp_path / ".venv"


class TestTheStartPathSeesIt:
    """The blocker the review found: `git pull` or a re-run install.sh keeps a
    venv whose stamp is current, so the start path skipped and the override
    never ran. An unfixed Brotli in the venv now reads as out of sync."""

    @pytest.mark.parametrize("dist, left", [
        ("brotli-1.2.0", True), ("Brotli-1.2.0", True), ("brotli-1.2.0rc1", True),
        ("brotli-1.2.1", False), ("brotli-1.10.0", False),
    ])
    def test_reads_the_venvs_own_metadata(self, tmp_path, dist, left):
        venv = _venv_with(tmp_path, "mitmproxy-12.3.0", dist)
        assert bool(security_pins.unfixed_in_venv(venv)) is left

    def test_a_venv_without_it_is_not_this_checks_business(self, tmp_path):
        assert security_pins.unfixed_in_venv(_venv_with(tmp_path, "mitmproxy-12.3.0")) == []

    def test_a_current_stamp_with_an_unfixed_brotli_is_out_of_sync(self, tmp_path):
        from server.__main__ import DEPS_STAMP_NAME, python_deps_state

        venv = _venv_with(tmp_path, "brotli-1.2.0")
        (venv / "bin").mkdir()
        (venv / "bin" / "pip").write_text("")
        (tmp_path / "pyproject.toml").write_text("")
        (venv / DEPS_STAMP_NAME).touch()          # newer than pyproject.toml
        state = python_deps_state(tmp_path)
        assert state["in_sync"] is False and "Brotli 1.2.0" in state["reason"]


class TestSetupApplies:
    def test_setup_installs_it_when_missing(self, monkeypatch):
        installed = {"Brotli": "1.2.0"}
        monkeypatch.setattr(security_pins, "apply",
                            lambda pip, **k: installed.update(Brotli="1.2.1"))
        result = setup.check_security_pins(_versions_live(installed), apply=True)
        assert result.status == CheckStatus.OK

    def test_a_failed_install_is_said(self, monkeypatch):
        monkeypatch.setattr(security_pins, "apply", lambda pip, **k: "no network")
        result = setup.check_security_pins(_versions(Brotli="1.2.0"), apply=True)
        assert result.status == CheckStatus.WARNING and "no network" in result.detail

    def test_setup_runs_the_check(self):
        """Removing the row from setup's report would leave nothing that says
        a venv is still on 1.2.0."""
        import inspect
        assert "check_security_pins(apply=True)" in inspect.getsource(setup.run_setup)


def _versions_live(installed):
    def version_of(name):
        return installed[name]
    return version_of


def test_doctor_names_an_unfixed_node(monkeypatch, capsys):
    from server.lifecycle import node_env
    from server.main import _report_node

    site = node_env.NodeSite("this command", "x", node_env.OK, "/n/node", "v22.22.2")
    monkeypatch.setattr(node_env, "probe", lambda: [site])
    monkeypatch.setattr(node_env, "manager_of", lambda path: "fnm")
    _report_node()
    out = capsys.readouterr().out
    assert "security: Node 22.22.2 lacks the fix" in out and "fnm install 22" in out
