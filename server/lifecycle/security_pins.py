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


def apply(pip: str, cwd: str | None = None, timeout: int = 300) -> str | None:
    """Install the overrides with `pip`, without touching anything else.

    Returns None on success, or what went wrong. Already-current packages cost
    pip a metadata read and no download.
    """
    try:
        result = subprocess.run(
            [pip, "install", "--no-deps", *requirements()],
            cwd=cwd, capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return str(exc)
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        return err.splitlines()[-1][:200] if err else f"pip exited {result.returncode}"
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
        found = None
        for info in infos:
            dist, _, version = info.name[: -len(".dist-info")].partition("-")
            if re.sub(r"[-_.]+", "_", dist).lower() == key:
                found = version
                break
        if found is None or _below(found, fixed):
            out.append((name, found, fixed, why))
    return out
