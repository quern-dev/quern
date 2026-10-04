"""The imports an older release's updater makes from this tree still resolve.

The `quern update` of 0.18.1-0.18.3 swaps the source tree and keeps running in
the same process, then imports from the *new* tree. A module those imports name
cannot simply move: the update would fail after the swap, with the new code
installed and nothing rebuilt or restarted (#212). `server/device/tool_updates.py`
and `tool_versions.py` are forwarders kept for exactly these lines, which are
copied verbatim from those releases' `server/lifecycle/updater.py`
(`_report_tool_updates`).
"""

from __future__ import annotations

import ast
import pathlib

import pytest

#: Verbatim from 0.18.1, 0.18.2 and 0.18.3.
OLD_UPDATER_IMPORTS = (
    "from server.device.tool_updates import actionable, format_offer, plan_updates",
    "from server.device.tool_versions import collect_sites",
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
    forwarders = {"server.device.tool_updates", "server.device.tool_versions"}
    offenders = []
    for path in server.rglob("*.py"):
        rel = path.relative_to(server.parent).with_suffix("")
        if ".".join(rel.parts) in forwarders:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module in forwarders:
                offenders.append(f"{rel}:{node.lineno}")
            if isinstance(node, ast.ImportFrom) and node.module == "server.device":
                if {a.name for a in node.names} & {"tool_updates", "tool_versions"}:
                    offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, offenders
