"""status.py's device rows against the fixture the MCP's port reads too (#605).

`quern remote list` (server/remote/status.py) and the MCP's list_remotes
(mcp/src/remote-status.ts) are two implementations of one answer; each runs
tests/fixtures/remote-status.json, so a change to one that the other lacks
fails here or in mcp/test/remote-status.test.mjs.
"""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path

import pytest

from server.remote import client, status

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "remote-status.json").read_text())
CASES = FIXTURE["cases"]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_device_rows(case):
    if case["rows"] is None:
        with pytest.raises(status._Unexpected):
            status._devices(case["answer"], case["label"])
    else:
        assert status._devices(case["answer"], case["label"]) == case["rows"]


ENTRY_IDS = [f"{c['name']}:{json.dumps(c['entry'])[:40]}" for c in FIXTURE["entries"]]


@pytest.mark.parametrize("case", FIXTURE["entries"], ids=ENTRY_IDS)
def test_entries(case, tmp_path, monkeypatch):
    file = tmp_path / "remotes.json"
    file.write_text(json.dumps({"remotes": {case["name"]: case["entry"]}}))
    monkeypatch.setattr(client, "remotes_path", lambda: file)
    if "error" in case:
        with pytest.raises(client.RemoteError) as e:
            client.load(case["name"])
        assert str(e.value) == case["error"].replace("{file}", str(file))
    else:
        r = client.load(case["name"])
        assert {"url": r.url, "key": r.key, "fingerprint": r.fingerprint,
                "label": r.label} == case["target"]


@pytest.mark.parametrize("case", FIXTURE["reasons"],
                         ids=[c["text"][:30] for c in FIXTURE["reasons"]])
def test_reasons(case):
    if "http" in case:
        e = urllib.error.HTTPError("https://x/health", case["http"], "x", None, None)
    else:
        e = status._Unexpected(case["unexpected"])
    assert status._reason(e) == case["text"]


@pytest.mark.parametrize("case", FIXTURE["health"],
                         ids=[json.dumps(c["answer"]) for c in FIXTURE["health"]])
def test_health(case):
    if case["version"] is None:
        with pytest.raises(status._Unexpected):
            status._health(case["answer"])
    else:
        assert status._health(case["answer"]) == case["version"]
