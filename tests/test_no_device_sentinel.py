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


def _default_value(node):
    """The literal a default expression settles on, or `_UNREADABLE`.

    Handles the spellings pydantic allows -- a bare literal,
    `Field(default=...)`, `Field("...")` -- and refuses the one it cannot
    evaluate, `Field(default_factory=...)`. The first version of this only
    understood bare literals, so it walked straight past
    `LogEntry.device_id`, which is a `Field(default="")` -- the single field
    whose sentinel caused the bug this whole file exists to prevent. A guard
    that silently skips a form is the same failure it is guarding against.
    """
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Call):
        func = node.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name == "Field":
            keywords = {kw.arg for kw in node.keywords}
            # A factory is computed at runtime and cannot be read here. It
            # must fail rather than fall through to the "required" branch
            # below, which would call `Field(default_factory=lambda:
            # "default")` clean -- the sentinel reintroduced through the one
            # spelling the guard could not see. Unreadable is the honest
            # answer; a hole is not a pass.
            if "default_factory" in keywords:
                return _UNREADABLE
            for kw in node.keywords:
                if kw.arg == "default":
                    return _default_value(kw.value)
            if node.args:
                return _default_value(node.args[0])
            # No default and no factory: the field is required, which cannot
            # carry a sentinel.
            return ""
    return _UNREADABLE


#: A default this scan could not evaluate. Fails loudly rather than passing
#: quietly -- an unreadable default is a hole in the guard, not a clean bill.
_UNREADABLE = object()


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
                    if name == "device_id":
                        yield path, node.lineno, _default_value(default)
                kwonly = zip(args.kwonlyargs, args.kw_defaults, strict=True)
                for arg, default in kwonly:
                    if arg.arg == "device_id" and default is not None:
                        yield path, node.lineno, _default_value(default)
            # pydantic fields: device_id: str = "..." or Field(default="...")
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.target.id == "device_id" and node.value is not None:
                    yield path, node.lineno, _default_value(node.value)


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
    assert value is not _UNREADABLE, (
        f"{path.name}:{lineno} has a device_id default this scan cannot "
        "evaluate. Teach `_default_value` the new spelling -- a default the "
        "guard cannot read is a hole in it, not a pass."
    )
    assert value in ("", None), (
        f"{path.name}:{lineno} defaults device_id to {value!r}. "
        "A non-udid sentinel is silently FOREIGN to every real device, so "
        'entries carrying it vanish from the trace. Use "" or None.'
    )


def test_the_scan_reaches_the_field_that_caused_the_bug():
    """Guard the guard, by name rather than by count.

    `LogEntry.device_id` is the field whose `"default"` sentinel silently
    dropped every app log line from every trace, and it is spelled
    `Field(default="")` -- which the first version of this scan did not
    understand and skipped without a word. A count-based check stayed green
    through that, because eleven other sites still matched.
    """
    found = {
        (path.name, lineno): value for path, lineno, value in _device_id_defaults()
    }
    models = {k: v for k, v in found.items() if k[0] == "models.py"}

    assert any(v == "" for v in models.values()), (
        "the scan found no models.py device_id default -- it is not reading "
        "`Field(default=...)`, which is how LogEntry spells it"
    )
    assert len(found) >= 8, f"only found {len(found)} device_id defaults; scan is broken"


class TestAnUnreadableDefaultIsNotAPass:
    """`_default_value` returning "looks fine" for a form it cannot evaluate
    is the hole this file exists to close, reopened one level up.

    `Field(default_factory=...)` is computed at runtime. Reading it statically
    is not possible, so the only honest answers are "fail" or "teach the
    scanner" -- never "fine"."""

    def _value_of(self, source: str):
        import ast as _ast

        tree = _ast.parse(source)
        [node] = [n for n in _ast.walk(tree) if isinstance(n, _ast.AnnAssign)]
        return _default_value(node.value)

    def test_a_factory_is_unreadable(self):
        got = self._value_of('device_id: str = Field(default_factory=lambda: "default")')

        assert got is _UNREADABLE

    def test_a_plain_field_default_still_reads(self):
        assert self._value_of('device_id: str = Field(default="")') == ""

    def test_a_positional_field_default_still_reads(self):
        assert self._value_of('device_id: str = Field("")') == ""

    def test_a_required_field_is_fine(self):
        """No default at all cannot be a sentinel."""
        assert self._value_of('device_id: str = Field(description="x")') == ""

    def test_an_unreadable_default_fails_the_guard(self):
        """The verdict has to reach an assertion, not just the helper."""
        with pytest.raises(AssertionError, match="cannot evaluate"):
            test_no_device_id_default_is_a_fake_udid(
                pathlib.Path("models.py"), 1, _UNREADABLE,
            )
