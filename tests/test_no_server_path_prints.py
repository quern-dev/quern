"""`print()` must not appear on a path a request can reach.

A `print` never reaches `logging`, so it never reaches the ring buffer: in
daemon mode it lands in the log file with no level, no timestamp and no
category, and `query_logs` cannot see it at all. On a request path that makes
it invisible to exactly the person debugging the request.

**This is a guard, not a cleanup.** Measured 2026-09-20: there are no
server-path prints to fix. #238 reported "511 `print()` calls" with
`device/controller_ui.py`, `proxy/cert_manager.py`, `api/proxy_certs.py` and
`device/tunneld.py` named as server-path offenders. All four are wrong:

- the first three are `fingerprint(` matched as a substring of `print(`
- `tunneld.py` is the `quern tunneld install|status|restart` CLI

All 500 real calls are in terminal-facing modules. The number was never the
problem; the risk is the next one added. So this test exists to fail when
somebody writes one, which is the only version of this that stays true.

See docs/proposals/logging-spec.md.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_SERVER = pathlib.Path(__file__).resolve().parents[1] / "server"

#: Modules whose caller is a terminal. A user running `quern setup` wants
#: stdout, not a ring-buffer entry, and routing that through `logging` would
#: make the CLI worse to use.
_TERMINAL_FACING = {
    "main.py",
    "__main__.py",
    "lifecycle/setup.py",
    "lifecycle/updater.py",
    "lifecycle/menubar.py",
    "lifecycle/daemon.py",
    "lifecycle/capture_env.py",
    "device/tunneld.py",  # the `quern tunneld ...` subcommands
}

#: The one deliberate exception on a server path: logging from inside the log
#: handler recurses, so its failure path must not use `logging`.
_ALLOWED_LINES = {
    ("sources/server_log.py", "_task_done"),
}


def _prints_in(path: pathlib.Path) -> list[tuple[int, str]]:
    """Every `print()` call, with the function that contains it."""
    tree = ast.parse(path.read_text())
    owners: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for line in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                owners.setdefault(line, node.name)
    return [
        (node.lineno, owners.get(node.lineno, "<module>"))
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "print"
    ]


def _server_path_modules() -> list[pathlib.Path]:
    return [
        p for p in sorted(_SERVER.rglob("*.py"))
        if str(p.relative_to(_SERVER)) not in _TERMINAL_FACING
    ]


@pytest.mark.parametrize(
    "module", _server_path_modules(), ids=lambda p: str(p.relative_to(_SERVER)),
)
def test_no_print_on_a_request_path(module):
    rel = str(module.relative_to(_SERVER))
    offenders = [
        (line, fn) for line, fn in _prints_in(module)
        if (rel, fn) not in _ALLOWED_LINES
    ]

    assert not offenders, (
        f"{rel} prints on a server path at "
        f"{', '.join(f'line {ln} in {fn}()' for ln, fn in offenders)}.\n"
        "A print never reaches logging, so it never reaches the ring buffer "
        "and query_logs cannot see it. Use server.logging_ext instead, or add "
        "the module to _TERMINAL_FACING if its caller really is a terminal."
    )


def test_the_terminal_facing_list_does_not_rot():
    """Every exemption must name a file that exists.

    An exemption for a module that has been deleted or moved is an exemption
    for nothing, and it silently widens the next time a file takes that path.
    """
    missing = [name for name in _TERMINAL_FACING if not (_SERVER / name).exists()]

    assert not missing, f"_TERMINAL_FACING names modules that no longer exist: {missing}"


def test_the_exemptions_are_still_needed():
    """An exemption for a module that no longer prints is dead permission."""
    unused = [
        name for name in _TERMINAL_FACING
        if not _prints_in(_SERVER / name)
    ]

    assert not unused, (
        f"these are exempt from the print rule but no longer print: {unused}. "
        "Remove them, so the exemption list stays a description of the code."
    )
