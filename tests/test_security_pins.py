"""Packages installed past a dependency's cap: CVE-2026-111497's Brotli 1.2.1."""

from __future__ import annotations

import stat

import pytest

from server.lifecycle import security_pins, setup
from server.lifecycle.setup import CheckStatus


def test_brotli_is_pinned_to_the_fixed_release():
    assert security_pins.requirements() == ["Brotli>=1.2.1"]


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

    def test_a_venv_without_it_is_unfixed(self, tmp_path):
        """CodeRabbit, #546: a current stamp and no Brotli at all must not read
        as safe, or the start path never installs one."""
        venv = _venv_with(tmp_path, "mitmproxy-12.3.0")
        assert security_pins.unfixed_in_venv(venv) == [
            ("Brotli", None, "1.2.1", security_pins.OVERRIDES[0][2])]

    def test_a_directory_nobody_installed_into_has_nothing_to_report(self, tmp_path):
        (tmp_path / ".venv" / "bin").mkdir(parents=True)
        assert security_pins.unfixed_in_venv(tmp_path / ".venv") == []

    def test_any_stale_metadata_counts(self, tmp_path):
        """An interrupted upgrade can leave both; the older may describe the
        files installed (CodeRabbit, #547)."""
        venv = _venv_with(tmp_path, "brotli-1.2.1", "Brotli-1.2.0")
        assert security_pins.unfixed_in_venv(venv)[0][1] == "1.2.0"

    def test_a_current_stamp_with_an_unfixed_brotli_is_out_of_sync(self, tmp_path):
        from server.__main__ import DEPS_STAMP_NAME, python_deps_state

        venv = _venv_with(tmp_path, "brotli-1.2.0")
        (venv / "bin").mkdir()
        (venv / "bin" / "pip").write_text("")
        (tmp_path / "pyproject.toml").write_text("")
        (venv / DEPS_STAMP_NAME).touch()          # newer than pyproject.toml
        state = python_deps_state(tmp_path)
        assert state["in_sync"] is False and "Brotli 1.2.0" in state["reason"]


def test_doctor_names_an_unfixed_node(monkeypatch, capsys):
    from server.lifecycle import node_env
    from server.main import _report_node

    site = node_env.NodeSite("this command", "x", node_env.OK, "/n/node", "v22.22.2")
    monkeypatch.setattr(node_env, "probe", lambda: [site])
    monkeypatch.setattr(node_env, "manager_of", lambda path: "fnm")
    _report_node()
    out = capsys.readouterr().out
    assert "security: Node 22.22.2 lacks the fix" in out and "fnm install 22" in out


class TestSetupChecksQuernsOwnVenv:
    """CodeRabbit, #546: setup run from another active venv does not re-exec,
    so the check must name quern's venv rather than read this interpreter."""

    def _venv(self, tmp_path, *dists):
        venv = _venv_with(tmp_path, *dists)
        (venv / "bin").mkdir()
        (venv / "bin" / "pip").write_text("")
        return venv

    def test_reports_the_hole_in_querns_venv(self, tmp_path):
        result = setup.check_security_pins(self._venv(tmp_path, "brotli-1.2.0"))
        assert result.status == CheckStatus.WARNING
        assert "Brotli 1.2.0" in result.message and "1.2.1+" in result.message
        assert "CVE-2026-111497" in result.detail and "doctor --fix" in result.detail

    def test_repairs_querns_venv_with_its_own_pip(self, tmp_path, monkeypatch):
        venv = self._venv(tmp_path, "brotli-1.2.0")
        used = []

        def apply(pip, **_k):
            used.append(pip)
            site = next(venv.glob("lib/python*/site-packages"))
            (site / "brotli-1.2.0.dist-info").rename(site / "brotli-1.2.1.dist-info")
        monkeypatch.setattr(security_pins, "apply", apply)
        result = setup.check_security_pins(venv, apply=True)
        assert result.status == CheckStatus.OK
        assert used == [str(venv / "bin" / "pip")]

    def test_a_failed_repair_is_said(self, tmp_path, monkeypatch):
        monkeypatch.setattr(security_pins, "apply", lambda pip, **k: "no network")
        result = setup.check_security_pins(self._venv(tmp_path, "brotli-1.2.0"), apply=True)
        assert result.status == CheckStatus.WARNING and "no network" in result.detail

    def test_ok_when_fixed(self, tmp_path):
        result = setup.check_security_pins(self._venv(tmp_path, "brotli-1.2.1"))
        assert result.status == CheckStatus.OK

    def test_no_venv_is_skipped_not_ok(self, tmp_path):
        """Could not check is not checked: SKIPPED, never OK."""
        assert setup.check_security_pins(None).status == CheckStatus.SKIPPED
        assert setup.check_security_pins(tmp_path / ".venv").status == CheckStatus.SKIPPED

    def test_setup_runs_the_check_on_querns_venv(self):
        import inspect
        source = inspect.getsource(setup.run_setup)
        assert 'check_security_pins(project_root / ".venv"' in source and "apply=True" in source
