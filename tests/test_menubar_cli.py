"""`quern menubar` and the app-version checks (#200, #201).

Nothing here touches the real ~/Applications, the running app, or the network:
the app directory, `pgrep`/`open` and the download are all faked.
"""

from __future__ import annotations

import plistlib
import sys
from pathlib import Path

import pytest

from server.lifecycle import menubar
from server.lifecycle import setup as setup_mod


def _make_app(path: Path, version: str | None) -> Path:
    (path / "Contents").mkdir(parents=True, exist_ok=True)
    info = {"CFBundleIdentifier": "dev.quern.menubar"}
    if version is not None:
        info["CFBundleShortVersionString"] = version
    (path / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
    return path


class Machine:
    """~/Applications, the app process, `open`, and a release download."""

    def __init__(self, monkeypatch, tmp_path, *, installed=None, running=False,
                 quern="0.18.5", wrapper=True, download=None):
        self.apps = tmp_path / "Applications"
        self.apps.mkdir()
        self.app = self.apps / "Quern.app"
        if installed is not None:
            _make_app(self.app, installed)
        self.running = running
        self.opens: list[str] = []
        self.downloads: list[str] = []
        self.open_result = (0, "")
        wrapper_path = tmp_path / "bin" / "quern"
        if wrapper:
            wrapper_path.parent.mkdir()
            wrapper_path.write_text("#!/bin/sh\n")

        monkeypatch.setattr(setup_mod, "MENUBAR_APP_DIR", self.apps)
        monkeypatch.setattr(setup_mod, "WRAPPER_PATH", wrapper_path)
        monkeypatch.setattr(menubar.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(setup_mod.platform, "system", lambda: "Darwin")
        monkeypatch.setattr("server.get_version", lambda: quern)
        monkeypatch.setattr(setup_mod, "_menubar_app_running", lambda: self.running)
        monkeypatch.setattr(setup_mod, "_quit_menubar_app", self._quit)
        monkeypatch.setattr(setup_mod, "_open_menubar_app", self._open)
        monkeypatch.setattr(setup_mod, "download_release_app",
                            download or self._download_ok)

    def _quit(self):
        self.running = False

    def _open(self, app):
        self.opens.append(str(app))
        if self.open_result[0] == 0:
            self.running = True
        return self.open_result

    def _download_ok(self, url, version, work):
        self.downloads.append(url)
        return _make_app(work / f"quern-{version}" / "Quern.app", version)

    def version(self):
        return menubar.app_version(self.app)


class TestState:
    def test_an_older_app_is_behind(self, monkeypatch, tmp_path):
        Machine(monkeypatch, tmp_path, installed="0.18.3", quern="0.18.5")
        s = menubar.state()
        assert s.installed and s.version == "0.18.3" and s.behind

    @pytest.mark.parametrize("app", ["0.18.5", "0.19.0", None])
    def test_current_newer_or_unreadable_is_not_behind(self, monkeypatch, tmp_path, app):
        """A dev build is the same version; a missing version is not evidence."""
        Machine(monkeypatch, tmp_path, installed=app, quern="0.18.5")
        assert not menubar.state().behind

    def test_not_installed(self, monkeypatch, tmp_path):
        Machine(monkeypatch, tmp_path)
        s = menubar.state()
        assert not s.installed and s.version is None and not s.behind

    def test_describe_says_what_to_run(self, monkeypatch, tmp_path):
        Machine(monkeypatch, tmp_path)
        assert "menubar install" in "\n".join(menubar.describe(menubar.state()))

    def test_describe_names_both_versions_when_behind(self, monkeypatch, tmp_path):
        Machine(monkeypatch, tmp_path, installed="0.18.3", quern="0.18.5", running=True)
        text = "\n".join(menubar.describe(menubar.state()))
        assert "v0.18.3" in text and "v0.18.5" in text and "menubar install" in text
        assert "menubar open" not in text


class TestOpen:
    def test_starts_a_stopped_app(self, monkeypatch, tmp_path):
        m = Machine(monkeypatch, tmp_path, installed="0.18.5")
        assert menubar.cmd_open() == 0
        assert m.opens == [str(m.app)]

    def test_leaves_a_running_app_alone(self, monkeypatch, tmp_path):
        m = Machine(monkeypatch, tmp_path, installed="0.18.5", running=True)
        assert menubar.cmd_open() == 0
        assert m.opens == []

    def test_says_how_to_install_when_there_is_nothing_to_open(self, monkeypatch, tmp_path, capsys):
        m = Machine(monkeypatch, tmp_path)
        assert menubar.cmd_open() == 1
        assert m.opens == [] and "menubar install" in capsys.readouterr().out

    def test_a_failed_open_is_a_failure(self, monkeypatch, tmp_path):
        m = Machine(monkeypatch, tmp_path, installed="0.18.5")
        m.open_result = (1, "error -600")
        assert menubar.cmd_open() == 1


class TestInstall:
    def test_a_current_app_is_not_downloaded_again(self, monkeypatch, tmp_path):
        """Including a dev build of the same version, which is how someone
        working on the app runs it."""
        m = Machine(monkeypatch, tmp_path, installed="0.18.5", running=True)
        assert menubar.cmd_install() == 0
        assert m.downloads == []

    def test_force_reinstalls(self, monkeypatch, tmp_path):
        m = Machine(monkeypatch, tmp_path, installed="0.18.5", running=True)
        assert menubar.cmd_install(force=True) == 0
        assert len(m.downloads) == 1

    def test_an_older_app_is_replaced_and_started(self, monkeypatch, tmp_path):
        m = Machine(monkeypatch, tmp_path, installed="0.18.3", running=True, quern="0.18.5")
        assert menubar.cmd_install() == 0
        assert m.downloads == [
            "https://github.com/quern-dev/quern/releases/download/v0.18.5/quern-0.18.5.tar.gz"
        ]
        assert m.version() == "0.18.5"
        assert m.opens == [str(m.app)] and m.running
        assert not (m.apps / "Quern.app.incoming").exists()
        assert [p.name for p in m.apps.iterdir()] == ["Quern.app"], "left a temp dir behind"

    def test_a_missing_app_is_installed(self, monkeypatch, tmp_path):
        m = Machine(monkeypatch, tmp_path, quern="0.18.5")
        assert menubar.cmd_install() == 0
        assert m.version() == "0.18.5"

    def test_an_unverified_download_is_not_installed(self, monkeypatch, tmp_path, capsys):
        def untrusted(url, version, work):
            raise setup_mod._UntrustedBundle("not ours")

        m = Machine(monkeypatch, tmp_path, installed="0.18.3", running=True, download=untrusted)
        assert menubar.cmd_install() == 1
        assert m.version() == "0.18.3" and m.running, "the working app was disturbed"
        out = capsys.readouterr().out
        assert "verification" in out and "Manual download" not in out

    def test_a_failed_download_keeps_the_old_app(self, monkeypatch, tmp_path, capsys):
        def offline(url, version, work):
            raise OSError("network is unreachable")

        m = Machine(monkeypatch, tmp_path, installed="0.18.3", running=True, download=offline)
        assert menubar.cmd_install() == 1
        assert m.version() == "0.18.3" and m.running
        assert "Manual download" in capsys.readouterr().out

    def test_without_the_wrapper_nothing_is_downloaded(self, monkeypatch, tmp_path, capsys):
        m = Machine(monkeypatch, tmp_path, wrapper=False)
        assert menubar.cmd_install() == 1
        assert m.downloads == [] and "setup" in capsys.readouterr().out

    def test_installed_but_not_started_is_reported(self, monkeypatch, tmp_path, capsys):
        m = Machine(monkeypatch, tmp_path, installed="0.18.3", running=True)
        m.open_result = (1, "error -600")
        assert menubar.cmd_install() == 1
        out = capsys.readouterr().out
        assert "stopped" in out and "menubar open" in out


class TestCommandLine:
    @pytest.mark.parametrize("argv, force, called", [
        ([], False, "status"), (["status"], False, "status"), (["open"], False, "open"),
        (["install"], False, "install"), (["install"], True, "install-force"),
    ])
    def test_dispatch(self, monkeypatch, argv, force, called):
        seen = []
        monkeypatch.setattr(menubar, "cmd_status", lambda: seen.append("status") or 0)
        monkeypatch.setattr(menubar, "cmd_open", lambda: seen.append("open") or 0)
        def install(force=False):
            seen.append("install-force" if force else "install")
            return 0

        monkeypatch.setattr(menubar, "cmd_install", install)
        assert menubar.main(argv, force=force) == 0
        assert seen == [called]

    @pytest.mark.parametrize("argv, force", [
        (["bogus"], False), (["install", "extra"], False), (["open"], True),
    ])
    def test_anything_else_is_usage(self, argv, force, capsys):
        assert menubar.main(argv, force=force) == 2
        assert "usage: quern menubar" in capsys.readouterr().out

    def test_the_entry_point_passes_force(self, monkeypatch):
        from server import __main__ as entry

        seen = []
        monkeypatch.setattr(menubar, "main",
                            lambda argv, force=False: seen.append((argv, force)) or 0)
        monkeypatch.setattr(entry, "_maybe_reexec_in_venv", lambda: None)
        monkeypatch.setattr(entry.sys, "argv", ["quern", "menubar", "install", "--force"])
        with pytest.raises(SystemExit) as exit_:
            entry.main()
        assert exit_.value.code == 0
        assert seen == [(["install"], True)]


class TestDoctorAndSetup:
    def test_doctor_reports_the_app(self, monkeypatch, tmp_path, capsys):
        from server import main

        Machine(monkeypatch, tmp_path, installed="0.18.3", quern="0.18.5", running=True)
        assert main._report_menubar() is True
        out = capsys.readouterr().out
        assert "Menu-bar app:" in out and "v0.18.3" in out

    def test_doctor_fix_installs_an_older_app(self, monkeypatch, tmp_path):
        from server import main

        m = Machine(monkeypatch, tmp_path, installed="0.18.3", quern="0.18.5", running=True)
        main._report_menubar(fix=True)
        assert m.version() == "0.18.5"

    def test_doctor_without_fix_changes_nothing(self, monkeypatch, tmp_path):
        from server import main

        m = Machine(monkeypatch, tmp_path, installed="0.18.3", quern="0.18.5")
        main._report_menubar()
        assert m.downloads == [] and m.version() == "0.18.3"

    def test_doctor_says_nothing_off_macos(self, monkeypatch, capsys):
        from server import main

        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr("platform.system", lambda: "Linux")
        assert main._report_menubar() is True
        assert "Menu-bar" not in capsys.readouterr().out

    def test_setup_warns_a_git_install_with_an_older_app(self, monkeypatch, tmp_path):
        Machine(monkeypatch, tmp_path, installed="0.18.3", quern="0.18.5")
        root = tmp_path / "clone"
        (root / ".git").mkdir(parents=True)
        result = setup_mod.check_menubar_current(root)
        assert result is not None
        assert result.status == setup_mod.CheckStatus.WARNING
        assert "menubar install" in result.detail

    def test_setup_is_quiet_when_current_or_a_release(self, monkeypatch, tmp_path):
        Machine(monkeypatch, tmp_path, installed="0.18.3", quern="0.18.5")
        release = tmp_path / "release"
        release.mkdir()
        assert setup_mod.check_menubar_current(release) is None

        # The same machine, with the app brought up to date.
        _make_app(setup_mod.MENUBAR_APP_DIR / "Quern.app", "0.18.5")
        clone = tmp_path / "clone"
        (clone / ".git").mkdir(parents=True)
        assert setup_mod.check_menubar_current(clone) is None
