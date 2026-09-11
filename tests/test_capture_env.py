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
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "capture-env.py"
MODULE = ROOT / "server" / "lifecycle" / "capture_env.py"

#: The oldest Python the fallback has to run on.
#:
#: Apple's Command Line Tools have shipped 3.9.6 as /usr/bin/python3 since Xcode
#: 14 and still do on current macOS, so 3.9 is the real floor -- not the 3.11
#: quern itself requires. Older Xcodes shipped 3.8.9, but a macOS that old is
#: outside Xcode's own support window, so it is not a floor anyone can be on.
#:
#: Found by running it, not by reasoning: the module reached for `datetime.UTC`,
#: which is 3.11, and the fallback died on the one kind of machine it exists for.
OLDEST_PYTHON = (3, 9)

#: Every top-level key the capture is allowed to emit. Adding one is a decision
#: about what a stranger will paste into a public issue, so it should not be
#: possible to make it by accident.
ALLOWED_KEYS = {
    "captured_at",
    "home",
    "home_is_external",
    "project_root",
    "path",
    "path_entries_total",
    "path_entries_omitted",
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
    from server.lifecycle import capture_env

    return capture_env


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
    for entry in data["path"]:
        assert set(entry) == {"index", "dir", "category", "tools"}
    for install in data["pymobiledevice3_installs"]:
        assert set(install) == {"kind", "path", "resolves_to"}
    plist = data["tunneld_plist"]
    assert "exists" in plist


def test_it_still_parses_on_the_oldest_python_it_must_run_on():
    """A syntax or stdlib feature newer than the floor makes the fallback fail
    on exactly the machine it exists for, and nothing else would catch it: the
    suite runs on 3.11+."""
    import subprocess

    candidates = ["/usr/bin/python3", f"python{OLDEST_PYTHON[0]}.{OLDEST_PYTHON[1]}"]
    for candidate in candidates:
        probe = subprocess.run(
            [candidate, "-c", "import sys; print(sys.version_info[:2])"],
            capture_output=True, text=True,
        )
        if probe.returncode != 0:
            continue
        version = eval(probe.stdout.strip())  # noqa: S307 - our own output
        if version > OLDEST_PYTHON:
            continue
        result = subprocess.run(
            [candidate, "-c",
             f"import sys; sys.path.insert(0, {str(ROOT)!r}); "
             "import server.lifecycle.capture_env"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, (
            f"capture_env does not import on Python {version}, which is what "
            f"scripts/capture-env.py falls back to:\n{result.stderr}"
        )
        return

    pytest.skip(f"no Python <= {OLDEST_PYTHON} available to check the floor")


def test_the_path_filter_keeps_a_directory_that_holds_a_tool():
    """The safety clause. A whitelist alone would hide an unexpected directory
    that a tool is genuinely resolved from — the one surprise worth reporting."""
    from server.lifecycle.capture_env import filter_path

    kept, omitted = filter_path(["/nowhere/interesting", "/usr/bin"])
    dirs = {entry["dir"] for entry in kept}
    assert "/usr/bin" in dirs
    assert omitted == 1


def test_the_path_filter_preserves_position():
    """Order decides which copy of a tool wins, so a survivor has to carry where
    it was — otherwise the capture cannot be replayed."""
    from server.lifecycle.capture_env import filter_path

    kept, _ = filter_path(["/nowhere", "/also/nowhere", "/usr/bin"])
    assert [e["index"] for e in kept] == [2]


def test_the_path_filter_drops_unrelated_software():
    """The reason it exists: a full PATH names everything installed."""
    from server.lifecycle.capture_env import filter_path

    kept, omitted = filter_path([
        "/Users/someone/.meteor",
        "/Users/someone/.lmstudio/bin",
        "/Users/someone/.cargo/bin",
    ])
    assert kept == []
    assert omitted == 3
