"""Imports older releases make from this tree during an update still resolve.

`quern update` swaps the source tree before anything restarts, so code from the
release being replaced is still running against the new files: the server
until its restart, and in 0.18.1-0.18.3 the updater itself, in-process. Any
import such code makes inside a function reads the new tree, so a module it
names cannot simply move (#212). The modules #396 moves leave forwarders at
their old paths for exactly these imports.

Each line below is verbatim from some release's `server/`, found by parsing
every release from v0.18.0 on for function-level imports of the moved modules.
A forwarder is removed once four releases have shipped after its move (#396);
its lines go with it.
"""

from __future__ import annotations

import ast
import importlib
import pathlib

import pytest

#: Each forwarder, and where its module lives now.
FORWARDED_TO = {
    "server.device.tool_updates": "server.tooling.tool_updates",
    "server.device.tool_versions": "server.tooling.tool_versions",
    "server.device.tool_probe": "server.tooling.tool_probe",
    "server.device.landmarks": "server.knowledge.landmarks",
}

#: Verbatim, from releases v0.18.0-v0.23.0. The tool_updates/tool_versions
#: updater lines are 0.18.1-0.18.3's in-process tool report (#400); the rest
#: are an old server's or CLI's, reachable while an update is between swap and
#: restart.
OLD_IMPORTS = (
    # #400
    "from server.device.tool_probe import probe_stdout",
    "from server.device.tool_updates import actionable",
    "from server.device.tool_updates import actionable, format_offer, plan_updates",
    "from server.device.tool_updates import format_report, plan_updates",
    "from server.device.tool_versions import collect_sites",
    "from server.device.tool_versions import collect_sites, upgrade_note",
    # 3c
    "from server.device.landmarks import LandmarkRegistry",
    "from server.device.landmarks import detect_collisions",
)


@pytest.mark.parametrize("line", OLD_IMPORTS)
def test_an_old_releases_import_resolves_to_the_current_code(line):
    namespace: dict = {}
    exec(line, namespace)  # noqa: S102 - the line under test, verbatim
    old = line.split()[1]
    current = importlib.import_module(FORWARDED_TO[old])
    for name in line.split(" import ")[1].split(", "):
        assert namespace[name] is getattr(current, name), name


def test_every_forwarder_has_old_imports_and_every_import_a_forwarder():
    named = {line.split()[1] for line in OLD_IMPORTS}
    assert named == set(FORWARDED_TO), named ^ set(FORWARDED_TO)


def test_nothing_in_this_tree_imports_through_the_forwarders():
    """They exist for older releases' code only. Imported from here, a test
    patching the real module would patch one copy while the caller read the
    forwarder's -- passing for the wrong reason."""
    server = pathlib.Path(__file__).resolve().parents[1] / "server"
    forwarders = set(FORWARDED_TO)
    short = {m.rsplit(".", 1)[1] for m in forwarders}
    offenders = []
    for path in server.rglob("*.py"):
        rel = path.relative_to(server.parent).with_suffix("")
        if ".".join(rel.parts) in forwarders:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            # `import server.device.x` as well as `from ... import` (CodeRabbit on #400).
            if isinstance(node, ast.Import) and any(
                alias.name in forwarders for alias in node.names
            ):
                offenders.append(f"{rel}:{node.lineno}")
            if isinstance(node, ast.ImportFrom) and node.module in forwarders:
                offenders.append(f"{rel}:{node.lineno}")
            if isinstance(node, ast.ImportFrom) and node.module == "server.device":
                if {a.name for a in node.names} & short:
                    offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, offenders
