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


# --------------------------------------------------------------------------
# The updater must not report success onto a stale venv (#72 review finding)
# --------------------------------------------------------------------------


def test_updater_propagates_a_failed_install(tmp_path, monkeypatch, capsys):
    """`quern update` must fail when dependencies could not be installed.

    This is the exact failure the PR exists to remove: the old code's pip call
    printed a warning and carried on, so an update announced success onto a
    venv that had not been reconciled. Delegating to _ensure_python_deps fixed
    the duplication but re-created the bug by discarding its return value.
    """
    from server.lifecycle import updater

    monkeypatch.setattr(updater, "_find_project_root", lambda: tmp_path)
    monkeypatch.setattr(updater, "_is_git_install", lambda _p: True)
    monkeypatch.setattr(updater, "_update_via_git", lambda _p: 0)
    monkeypatch.setattr(updater, "_rebuild_and_restart", lambda _p: False)

    assert updater.run_update() == 1
    assert "Update incomplete" in capsys.readouterr().out


def test_updater_reports_success_when_deps_install(tmp_path, monkeypatch):
    from server.lifecycle import updater

    monkeypatch.setattr(updater, "_find_project_root", lambda: tmp_path)
    monkeypatch.setattr(updater, "_is_git_install", lambda _p: True)
    monkeypatch.setattr(updater, "_update_via_git", lambda _p: 0)
    monkeypatch.setattr(updater, "_rebuild_and_restart", lambda _p: True)

    assert updater.run_update() == 0


# --------------------------------------------------------------------------
# Upgrade strategy: which call sites move versions forward
# --------------------------------------------------------------------------
#
# pip's default strategy (only-if-needed) leaves any already-satisfying version
# alone, so a venv drifts arbitrarily far behind while every declared floor
# stays satisfied. Measured on this project: an eager run moved 31 packages,
# including starlette across a 0.x -> 1.x major, off a venv that pip considered
# fully in sync. So "in sync" says nothing about how current the venv is, and
# the distinction below is the only thing that makes `quern update` an upgrade.


def _capture_pip(monkeypatch, project):
    """Record the argv `_ensure_python_deps` hands to pip."""
    seen: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        seen.append(cmd)
        return _pip_result(0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return seen


def test_the_default_install_does_not_upgrade(project, monkeypatch):
    """The start path runs on every launch; it must stay a constraint check.

    Making this eager would turn each start into a network-bound resolve of the
    whole transitive tree.
    """
    seen = _capture_pip(monkeypatch, project)
    assert _ensure_python_deps(quiet=True) is True
    assert seen == [[str(project / ".venv" / "bin" / "pip"), "install", "-e", "."]]
    assert "--upgrade" not in seen[0]


def test_eager_asks_pip_to_move_transitive_deps(project, monkeypatch):
    seen = _capture_pip(monkeypatch, project)
    assert _ensure_python_deps(quiet=True, eager=True) is True
    assert seen[0][-3:] == ["--upgrade", "--upgrade-strategy", "eager"]


def test_force_alone_is_not_an_upgrade(project, monkeypatch):
    """`quern doctor --fix` repairs a venv; it does not roll it forward.

    Bundling an upgrade into a repair would change more than the reported fault,
    which makes a failed repair much harder to attribute.
    """
    seen = _capture_pip(monkeypatch, project)
    assert _ensure_python_deps(quiet=True, force=True) is True
    assert "--upgrade" not in seen[0]


def test_a_failed_eager_install_is_not_stamped(project, monkeypatch):
    """Same contract as the non-eager path: failure must not look done."""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _pip_result(1, "boom"))
    assert _ensure_python_deps(quiet=True, eager=True) is False
    assert not _stamp(project).exists()


def test_update_asks_for_an_eager_install(monkeypatch):
    """Pin the wiring: `quern update` is the one caller that upgrades.

    Tested at the call site because the flag is only meaningful if the updater
    actually passes it -- a correct `_ensure_python_deps` wired to nothing would
    leave `quern update` silently non-upgrading, which is the bug this prevents.
    """
    import inspect

    from server.lifecycle import updater

    source = inspect.getsource(updater)
    assert "_ensure_python_deps(quiet=False, force=True, eager=True)" in source


# --------------------------------------------------------------------------
# A forced install that fails must not be remembered as done
# --------------------------------------------------------------------------
#
# Not writing the stamp on failure is enough only when the stamp is absent or
# older than pyproject.toml. A *forced* install runs regardless of the stamp, so
# it can fail against one that is already current from an earlier success --
# and leaving it alone records the failure as complete. The next start then
# reads "up to date" and skips, which makes the message the failure path prints
# ("starting Quern again will retry automatically") false.
#
# Reproduced before fixing: prior success -> failing eager install -> the next
# start reported in_sync=True with reason "up to date".


def test_a_failed_forced_install_clears_a_current_stamp(project, monkeypatch):
    _stamp(project).touch()
    assert python_deps_state()["in_sync"] is True

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _pip_result(1, "boom"))
    assert _ensure_python_deps(quiet=True, force=True, eager=True) is False

    assert not _stamp(project).exists()
    state = python_deps_state()
    assert state["in_sync"] is False, "the failed upgrade was remembered as done"


def test_the_next_start_retries_after_a_failed_eager_update(project, monkeypatch):
    """End to end: the promise the failure message makes must hold.

    An eager `quern update` fails against a healthy stamp; the following start
    must run pip again rather than skipping on a venv that may be half upgraded.
    """
    _stamp(project).touch()

    calls: list[list[str]] = []

    def failing(cmd, **_kw):
        calls.append(cmd)
        return _pip_result(1, "boom")

    monkeypatch.setattr(subprocess, "run", failing)
    assert _ensure_python_deps(quiet=True, force=True, eager=True) is False

    # The start path: stamp-gated, no force.
    def succeeding(cmd, **_kw):
        calls.append(cmd)
        return _pip_result(0)

    monkeypatch.setattr(subprocess, "run", succeeding)
    assert _ensure_python_deps(quiet=True) is True

    assert len(calls) == 2, "the start path skipped instead of retrying"
    assert "--upgrade" not in calls[1], "the retry must stay non-eager"
    assert _stamp(project).exists(), "a successful retry should stamp again"


def test_a_successful_forced_install_still_stamps(project, monkeypatch):
    """Guard the other direction: invalidating on failure must not also clear
    the stamp on the success path."""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _pip_result(0))
    assert _ensure_python_deps(quiet=True, force=True, eager=True) is True
    assert _stamp(project).exists()


@pytest.mark.parametrize("failure", [
    subprocess.TimeoutExpired(cmd="pip", timeout=300),
    FileNotFoundError("pip is gone"),
])
def test_a_raised_install_failure_also_clears_the_stamp(project, monkeypatch, failure):
    """The first version of this fix cleared the stamp only on a non-zero exit.

    A raised failure returned early and left a current stamp behind, so the next
    start skipped reconciliation exactly as before. The timeout case is the one
    that matters most: an eager install moves the whole transitive tree, and a
    300s timeout is the likeliest way to end up with it half applied.
    """
    _stamp(project).touch()
    assert python_deps_state()["in_sync"] is True

    def raising(*_a, **_k):
        raise failure

    monkeypatch.setattr(subprocess, "run", raising)
    assert _ensure_python_deps(quiet=True, force=True, eager=True) is False

    assert not _stamp(project).exists()
    assert python_deps_state()["in_sync"] is False
