"""Tests for automatic Python dependency reconciliation.

`quern update` could report success onto a stale venv: its `pip install` failure
was a warning, and it skipped the rebuild entirely when the workspace was not
pullable (the #42 feature-branch case). Neither path saw a manual `git pull` or a
branch switch at all, which is the common case when work happens on branches.

The fix mirrors `_ensure_mcp_built`: an mtime stamp decides cheaply whether work
is needed, and the install runs automatically. The property that makes recovery
need no user intervention is that **the stamp is written only on success** — a
failed install is never remembered as done, so the next start simply retries.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from server.__main__ import DEPS_STAMP_NAME, _ensure_python_deps, python_deps_state


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project root with a venv-shaped layout and a pyproject."""
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "pip").write_text("#!/bin/sh\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "quern"\n')
    monkeypatch.setattr("server.__main__._find_project_root", lambda: tmp_path)
    return tmp_path


def _stamp(project):
    return project / ".venv" / DEPS_STAMP_NAME


def _pip_result(returncode: int, stderr: str = ""):
    return SimpleNamespace(returncode=returncode, stdout="", stderr=stderr)


# --------------------------------------------------------------------------
# The read-only state check
# --------------------------------------------------------------------------


def test_no_venv_is_not_applicable(tmp_path, monkeypatch):
    """A tarball or system install has no venv to reconcile."""
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    monkeypatch.setattr("server.__main__._find_project_root", lambda: tmp_path)
    state = python_deps_state()
    assert state["applicable"] is False
    assert state["in_sync"] is True


def test_missing_stamp_reads_as_out_of_sync(project):
    state = python_deps_state()
    assert state["applicable"] is True
    assert state["in_sync"] is False
    assert "never been reconciled" in state["reason"]


def test_pyproject_newer_than_stamp_is_out_of_sync(project):
    """The manual `git pull` and branch-switch case."""
    _stamp(project).touch()
    import os
    stamp_mtime = _stamp(project).stat().st_mtime
    os.utime(project / "pyproject.toml", (stamp_mtime + 10, stamp_mtime + 10))

    state = python_deps_state()
    assert state["in_sync"] is False
    assert "newer" in state["reason"]


def test_stamp_newer_than_pyproject_is_in_sync(project):
    _stamp(project).touch()
    assert python_deps_state()["in_sync"] is True


# --------------------------------------------------------------------------
# The heal
# --------------------------------------------------------------------------


def test_in_sync_does_no_work(project, monkeypatch):
    """The common path must stay two stat calls — no pip, no network."""
    _stamp(project).touch()
    called = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.append(a) or _pip_result(0))

    assert _ensure_python_deps(quiet=True) is True
    assert called == [], "pip was invoked when the venv was already in sync"


def test_out_of_sync_installs_and_stamps(project, monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _pip_result(0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert _ensure_python_deps(quiet=True) is True
    assert _stamp(project).exists(), "success must write the stamp"
    assert calls and calls[0][1:] == ["install", "-e", "."]


def test_failure_does_not_write_the_stamp(project, monkeypatch):
    """The property the whole design rests on.

    If a failed install stamped, the next start would believe it was done and
    the venv would stay broken until a human intervened. Leaving the stamp
    absent is what makes recovery automatic once the network returns.
    """
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _pip_result(1, "Could not fetch URL https://pypi.org/simple/"),
    )

    assert _ensure_python_deps(quiet=True) is False
    assert not _stamp(project).exists(), "a failed install must not look done"

    # The next start retries by itself, and succeeds once the network is back.
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _pip_result(0))
    assert _ensure_python_deps(quiet=True) is True
    assert _stamp(project).exists()


def test_timeout_is_treated_as_failure(project, monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="pip", timeout=300)

    monkeypatch.setattr(subprocess, "run", boom)
    assert _ensure_python_deps(quiet=True) is False
    assert not _stamp(project).exists()


def test_force_installs_even_when_in_sync(project, monkeypatch):
    """What `quern doctor --fix` relies on."""
    _stamp(project).touch()
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: calls.append(cmd) or _pip_result(0))

    assert _ensure_python_deps(quiet=True, force=True) is True
    assert calls, "--fix must install even when the stamp says in sync"


def test_no_venv_heals_trivially(tmp_path, monkeypatch):
    """Nothing to reconcile must not be reported as a failure."""
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    monkeypatch.setattr("server.__main__._find_project_root", lambda: tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("pip should not run"))
    assert _ensure_python_deps(quiet=True) is True
