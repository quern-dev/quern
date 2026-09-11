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

import json
import re
import subprocess
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
    "sys_prefix",
    "sys_base_prefix",
    "virtual_env",
    "path",
    "path_entries_total",
    "path_entries_omitted",
    "which_pymobiledevice3",
    "which_pymobiledevice3_as_setup_sees_it",
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
def captured():
    """Capture, recording every file the script opens, at the interpreter level.

    An audit hook rather than wrapping functions. Wrapping `builtins.open`,
    `Path.read_text` and `subprocess.run` covered the calls someone thought of:
    `Path.read_bytes` goes through `io.open`, and a `subprocess.run(..., shell=True)`
    passes a string rather than a list. Both were measured reading
    ~/.quern/api-key with the suite green. `sys.addaudithook` sees `open`,
    `os.listdir`, `subprocess.Popen` and `os.system` at one layer, below every
    wrapper, so a new way to read a file does not need a new watcher.
    """
    import sys as real_sys

    touched: list[str] = []

    def hook(event: str, args: tuple) -> None:
        if event in ("open", "os.listdir", "os.scandir"):
            touched.append(str(args[0]))
        elif event in ("subprocess.Popen", "os.system", "os.exec"):
            touched.append(" ".join(str(a) for a in args))

    real_sys.addaudithook(hook)
    data = _load().capture(ROOT)
    # Audit hooks cannot be removed, so record only while capturing.
    touched_snapshot = list(touched)
    touched.clear()
    return data, touched_snapshot


def test_it_emits_only_the_fields_it_is_allowed_to(captured):
    data, _ = captured
    unexpected = set(data) - ALLOWED_KEYS
    assert not unexpected, (
        f"capture-env.py grew {sorted(unexpected)}. That output goes into public "
        "issues — add it to ALLOWED_KEYS deliberately, having decided it is safe."
    )


def test_it_never_reads_a_file_that_holds_a_credential(captured):
    _, touched = captured
    seen = {Path(p).name for p in touched if "/.quern" in p.replace("\\", "/")}
    leaked = seen & FORBIDDEN
    assert not leaked, f"capture-env read {sorted(leaked)} from ~/.quern"


def test_it_does_not_go_near_the_quern_directory_at_all(captured):
    """Stronger than the denylist, and the reason it is worth having: a named
    list only catches the files someone thought of. Nothing in ~/.quern is
    needed to describe the environment."""
    _, touched = captured
    inside = [p for p in touched if "/.quern/" in p.replace("\\", "/")]
    assert not inside, f"capture-env touched {inside}"


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
    # Pinned like the other nested shapes. This was the one hole in the "a new
    # field fails the build" promise: returning the parsed plist wholesale would
    # have published whatever it happened to carry, with a green suite.
    assert set(plist) <= {"exists", "unreadable", "program_arguments", "standard_out_path"}


def test_it_still_parses_on_the_oldest_python_it_must_run_on():
    """A syntax or stdlib feature newer than the floor makes the fallback fail
    on exactly the machine it exists for, and nothing else would catch it: the
    suite runs on 3.11+."""

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

    # A silent skip means the floor reads as verified on any runner without an
    # old interpreter. macOS always has one at /usr/bin/python3; say so loudly
    # if that ever stops being true rather than passing by default.
    pytest.skip(
        f"no Python <= {OLDEST_PYTHON} found — the floor is UNVERIFIED on this "
        "machine, not confirmed"
    )


def test_the_path_filter_keeps_a_directory_that_holds_a_tool(tmp_path):
    """The safety clause, which the README and CHANGELOG both single out.

    It has to be a directory that matches *no* category, or the test passes on
    the category and proves nothing — which is what the first version did, using
    /usr/bin, a member of SYSTEM_DIRS.
    """
    from server.lifecycle.capture_env import _categorise, filter_path

    odd = tmp_path / "toolbox"
    odd.mkdir()
    (odd / "adb").write_text("")
    assert _categorise(str(odd)) is None, "this directory must match no category"

    kept, omitted = filter_path(["/nowhere/interesting", str(odd)])

    assert [e["dir"] for e in kept] == [str(odd)]
    assert kept[0]["category"] == "holds-a-tool"
    assert kept[0]["tools"] == ["adb"]
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


def test_the_two_pipx_candidate_lists_agree():
    """`capture_env.CANDIDATES` and `tunneld.pipx_candidates()` answer the same
    question in two files, and nothing made them agree — they already differed
    over the `~/.local/bin` shim. A capture that looks in fewer places than the
    lookup does describes a machine the lookup does not see."""
    from pathlib import Path

    from server.device.tunneld import pipx_candidates
    from server.lifecycle.capture_env import CANDIDATES

    home = str(Path.home())
    captured = {template.format(home=home) for template in CANDIDATES.values()}
    looked_up = {str(p) for p in pipx_candidates()}

    missing = looked_up - captured
    assert not missing, (
        f"the lookup checks {sorted(missing)} but the capture never records them"
    )
