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
from importlib import metadata
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


def unfixed(version_of=metadata.version) -> list[tuple[str, str | None, str, str]]:
    """Each override that is not installed at its fixed version in this
    interpreter: (name, installed version or None, fixed version, why).

    A package that is not installed at all is listed with None: it is a
    dependency of quern's, so its absence is a broken venv, not a safe one.
    """
    out = []
    for name, fixed, why in OVERRIDES:
        try:
            installed = version_of(name)
        except metadata.PackageNotFoundError:
            out.append((name, None, fixed, why))
            continue
        if _below(installed, fixed):
            out.append((name, installed, fixed, why))
    return out


def unfixed_in_venv(venv: Path) -> list[tuple[str, str, str, str]]:
    """The same answer as `unfixed`, for the venv at `venv`, read from its
    `*.dist-info` directories rather than by importing anything.

    For the start path's cheap "is the venv current?" check, which runs in
    whatever interpreter asked and must not run pip. Unlike `unfixed`, a
    package that is not there at all is not listed: a missing dependency is
    the install's business, and the stamp already covers it.
    """
    out = []
    for name, fixed, why in OVERRIDES:
        key = re.sub(r"[-_.]+", "_", name).lower()
        for info in venv.glob("lib/python*/site-packages/*.dist-info"):
            dist, _, version = info.name[: -len(".dist-info")].partition("-")
            if re.sub(r"[-_.]+", "_", dist).lower() == key and _below(version, fixed):
                out.append((name, version, fixed, why))
                break
    return out
