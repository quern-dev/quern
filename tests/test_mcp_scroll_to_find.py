"""The MCP wrapper forwards `scroll_to_find` in both directions.

`mcp/` has no TypeScript tests, so the only guard available is reading the
source -- which is how `tests/test_mcp_defaults.py` already checks that file.

The bug this pins: the forwarding line read `=== false`, which was correct
while the schema had `.default(true)` (omitting `true` was right, because
`true` *was* the server default). When the schema became `.optional()` for the
tri-state, unset and `true` started producing an identical body -- so
`scroll_to_find: true` never reached the server, and the retry quern itself
recommends returned a byte-identical response and looped.
"""

from __future__ import annotations

import pathlib
import re

DEVICE_UI_TS = (
    pathlib.Path(__file__).resolve().parents[1] / "mcp" / "src" / "tools" / "device-ui.ts"
)


def _forwarding_line() -> str:
    source = DEVICE_UI_TS.read_text()
    match = re.search(r"^\s*if \([^\n]*scroll_to_find[^\n]*body\.scroll_to_find[^\n]*$",
                      source, re.M)
    assert match, "no line forwarding scroll_to_find into the request body"
    return match.group(0).strip()


def test_true_is_forwarded_not_dropped():
    """The whole point of the escape hatch. A `=== false` test here silently
    discards `true`."""
    line = _forwarding_line()

    assert "=== false" not in line, (
        f"only false is forwarded, so scroll_to_find=true never reaches the "
        f"server: {line}"
    )


def test_the_forwarding_is_conditional_on_being_set():
    """Unset must stay unset, or the server can never see `None` and the
    knowledge base is never consulted."""
    line = _forwarding_line()

    assert "!== undefined" in line, f"unset is not distinguished from set: {line}"


def test_the_schema_is_optional_so_the_server_can_see_unset():
    """A `.default(true)` here would send `true` on every call and the
    tri-state would be unreachable from MCP."""
    source = DEVICE_UI_TS.read_text()
    block = source[source.index("scroll_to_find: z"):][:220]

    assert ".optional()" in block, block
    assert ".default(" not in block, block
