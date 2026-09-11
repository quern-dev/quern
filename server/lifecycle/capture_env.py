"""Record the facts Quern's environment checks read, for attaching to an issue.

Not a disk snapshot. What the checks consult is a handful of paths, what they
resolve to, and the order of PATH -- a few hundred bytes that can be rebuilt in
a temporary directory, which is what ``tests/fixtures/envs/`` replays. The point
is to turn "works on my machine" into a file: a configuration that produced a
wrong answer once becomes a permanent test, on every machine, without anyone
owning the hardware that found it.

Stdlib only, deliberately, and written to run on older Pythons than quern
itself requires. This is reached for when something is broken, and a diagnostic
that needs a working venv is no use on an install whose venv is the problem --
so ``scripts/capture-env.py`` imports it under whatever ``python3`` is around.
That is Python 3.9 on a stock macOS, which is why this uses
``datetime.timezone.utc`` rather than the 3.11 ``datetime.UTC`` it reached for
first. ``tests/test_capture_env.py`` pins the floor.

What it may contain is pinned by ``tests/test_capture_env.py`` rather than
remembered. The output is meant for a public issue, and ``~/.quern`` also holds
an API key, certificate fingerprints and device identifiers.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

TUNNELD_PLIST = Path("/Library/LaunchDaemons/com.quern.tunneld.plist")

#: Where a pymobiledevice3 can legitimately live. Each is a different install
#: with a different lifetime, and the checks conflating them is what this was
#: first written to reproduce.
CANDIDATES = {
    "pipx-user": "{home}/.local/pipx/venvs/pymobiledevice3/bin/pymobiledevice3",
    "pipx-user-shim": "{home}/.local/bin/pymobiledevice3",
    "pipx-global": "/opt/pipx/venvs/pymobiledevice3/bin/pymobiledevice3",
    "pipx-global-shim": "/usr/local/bin/pymobiledevice3",
}

#: PATH entries worth publishing, by what they are. Everything else is dropped:
#: a full PATH names every piece of software on the machine, which is more than
#: a bug report needs and more than most people would choose to publish.
PATH_CATEGORIES = {
    "quern": ("quern", "/.local/bin", "/.local/share"),
    "python": ("python", "pyenv", "pipx", "venv", "conda"),
    "node": ("fnm", "nvm", "node", "volta", "npm", "yarn", "corepack"),
    "android": ("android", "platform-tools", "build-tools"),
    "apple": ("xcode", "commandlinetools", "cryptexes", "/library/apple"),
}

#: Matched whole, not as substrings. "/bin" appears inside most of a PATH.
SYSTEM_DIRS = frozenset({
    "/usr/bin", "/bin", "/usr/sbin", "/sbin",
    "/usr/local/bin", "/usr/local/sbin",
    "/opt/homebrew/bin", "/opt/homebrew/sbin",
})

#: The external tools quern shells out to. A directory holding one of these is
#: kept whatever it is called -- the categories above are a convenience, and a
#: whitelist that hides the directory a tool is actually resolved from would
#: defeat the only thing this file is for.
TOOLS = frozenset({
    "adb", "brew", "emulator", "git", "idb", "ideviceinstaller",
    "ios_webkit_debug_proxy", "ios-webkit-debug-proxy", "java", "mitmdump",
    "node", "npm", "pipx", "pymobiledevice3", "python3", "swiftc", "xcodebuild",
    "xcrun",
})


def _resolve(path: Path) -> str | None:
    try:
        return str(path.resolve())
    except OSError:
        return None


def _categorise(directory: str) -> str | None:
    if directory in SYSTEM_DIRS:
        return "system"
    lowered = directory.lower()
    for name, needles in PATH_CATEGORIES.items():
        if any(needle in lowered for needle in needles):
            return name
    return None


def _tools_in(directory: str) -> list[str]:
    try:
        entries = set(os.listdir(directory))
    except OSError:
        return []
    return sorted(entries & TOOLS)


def filter_path(entries: list[str]) -> tuple[list[dict], int]:
    """Keep the PATH entries a bug report needs, and say how many were dropped.

    An entry survives if it is recognisably relevant *or* if it holds one of the
    tools quern uses. The second clause is what makes the filter safe: a
    whitelist alone would hide an unexpected directory that a tool is genuinely
    being resolved from, which is exactly the kind of surprise worth reporting.

    Each survivor keeps its original index, so the order that decides which copy
    of a tool wins is still reconstructible without publishing the rest.
    """
    kept: list[dict] = []
    for index, directory in enumerate(entries):
        category = _categorise(directory)
        tools = _tools_in(directory)
        if category is None and not tools:
            continue
        kept.append({
            "index": index,
            "dir": directory,
            "category": category or "holds-a-tool",
            "tools": tools,
        })
    return kept, len(entries) - len(kept)


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

    entries = os.environ.get("PATH", "").split(":")
    kept, omitted = filter_path([e for e in entries if e])

    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "home": str(home),
        "home_is_external": str(home).startswith("/Volumes/"),
        "project_root": str(project_root),
        "path": kept,
        "path_entries_total": len([e for e in entries if e]),
        "path_entries_omitted": omitted,
        "which_pymobiledevice3": shutil.which("pymobiledevice3"),
        "pymobiledevice3_installs": installs,
        "tunneld_plist": _plist(),
    }


def find_project_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return Path.cwd()


def run(destination: str | None = None) -> int:
    """Write the capture to `destination`, or print it. Returns an exit code."""
    text = json.dumps(capture(find_project_root()), indent=2) + "\n"
    if destination:
        try:
            Path(destination).write_text(text, encoding="utf-8")
        except OSError as exc:
            print(f"Error: could not write {destination}: {exc}", file=sys.stderr)
            return 1
        print(f"Wrote {destination}")
        print("Attach it to your issue — it contains no keys, certificates or")
        print("device identifiers, and names only the PATH entries that matter.")
        return 0
    print(text, end="")
    return 0
