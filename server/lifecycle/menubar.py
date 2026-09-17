"""`quern menubar`: install, open and report on the menu-bar app (#200, #201).

Before this the app had three ways in and none of them was a command:
- a release install got it from setup;
- a git install got it only from `scripts/install-menubar-app.sh`, run from
  inside the clone, which nothing mentioned;
- and once it was quit, the only way back was knowing the bundle's path.

`quern update` never touches it on a git install, so it drifted behind with
nothing saying so -- the menu showed no version of its own to notice by.
"""

from __future__ import annotations

import os
import platform
import plistlib
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from server.lifecycle import setup


@dataclass(frozen=True)
class AppState:
    path: Path
    installed: bool
    version: str | None      # CFBundleShortVersionString, or None
    running: bool
    quern_version: str       # what the CLI is

    @property
    def behind(self) -> bool:
        """Installed, with a version older than quern's own."""
        return (self.installed and self.version is not None
                and _older(self.version, self.quern_version))


def _older(a: str, b: str) -> bool:
    from packaging.version import InvalidVersion, Version

    try:
        return Version(a) < Version(b)
    except InvalidVersion:
        return False    # unreadable is not evidence of old


def app_version(app: Path) -> str | None:
    try:
        with open(app / "Contents" / "Info.plist", "rb") as f:
            return plistlib.load(f).get("CFBundleShortVersionString")
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None


def state() -> AppState:
    from server import get_version

    app = setup.menubar_app_path()
    installed = app.is_dir()
    return AppState(
        path=app,
        installed=installed,
        version=app_version(app) if installed else None,
        running=installed and setup._menubar_app_running(app),
        quern_version=get_version(),
    )


def describe(s: AppState) -> list[str]:
    """Lines for `quern menubar status` and `quern doctor`."""
    if not s.installed:
        return [f"not installed (expected at {s.path})",
                f"install it with: {setup.quern_cmd()} menubar install"]
    lines = [f"v{s.version or '?'} at {s.path} — {'running' if s.running else 'not running'}"]
    if s.behind:
        lines.append(f"older than quern (v{s.quern_version}); "
                     f"update it with: {setup.quern_cmd()} menubar install")
    if not s.running:
        lines.append(f"start it with: {setup.quern_cmd()} menubar open")
    return lines


def cmd_status() -> int:
    if platform.system() != "Darwin":
        print("The Quern app is macOS only.")
        return 1
    s = state()
    print("Quern app:")
    for line in describe(s):
        print(f"  {line}")
    return 0


def cmd_open() -> int:
    """Start the installed app, leaving a running one alone."""
    if platform.system() != "Darwin":
        print("The Quern app is macOS only.")
        return 1
    s = state()
    if not s.installed:
        print(f"The Quern app is not installed. Run: {setup.quern_cmd()} menubar install")
        return 1
    if s.running:
        print(f"Already running (v{s.version or '?'}).")
        return 0
    rc, err = setup._open_menubar_app(s.path)
    if rc != 0:
        print(f"Could not start it: {err.strip() or 'open failed'}")
        return 1
    print(f"Started v{s.version or '?'} from {s.path}.")
    return 0


def cmd_install(force: bool = False) -> int:
    """Fetch the signed app matching this quern, and install and start it.

    The release's app, verified exactly as setup verifies it, for git
    installs too: a clone's version is the release it is based on. Someone
    working on the app itself builds it (`scripts/install-menubar-app.sh
    --build`), and an installed app of the same version -- which a dev build
    is -- is left alone unless `--force` says otherwise.
    """
    if platform.system() != "Darwin":
        print("The Quern app is macOS only.")
        return 1
    s = state()
    if s.installed and not force and not s.behind:
        print(f"Already installed: v{s.version or '?'} (quern is v{s.quern_version}). "
              "Use --force to reinstall.")
        return cmd_open() if not s.running else 0

    if not setup.WRAPPER_PATH.exists():
        # The app drives quern through this wrapper and nothing else: a GUI app
        # does not have your shell's PATH. Installing without it gives an app
        # that launches and cannot start anything.
        print(f"{setup.WRAPPER_PATH} is missing, so the app would have nothing to drive.")
        print(f"Run `{setup.quern_cmd()} setup` first.")
        return 1

    version = s.quern_version
    url = f"https://github.com/quern-dev/quern/releases/download/v{version}/quern-{version}.tar.gz"
    print(f"Fetching the signed menu-bar app from v{version}...")
    apps = s.path.parent
    try:
        apps.mkdir(parents=True, exist_ok=True)
        # In ~/Applications, so the install is a rename (see download_release_app).
        with tempfile.TemporaryDirectory(dir=apps, prefix=".quern-app-") as tmp:
            fresh = setup.download_release_app(url, version, Path(tmp))
            stopped = s.installed and setup._menubar_app_running(s.path)
            setup._quit_menubar_app()
            staging = s.path.with_name("Quern.app.incoming")
            shutil.rmtree(staging, ignore_errors=True)
            os.replace(fresh, staging)
            shutil.rmtree(s.path, ignore_errors=True)
            os.replace(staging, s.path)
    except setup._UntrustedBundle as e:
        print(f"The downloaded app failed verification and was not installed: {e}")
        print("This is not a network problem. Don't install it by hand; please report it.")
        return 1
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as e:
        print(f"Could not install the menu-bar app: {e}")
        print(f"Manual download: https://github.com/quern-dev/quern/releases/tag/v{version}")
        return 1

    rc, err = setup._open_menubar_app(s.path)
    if rc != 0:
        stopped_note = " The previous version was stopped to install this one." if stopped else ""
        print(f"Installed v{version}, but it did not start: {err.strip()}.{stopped_note}")
        print(f"Start it with: {setup.quern_cmd()} menubar open")
        return 1
    print(f"Installed and started v{version} from {s.path}.")
    return 0


USAGE = """\
usage: quern menubar [status|open|install [--force]]

  status             Show the installed Quern app's version and whether it runs
  open               Start the Quern app in the menu bar (a running one is left alone)
  install [--force]  Install the signed app matching this quern, then start it.
                     Skips an app that is already current unless --force.
"""


def main(argv: list[str], force: bool = False) -> int:
    if not argv or argv[0] == "status":
        return cmd_status()
    if argv[0] in ("-h", "--help", "help"):
        print(USAGE, end="")
        return 0
    if argv[0] == "open" and len(argv) == 1 and not force:
        return cmd_open()
    if argv[0] == "install" and len(argv) == 1:
        return cmd_install(force=force)
    print(USAGE, end="")
    return 2
