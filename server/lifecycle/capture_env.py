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
    # pipx 1.5 moved the default PIPX_HOME on macOS. Both layouts are live on
    # real machines, and a capture that looks in fewer places than the lookup
    # does describes a machine the lookup does not see.
    "pipx-user-appsupport": (
        "{home}/Library/Application Support/pipx/venvs/pymobiledevice3"
        "/bin/pymobiledevice3"
    ),
    "pipx-global": "/opt/pipx/venvs/pymobiledevice3/bin/pymobiledevice3",
    "pipx-global-shim": "/usr/local/bin/pymobiledevice3",
}

#: PATH entries worth publishing, by what they are. Everything else is dropped:
#: a full PATH names every piece of software on the machine, which is more than
#: a bug report needs and more than most people would choose to publish.
PATH_CATEGORIES = {
    "quern": ("quern", "/.local/bin", "/.local/share"),
    "python": ("pyenv", "pipx", "conda"),
    "node": ("fnm", "nvm", "volta", "corepack"),
    "android": ("android", "platform-tools", "build-tools"),
    "apple": ("xcode", "commandlinetools", "cryptexes", "/library/apple"),
}
# Dropped from the lists above on purpose: "venv", "node", "python". They match
# as substrings, so `/Users/x/Clients/AcmeBank/.venv/bin` and
# `…/SomeClientApp/node_modules/.bin` -- both ordinary under direnv or nvm --
# were published in full into a file meant for a public issue. Any such
# directory that actually matters holds a tool, and the clause below keeps it on
# that basis instead.

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
    except (OSError, RuntimeError):
        # RuntimeError is a symlink loop on Python 3.9 -- the one interpreter
        # this file is written to run under. 3.11 raises OSError instead, so
        # catching only that is the narrow-subclass mistake on exactly the
        # version that matters.
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


def _which_with_venv_first() -> str | None:
    """What `which` returns once `run_setup` has prepended the venv.

    Setup does this before any check runs, so this -- not the plain lookup --
    is the value the checks actually see. Reproduced rather than described, so
    a capture taken from an ordinary shell still records the shadowing.
    """
    if sys.prefix == sys.base_prefix:
        return shutil.which("pymobiledevice3")
    venv_bin = str(Path(sys.prefix) / "bin")
    path = os.environ.get("PATH", "")
    if venv_bin in path.split(":"):
        return shutil.which("pymobiledevice3")
    return shutil.which("pymobiledevice3", path=venv_bin + ":" + path)


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
    try:
        data = json.loads(out.stdout)
    except ValueError as exc:
        # plutil exiting 0 with something that is not JSON. Outside the try
        # above, this was a traceback from the command whose entire job is to
        # produce a readable report when things are wrong.
        return {"exists": True, "unreadable": f"not JSON: {exc}"}
    # Named fields, not the whole plist. Returning `data` would publish whatever
    # a future plist happens to carry, and tests/test_capture_env.py pins these.
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

    # The interpreter's own venv, which is the missing half of the picture.
    # `run_setup` prepends `sys.prefix/bin` to PATH before any check runs, so a
    # capture taken from a shell records a PATH in which nothing is shadowed --
    # and a user hitting that exact bug would attach a report showing everything
    # resolving correctly. Without these two fields the failing state is an
    # assumption in the replay rather than data from the machine.
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "home": str(home),
        "home_is_external": str(home).startswith("/Volumes/"),
        "project_root": str(project_root),
        "path": kept,
        "path_entries_total": len([e for e in entries if e]),
        "path_entries_omitted": omitted,
        "sys_prefix": sys.prefix,
        "sys_base_prefix": sys.base_prefix,
        "virtual_env": os.environ.get("VIRTUAL_ENV"),
        "which_pymobiledevice3": shutil.which("pymobiledevice3"),
        "which_pymobiledevice3_as_setup_sees_it": _which_with_venv_first(),
        "pymobiledevice3_installs": installs,
        "tunneld_plist": _plist(),
    }


def find_project_root() -> Path:
    """The checkout this module lives in.

    Walks up from this file, not from the working directory. Falling back to
    `cwd()` reported a fabricated root as fact and then looked for the shadowing
    console script in the wrong place -- omitting the very thing the capture
    exists to record -- whenever it was run from elsewhere.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return here.parent.parent.parent


def run(destination: str | None = None) -> int:
    """Write the capture to `destination`, or print it. Returns an exit code.

    Broad catch, deliberately. This is the command someone runs when their
    install is misbehaving, so an unlucky read on a machine that is already
    wrong must not turn the whole report into a stack trace -- which is the
    outcome the wrapper script's own error path calls the least useful possible
    output.
    """
    try:
        text = json.dumps(capture(find_project_root()), indent=2) + "\n"
    except Exception as exc:  # noqa: BLE001 - a diagnostic must still report
        print(f"Error: could not complete the capture: {exc!r}", file=sys.stderr)
        print("Please include this message in your report.", file=sys.stderr)
        return 1
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
