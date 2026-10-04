"""Imports older releases make from this tree during an update still resolve.

`quern update` swaps the source tree before anything restarts, so code from the
release being replaced is still running against the new files: the server
until its restart, and in 0.18.1-0.18.3 the updater itself, in-process. Any
import such code makes inside a function reads the new tree, so a module it
names cannot simply move (#212). The modules #396 moves leave forwarders at
their old paths for exactly these imports.

Each line below is verbatim from some release's `server/`, found by parsing
every release tag from v0.18.0 on, pre-releases included, for function-level
imports of the moved modules. A forwarder is removed once four releases have
shipped after its move (#396); its lines go with it.

Not every line is reachable. An import in code that already ran -- at startup,
or behind a module the server had loaded -- resolves from `sys.modules` and
never reads the new tree. The tooling lines are reached: v0.18.2's updater
crashed on them in a rehearsal (#400). Others are forwarded as a precaution,
because a few names are cheaper to forward than it is to prove, for every
release, that nothing reaches them after a swap. Imports that only run at a
fresh process's start or a CLI's dispatch are not listed: no old process makes
them after the swap.
"""

from __future__ import annotations

import ast
import importlib
import pathlib

import pytest

_SERVER = pathlib.Path(__file__).resolve().parents[1] / "server"
#: Each forwarder, and where its module lives now.
FORWARDED_TO = {
    "server.device.tool_updates": "server.tooling.tool_updates",
    "server.device.tool_versions": "server.tooling.tool_versions",
    "server.device.tool_probe": "server.tooling.tool_probe",
    "server.device.landmarks": "server.knowledge.landmarks",
    "server.device.web_content": "server.device.web.web_content",
    "server.device.web_probing": "server.device.web.web_probing",
    "server.device.webinspector": "server.device.web.webinspector",
}

#: Verbatim, from releases v0.18.0-v0.24.0-beta.1. The tool_updates and
#: tool_versions updater lines are 0.18.1-0.18.3's in-process tool report
#: (#400); the rest are an old server's, run between swap and restart.
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
    # 3d
    "from server.device.web_content import _is_app",
    "from server.device.web_content import _texts_correspond, normalise",
    "from server.device.web_content import collect_web_content, from_probe",
    "from server.device.web_probing import app_frame",
    "from server.device.web_probing import hit_contains",
    "from server.device.web_probing import sweep_web_content",
    # Parenthesized over two lines in the original.
    "from server.device.webinspector import WebInspectorError, simulator_udid_for_application",
    "from server.device.webinspector import SimulatorWebInspector",
    "from server.device.webinspector import simulator_udid_for_application",
)


@pytest.mark.parametrize("line", OLD_IMPORTS)
def test_an_old_releases_import_resolves_to_the_current_code(line):
    namespace: dict = {}
    exec(line, namespace)  # noqa: S102 - the line under test, verbatim
    old = line.split()[1]
    current = importlib.import_module(FORWARDED_TO[old])
    for name in line.split(" import ")[1].split(", "):
        assert namespace[name] is getattr(current, name), name


def _named_by_old_imports() -> dict[str, set[str]]:
    named: dict[str, set[str]] = {}
    for line in OLD_IMPORTS:
        module, names = line.removeprefix("from ").split(" import ")
        named.setdefault(module, set()).update(names.split(", "))
    return named


def test_every_forwarder_has_old_imports_and_every_import_a_forwarder():
    named = set(_named_by_old_imports())
    assert named == set(FORWARDED_TO), named ^ set(FORWARDED_TO)


@pytest.mark.parametrize("forwarder", sorted(FORWARDED_TO))
def test_a_forwarder_forwards_exactly_the_names_old_releases_import(forwarder):
    """No more, so nothing new comes to depend on one before its sunset; and
    no `*`, which would forward names nobody checked."""
    path = _SERVER.parent.joinpath(*forwarder.split(".")).with_suffix(".py")
    tree = ast.parse(path.read_text())
    body = [n for n in tree.body if not (
        isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
    assert all(isinstance(n, ast.ImportFrom) for n in body), forwarder
    assert {n.module for n in body} == {FORWARDED_TO[forwarder]}
    forwarded = {alias.name for n in body for alias in n.names}
    assert forwarded == _named_by_old_imports()[forwarder]


def _absolute(path: pathlib.Path, node: ast.ImportFrom) -> str:
    """The module a `from` import names, with a relative one resolved."""
    if not node.level:
        return node.module or ""
    # A module's package and an __init__'s are both its parts minus the last.
    package = path.relative_to(_SERVER.parent).with_suffix("").parts[:-1]
    package = package[: len(package) - (node.level - 1)]
    return ".".join(package + ((node.module,) if node.module else ()))


def _reaches_a_forwarder(path: pathlib.Path, node: ast.AST) -> bool:
    forwarders = set(FORWARDED_TO)
    if isinstance(node, ast.Import):
        # `import server.device.x` as well as `from ... import` (CodeRabbit on #400).
        return any(alias.name in forwarders for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        base = _absolute(path, node)
        return base in forwarders or any(
            f"{base}.{alias.name}" in forwarders for alias in node.names)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        # importlib.import_module("..."), and patch targets in tests:
        # monkeypatch.setattr("server.device.x.name", ...).
        return any(node.value == m or node.value.startswith(m + ".")
                   for m in forwarders)
    return False


def test_nothing_in_this_tree_imports_through_the_forwarders():
    """They exist for older releases' code only. Imported from here, a test
    patching the real module would patch one copy while the caller read the
    forwarder's -- passing for the wrong reason. So tests are scanned as well
    as the server, for patch targets as well as imports."""
    tests = pathlib.Path(__file__).resolve().parent
    offenders = []
    for path in [*_SERVER.rglob("*.py"), *tests.rglob("*.py")]:
        dotted = ".".join(path.relative_to(_SERVER.parent).with_suffix("").parts)
        if dotted in FORWARDED_TO or path == pathlib.Path(__file__).resolve():
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if _reaches_a_forwarder(path, node):
                offenders.append(f"{path.relative_to(_SERVER.parent)}:{node.lineno}")
    assert not offenders, offenders
