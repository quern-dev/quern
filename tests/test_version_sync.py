"""The project version is declared twice; nothing checked they agreed.

`server/__init__.py:get_version()` reads pyproject.toml and describes it as the
"single source of truth", which is true for the Python server and not true for
the project: `mcp/package.json` carries its own copy, published to npm and
reported by the MCP wrapper.

Nothing compared them, so a release could ship a server announcing one version
on /health while the MCP wrapper announced another -- the kind of drift that is
invisible until two machines disagree and neither can say why.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
PACKAGE_JSON = ROOT / "mcp" / "package.json"


def _pyproject_version() -> str:
    for line in PYPROJECT.read_text().splitlines():
        if line.startswith("version"):
            return line.split('"')[1]
    raise AssertionError("no version line in pyproject.toml")


@pytest.mark.parametrize("path", [PYPROJECT, PACKAGE_JSON])
def test_version_sources_exist(path):
    """Fail loudly rather than silently passing on a missing file."""
    assert path.exists(), f"{path} is missing — this guard cannot run."


def test_the_two_declared_versions_agree():
    npm = json.loads(PACKAGE_JSON.read_text())["version"]
    assert npm == _pyproject_version(), (
        "pyproject.toml and mcp/package.json disagree. Both ship in a release; "
        "the server reports one on /health and the MCP wrapper publishes the "
        "other."
    )


def test_the_server_reports_the_declared_version():
    from server import get_version

    assert get_version() == _pyproject_version()


# The published SemVer 2.0.0 grammar, verbatim from semver.org, rather than a
# hand-rolled approximation. Two earlier attempts here were each wrong in a
# different direction: `\d+` per part accepted `01.2.3`, and tightening it to
# `-[a-z]+\.\d+` then rejected `1.2.3-beta`, `1.2.3-0` and build metadata --
# all valid, so a legitimate release would have failed CI. The point of this
# guard is to catch a typo, not to invent a dialect.
_SEMVER = re.compile(
    r"(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+(?P<buildmetadata>[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)


def test_the_version_is_a_release_shape():
    """Guards a typo in the one field a release is cut from."""
    version = _pyproject_version()
    assert _SEMVER.fullmatch(version), version


@pytest.mark.parametrize("version", [
    "0.15.0", "1.2.3", "0.0.1", "10.20.30",
    "0.15.0-beta.1", "10.0.0-rc.12",
    # Valid forms an earlier version of this guard rejected, any of which would
    # have failed CI on a legitimate release.
    "1.2.3-beta", "1.2.3-0", "1.2.3-alpha.1.2", "1.2.3-alpha-1",
    "1.2.3+build.1", "1.2.3-beta.1+exp.sha.5114f85",
])
def test_valid_versions_are_accepted(version):
    assert _SEMVER.fullmatch(version), version


@pytest.mark.parametrize("version", [
    "01.2.3",           # leading zero in major
    "1.02.3",           # in minor
    "1.2.03",           # in patch
    "1.2.3-beta.01",    # in a numeric prerelease identifier
    "1.2.3-01",         # same, as the only identifier
    "1.2",              # too few parts
    "1.2.3.4",          # too many
    "v1.2.3",           # tag prefix, not a version
    "1.2.3-",           # empty prerelease
    "",
])
def test_invalid_versions_are_rejected(version):
    """npm requires valid SemVer, and both files ship as packages."""
    assert not _SEMVER.fullmatch(version), version
