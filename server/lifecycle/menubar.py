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

import http.client
import os
import platform
import plistlib
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from server.lifecycle import releases, setup


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

    @property
    def needs_install(self) -> bool:
        """Whether an install would repair something.

        Missing, behind, or present with a version nothing can parse. That last
        case is not just a missing Info.plist: `"1.0 (build 3)"` parses as
        nothing, is not "behind", and was therefore skipped by the ordinary
        install path -- recoverable only with `--force`, which nobody knows to
        reach for. A *newer* version is left alone, so a dev build survives.
        """
        return not self.installed or self.damaged or self.behind

    @property
    def damaged(self) -> bool:
        """A bundle is there but does not say what it is.

        A half-replaced app looks exactly like this, and it used to read as
        "already installed" -- so a retry after a failed swap reported success
        over the wreckage, and `doctor --fix` passed it by.
        """
        return self.installed and _major_minor(self.version) is None


def _major_minor(version: str | None) -> tuple | None:
    """The parsed version, or None if it does not parse at all."""
    from packaging.version import InvalidVersion, Version

    if not version:
        return None
    try:
        return (Version(version),)
    except InvalidVersion:
        return None


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
    if s.damaged:
        return [f"at {s.path}, but it does not report a version — it may be "
                "damaged or half-installed",
                f"reinstall it with: {setup.quern_cmd()} menubar install --force"]
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
    if not force and not s.needs_install:
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
    url = releases.download_url(version)
    print(f"Fetching the signed menu-bar app from v{version}...")
    apps = s.path.parent
    try:
        apps.mkdir(parents=True, exist_ok=True)
        # In ~/Applications, so the install is a rename (see download_release_app).
        with tempfile.TemporaryDirectory(dir=apps, prefix=".quern-app-") as tmp:
            fresh = setup.download_release_app(url, version, Path(tmp))
            stopped = s.installed and setup._menubar_app_running(s.path)
            if stopped:
                # Only when the bundle being replaced is the one running:
                # asking by application name would otherwise stop a Quern from
                # somewhere else, which this command has no business doing.
                if not setup._quit_menubar_app(s.path):
                    # Replacing the bundle under a live app leaves it running
                    # an image with no name on disk. Stop instead, with the
                    # old app still there and still working.
                    raise RuntimeError(
                        f"the app at {s.path} would not quit, so it was not "
                        "replaced. Quit it from its menu and try again"
                    )
            staging = s.path.with_name("Quern.app.incoming")
            shutil.rmtree(staging, ignore_errors=True)
            os.replace(fresh, staging)
            # The old bundle is moved aside, not deleted: `rmtree(...,
            # ignore_errors=True)` can leave part of it behind -- an
            # undeletable child is enough -- and the rename that followed then
            # failed with ENOTEMPTY, having already gutted the app the user was
            # running. Measured. A rename cannot half-succeed, so the worst
            # case now is the old app back where it was.
            replaced = s.path.with_name("Quern.app.replaced")
            shutil.rmtree(replaced, ignore_errors=True)
            if s.installed:
                os.replace(s.path, replaced)
            try:
                os.replace(staging, s.path)
            except OSError:
                if replaced.exists():
                    os.replace(replaced, s.path)
                raise
            shutil.rmtree(replaced, ignore_errors=True)
    except setup._UntrustedBundle as e:
        print(f"The downloaded app failed verification and was not installed: {e}")
        print("This is not a network problem. Don't install it by hand; please report it.")
        return 1
    except (OSError, RuntimeError, ValueError, http.client.HTTPException,
            subprocess.SubprocessError) as e:
        # HTTPException is not an OSError: a truncated response mid-download
        # came out as a traceback rather than as this message.
        print(f"Could not install the Quern app: {e}")
        if s.installed and not s.path.is_dir():
            # Only reachable if the restore above also failed.
            print(f"The app that was there is at {s.path.with_name('Quern.app.replaced')}")
        print(f"Manual download: https://github.com/quern-dev/quern/releases/tag/v{version}")
        return 1

    # "Is anything running that is not the copy just installed?" -- which is
    # the question, and was asked as `not stopped` before: with *both* ours and
    # another checkout's copy running, that skipped the check entirely and
    # claimed a launch that never happened.
    if setup._menubar_app_running() and not setup._menubar_app_running(s.path):
        # `open` activates a running instance rather than starting the new
        # binary, so say so instead of reporting a launch that did not happen.
        print(f"Installed v{version} to {s.path}, but another Quern app is "
              "running from somewhere else. Quit it, then run "
              f"`{setup.quern_cmd()} menubar open`.")
        # Same outcome as a failed launch -- a new app on disk and nothing in
        # the menu bar -- so the same status. `install && ...` should not
        # continue as though the app were up.
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
