"""A recording's files, downloaded from another machine (#254, phase 3b).

Served to the recording's own principal (the owner gets any), and only the
files a recording writes -- not whatever else is in its directory, and not
through a symlink placed there.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from server import device_access
from server.recording.recorder import RecordingError
from server.session_registry import SessionRegistry

KEY = "k" * 32


class FakeRecordings:
    def __init__(self, recs):
        self._recs = {r.id: r for r in recs}
        self.live: set = set()

    def get(self, rec_id):
        if rec_id not in self._recs:
            raise RecordingError(f"no recording {rec_id}")
        return self._recs[rec_id]

    def list(self):
        return list(self._recs.values())

    def is_live(self, directory):
        return directory in self.live


@pytest.fixture
def world(tmp_path):
    import server.main as m
    from server.config import ServerConfig

    d = tmp_path / "rec_alice"
    d.mkdir()
    (d / "events.jsonl").write_text('{"type": "start"}\n')
    (d / "manifest.json").write_text("{}")
    (d / "video-1.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"v" * 100)
    (d / "video-1.log").write_text("encoder log")
    (d / "notes.txt").write_text("not the recording's")
    secret = tmp_path / "secret"
    secret.write_text("the api key")
    os.symlink(secret, d / "video-9.mp4")
    rec = SimpleNamespace(id="rec_alice", dir=d, principal="client:alice")
    owners = SimpleNamespace(id="rec_owner", dir=tmp_path, principal="owner")

    app = m.create_app(ServerConfig(api_key=KEY))
    app.state.clients.path = tmp_path / "clients.json"
    app.state.clients.seen_path = tmp_path / "clients-seen.json"
    app.state.recordings = FakeRecordings([rec, owners])
    registry = SessionRegistry(grace_seconds=180)
    app.state.sessions = registry
    device_access.install(registry)
    alice, bob = app.state.clients.add("alice"), app.state.clients.add("bob")
    try:
        yield TestClient(app), app, alice, bob, d
    finally:
        device_access.install(None)


def _h(key):
    return {"Authorization": f"Bearer {key}", "X-Quern-Session": "s1"}


OWNER = {"Authorization": f"Bearer {KEY}"}


def test_the_owner_lists_only_the_recordings_own_files(world):
    c, *_ = world
    r = c.get("/api/v1/recordings/rec_alice/files", headers=OWNER)
    assert r.status_code == 200, r.text
    assert [f["name"] for f in r.json()["files"]] == [
        "events.jsonl", "manifest.json", "video-1.log", "video-1.mp4"]
    assert r.json()["live"] is False


def test_a_file_is_streamed_whole(world):
    c, _, alice, _, d = world
    r = c.get("/api/v1/recordings/rec_alice/files/video-1.mp4", headers=_h(alice))
    assert r.status_code == 200 and r.content == (d / "video-1.mp4").read_bytes()
    assert r.headers["content-type"] == "video/mp4"


@pytest.mark.parametrize("name", ["notes.txt", "video-9.mp4", "..", "api-key"])
def test_nothing_but_the_recordings_files_and_no_symlink(world, name):
    """video-9.mp4 has the right name and points at a file outside."""
    c, *_ = world
    r = c.get(f"/api/v1/recordings/rec_alice/files/{name}", headers=OWNER)
    assert r.status_code == 404, r.text
    assert b"the api key" not in r.content


def test_another_keys_recording_reads_as_missing(world):
    """In shared mode too: a client may list every recording, but downloads
    only its own."""
    c, _, _, bob, _ = world
    assert c.get("/api/v1/recordings/rec_alice/files", headers=_h(bob)).status_code == 404
    assert c.get("/api/v1/recordings/rec_alice/files/events.jsonl",
                 headers=_h(bob)).status_code == 404
    assert c.get("/api/v1/recordings/rec_owner/files", headers=_h(bob)).status_code == 404
    assert c.get("/api/v1/recordings/rec_nope/files", headers=OWNER).status_code == 404


def test_a_recording_still_being_made_says_so(world):
    c, app, alice, _, d = world
    app.state.recordings.live.add(d)
    assert c.get("/api/v1/recordings/rec_alice/files", headers=_h(alice)).json()["live"] is True


def test_a_finished_recording_is_still_found_after_a_restart(world, tmp_path, monkeypatch):
    """Only running recordings are kept across a restart; a client's finished
    one could never be downloaded again (review)."""
    import json

    from server import config

    c, app, alice, bob, _ = world
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "cfg")
    old = tmp_path / "cfg" / "recordings" / "rec_0123456789ab"
    old.mkdir(parents=True)
    (old / "manifest.json").write_text(json.dumps({"principal": "client:alice"}))
    (old / "events.jsonl").write_text("{}\n")
    legacy = tmp_path / "cfg" / "recordings" / "rec_ba9876543210"
    legacy.mkdir()
    (legacy / "manifest.json").write_text("{}")
    assert c.get("/api/v1/recordings/rec_0123456789ab/files",
                 headers=_h(alice)).status_code == 200
    assert c.get("/api/v1/recordings/rec_0123456789ab/files/events.jsonl",
                 headers=_h(alice)).content == b"{}\n"
    assert c.get("/api/v1/recordings/rec_0123456789ab/files", headers=_h(bob)).status_code == 404
    assert c.get("/api/v1/recordings/rec_ba9876543210/files", headers=_h(alice)).status_code == 404
    assert c.get("/api/v1/recordings/rec_ba9876543210/files", headers=OWNER).status_code == 200


def test_a_file_still_growing_is_sent_as_it_was_when_opened(tmp_path):
    """A live recording grows while it is sent: past its Content-Length, the
    connection was dropped with a traceback (review)."""
    from server.api.recordings import _stat_sized

    f = tmp_path / "events.jsonl"
    f.write_bytes(b"a" * 3_000_000)
    size, chunks, _ = _stat_sized(f)
    with open(f, "ab") as more:
        more.write(b"b" * 1_000_000)
    assert size == 3_000_000 and b"".join(chunks) == b"a" * 3_000_000
