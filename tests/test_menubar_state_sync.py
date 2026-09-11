"""Pin the menu-bar app's reads of `~/.quern/*.json` to what the server writes.

`StateReader.swift` says its field names "mirror server/lifecycle/state.py
exactly". That was a hand-maintained claim with nothing enforcing it, which is
the same drift `CONTRIBUTING.md` describes for the API reference — and the
reason `tests/test_readme_sync.py` exists. There was no equivalent here, and the
cost of a rename is not an error: the app reads a key that is absent, gets nil,
and reports the daemon stopped forever.

It found one on the first run. Swift read `localized_name` as a fallback for the
device name and the server has never written that key in any file.

Text search, deliberately, except for state.json where a TypedDict declares the
schema outright. A rename is the failure mode, and a rename moves the string.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from server.lifecycle.state import ServerState

ROOT = Path(__file__).resolve().parent.parent
READER = ROOT / "macos" / "QuernMenuBar" / "Sources" / "StateReader.swift"

#: Which module writes each file the reader opens. A file missing from here
#: fails the test rather than being skipped -- an unpinned read is exactly what
#: this exists to prevent, so a new one must not pass by default.
WRITERS = {
    "state.json": None,  # declared by ServerState below, not searched
    "active-device.json": ROOT / "server" / "lifecycle" / "state.py",
    "update-info.json": ROOT / "server" / "lifecycle" / "update_check.py",
    "config.json": ROOT / "server" / "config.py",
}


def _reads_by_file() -> dict[str, set[str]]:
    """Every `d["key"]` in the reader, grouped by the file its function opens."""
    source = READER.read_text(encoding="utf-8")
    found: dict[str, set[str]] = {}
    # Each reader is one function that opens one file, so splitting on the
    # declaration keeps keys with the file they came from.
    for chunk in re.split(r"\n    (?:private )?static func ", source):
        files = re.findall(r'json\("([^"]+)"\)', chunk)
        if len(files) != 1:
            continue
        keys = set(re.findall(r'd\["([^"]+)"\]', chunk))
        found.setdefault(files[0], set()).update(keys)
    return found


def test_the_reader_was_parsed_at_all():
    """A regex that silently matches nothing would make every test below pass."""
    reads = _reads_by_file()
    assert reads, "found no json() reads — the parse is broken, not the app"
    assert "state.json" in reads
    assert len(reads["state.json"]) >= 5


def test_every_file_the_app_reads_has_a_known_writer():
    unknown = set(_reads_by_file()) - set(WRITERS)
    assert not unknown, (
        f"the menu bar reads {sorted(unknown)}, which nothing here pins. "
        "Add the writing module to WRITERS so renames are caught."
    )


def test_state_json_keys_are_declared_by_ServerState():
    declared = set(ServerState.__annotations__)
    read = _reads_by_file().get("state.json", set())
    missing = read - declared
    assert not missing, (
        f"StateReader reads {sorted(missing)} from state.json, which ServerState "
        "does not declare. The app gets nil and reports the daemon stopped."
    )


@pytest.mark.parametrize("filename", [f for f in WRITERS if WRITERS[f] is not None])
def test_sidecar_keys_appear_in_the_module_that_writes_them(filename):
    source = WRITERS[filename].read_text(encoding="utf-8")
    read = _reads_by_file().get(filename, set())
    assert read, f"no reads parsed for {filename}"
    missing = sorted(k for k in read if f'"{k}"' not in source)
    assert not missing, (
        f"StateReader reads {missing} from {filename}, which "
        f"{WRITERS[filename].relative_to(ROOT)} never writes."
    )
