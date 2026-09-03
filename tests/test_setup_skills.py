"""Installing and retiring Claude Code skills.

Skills are symlinked into ~/.claude/skills/. Adding one is easy; removing one
is where it goes wrong -- the link survives on every machine that ever ran
setup, pointing at a directory that no longer exists.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from server.lifecycle.setup import _install_skills


@pytest.fixture
def home(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    (fake_home / ".claude" / "skills").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    return fake_home


def _project(tmp_path, *skills):
    root = tmp_path / "project"
    (root / "skills").mkdir(parents=True)
    for name in skills:
        (root / "skills" / name).mkdir()
        (root / "skills" / name / "SKILL.md").write_text("# skill\n")
    return root


def test_skills_are_linked(tmp_path, home):
    root = _project(tmp_path, "quern-api")
    result = _install_skills(root)
    link = home / ".claude" / "skills" / "quern-api"
    assert link.is_symlink() and link.exists()
    assert "Linked 1" in result.message


def test_a_retired_skill_leaves_no_dangling_link(tmp_path, home):
    """The case this exists for: a skill is removed from the repo, and the
    machine is left pointing at a directory that is gone."""
    root = _project(tmp_path, "quern-api", "quern-landmark-migration")
    _install_skills(root)
    stale = home / ".claude" / "skills" / "quern-landmark-migration"
    assert stale.is_symlink()

    # Retire it.
    import shutil
    shutil.rmtree(root / "skills" / "quern-landmark-migration")
    result = _install_skills(root)

    assert not stale.is_symlink(), "the dangling link survived"
    assert (home / ".claude" / "skills" / "quern-api").exists()
    assert "stale" in result.message


def test_someone_elses_broken_link_is_left_alone(tmp_path, home):
    """Only links into our own skills directory are ours to remove."""
    root = _project(tmp_path, "quern-api")
    foreign = home / ".claude" / "skills" / "someone-elses"
    foreign.symlink_to(tmp_path / "elsewhere" / "someone-elses")
    assert foreign.is_symlink() and not foreign.exists()

    _install_skills(root)

    assert foreign.is_symlink(), "a broken link we did not create was removed"


def test_a_real_directory_is_never_replaced(tmp_path, home):
    """A user's own skill of the same name is theirs, not ours to overwrite."""
    root = _project(tmp_path, "quern-api")
    theirs = home / ".claude" / "skills" / "quern-api"
    theirs.mkdir()
    (theirs / "SKILL.md").write_text("# mine\n")

    _install_skills(root)

    assert theirs.is_dir() and not theirs.is_symlink()
    assert (theirs / "SKILL.md").read_text() == "# mine\n"


def test_relinking_an_unchanged_install_is_a_no_op(tmp_path, home):
    root = _project(tmp_path, "quern-api")
    _install_skills(root)
    before = os.readlink(home / ".claude" / "skills" / "quern-api")
    result = _install_skills(root)
    assert os.readlink(home / ".claude" / "skills" / "quern-api") == before
    assert "already linked" in (result.detail or "")
