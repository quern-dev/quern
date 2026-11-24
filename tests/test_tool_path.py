"""Setup and update find Homebrew and pipx when PATH doesn't name them.

Measured on a Mac updated over ssh, whose shell had sshd's PATH alone: setup
reported Homebrew "Not installed", the pymobiledevice3 upgrade failed for want
of pipx, and the update ended "Update incomplete" although the code had
updated. Each test builds its own directories; nothing here reads the real
/opt/homebrew.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from server.lifecycle import tool_path


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    """Stand-ins for Homebrew's bin and sbin, an Intel /usr/local/bin and
    pipx's bin dir -- all existing -- and a PATH of sshd's four."""
    brew_bin, brew_sbin, local_bin = (tmp_path / "brew" / "bin", tmp_path / "brew" / "sbin",
                                      tmp_path / "usr-local" / "bin")
    for d in (brew_bin, brew_sbin, local_bin):
        d.mkdir(parents=True)
    pipx_bin = tmp_path / "home" / ".local" / "bin"
    pipx_bin.mkdir(parents=True)
    monkeypatch.setattr(tool_path, "STANDARD_DIRS", (str(brew_bin), str(brew_sbin),
                                                     str(local_bin)))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.delenv("PIPX_BIN_DIR", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    return {"brew": str(brew_bin), "sbin": str(brew_sbin), "local": str(local_bin),
            "pipx": str(pipx_bin)}


def _path() -> list[str]:
    return os.environ["PATH"].split(os.pathsep)


def test_an_ssh_shells_path_gains_homebrew_and_pipx(dirs) -> None:
    added = tool_path.add_standard_tool_dirs()

    assert added == [dirs["brew"], dirs["sbin"], dirs["local"], dirs["pipx"]]
    assert _path() == ["/usr/bin", "/bin", "/usr/sbin", "/sbin", *added]


def test_what_the_user_put_first_still_wins(dirs, monkeypatch) -> None:
    """Appended, never prepended: a user's own Homebrew elsewhere, or a
    pinned tool directory, is not shadowed by the standard one."""
    monkeypatch.setenv("PATH", f"/my/tools:{dirs['brew']}:/usr/bin")

    added = tool_path.add_standard_tool_dirs()

    assert dirs["brew"] not in added, "a directory already there was added again"
    assert _path()[:3] == ["/my/tools", dirs["brew"], "/usr/bin"]


@pytest.mark.parametrize("given", [":/usr/bin", "/usr/bin::/bin", "/usr/bin:"])
def test_empty_entries_are_kept(dirs, monkeypatch, given) -> None:
    """An empty entry is the current directory: the user's to keep, in
    its place (CR)."""
    monkeypatch.setenv("PATH", given)

    added = tool_path.add_standard_tool_dirs()

    assert os.environ["PATH"] == given + os.pathsep + os.pathsep.join(added)


@pytest.mark.parametrize("unset", [True, False])
def test_an_unset_or_empty_path_gains_no_current_directory(dirs, monkeypatch, unset) -> None:
    if unset:
        monkeypatch.delenv("PATH")
    else:
        monkeypatch.setenv("PATH", "")

    added = tool_path.add_standard_tool_dirs()

    assert os.environ["PATH"] == os.pathsep.join(added)
    assert "" not in _path()


def test_a_directory_that_does_not_exist_is_not_added(dirs, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(tool_path, "STANDARD_DIRS", (str(tmp_path / "no-such-brew"),))

    added = tool_path.add_standard_tool_dirs()

    assert str(tmp_path / "no-such-brew") not in _path()
    assert added == [dirs["pipx"]]


def test_a_second_call_adds_nothing(dirs) -> None:
    tool_path.add_standard_tool_dirs()
    before = os.environ["PATH"]

    assert tool_path.add_standard_tool_dirs() == []
    assert os.environ["PATH"] == before


def test_pipx_bin_dir_is_honoured(dirs, monkeypatch, tmp_path) -> None:
    custom = tmp_path / "pipx-bin"
    custom.mkdir()
    monkeypatch.setenv("PIPX_BIN_DIR", str(custom))

    added = tool_path.add_standard_tool_dirs()

    assert str(custom) in added and dirs["pipx"] not in added


@pytest.mark.parametrize("argv", [["quern", "setup"], ["quern", "update"]])
def test_setup_and_update_run_with_the_standard_dirs_on_path(dirs, monkeypatch, argv) -> None:
    """Through `main`, where it matters: what the command itself sees.
    `update`'s `--finish` child and the server it restarts inherit it."""
    import server.__main__ as entry
    from server.lifecycle import setup, updater

    seen = {}

    def record(**_kwargs) -> int:
        seen["path"] = os.environ["PATH"]
        return 0

    monkeypatch.setattr(setup, "run_setup", record)
    monkeypatch.setattr(updater, "run_update", record)
    monkeypatch.setattr(entry.sys, "argv", argv)
    with pytest.raises(SystemExit) as exc:
        entry.main()

    assert exc.value.code == 0
    path = seen["path"].split(os.pathsep)
    assert dirs["brew"] in path and dirs["pipx"] in path, seen
