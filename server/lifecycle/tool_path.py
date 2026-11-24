"""Where the tools setup and update need live, when PATH doesn't say.

`quern setup` and `quern update` find Homebrew, pipx and the tools pipx
installs on PATH, and run them by name. A shell that read no startup files --
an ssh command, a launchd job -- has only sshd's or launchd's
`/usr/bin:/bin:/usr/sbin:/sbin`. Measured on a Mac updated over ssh: setup
reported Homebrew "Not installed", the pymobiledevice3 upgrade failed with "No
such file or directory: 'pipx'", and the update ended "Update incomplete"
although the code had updated. Run from a login shell, the same update finished.

So both commands add the standard places to their own PATH before anything
else, and every lookup and subprocess after -- including the server the update
restarts -- finds what a login shell would.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Homebrew on Apple Silicon and on Intel, then where pipx puts what it
#: installs. In this order, after whatever PATH already holds.
STANDARD_DIRS = ("/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin")


def _pipx_bin_dir() -> Path:
    configured = os.environ.get("PIPX_BIN_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".local" / "bin"


def add_standard_tool_dirs() -> list[str]:
    """Append the standard tool directories this PATH lacks, and return
    them. Appended, never prepended: a directory the user put first still
    wins, so nothing they chose is shadowed. Only directories that exist."""
    current = os.environ.get("PATH")
    entries = current.split(os.pathsep) if current else []
    added = []
    for candidate in (*STANDARD_DIRS, str(_pipx_bin_dir())):
        if candidate in entries or candidate in added:
            continue
        if Path(candidate).is_dir():
            added.append(candidate)
    if added:
        # The existing PATH kept as it is, empty entries too -- an empty entry
        # is the current directory, and the user's to keep (CR) -- and an
        # unset or empty one not given a current-directory entry it never had.
        tail = os.pathsep.join(added)
        os.environ["PATH"] = f"{current}{os.pathsep}{tail}" if current else tail
    return added
