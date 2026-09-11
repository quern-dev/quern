"""Guardrails for the diagnostic users are asked to attach to issues.

`scripts/capture-env.py` is meant to be run on a stranger's machine and pasted
into a public bug report. What it captures today is safe — paths, PATH order,
and the tunneld daemon's arguments. What makes that fragile is the
neighbourhood: `~/.quern` also holds `api-key`, a `state.json` carrying
`api_key`, certificate fingerprints and device identifiers. Any of those is one
"include the server state too" away from a public issue.

So the safety is pinned here rather than left as a thing to remember. These
tests fail when the capture grows a new field, reads a file it has no business
reading, or emits anything credential-shaped.
"""

from __future__ import annotations

import builtins
import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "capture-env.py"

#: Every top-level key the capture is allowed to emit. Adding one is a decision
#: about what a stranger will paste into a public issue, so it should not be
#: possible to make it by accident.
ALLOWED_KEYS = {
    "captured_at",
    "home",
    "home_is_external",
    "project_root",
    "path",
    "which_pymobiledevice3",
    "pymobiledevice3_installs",
    "tunneld_plist",
}

#: Files under ~/.quern that must never be read while capturing. Named rather
#: than pattern-matched: a denylist you can read is worth more here than a
#: clever one.
FORBIDDEN = {
    "api-key",
    "state.json",
    "cert-state.json",
    "device-pool.json",
    "active-device.json",
}


def _load():
    spec = importlib.util.spec_from_file_location("capture_env", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def captured(monkeypatch):
    """Capture, recording every file path the script opens while it runs."""
    opened: list[str] = []
    real_open = builtins.open
    real_read_text = Path.read_text

    def watched_open(file, *a, **kw):
        opened.append(str(file))
        return real_open(file, *a, **kw)

    def watched_read_text(self, *a, **kw):
        opened.append(str(self))
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(builtins, "open", watched_open)
    monkeypatch.setattr(Path, "read_text", watched_read_text)
    data = _load().capture(ROOT)
    monkeypatch.undo()
    return data, opened


def test_it_emits_only_the_fields_it_is_allowed_to(captured):
    data, _ = captured
    unexpected = set(data) - ALLOWED_KEYS
    assert not unexpected, (
        f"capture-env.py grew {sorted(unexpected)}. That output goes into public "
        "issues — add it to ALLOWED_KEYS deliberately, having decided it is safe."
    )


def test_it_never_reads_a_file_that_holds_a_credential(captured):
    _, opened = captured
    touched = {Path(p).name for p in opened if "/.quern/" in p.replace("\\", "/")}
    leaked = touched & FORBIDDEN
    assert not leaked, f"capture-env.py read {sorted(leaked)} from ~/.quern"


def test_the_output_carries_nothing_credential_shaped(captured):
    data, _ = captured
    text = json.dumps(data)

    assert not re.search(r"(?i)\b(api[_-]?key|secret|token|password|bearer)\b", text)
    # Device UDIDs and certificate fingerprints are the two identifiers quern
    # handles that a user has no reason to publish.
    assert not re.search(r"\b[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b", text)
    assert not re.search(r"\b[0-9a-f]{64}\b", text)


def test_a_captured_fixture_is_replayable(captured):
    """The output's whole purpose is to be replayed by test_env_replay.py. A
    capture missing what that needs is a file nobody can act on."""
    data, _ = captured
    assert isinstance(data["path"], list) and data["path"], "PATH order is the diagnostic"
    for install in data["pymobiledevice3_installs"]:
        assert set(install) == {"kind", "path", "resolves_to"}
    plist = data["tunneld_plist"]
    assert "exists" in plist
