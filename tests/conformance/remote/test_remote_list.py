"""`quern remote list` against a real paired server: what a client is told.

`list_remotes` in the MCP builds the same rows (`mcp/src/remote-status.ts`),
kept in step with this one by `tests/fixtures/remote-status.json`; this is the
Python side, run against a listener rather than a fixture.
"""

from __future__ import annotations

import json
import shutil
import socket
from pathlib import Path

from .conftest import Paired, Server, quern_cli, session_for


def _list(state_dir: Path) -> tuple[int, dict]:
    done = quern_cli(state_dir, "remote", "list", "--json", timeout=120)
    try:
        return done.returncode, json.loads(done.stdout)
    except ValueError as e:
        raise AssertionError(f"not JSON ({e}):\n{done.stdout}{done.stderr}") from e


def _row(report: dict, name: str) -> dict:
    rows = {r.get("name"): r for r in report["remotes"]}
    assert name in rows, f"no row for {name!r}: {report}"
    return rows[name]


def _with_record(paired: Paired, tmp_path: Path, name: str, **entry) -> Path:
    """A copy of the client's state with one more remote in `remotes.json`:
    the file is the client's own, and `quern connect` will not save a record
    it cannot check, which is what these records are."""
    state = tmp_path / "client"
    shutil.copytree(paired.state_dir, state)
    remotes = json.loads((state / "remotes.json").read_text())
    remotes["remotes"][name] = {"url": paired.url, "fingerprint": paired.fingerprint,
                                "key": paired.key, "label": paired.label, **entry}
    (state / "remotes.json").write_text(json.dumps(remotes))
    return state


def test_a_paired_server_is_listed_up_with_its_version(as_owner, alice: Paired) -> None:
    code, report = _list(alice.state_dir)
    row = _row(report, alice.name)
    health = as_owner.json_ok("GET", "/health", authenticated=False)
    assert code == 0, report
    assert row["reachable"] is True, row
    assert row["version"] == health["version"], row
    assert isinstance(row["devices"], list), row


def test_a_device_another_client_holds_is_not_listed_free(
    server: Server, alice: Paired, bob: Paired, simulator: str,
) -> None:
    as_alice = alice.client(server)
    held = session_for(as_alice)
    try:
        held.reserve(simulator)
        _, report = _list(bob.state_dir)
    finally:
        held.close()
        as_alice.close()
    devices = {d["udid"]: d for d in _row(report, bob.name)["devices"]}
    assert simulator in devices, "the held simulator is not listed at all"
    assert devices[simulator]["free"] is False, devices[simulator]


def test_a_server_that_does_not_answer_says_why_and_fails_the_list(
    alice: Paired, tmp_path: Path,
) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]
    state = _with_record(alice, tmp_path, "gone", url=f"https://127.0.0.1:{closed}")

    code, report = _list(state)
    row = _row(report, "gone")
    assert row["reachable"] is False, row
    assert row["error"], "a down server with no reason"
    assert _row(report, alice.name)["reachable"] is True, "one down cost the others"
    assert code != 0, "a list with a server down exited 0"


def test_a_server_whose_certificate_is_not_the_pinned_one_is_refused(
    alice: Paired, tmp_path: Path,
) -> None:
    """The listener as it is, against a pin it does not match: what a
    rotated certificate, or something in between, looks like from here."""
    state = _with_record(alice, tmp_path, "repinned", fingerprint="00" * 32)

    _, report = _list(state)
    row = _row(report, "repinned")
    assert row["reachable"] is False, row
    assert "certificate" in (row["error"] or "").lower(), row


def test_connect_with_a_key_will_not_keep_a_wrong_fingerprint(
    server: Server, alice: Paired, tmp_path: Path,
) -> None:
    """Checked before it is kept: a typo in the fingerprint is found at
    connect, not by the first agent that uses it."""
    state = tmp_path / "client"
    state.mkdir()
    done = quern_cli(state, "connect", server.remote_url, "--key", alice.key,
                     "--fingerprint", "00" * 32, "--name", "typo")
    assert done.returncode != 0, done.stdout
    assert not (state / "remotes.json").exists() or "typo" not in (
        state / "remotes.json").read_text()
