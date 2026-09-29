"""`validate_landmarks` sends its arguments where the handler reads them.

`docs/screen-landmarks.md` documents `validate_landmarks(path=...)` as
validating that path. It never did. The handler declares `source` and `app` as
bare scalars, which FastAPI reads from the **query string**, and the MCP tool
sent them in a JSON body -- silently discarded. Every call validated the whole
loaded registry instead: a plausible answer to a different question.

Found by calling the endpoint during live testing and getting a response that
did not match the knowledge base being pointed at.
"""

from __future__ import annotations

import pathlib
import re

LANDMARKS_TS = (
    pathlib.Path(__file__).resolve().parents[1] / "mcp" / "src" / "tools" / "landmarks.ts"
)
LANDMARKS_PY = (
    pathlib.Path(__file__).resolve().parents[1] / "server" / "api" / "landmarks.py"
)


def _validate_call() -> str:
    source = LANDMARKS_TS.read_text()
    match = re.search(r'apiRequest\(\s*"POST",\s*"/api/v1/landmarks/validate"[^;]*;',
                      source, re.S)
    assert match, "no call to the validate endpoint"
    return match.group(0)


def test_the_handler_still_takes_query_parameters():
    """Pinned from the other side too. If this ever becomes a Pydantic body
    model, the tool has to change with it -- and the two drifting apart is
    exactly how the bug arose."""
    source = LANDMARKS_PY.read_text()
    signature = source[source.index("async def validate_landmarks"):][:220]

    assert "source: str | None = None" in signature
    assert "body:" not in signature


def test_arguments_are_sent_as_query_parameters():
    call = _validate_call()

    assert "undefined" not in call, (
        f"params slot is undefined, so app/source go in the body and are "
        f"discarded: {call}"
    )


def test_both_arguments_are_forwarded():
    source = LANDMARKS_TS.read_text()
    start = source.index('"/api/v1/landmarks/validate"') - 700
    block = source[start:]
    block = block[: block.index('"/api/v1/landmarks/validate"') + 60]

    assert "params.source = path" in block, "path is not forwarded"
    assert "params.app = app" in block, "app is not forwarded"
