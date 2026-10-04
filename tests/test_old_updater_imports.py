"""Imports older releases make from this tree during an update still resolve.

`quern update` swaps the source tree before anything restarts, so code from the
release being replaced is still running against the new files: the server
until its restart, and in 0.18.1-0.18.3 the updater itself, in-process. Any
import such code makes inside a function reads the new tree, so a module it
names cannot simply move (#212). `server/device/tool_updates.py`,
`tool_versions.py` and `tool_probe.py` are forwarders kept for these.

The pinned set is every name imported inside a function, from the three moved
modules, by any release from v0.18.0 to v0.23.0 -- found by parsing each tag's
`server/` (CodeRabbit on #400 found the two the first version missed). The same
scan found no such imports of the modules moved in #396's phases 2 and 3a.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

#: Every function-level import of the moved modules in any release from v0.18.0
#: to v0.23.0, verbatim (`git grep` over each tag's `server/`). The updater's
#: lines are 0.18.1-0.18.3's in-process tool report; the rest are an old
#: server's or CLI's, reachable while an update is between swap and restart.
OLD_UPDATER_IMPORTS = (
    "from server.device.tool_probe import probe_stdout",
    "from server.device.tool_updates import actionable",
    "from server.device.tool_updates import actionable, format_offer, plan_updates",
    "from server.device.tool_updates import format_report, plan_updates",
    "from server.device.tool_versions import collect_sites",
    "from server.device.tool_versions import collect_sites, upgrade_note",
)


@pytest.mark.parametrize("line", OLD_UPDATER_IMPORTS)
def test_an_old_updaters_import_resolves_to_the_current_code(line):
    namespace: dict = {}
    exec(line, namespace)  # noqa: S102 - the line under test, verbatim
    module = line.split()[1].replace("server.device.", "server.tooling.")
    current = __import__(module, fromlist=["_"])
    for name in line.split(" import ")[1].split(", "):
        assert namespace[name] is getattr(current, name), name


def test_nothing_in_this_tree_imports_through_the_forwarders():
    """They exist for old updaters only. Imported from here, a test patching
    `server.tooling...` would patch the real module while the caller read the
    forwarder's copy -- passing for the wrong reason."""
    server = pathlib.Path(__file__).resolve().parents[1] / "server"
    forwarders = {"server.device.tool_updates", "server.device.tool_versions",
                  "server.device.tool_probe"}
    offenders = []
    for path in server.rglob("*.py"):
        rel = path.relative_to(server.parent).with_suffix("")
        if ".".join(rel.parts) in forwarders:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module in forwarders:
                offenders.append(f"{rel}:{node.lineno}")
            if isinstance(node, ast.ImportFrom) and node.module == "server.device":
                if {a.name for a in node.names} & {"tool_updates", "tool_versions",
                                                   "tool_probe"}:
                    offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, offenders
