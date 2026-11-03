"""status.py's device rows against the fixture the MCP's port reads too (#605).

`quern remote list` (server/remote/status.py) and the MCP's list_remotes
(mcp/src/remote-status.ts) are two implementations of one answer; each runs
tests/fixtures/remote-status.json, so a change to one that the other lacks
fails here or in mcp/test/remote-status.test.mjs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from server.remote import status

CASES = json.loads((Path(__file__).parent / "fixtures" / "remote-status.json").read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_device_rows(case):
    if case["rows"] is None:
        with pytest.raises(status._Unexpected):
            status._devices(case["answer"], case["label"])
    else:
        assert status._devices(case["answer"], case["label"]) == case["rows"]
