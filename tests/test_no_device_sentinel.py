"""No source may stamp a device_id that is not a device.

`"default"` was the default for `device_id` on nine adapters and five models.
It is not a udid, so `owns(action_udid, "default")` is FOREIGN -- "never the
same work" -- and every entry carrying it was silently discarded from every
trace. Empty reads as UNKNOWN_WORK instead, which is attributed on time with a
caveat: honest rather than wrong.

That cost two rounds. The first fix changed `LogEntry` and three construction
sites and was reported as done, while `POST /logs/oslog/start` went on building
its adapter without a `device_id` and inheriting the sentinel from the
constructor. Fixing call sites cannot fix a poisonous default -- the next
construction reintroduces it -- so the default is what these tests pin.

See docs/proposals/logging-spec.md.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_SERVER = pathlib.Path(__file__).resolve().parents[1] / "server"


def _device_id_defaults():
    """Every `device_id` parameter or field default in server/, with location."""
    for path in sorted(_SERVER.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            # function parameters: def __init__(self, device_id="...")
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                args = node.args
                names = [a.arg for a in args.args[-len(args.defaults):]] if args.defaults else []
                for name, default in zip(names, args.defaults, strict=True):
                    if name == "device_id" and isinstance(default, ast.Constant):
                        yield path, node.lineno, default.value
                kwonly = zip(args.kwonlyargs, args.kw_defaults, strict=True)
                for arg, default in kwonly:
                    if arg.arg == "device_id" and isinstance(default, ast.Constant):
                        yield path, node.lineno, default.value
            # pydantic fields: device_id: str = "..."
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.target.id == "device_id" and isinstance(node.value, ast.Constant):
                    yield path, node.lineno, node.value.value


@pytest.mark.parametrize(
    ("path", "lineno", "value"),
    [(p, n, v) for p, n, v in _device_id_defaults()],
    ids=lambda x: str(x),
)
def test_no_device_id_default_is_a_fake_udid(path, lineno, value):
    """A default must be empty or None. Never a placeholder word.

    Both safe values mean "no device", which attribution handles by falling
    back to time with a caveat. A word like "default" instead claims to *be* a
    device, and compares unequal to every real one -- so it does not degrade
    the attribution, it deletes it, without saying so.
    """
    assert value in ("", None), (
        f"{path.name}:{lineno} defaults device_id to {value!r}. "
        "A non-udid sentinel is silently FOREIGN to every real device, so "
        'entries carrying it vanish from the trace. Use "" or None.'
    )


def test_the_scan_actually_finds_something():
    """Guard the guard. An AST walk that silently matches nothing passes every
    assertion above -- the exact shape this file exists to catch."""
    found = list(_device_id_defaults())

    assert len(found) >= 8, f"only found {len(found)} device_id defaults; scan is broken"
