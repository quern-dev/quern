"""Constants the MCP wrapper repeats from the Python server.

TypeScript cannot import from `server/`, so a few values exist twice. Twice
means they can disagree, and a disagreement here is silent: the wrapper would
go on talking to the old port while the server listened on the new one, and
the only symptom would be "cannot reach Quern" on a machine where quern is
plainly running.

The same guard already exists for the Node version floor in
`tests/test_node_env.py`; this is the port.
"""

from __future__ import annotations

import re
from pathlib import Path

from server.lifecycle.ports import DEFAULT_SERVER_PORT

CONFIG_TS = Path(__file__).resolve().parents[1] / "mcp" / "src" / "config.ts"


def test_the_wrapper_default_port_matches_the_server():
    source = CONFIG_TS.read_text()
    match = re.search(r"DEFAULT_SERVER_PORT\s*=\s*(\d+)", source)
    assert match, f"no DEFAULT_SERVER_PORT in {CONFIG_TS}"
    assert int(match.group(1)) == DEFAULT_SERVER_PORT, (
        "mcp/src/config.ts and server/lifecycle/ports.py disagree about the "
        "default port, so the wrapper would look somewhere the server is not"
    )


def test_the_wrapper_does_not_hardcode_the_port_anywhere_else():
    """One definition, or the next change updates one of them."""
    source = CONFIG_TS.read_text()
    literals = re.findall(rf"\b{DEFAULT_SERVER_PORT}\b", source)
    assert len(literals) == 1, (
        f"{len(literals)} occurrences of {DEFAULT_SERVER_PORT} in config.ts; "
        "it should appear once, as DEFAULT_SERVER_PORT"
    )
