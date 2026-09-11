#!/usr/bin/env python3
"""Record the facts about this machine that Quern's environment checks read.

Not a disk snapshot. A disk snapshot of a machine whose home is on an external
volume is both enormous and unusable in CI; what the checks actually consult is
a handful of paths, what they resolve to, and the order of PATH. Those fit in a
few hundred bytes and can be rebuilt in a temporary directory, which is what
`tests/fixtures/envs/` replays.

The point is to turn "works on my machine" into a file. A configuration that
produced a wrong answer once can then be a permanent test, on every machine,
without anyone owning the hardware that found it.

Read-only. Writes nothing outside the file you name.

    python3 scripts/capture-env.py                      # to stdout
    python3 scripts/capture-env.py tests/fixtures/envs/mine.json
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

TUNNELD_PLIST = Path("/Library/LaunchDaemons/com.quern.tunneld.plist")

#: Where a pymobiledevice3 can legitimately live. Each is a different install
#: with a different lifetime, and the checks conflating them is what this was
#: written to reproduce.
CANDIDATES = {
    "pipx-user": "{home}/.local/pipx/venvs/pymobiledevice3/bin/pymobiledevice3",
    "pipx-user-shim": "{home}/.local/bin/pymobiledevice3",
    "pipx-global": "/opt/pipx/venvs/pymobiledevice3/bin/pymobiledevice3",
    "pipx-global-shim": "/usr/local/bin/pymobiledevice3",
}


def _resolve(path: Path) -> str | None:
    try:
        return str(path.resolve())
    except OSError:
        return None


def _home_is_external(home: Path) -> bool:
    return str(home).startswith("/Volumes/")


def _plist() -> dict:
    if not TUNNELD_PLIST.exists():
        return {"exists": False}
    try:
        out = subprocess.run(
            ["plutil", "-convert", "json", "-o", "-", str(TUNNELD_PLIST)],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"exists": True, "unreadable": str(exc)}
    if out.returncode != 0:
        return {"exists": True, "unreadable": out.stderr.strip()}
    data = json.loads(out.stdout)
    return {
        "exists": True,
        "program_arguments": data.get("ProgramArguments"),
        "standard_out_path": data.get("StandardOutPath"),
    }


def capture(project_root: Path) -> dict:
    home = Path.home()
    venv_script = project_root / ".venv" / "bin" / "pymobiledevice3"

    installs = []
    for kind, template in CANDIDATES.items():
        path = Path(template.format(home=home))
        if path.exists():
            installs.append({"kind": kind, "path": str(path), "resolves_to": _resolve(path)})
    if venv_script.exists():
        installs.append({
            "kind": "project-venv-console-script",
            "path": str(venv_script),
            "resolves_to": _resolve(venv_script),
        })

    return {
        "captured_at": datetime.now(UTC).isoformat(),
        "home": str(home),
        "home_is_external": _home_is_external(home),
        "project_root": str(project_root),
        # Order matters and is the whole story in at least one bug: setup
        # prepends the project venv, so a console script there wins over the
        # pipx CLI for anything using shutil.which.
        "path": os.environ.get("PATH", "").split(":"),
        "which_pymobiledevice3": shutil.which("pymobiledevice3"),
        "pymobiledevice3_installs": installs,
        "tunneld_plist": _plist(),
    }


def main() -> int:
    project_root = Path(__file__).resolve().parent.parent
    data = capture(project_root)
    text = json.dumps(data, indent=2) + "\n"
    if len(sys.argv) > 1:
        Path(sys.argv[1]).write_text(text, encoding="utf-8")
        print(f"wrote {sys.argv[1]}")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
