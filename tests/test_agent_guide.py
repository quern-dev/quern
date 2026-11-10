"""Keep the agent guide's resources, files and links in step.

Agents read the guide over MCP: `quern://guide` is the overview
(docs/agent-guide.md), and each topic is its own resource,
`quern://guide/<topic>`, read from docs/agent-guide/<topic>.md and registered
from AGENT_GUIDE_TOPICS in mcp/src/index.ts. A topic's page is its contents
list, and each line of it of the form "- `quern://guide/<topic>/<page>` --
..." registers a page, read from docs/agent-guide/<topic>/<page>.md.

A relative markdown link does not
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
# The app knowledge base guide is laid out the same way: a contents page whose
# lines register its pages.
KB_GUIDE = REPO_ROOT / "docs" / "app-knowledge-guide.md"
KB_DIR = REPO_ROOT / "docs" / "app-knowledge-guide"
KB_BASE = "quern://app-knowledge-guide"

# The whole address: a longer one must not pass as the registered prefix.
_URI = re.compile(r"quern://[a-z][a-z0-9-]*(?:/[a-z][a-z0-9-]*)*(?![a-z0-9/-])")


def _topics() -> list[str]:
    block = re.search(r"AGENT_GUIDE_TOPICS[^=]*=\s*\[(.*?)\n\];", INDEX_TS.read_text(), re.S)
    assert block, "AGENT_GUIDE_TOPICS not found in mcp/src/index.ts"
    return re.findall(r'\[\s*"([a-z][a-z0-9-]*)"\s*,', block.group(1))


def _listed(base: str, contents: Path) -> list[str]:
    """The pages a contents page names: the same rule as guidePages() in
    mcp/src/guide-pages.ts, which registers them."""
    line = re.compile(rf"^- `{re.escape(base)}/([a-z][a-z0-9-]*)` -- .+$", re.M)
    return line.findall(contents.read_text())


def _section_pages(topic: str) -> list[str]:
    return _listed(f"quern://guide/{topic}", TOPICS_DIR / f"{topic}.md")


def _contents_pages() -> list[tuple[str, Path, Path]]:
    """Every contents page: (its address, its file, the directory of its pages)."""
    return [*((f"quern://guide/{t}", TOPICS_DIR / f"{t}.md", TOPICS_DIR / t) for t in _topics()),
            (KB_BASE, KB_GUIDE, KB_DIR)]


def _registered() -> set[str]:
    src = INDEX_TS.read_text()
    fixed = set(re.findall(r'server\.resource\(\s*"[^"]+",\s*"(quern://[^"]+)"', src))
    topics = {f"quern://guide/{t}" for t in _topics()}
    pages = {f"{base}/{p}" for base, contents, _ in _contents_pages()
             for p in _listed(base, contents)}
    return fixed | topics | pages


def _guide_pages() -> list[Path]:
    return [OVERVIEW, *sorted(TOPICS_DIR.glob("*.md")), *sorted(TOPICS_DIR.glob("*/*.md")),
            KB_GUIDE, *sorted(KB_DIR.glob("*.md"))]


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
        # The target, with or without a title: [text](page.md "Title").
        links = [m for m in re.findall(r"\]\(\s*([^)\s]+)[^)]*\)", page.read_text())
                 if not m.startswith(("http://", "https://", "#", "quern://"))]
        if links:
            bad[page.relative_to(REPO_ROOT).as_posix()] = links
    assert not bad, (
        f"relative links an agent can't follow; name the quern:// resource or a URL: {bad}")


def test_every_listed_page_has_its_file_and_every_file_is_listed():
    for base, contents, folder in _contents_pages():
        listed = _listed(base, contents)
        files = {p.stem for p in folder.glob("*.md")}
        where = folder.relative_to(REPO_ROOT).as_posix()
        assert listed, f"{base}: its contents page lists no pages"
        assert len(listed) == len(set(listed)), f"{base}: a page listed twice: {listed}"
        assert not set(listed) - files, (
            f"{base}: listed, no file in {where}/: {sorted(set(listed) - files)}")
        assert not files - set(listed), (
            f"{base}: files its contents page never lists, so nothing serves them: "
            f"{sorted(files - set(listed))}")


def test_every_page_says_where_it_sits():
    """A page read on its own says what it is part of and where to start."""
    for base, contents, folder in _contents_pages():
        start = "`quern://guide`" if base.startswith("quern://guide/") else f"`{base}`"
        for page in _listed(base, contents):
            head = (folder / f"{page}.md").read_text()[:400]
            assert f"`{base}/{page}`" in head and start in head, (
                f"{base}/{page}: its header does not name its own address and where to start")


def test_the_overview_tool_map_points_at_pages_that_exist():
    """The tool map names exact pages, so a job is one read away."""
    table = OVERVIEW.read_text().split("## Which tool for which job", 1)[1].split("\n---", 1)[0]
    rows = [r for r in table.splitlines() if r.startswith("| ") and not r.startswith("| You want")]
    assert rows, "no tool-map rows found"
    registered = _registered()
    for row in rows:
        target = re.findall(r"`(quern://guide/[a-z0-9/-]+)`\s*\|\s*$", row)
        assert target, f"a tool-map row names no page: {row[:80]}"
        assert target[0] in registered, f"tool-map row points at {target[0]}, which nothing serves"
