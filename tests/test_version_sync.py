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


def test_the_version_is_a_release_shape():
    """Guards a typo in the one field a release is cut from. Accepts
    `1.2.3` and prerelease suffixes like `1.2.3-beta.1`."""
    version = _pyproject_version()
    assert re.fullmatch(r"\d+\.\d+\.\d+(?:-[a-z]+\.\d+)?", version), version
