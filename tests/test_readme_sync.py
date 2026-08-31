"""Guard tests that keep README.md in sync with the code it documents.

The README's MCP tool table and API endpoint tables are hand-maintained, so
they drift silently every time a feature lands without a matching doc edit.
By v0.13.4 the tool table had fallen 37 tools behind the source and the prose
count ("78 tools") matched neither the table nor the code. These tests make
that drift a test failure instead of something a reader discovers first.

Each test reports the exact symbols to add or remove, so a failure is a
to-do list rather than a puzzle.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
README = REPO_ROOT / "README.md"
MCP_TOOLS_DIR = REPO_ROOT / "mcp" / "src" / "tools"
SERVER_DIR = REPO_ROOT / "server"

# Routes deliberately left out of the endpoint tables. Both are named in the
# README's public-paths sentence, so they aren't hidden — they just don't earn
# a table row. Anything else missing is drift.
UNDOCUMENTED_ROUTES_ALLOWLIST = {
    ("GET", "/"),          # redirects to /docs
    ("GET", "/video-test"), # internal preview test page
}


def _normalize_path(path: str) -> str:
    """Collapse path params and trailing slashes so `/x/{id}/` == `/x/{}`."""
    path = re.sub(r"\{[^}]*\}", "{}", path)
    return path.rstrip("/") or "/"


# --------------------------------------------------------------------------
# Source of truth: the code
# --------------------------------------------------------------------------


def registered_tools() -> set[str]:
    """Every tool name passed to server.registerTool() in mcp/src/tools/."""
    names: set[str] = set()
    for ts_file in sorted(MCP_TOOLS_DIR.glob("*.ts")):
        names.update(
            re.findall(
                r'server\.registerTool\(\s*"([a-z_0-9]+)"',
                ts_file.read_text(),
            )
        )
    return names


def registered_routes() -> set[tuple[str, str]]:
    """Every (METHOD, full_path) served by a FastAPI router or the app."""
    routes: set[tuple[str, str]] = set()
    for py_file in sorted(SERVER_DIR.rglob("*.py")):
        text = py_file.read_text()
        prefix_match = re.search(r'APIRouter\(\s*prefix="([^"]*)"', text)
        prefix = prefix_match.group(1) if prefix_match else ""
        for method, path in re.findall(
            r'@(?:app|router)\.(get|post|put|delete|patch)\("([^"]*)"', text
        ):
            routes.add((method.upper(), _normalize_path(prefix + path)))
    return routes


# --------------------------------------------------------------------------
# What the README claims
# --------------------------------------------------------------------------


def _readme_section(start_heading: str, end_heading: str) -> str:
    text = README.read_text()
    start = text.index(start_heading)
    end = text.index(end_heading, start)
    return text[start:end]


def documented_tools() -> set[str]:
    """Tool names in the second column of the MCP Tools table."""
    section = _readme_section("## MCP Tools", "## API Endpoints")
    names: set[str] = set()
    for line in section.splitlines():
        if not line.startswith("|") or line.startswith("|--"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) != 2 or cells[0] == "Category":
            continue
        names.update(re.findall(r"`([a-z_0-9]+)`", cells[1]))
    return names


def documented_tool_count() -> int:
    """The count asserted in the prose above the table ('107 tools ...')."""
    section = _readme_section("## MCP Tools", "## API Endpoints")
    match = re.search(r"\b(\d+) tools available via MCP\b", section)
    assert match, "Could not find the 'N tools available via MCP' claim in README.md"
    return int(match.group(1))


def documented_routes() -> set[tuple[str, str]]:
    """(METHOD, path) pairs from every API endpoint table."""
    section = _readme_section("## API Endpoints", "## Architecture")
    routes: set[tuple[str, str]] = set()
    for line in section.splitlines():
        match = re.match(
            r"\|\s*(GET|POST|PUT|DELETE|PATCH)\s*\|\s*`([^`]+)`", line
        )
        if match:
            routes.add((match.group(1), _normalize_path(match.group(2))))
    return routes


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


def _format(items: set) -> str:
    return "\n".join(f"  - {i}" for i in sorted(map(str, items)))


def test_readme_tool_table_matches_registered_tools():
    actual = registered_tools()
    documented = documented_tools()

    missing = actual - documented
    stale = documented - actual

    problems = []
    if missing:
        problems.append(
            f"{len(missing)} tool(s) registered in mcp/src/tools/ but absent "
            f"from the README MCP Tools table:\n{_format(missing)}"
        )
    if stale:
        problems.append(
            f"{len(stale)} tool(s) listed in the README MCP Tools table but no "
            f"longer registered in mcp/src/tools/:\n{_format(stale)}"
        )
    assert not problems, "\n\n".join(problems)


def test_readme_tool_count_matches_table():
    claimed = documented_tool_count()
    actual = len(registered_tools())
    assert claimed == actual, (
        f"README says '{claimed} tools available via MCP' but "
        f"{actual} tools are registered in mcp/src/tools/. "
        f"Update the sentence above the MCP Tools table."
    )


def test_readme_endpoint_tables_match_registered_routes():
    actual = registered_routes()
    documented = documented_routes()

    missing = actual - documented - UNDOCUMENTED_ROUTES_ALLOWLIST
    stale = documented - actual

    problems = []
    if missing:
        problems.append(
            f"{len(missing)} route(s) served by the API but absent from the "
            f"README endpoint tables:\n{_format(missing)}\n"
            f"Add a table row, or add the route to "
            f"UNDOCUMENTED_ROUTES_ALLOWLIST with a reason."
        )
    if stale:
        problems.append(
            f"{len(stale)} route(s) documented in the README endpoint tables "
            f"but not served by the API:\n{_format(stale)}"
        )
    assert not problems, "\n\n".join(problems)


def test_undocumented_route_allowlist_is_not_stale():
    """The allowlist should never outlive the routes it excuses."""
    actual = registered_routes()
    orphans = UNDOCUMENTED_ROUTES_ALLOWLIST - actual
    assert not orphans, (
        f"UNDOCUMENTED_ROUTES_ALLOWLIST excuses route(s) that no longer "
        f"exist:\n{_format(orphans)}\nRemove them from the allowlist."
    )


@pytest.mark.parametrize("path", [README, MCP_TOOLS_DIR, SERVER_DIR])
def test_documentation_sources_exist(path: Path):
    """Fail loudly rather than silently passing on an empty scan."""
    assert path.exists(), f"{path} is missing — these guard tests cannot run."
