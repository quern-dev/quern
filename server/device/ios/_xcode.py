"""Xcode-availability preflight for iOS toolchain probes.

Direct invocation of ``xcrun simctl`` / ``xcrun devicectl`` on a Mac with
no Xcode and no Command Line Tools triggers the macOS *"install developer
tools"* system dialog. The dialog is rendered by Apple's ``xcselect``
library before ``xcrun`` exits, so wrapping the call in ``try/except``
catches the eventual error but the user has already seen (and may be
forced to dismiss) the dialog.

This module provides a sync preflight using ``xcode-select -p``, which
returns the active developer dir if anything is installed and exits
non-zero otherwise — without ever triggering the install dialog. Backends
and setup checks gate their ``xcrun`` calls on ``xcode_available()`` so
Android-only machines never see the dialog.
"""

from __future__ import annotations

import functools
import os
import subprocess


@functools.lru_cache(maxsize=1)
def xcode_available() -> bool:
    """Return True iff Xcode / CLT is installed and ``xcrun`` will work
    without triggering the macOS install dialog.

    The same goes for ``/usr/bin/swift`` and ``swiftc``: they are stubs that
    exist on every Mac, so ``shutil.which`` finds them whether or not a
    toolchain is installed, and running one shows the dialog (#659). Gate on
    this, never on ``which``.

    Checks ``xcode-select -p`` (safe — never triggers the dialog) and the
    ``DEVELOPER_DIR`` environment variable (which Quern's setup script
    sets as a runtime fix-up when Xcode lives somewhere ``xcode-select``
    isn't pointed at). Cached; call ``xcode_available.cache_clear()``
    after mutating ``DEVELOPER_DIR`` or installing Xcode mid-session.
    """
    if os.environ.get("DEVELOPER_DIR"):
        return os.path.isdir(os.environ["DEVELOPER_DIR"])
    try:
        result = subprocess.run(
            ["xcode-select", "-p"],
            capture_output=True, text=True, timeout=2,
        )
    except Exception:
        return False
    # The directory has to exist too: after Xcode is deleted, xcode-select
    # still prints its old path, and the stubs then show the dialog (#659).
    path = result.stdout.strip()
    return result.returncode == 0 and bool(path) and os.path.isdir(path)
