"""Keep the agent guide's resources, files and links in step.

Agents read the guide over MCP: `quern://guide` is the overview
(docs/agent-guide.md), and each topic is its own resource,
`quern://guide/<topic>`, read from docs/agent-guide/<topic>.md and registered
from AGENT_GUIDE_TOPICS in mcp/src/index.ts. A relative markdown link does not
resolve for an agent reading a resource, so the guide names other pages by
their quern:// address -- and an address nothing registers reads exactly like
a page that exists until an agent asks for it.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INDEX_TS = REPO_ROOT / "mcp" / "src" / "index.ts"
TOOLS_DIR = REPO_ROOT / "mcp" / "src" / "tools"
OVERVIEW = REPO_ROOT / "docs" / "agent-guide.md"
TOPICS_DIR = REPO_ROOT / "docs" / "agent-guide"

_URI = re.compile(r"quern://[a-z][a-z0-9-]*(?:/[a-z][a-z0-9-]*)?")


def _topics() -> list[str]:
    block = re.search(r"AGENT_GUIDE_TOPICS[^=]*=\s*\[(.*?)\n\];", INDEX_TS.read_text(), re.S)
    assert block, "AGENT_GUIDE_TOPICS not found in mcp/src/index.ts"
    return re.findall(r'\[\s*"([a-z][a-z0-9-]*)"\s*,', block.group(1))


def _registered() -> set[str]:
    src = INDEX_TS.read_text()
    fixed = set(re.findall(r'server\.resource\(\s*"[^"]+",\s*"(quern://[^"]+)"', src))
    return fixed | {f"quern://guide/{t}" for t in _topics()}


def _guide_pages() -> list[Path]:
    return [OVERVIEW, *sorted(TOPICS_DIR.glob("*.md"))]


def test_every_topic_has_its_file_and_every_file_is_a_topic():
    topics = set(_topics())
    files = {p.stem for p in TOPICS_DIR.glob("*.md")}
    assert topics, "no topics registered"
    assert not topics - files, f"registered, no file in docs/agent-guide/: {sorted(topics - files)}"
    assert not files - topics, (
        f"files no resource serves, add to AGENT_GUIDE_TOPICS: {sorted(files - topics)}")


def test_the_overview_points_to_every_topic():
    overview = OVERVIEW.read_text()
    missing = [t for t in _topics() if f"quern://guide/{t}" not in overview]
    assert not missing, f"topics the overview never names, so no agent finds them: {missing}"


def test_every_quern_address_the_guide_and_tools_name_is_registered():
    registered = _registered()
    named = {}
    for page in [*_guide_pages(), *sorted(TOOLS_DIR.glob("*.ts")), INDEX_TS]:
        for uri in _URI.findall(page.read_text()):
            named.setdefault(uri, page.relative_to(REPO_ROOT).as_posix())
    unknown = {uri: where for uri, where in named.items() if uri not in registered}
    assert not unknown, f"quern:// addresses nothing registers: {unknown}"


def test_guide_pages_use_no_relative_links():
    # [text](path.md) resolves on GitHub and nowhere an agent reads it.
    bad = {}
    for page in _guide_pages():
        links = [m for m in re.findall(r"\]\(([^)\s]+)\)", page.read_text())
                 if not m.startswith(("http://", "https://", "#", "quern://"))]
        if links:
            bad[page.relative_to(REPO_ROOT).as_posix()] = links
    assert not bad, (
        f"relative links an agent can't follow; name the quern:// resource or a URL: {bad}")
