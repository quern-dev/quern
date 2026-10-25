"""Python packages installed newer than a dependency's own cap allows.

CVE-2026-111497 is an out-of-bounds write in the Brotli decoder, fixed in the
`Brotli` package's 1.2.1. mitmproxy decodes `br` response bodies with it, from
whatever servers the app under test talks to -- attacker-chosen bytes, on the
developer's Mac. But every mitmproxy release up to 12.3.0 declares
`Brotli<=1.2.0`, so a plain `Brotli>=1.2.1` in pyproject.toml makes pip refuse
the install or reach back for a mitmproxy old enough to have no cap, and every
`pip install -e .` puts 1.2.0 back.

So it is installed after each of those, with `--no-deps`: a patch release with
the same API, which pip then reports as conflicting with mitmproxy's cap. Remove
the entry, and pin both in pyproject.toml, once a mitmproxy release requires
Brotli 1.2.1 itself (mitmproxy/mitmproxy#8514).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from packaging.version import InvalidVersion, Version

#: (distribution, first fixed version, why). Each is installed at or above the
#: version after every dependency install.
OVERRIDES: tuple[tuple[str, str, str], ...] = (
    ("Brotli", "1.2.1", "CVE-2026-111497 (Brotli decoder); mitmproxy caps it at 1.2.0"),
)


def requirements() -> list[str]:
    """The pip arguments that install every override: `Brotli>=1.2.1`."""
    return [f"{name}>={version}" for name, version, _ in OVERRIDES]


def _pip(pip: str, args: list[str], cwd: str | None, timeout: int) -> str | None:
    try:
        result = subprocess.run(
            [pip, *args], cwd=cwd, capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return str(exc)
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        return err.splitlines()[-1][:200] if err else f"pip exited {result.returncode}"
    return None


def _metadata(venv: Path, name: str) -> list[tuple[Path, str]]:
    """Every `*.dist-info` directory for `name` in the venv, with its version."""
    key = re.sub(r"[-_.]+", "_", name).lower()
    out = []
    for info in venv.glob("lib/python*/site-packages/*.dist-info"):
        dist, _, version = info.name[: -len(".dist-info")].partition("-")
        if re.sub(r"[-_.]+", "_", dist).lower() == key:
            out.append((info, version))
    return out


def apply(pip: str, cwd: str | None = None, timeout: int = 300) -> str | None:
    """Install the overrides with `pip` (in the venv `pip` belongs to), without
    touching anything else. Returns None on success, or what went wrong.

    An interrupted upgrade can leave an old version's metadata beside the fixed
    one. pip then sees the fixed one, does nothing, and `unfixed_in_venv` goes
    on reporting the old -- a venv `doctor --fix` could never clear, reinstalled
    at every start (CodeRabbit, #548). So when that is what is left, the fixed
    release is reinstalled over whatever files are there, and only then is the
    stale metadata removed: metadata, never a package's files, and never when
    no fixed metadata stands beside it.
    """
    problem = _pip(pip, ["install", "--no-deps", *requirements()], cwd, timeout)
    if problem:
        return problem
    venv = Path(pip).parent.parent
    for name, fixed, _ in OVERRIDES:
        found = _metadata(venv, name)
        stale = [info for info, version in found if _below(version, fixed)]
        if not stale or len(stale) == len(found):
            continue
        problem = _pip(pip, ["install", "--no-deps", "--force-reinstall",
                             f"{name}>={fixed}"], cwd, timeout)
        if problem:
            return problem
        for info, version in _metadata(venv, name):
            if _below(version, fixed):
                try:
                    shutil.rmtree(info)
                except OSError as exc:
                    return f"could not remove {info.name}: {exc}"
    return None


def _below(installed: str, fixed: str) -> bool:
    """Whether `installed` is older than `fixed`. A version that cannot be read
    counts as older: "could not tell" is not "fixed"."""
    try:
        return Version(installed) < Version(fixed)
    except InvalidVersion:
        return True


def unfixed_in_venv(venv: Path) -> list[tuple[str, str | None, str, str]]:
    """Each override not installed at its fixed version in the venv at `venv`:
    (name, installed version or None, fixed version, why). Read from its
    `*.dist-info` directories, with no pip and no import, so it can answer for
    a venv other than the running interpreter's, and cheaply enough for the
    start path.

    Missing counts as unfixed (CodeRabbit, #546): a venv with
    a current stamp and no Brotli would otherwise never get one. A directory
    with no site-packages at all is not a venv anyone installed into, so it has
    nothing to report.
    """
    sites = list(venv.glob("lib/python*/site-packages"))
    if not sites:
        return []
    infos = [info for site in sites for info in site.glob("*.dist-info")]
    out = []
    for name, fixed, why in OVERRIDES:
        key = re.sub(r"[-_.]+", "_", name).lower()
        versions = []
        for info in infos:
            dist, _, version = info.name[: -len(".dist-info")].partition("-")
            if re.sub(r"[-_.]+", "_", dist).lower() == key:
                versions.append(version)
        # Every match, not the first: an interrupted upgrade can leave both
        # 1.2.0's metadata and 1.2.1's, and the older may describe the files
        # actually installed (CodeRabbit, #547).
        stale = [v for v in versions if _below(v, fixed)]
        if not versions or stale:
            out.append((name, stale[0] if stale else None, fixed, why))
    return out
