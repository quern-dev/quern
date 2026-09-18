"""`quern menubar` and the app-version checks (#200, #201).

Nothing here touches the real ~/Applications, the running app, or the network:
the app directory, `pgrep`/`open` and the download are all faked.
"""

from __future__ import annotations

import plistlib
import sys
from pathlib import Path
from types import SimpleNamespace

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
        self.asked: list = []
        self.other_running = False
        self.downloads: list[str] = []
        self.quits: list = []
        self.refuses_to_quit = False
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
        monkeypatch.setattr(setup_mod, "_menubar_app_running", self._running)
        monkeypatch.setattr(setup_mod, "_quit_menubar_app", self._quit)
        monkeypatch.setattr(setup_mod, "_open_menubar_app", self._open)
        monkeypatch.setattr(setup_mod, "download_release_app",
                            download or self._download_ok)

    def _running(self, app=None):
        self.asked.append(app)
        # `other` stands for a Quern running from somewhere else: the generic
        # question says yes, the question about *this* bundle says no.
        if app is None:
            return self.running or self.other_running
        return self.running

    def _quit(self, app=None):
        self.quits.append(app)
        if self.refuses_to_quit:
            return False
        self.running = False
        return True

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


class TestTheRightAppIsAskedAbout:
    def test_status_asks_about_its_own_bundle(self, monkeypatch, tmp_path):
        """With any other Quern.app running, a general match called a
        never-launched install "running". Found live."""
        m = Machine(monkeypatch, tmp_path, installed="0.18.5")
        menubar.state()
        assert m.asked == [m.app]

    def test_nothing_installed_is_never_running(self, monkeypatch, tmp_path):
        m = Machine(monkeypatch, tmp_path, running=True)   # another copy runs
        assert menubar.state().running is False
        assert m.asked == []

    def test_a_first_install_does_not_claim_it_stopped_one(self, monkeypatch, tmp_path, capsys):
        m = Machine(monkeypatch, tmp_path, running=True)   # another copy runs
        m.open_result = (1, "error -600")
        menubar.cmd_install()
        assert "stopped" not in capsys.readouterr().out

    def test_the_pattern_names_the_bundle(self, monkeypatch):
        seen = []
        def run(cmd, timeout=30):
            seen.append(cmd)
            return (1, "", "")

        monkeypatch.setattr(setup_mod, "_run", run)
        setup_mod._menubar_app_running(Path("/Users/u/Applications/Quern.app"))
        setup_mod._menubar_app_running()
        assert seen[0] == ["pgrep", "-f",
                           r"/Users/u/Applications/Quern\.app/Contents/MacOS/QuernMenuBar"]
        assert seen[1] == ["pgrep", "-f", "Quern.app/Contents/MacOS/QuernMenuBar"]


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


class TestAFailedSwapKeepsAWorkingApp:
    """An agent review reproduced this: `rmtree(ignore_errors=True)` can leave
    part of a bundle behind, the rename then fails with ENOTEMPTY, and the app
    the user was running has already been gutted."""

    def test_the_old_app_survives_a_failed_rename(self, monkeypatch, tmp_path, capsys):
        m = Machine(monkeypatch, tmp_path, installed="0.18.3", running=True, quern="0.18.5")
        real_replace = menubar.os.replace
        calls = {"n": 0}

        def replace(src, dst):
            # Only the staging move fails, which is the real shape: ENOTEMPTY
            # because a remnant of the old bundle is still there. Putting the
            # old one back targets a name nothing holds.
            calls["n"] += 1
            if str(src).endswith("Quern.app.incoming"):
                raise OSError(66, "Directory not empty")
            return real_replace(src, dst)

        monkeypatch.setattr(menubar.os, "replace", replace)
        assert menubar.cmd_install() == 1
        assert m.version() == "0.18.3", "the working app was destroyed"
        assert "Could not install" in capsys.readouterr().out

    def test_a_half_installed_app_is_not_called_installed(self, monkeypatch, tmp_path):
        """The wreckage satisfied is_dir(), so a retry said "already installed"
        and exited 0, and doctor --fix passed it by."""
        m = Machine(monkeypatch, tmp_path, quern="0.18.5")
        (m.app / "Contents").mkdir(parents=True)          # no Info.plist
        s = menubar.state()
        assert s.installed and s.version is None and s.damaged
        assert "damaged" in "\n".join(menubar.describe(s))

        assert menubar.cmd_install() == 0
        assert m.downloads, "a damaged app was not replaced"
        assert m.version() == "0.18.5"

    def test_a_version_nothing_can_parse_is_repaired_without_force(self, monkeypatch, tmp_path):
        """`"1.0 (build 3)"` is present and unparseable: not missing, not
        behind, so the ordinary install path skipped it and only `--force`
        could repair it -- which nobody knows to reach for."""
        m = Machine(monkeypatch, tmp_path, installed="1.0 (build 3)", quern="0.18.5")
        assert menubar.state().needs_install
        assert menubar.cmd_install() == 0
        assert m.downloads and m.version() == "0.18.5"

    def test_a_newer_app_is_still_left_alone(self, monkeypatch, tmp_path):
        """A dev build ahead of the release must survive the same predicate."""
        m = Machine(monkeypatch, tmp_path, installed="0.19.0", running=True, quern="0.18.5")
        assert not menubar.state().needs_install
        assert menubar.cmd_install() == 0
        assert m.downloads == [] and m.version() == "0.19.0"

    def test_an_unreadable_version_is_not_treated_as_current(self):
        """`_older` bailing to False on an unparseable version is right; the
        branch was never exercised, because `behind` short-circuits on None."""
        assert menubar._older("1.0 (build 3)", "0.18.5") is False
        assert menubar._older("0.18.4", "0.18.5") is True

    def test_the_download_lands_beside_the_destination(self, monkeypatch, tmp_path):
        """`dir=apps` is why the install is a rename. Moving it to $TMPDIR
        makes every install fail with EXDEV on a machine whose install and
        temp directory are on different volumes -- which is this project's own
        machine, and the reason the docstring says so."""
        m = Machine(monkeypatch, tmp_path, quern="0.18.5")
        seen = {}
        real = menubar.tempfile.TemporaryDirectory

        def recording(*a, **kw):
            seen.update(kw)
            return real(*a, **kw)

        monkeypatch.setattr(menubar.tempfile, "TemporaryDirectory", recording)
        menubar.cmd_install()
        assert seen.get("dir") == m.apps

    def test_another_quern_is_not_quit_out_from_under_someone(
        self, monkeypatch, tmp_path, capsys,
    ):
        """`_quit_menubar_app` asks by application name, so quitting
        unconditionally stopped a Quern running from another checkout."""
        m = Machine(monkeypatch, tmp_path, installed="0.18.3", quern="0.18.5")
        m.other_running = True
        # 1, not 0: a new app on disk and nothing in the menu bar is the same
        # outcome as a failed launch, and `install && ...` must not carry on.
        assert menubar.cmd_install() == 1
        assert m.quits == [], "quit an app this command does not own"
        assert m.opens == [], "activated someone else's app instead of starting ours"
        out = capsys.readouterr().out
        assert "running from somewhere else" in out and "menubar open" in out

    def test_both_ours_and_another_copy_running_is_still_reported(
        self, monkeypatch, tmp_path, capsys,
    ):
        """Asking `not stopped` skipped the check when ours was running too,
        and claimed a launch that never happened."""
        m = Machine(monkeypatch, tmp_path, installed="0.18.3", running=True, quern="0.18.5")
        m.other_running = True
        assert menubar.cmd_install() == 1
        assert "running from somewhere else" in capsys.readouterr().out
        assert m.opens == [], "activated the other copy and called it ours"

    def test_a_running_app_is_quit_before_it_is_replaced(self, monkeypatch, tmp_path):
        """`open` activates a running instance rather than starting the new
        binary, so without the quit the old build keeps running."""
        m = Machine(monkeypatch, tmp_path, installed="0.18.3", running=True, quern="0.18.5")
        assert menubar.cmd_install() == 0
        assert m.quits == [m.app], "the running app was replaced underneath itself"

    def test_a_stale_incoming_bundle_is_cleared_first(self, monkeypatch, tmp_path):
        m = Machine(monkeypatch, tmp_path, installed="0.18.3", quern="0.18.5")
        stale = m.apps / "Quern.app.incoming"
        _make_app(stale, "0.0.1")
        assert menubar.cmd_install() == 0
        assert not stale.exists(), "a stale staging bundle was left behind"
        assert m.version() == "0.18.5"

    def test_an_app_that_will_not_quit_is_not_replaced(self, monkeypatch, tmp_path, capsys):
        """Swapping the bundle under a live app leaves it executing an image
        with no name on disk -- the state the quit exists to avoid."""
        m = Machine(monkeypatch, tmp_path, installed="0.18.3", running=True, quern="0.18.5")
        m.refuses_to_quit = True
        assert menubar.cmd_install() == 1
        assert m.version() == "0.18.3", "replaced the bundle under a running app"
        out = capsys.readouterr().out
        assert "would not quit" in out and "menu" in out

    def test_a_truncated_download_is_reported_not_raised(self, monkeypatch, tmp_path, capsys):
        """HTTPException is not an OSError, so it escaped the handler."""
        import http.client

        def truncated(url, version, work):
            raise http.client.IncompleteRead(b"half")

        m = Machine(monkeypatch, tmp_path, installed="0.18.3", running=True,
                    download=truncated)
        assert menubar.cmd_install() == 1
        assert m.version() == "0.18.3"
        assert "Could not install" in capsys.readouterr().out


class TestQuittingWaitsForTheRightApp:
    """`_quit_menubar_app` asks by application name; the *wait* is what knows
    which bundle. Reverting that wait to the generic form passed the whole
    suite, because every test replaced the function wholesale."""

    def test_it_waits_for_the_bundle_it_was_given(self, monkeypatch):
        import re

        ours = Path("/Users/u/Applications/Quern.app")
        asked = []
        alive = {"ours": True}

        def run(cmd, timeout=30):
            if cmd[0] == "pgrep":
                asked.append(cmd[-1])
                return (0, "4242", "") if alive["ours"] else (1, "", "")
            if cmd[0] == "osascript":
                alive["ours"] = False
            return (0, "", "")

        monkeypatch.setattr(setup_mod, "_run", run)
        monkeypatch.setattr(setup_mod.time, "sleep", lambda _s: None)
        assert setup_mod._quit_menubar_app(ours) is True
        assert asked, "it never checked whether the app had gone"
        # One generic question -- "is another copy running?" -- and every
        # other about this bundle. The *wait* must be bundle-specific.
        generic = [p for p in asked if re.escape(str(ours)) not in p]
        assert len(generic) <= 1, asked
        assert re.escape(str(ours)) in asked[-1], asked

    def test_another_copy_running_means_a_signal_not_an_applescript(self, monkeypatch):
        """AppleScript addresses an application by name, so with two copies
        running it could stop the wrong one. A signal cannot."""
        ours = Path("/Users/u/Applications/Quern.app")
        cmds = []
        alive = {"ours": True}

        def run(cmd, timeout=30):
            cmds.append(cmd)
            if cmd[0] == "pgrep":
                if str(ours).replace(".", "\\.") in cmd[-1]:
                    return (0, "111", "") if alive["ours"] else (1, "", "")
                return (0, "111 222", "")          # ours plus somebody else's
            if cmd[0] == "kill":
                alive["ours"] = False
            return (0, "", "")

        monkeypatch.setattr(setup_mod, "_run", run)
        monkeypatch.setattr(setup_mod.time, "sleep", lambda _s: None)
        assert setup_mod._quit_menubar_app(ours) is True
        assert not [c for c in cmds if c[0] == "osascript"], "asked by name with two copies up"
        kills = [c for c in cmds if c[0] == "kill"]
        assert kills == [["kill", "-TERM", "111"]], kills

    def test_an_app_that_will_not_die_is_reported_as_still_running(self, monkeypatch):
        ours = Path("/Users/u/Applications/Quern.app")

        def run(cmd, timeout=30):
            if cmd[0] == "pgrep":
                return (0, "111", "")              # never goes away
            return (0, "", "")

        monkeypatch.setattr(setup_mod, "_run", run)
        monkeypatch.setattr(setup_mod.time, "sleep", lambda _s: None)
        assert setup_mod._quit_menubar_app(ours) is False

    def test_it_returns_at_once_when_that_bundle_was_never_running(self, monkeypatch):
        calls = []

        def run(cmd, timeout=30):
            calls.append(cmd[0])
            return (1, "", "") if cmd[0] == "pgrep" else (0, "", "")

        monkeypatch.setattr(setup_mod, "_run", run)
        monkeypatch.setattr(setup_mod.time, "sleep",
                            lambda _s: pytest.fail("waited for an app that was not running"))
        setup_mod._quit_menubar_app(Path("/Users/u/Applications/Quern.app"))
        assert calls.count("pgrep") == 1


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


def main_report(monkeypatch, *, fix=False):
    from server import main

    return main._report_menubar(fix=fix)


class TestDoctorAndSetup:
    def test_doctor_reports_the_app(self, monkeypatch, tmp_path, capsys):
        from server import main

        Machine(monkeypatch, tmp_path, installed="0.18.3", quern="0.18.5", running=True)
        assert main._report_menubar() == (True, None)
        out = capsys.readouterr().out
        assert "Quern app:" in out and "v0.18.3" in out

    def test_doctor_fix_reports_a_failed_install(self, monkeypatch, tmp_path):
        """It printed "failed verification" and exited 0."""
        from server import main

        def untrusted(url, version, work):
            raise setup_mod._UntrustedBundle("not ours")

        Machine(monkeypatch, tmp_path, installed="0.18.3", quern="0.18.5", download=untrusted)
        assert main._report_menubar(fix=True) == (True, False), "a failed repair"

    def test_a_failed_repair_reaches_doctors_exit_code(self, monkeypatch, tmp_path):
        """`--fix` looked only at the Python-dependency repair, so a failed
        app install exited 0 anyway."""
        from server import main

        def untrusted(url, version, work):
            raise setup_mod._UntrustedBundle("not ours")

        Machine(monkeypatch, tmp_path, installed="0.18.3", quern="0.18.5", download=untrusted)
        monkeypatch.setattr(main, "_fetch_device_tools", lambda: ({"simctl": True}, ""))
        monkeypatch.setattr(main, "_report_python_deps", lambda _fix: True)
        monkeypatch.setattr(main, "_report_external_tools", lambda _fix: None)
        monkeypatch.setattr(main, "_report_node", lambda: True)
        monkeypatch.setattr(main, "_report_service_health", lambda _fix=False: True)
        with pytest.raises(SystemExit) as exit_:
            main._cmd_doctor(SimpleNamespace(fix=True))
        assert exit_.value.code == 1

    def test_doctor_fix_installs_an_older_app(self, monkeypatch, tmp_path):
        from server import main

        m = Machine(monkeypatch, tmp_path, installed="0.18.3", quern="0.18.5", running=True)
        main._report_menubar(fix=True)
        assert m.version() == "0.18.5"

    def test_doctor_fix_leaves_a_current_app_alone_and_says_nothing(
        self, monkeypatch, tmp_path, capsys,
    ):
        from server import main

        m = Machine(monkeypatch, tmp_path, installed="0.18.5", quern="0.18.5", running=True)
        main._report_menubar(fix=True)
        assert m.downloads == []
        assert "installing" not in capsys.readouterr().out

    def test_doctor_fix_repairs_a_damaged_app(self, monkeypatch, tmp_path):
        """The CHANGELOG says --fix reinstalls one that is "stale or damaged";
        only the stale half was pinned."""
        m = Machine(monkeypatch, tmp_path, quern="0.18.5")
        (m.app / "Contents").mkdir(parents=True)          # installed, no version
        checked, repaired = main_report(monkeypatch, fix=True)
        assert (checked, repaired) == (True, True)
        assert m.version() == "0.18.5"

    def test_doctor_fix_does_not_install_an_app_that_was_never_there(
        self, monkeypatch, tmp_path, capsys,
    ):
        """A first install writes a GUI app into ~/Applications and launches
        it. `doctor` runs unattended; `describe()` prints the command instead."""
        m = Machine(monkeypatch, tmp_path, quern="0.18.5")
        checked, repaired = main_report(monkeypatch, fix=True)
        assert (checked, repaired) == (True, None)
        assert m.downloads == []
        assert "menubar install" in capsys.readouterr().out

    def test_doctor_without_fix_changes_nothing(self, monkeypatch, tmp_path):
        from server import main

        m = Machine(monkeypatch, tmp_path, installed="0.18.3", quern="0.18.5")
        main._report_menubar()
        assert m.downloads == [] and m.version() == "0.18.3"

    def test_doctor_says_nothing_off_macos(self, monkeypatch, capsys):
        from server import main

        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr("platform.system", lambda: "Linux")
        assert main._report_menubar() == (True, None)
        assert "Quern app" not in capsys.readouterr().out

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
