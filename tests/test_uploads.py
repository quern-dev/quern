"""Apps uploaded to install (#254, phase 3b).

The store as a unit -- kinds, the cap, a .app's archive unpacked safely, the
sweep -- then the routes through the real app with a client key: upload,
install by id, and what one key may not do with another's, or with a path.
"""

from __future__ import annotations

import asyncio
import io
import os
import tarfile

import pytest
from fastapi.testclient import TestClient

from server import device_access
from server.session_registry import SessionRegistry
from server.uploads import TTL_SECONDS, UploadError, UploadStore, kind_of

KEY = "k" * 32
SIM = "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
OTHER_SIM = "11111111-2222-3333-4444-555555555555"
FREE_SIM = "99999999-8888-7777-6666-555555555555"


async def _chunks(*parts: bytes):
    for p in parts:
        yield p


def _receive(store, principal, name, *parts, declared=None):
    return asyncio.run(store.receive(principal, name, _chunks(*parts), declared))


def _tar(members: list[tuple[str, str, bytes | str]]) -> bytes:
    """A gzipped tar: (name, "file"|"dir"|"symlink", data or link target)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, kind, data in members:
            info = tarfile.TarInfo(name)
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = data
                tar.addfile(info)
            else:
                info.size = len(data)
                info.mode = 0o755
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


BUNDLE = [
    ("My.app", "dir", b""),
    ("My.app/My", "file", b"\xcf\xfa\xed\xfe binary"),
    ("My.app/Info.plist", "file", b"<plist/>"),
    ("My.app/Frameworks", "dir", b""),
    ("My.app/Frameworks/K.framework", "dir", b""),
    ("My.app/Frameworks/K.framework/Versions", "dir", b""),
    ("My.app/Frameworks/K.framework/Versions/A", "dir", b""),
    ("My.app/Frameworks/K.framework/Versions/A/K", "file", b"lib"),
    # A bundle's frameworks are symlinks; a zip would have lost them.
    ("My.app/Frameworks/K.framework/Versions/Current", "symlink", "A"),
    ("My.app/Frameworks/K.framework/K", "symlink", "Versions/Current/K"),
]


@pytest.fixture
def store(tmp_path):
    return UploadStore(tmp_path / "uploads", max_bytes=1024 * 1024)


class TestKinds:
    def test_the_three_kinds(self):
        assert kind_of("My.ipa") == "ipa"
        assert kind_of("My.apk") == "apk"
        assert kind_of("My.app.tar.gz") == "app"

    @pytest.mark.parametrize("name", ["", "../x.ipa", "a/b.ipa", ".ipa", ".hidden.ipa",
                                      "My.app", "My.zip", "My.app.zip"])
    def test_anything_else_is_refused(self, name):
        with pytest.raises(UploadError):
            kind_of(name)


class TestTheStore:
    def test_an_ipa_is_kept_for_the_key_that_sent_it(self, store):
        up = _receive(store, "client:alice", "My.ipa", b"PK\x03\x04", b"rest")
        assert up.path.read_bytes() == b"PK\x03\x04rest" and up.size == 8
        assert store.get(up.id, "client:alice") is up
        with pytest.raises(UploadError) as e:
            store.get(up.id, "client:bob")
        assert e.value.status == 404, "another key's reads as missing"
        with pytest.raises(UploadError):
            store.get(up.id, "owner")
        store.discard(up.id)
        assert not up.path.exists()

    def test_a_declared_size_past_the_cap_is_refused_before_reading(self, store):
        with pytest.raises(UploadError) as e:
            _receive(store, "client:alice", "My.ipa", b"x", declared=2 * 1024 * 1024)
        assert e.value.status == 413
        assert not any(store.root.iterdir()) if store.root.exists() else True

    def test_a_stream_past_the_cap_is_cut_off_and_nothing_left(self, store):
        """Content-Length can lie, or be absent: the count is what decides."""
        with pytest.raises(UploadError) as e:
            _receive(store, "client:alice", "My.ipa", b"x" * 700_000, b"x" * 700_000)
        assert e.value.status == 413
        assert list(store.root.iterdir()) == []
        assert store.list("client:alice") == []

    def test_the_cap_is_what_a_key_holds_at_once(self, store):
        _receive(store, "client:alice", "A.ipa", b"x" * 600_000)
        with pytest.raises(UploadError) as e:
            _receive(store, "client:alice", "B.ipa", b"x" * 600_000)
        assert e.value.status == 413
        assert _receive(store, "client:bob", "B.ipa", b"x" * 600_000), "bob's is his own"

    def test_an_empty_upload_is_refused(self, store):
        with pytest.raises(UploadError):
            _receive(store, "client:alice", "My.apk")

    def test_a_bundle_is_unpacked_with_its_symlinks(self, store):
        up = _receive(store, "client:alice", "My.app.tar.gz", _tar(BUNDLE))
        assert up.kind == "app" and up.path.name == "My.app" and up.path.is_dir()
        current = up.path / "Frameworks/K.framework/Versions/Current"
        assert current.is_symlink() and os.readlink(current) == "A"
        assert (up.path / "Frameworks/K.framework/K").read_bytes() == b"lib"
        assert not (up.path.parent.parent / "My.app.tar.gz").exists(), "the archive is gone"

    @pytest.mark.parametrize("members, why", [
        ([("My.app", "dir", b""), ("My.app/../../escape", "file", b"x")], "parent"),
        ([("My.app", "dir", b""), ("/etc/evil", "file", b"x")], "absolute"),
        ([("My.app", "dir", b""), ("My.app/link", "symlink", "/etc/passwd")], "abs link"),
        ([("My.app", "dir", b""), ("My.app/link", "symlink", "../../../../etc")], "out link"),
        ([("My.app", "dir", b""), ("Other.app", "dir", b"")], "two bundles"),
        ([("Payload", "dir", b""), ("Payload/x", "file", b"x")], "not a .app"),
    ])
    def test_an_archive_that_would_leave_its_directory_is_refused(self, store, tmp_path,
                                                                  members, why):
        with pytest.raises(UploadError):
            _receive(store, "client:alice", "My.app.tar.gz", _tar(members))
        assert list(store.root.iterdir()) == [], why
        assert not (tmp_path / "escape").exists()

    def test_an_archive_that_unpacks_past_the_cap_is_refused(self, store):
        """Zeros compress to nothing: the sizes inside are what count."""
        big = [("My.app", "dir", b""), ("My.app/zeros", "file", b"\0" * 3 * 1024 * 1024)]
        data = _tar(big)
        assert len(data) < store.max_bytes, "small on the wire"
        with pytest.raises(UploadError) as e:
            _receive(store, "client:alice", "My.app.tar.gz", data)
        assert e.value.status == 413

    def test_a_corrupt_archive_is_refused(self, store):
        with pytest.raises(UploadError):
            _receive(store, "client:alice", "My.app.tar.gz", b"\x1f\x8b not really")

    def test_the_sweep_takes_the_expired_and_a_revoked_keys(self, tmp_path):
        now = [1000.0]
        store = UploadStore(tmp_path / "u", max_bytes=1 << 20, clock=lambda: now[0])
        old = _receive(store, "client:alice", "Old.ipa", b"x")
        now[0] += TTL_SECONDS - 1
        fresh = _receive(store, "client:alice", "New.ipa", b"x")
        bobs = _receive(store, "client:bob", "B.ipa", b"x")
        owners = _receive(store, "owner", "O.ipa", b"x")
        now[0] += 1
        gone = store.sweep(lambda who: who != "client:bob")
        assert sorted(gone) == sorted([old.id, bobs.id])
        assert {u.id for u in store.list("client:alice")} == {fresh.id}
        assert store.list("owner")[0].id == owners.id
        assert not old.path.exists() and not bobs.path.exists()

    def test_a_restart_starts_empty(self, store):
        up = _receive(store, "client:alice", "My.ipa", b"x")
        store.reset()
        assert not up.path.exists() and store.list("client:alice") == []


# ---------------------------------------------------------------------------
# Through the real app
# ---------------------------------------------------------------------------


class FakeController:
    def __init__(self, fail=False):
        self.installed: list[tuple[str, str | None, bytes | None]] = []
        self.fail = fail

    async def install_app(self, app_path, udid=None):
        from pathlib import Path

        from server.models import DeviceError

        p = Path(app_path)
        self.installed.append((app_path, udid, p.read_bytes() if p.is_file() else None))
        if self.fail:
            raise DeviceError(f"install failed: {app_path} is not an app"
                              if getattr(self, "message_with_path", False) else "install failed",
                              tool="simctl")
        return udid


@pytest.fixture
def world(tmp_path):
    import server.main as m
    from server.config import ServerConfig
    from server.uploads import UploadStore

    app = m.create_app(ServerConfig(api_key=KEY))
    app.state.clients.path = tmp_path / "clients.json"
    app.state.clients.seen_path = tmp_path / "clients-seen.json"
    app.state.uploads = UploadStore(tmp_path / "uploads", max_bytes=1 << 20)
    registry = SessionRegistry(grace_seconds=180)
    app.state.sessions = registry
    device_access.install(registry)
    app.state.device_controller = FakeController()
    alice, bob = app.state.clients.add("alice"), app.state.clients.add("bob")
    for label, udid in (("alice", SIM), ("bob", OTHER_SIM)):
        sid = f"{label}:s1"
        registry.open(sid, principal=f"client:{label}")
        registry.get(sid).state = "live"
        registry.reserve(sid, [udid], on_release="none", booted=[])
    try:
        yield TestClient(app), app, alice, bob
    finally:
        device_access.install(None)


def _h(key):
    return {"Authorization": f"Bearer {key}", "X-Quern-Session": "s1"}


def _upload(c, key, name, body):
    return c.post("/api/v1/uploads", params={"name": name}, content=body,
                  headers={**_h(key), "Content-Type": "application/octet-stream"})


class TestTheRoutes:
    def test_upload_then_install_by_id_and_it_is_gone(self, world):
        c, app, alice, _ = world
        r = _upload(c, alice, "My.ipa", b"IPA-BYTES")
        assert r.status_code == 200, r.text
        up = r.json()
        assert up["kind"] == "ipa" and up["bytes"] == 9 and up["expires_in"] > 3500
        r = c.post("/api/v1/device/app/install", json={"upload_id": up["upload_id"], "udid": SIM},
                   headers=_h(alice))
        assert r.status_code == 200, r.text
        assert r.json() == {"status": "installed", "udid": SIM, "upload": "My.ipa"}
        path, udid, data = app.state.device_controller.installed[-1]
        assert (udid, data) == (SIM, b"IPA-BYTES")
        assert c.get("/api/v1/uploads", headers=_h(alice)).json()["uploads"] == []

    def test_a_failed_install_keeps_the_upload_to_try_again(self, world):
        c, app, alice, _ = world
        app.state.device_controller.fail = True
        up = _upload(c, alice, "My.ipa", b"x").json()
        r = c.post("/api/v1/device/app/install", json={"upload_id": up["upload_id"], "udid": SIM},
                   headers=_h(alice))
        assert r.status_code >= 400
        assert [u["upload_id"] for u in
                c.get("/api/v1/uploads", headers=_h(alice)).json()["uploads"]] == [up["upload_id"]]

    def test_another_keys_upload_cannot_be_installed_listed_or_deleted(self, world):
        c, app, alice, bob = world
        up = _upload(c, alice, "My.ipa", b"x").json()["upload_id"]
        r = c.post("/api/v1/device/app/install", json={"upload_id": up, "udid": OTHER_SIM},
                   headers=_h(bob))
        assert r.status_code == 404
        assert app.state.device_controller.installed == []
        assert c.get("/api/v1/uploads", headers=_h(bob)).json()["uploads"] == []
        assert c.delete(f"/api/v1/uploads/{up}", headers=_h(bob)).status_code == 404
        assert c.delete(f"/api/v1/uploads/{up}", headers=_h(alice)).status_code == 200

    def test_a_path_is_owner_only(self, world):
        """It was not: a client key made the server install from any path on
        the Mac (found mapping 3b)."""
        c, app, alice, _ = world
        r = c.post("/api/v1/device/app/install", json={"app_path": "/etc", "udid": SIM},
                   headers=_h(alice))
        assert r.status_code == 403 and "app_path" in r.json()["detail"]
        assert app.state.device_controller.installed == []
        r = c.post("/api/v1/device/app/install", json={"app_path": "/tmp/My.app",
                                                        "udid": FREE_SIM},
                   headers={"Authorization": f"Bearer {KEY}"})
        assert r.status_code == 200, r.text
        assert app.state.device_controller.installed[-1][:2] == ("/tmp/My.app", FREE_SIM)

    def test_install_takes_one_source(self, world):
        c, *_ = world
        owner = {"Authorization": f"Bearer {KEY}"}
        for body in ({"udid": FREE_SIM},
                     {"app_path": "/x.app", "upload_id": "up_1", "udid": FREE_SIM}):
            assert c.post("/api/v1/device/app/install", json=body, headers=owner).status_code == 422

    def test_the_body_is_not_read_for_device_ids(self, world):
        """The access guard reads a write's JSON for devices it names; an
        upload's body is an app. One that happens to parse as naming another
        key's device must not be refused -- or read into memory to find out."""
        c, _, alice, _ = world
        r = _upload(c, alice, "My.ipa", f'{{"udid": "{OTHER_SIM}"}}'.encode())
        assert r.status_code == 200, r.text

    def test_past_the_cap_is_413(self, world):
        c, _, alice, _ = world
        r = _upload(c, alice, "Big.ipa", b"x" * ((1 << 20) + 1))
        assert r.status_code == 413 and "cap" in r.json()["detail"]

    def test_a_bundle_through_the_route(self, world):
        c, app, alice, _ = world
        up = _upload(c, alice, "My.app.tar.gz", _tar(BUNDLE)).json()
        assert up["kind"] == "app", up
        r = c.post("/api/v1/device/app/install", json={"upload_id": up["upload_id"], "udid": SIM},
                   headers=_h(alice))
        assert r.status_code == 200, r.text
        assert app.state.device_controller.installed[-1][0].endswith("/My.app")


# ---------------------------------------------------------------------------
# Review: the cap held against compression and concurrency; tar by name
# ---------------------------------------------------------------------------


class TestTheCapHolds:
    def test_a_bundle_counts_what_it_unpacks_to(self, store):
        """A small archive of zeros counted as small, and five of them put
        many times the cap on disk (review, reproduced)."""
        zeros = [("My.app", "dir", b""), ("My.app/zeros", "file", b"\0" * 600_000)]
        up = _receive(store, "client:alice", "My.app.tar.gz", _tar(zeros))
        assert up.size >= 600_000, "held is the unpacked size, not the archive's"
        with pytest.raises(UploadError) as e:
            _receive(store, "client:alice", "Two.app.tar.gz", _tar(zeros))
        assert e.value.status == 413

    def test_uploads_side_by_side_cannot_both_take_the_room(self, store):
        """Each took the room free at its start, and four together held
        several times the cap (review, reproduced)."""
        async def slow(n):
            for _ in range(n):
                await asyncio.sleep(0)
                yield b"x" * 100_000

        async def both():
            return await asyncio.gather(
                store.receive("client:alice", "A.ipa", slow(7)),
                store.receive("client:alice", "B.ipa", slow(7)),
                return_exceptions=True)

        results = asyncio.run(both())
        refused = [r for r in results if isinstance(r, UploadError)]
        assert len(refused) == 1 and refused[0].status == 413
        assert store.held("client:alice") <= store.max_bytes
        assert store._inflight == {}, "nothing left counted in flight"

    def test_bundles_unpacking_side_by_side_cannot_both_take_the_room(self, store):
        """Unpacking runs in a thread, and the unpacked bytes were not counted
        until it ended: two bundles each unpacked to most of the cap
        (CodeRabbit)."""
        zeros = _tar([("My.app", "dir", b""), ("My.app/zeros", "file", b"\0" * 600_000)])

        async def both():
            return await asyncio.gather(
                store.receive("client:alice", "A.app.tar.gz", _chunks(zeros)),
                store.receive("client:alice", "B.app.tar.gz", _chunks(zeros)),
                return_exceptions=True)

        results = asyncio.run(both())
        assert sum(isinstance(r, UploadError) for r in results) == 1, results
        assert store.held("client:alice") <= store.max_bytes
        assert store._inflight == {}

    def test_a_failed_upload_gives_its_room_back(self, store):
        with pytest.raises(UploadError):
            _receive(store, "client:alice", "My.app.tar.gz", b"\x1f\x8b broken")
        assert store.held("client:alice") == 0
        assert _receive(store, "client:alice", "My.ipa", b"x" * 900_000)

    def test_an_archive_with_too_many_members_is_refused(self, store, monkeypatch):
        import server.uploads as uploads

        monkeypatch.setattr(uploads, "MAX_MEMBERS", 3)
        members = [("My.app", "dir", b"")] + [(f"My.app/f{i}", "file", b"x") for i in range(5)]
        with pytest.raises(UploadError) as e:
            _receive(store, "client:alice", "My.app.tar.gz", _tar(members))
        assert e.value.status == 413

    @pytest.mark.parametrize("name", ["a\0b.ipa", "x" * 250 + ".ipa"])
    def test_names_the_disk_would_refuse_are_refused_first(self, name):
        with pytest.raises(UploadError) as e:
            kind_of(name)
        assert e.value.status == 400


def _info(name, kind="file", link=""):
    info = tarfile.TarInfo(name)
    info.type = {"file": tarfile.REGTYPE, "dir": tarfile.DIRTYPE, "sym": tarfile.SYMTYPE,
                 "hard": tarfile.LNKTYPE, "fifo": tarfile.FIFOTYPE}[kind]
    info.linkname = link
    return info


class TestByNameBeforeTarfile:
    """Old Pythons' `data` filter had escapes through symlinks; this check
    does not depend on it (review)."""

    def test_a_real_bundle_passes(self):
        from server.uploads import unsafe_member

        members = [_info("My.app", "dir"), _info("My.app/F/K.framework", "dir"),
                   _info("My.app/F/K.framework/Versions", "dir"),
                   _info("My.app/F/K.framework/Versions/A", "dir"),
                   _info("My.app/F/K.framework/Versions/A/K"),
                   _info("My.app/F/K.framework/Versions/Current", "sym", "A"),
                   _info("My.app/F/K.framework/K", "sym", "Versions/Current/K"),
                   _info("My.app/PlugIns/x", "sym", "../F/K.framework")]
        assert unsafe_member(members) is None

    @pytest.mark.parametrize("members, why", [
        ([_info("My.app", "dir"), _info("My.app/h", "hard", "My.app/x")], "hard link"),
        ([_info("My.app", "dir"), _info("My.app/p", "fifo")], "not a file"),
        ([_info("My.app", "dir"), _info("My.app/l", "sym", "/etc")], "links outside"),
        ([_info("My.app", "dir"), _info("My.app/l", "sym", "../..")], "links outside"),
        ([_info("My.app", "dir"), _info("My.app/a/l", "sym", "../../../x")], "links outside"),
        ([_info("My.app", "dir"), _info("My.app/l", "sym", "."), _info("My.app/l/f")],
         "inside a symlink"),
        ([_info("My.app", "dir"), _info("My.app/f"), _info("My.app/./f")], "twice"),
        ([_info("../x")], "not a name"),
        ([_info("/abs")], "not a name"),
    ])
    def test_anything_that_could_write_outside_is_named(self, members, why):
        from server.uploads import unsafe_member

        assert why in (unsafe_member(members) or "")


def test_the_check_by_name_runs_on_a_real_upload(store):
    """A hard link inside the bundle is one the data filter itself allows:
    refused, it shows the name check is on the upload path, not just a unit."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        d = tarfile.TarInfo("My.app")
        d.type = tarfile.DIRTYPE
        tar.addfile(d)
        f = tarfile.TarInfo("My.app/f")
        f.size = 1
        tar.addfile(f, io.BytesIO(b"x"))
        h = tarfile.TarInfo("My.app/h")
        h.type = tarfile.LNKTYPE
        h.linkname = "My.app/f"
        tar.addfile(h)
    with pytest.raises(UploadError) as e:
        _receive(store, "client:alice", "My.app.tar.gz", buf.getvalue())
    assert "hard link" in str(e.value)


class TestInstallingKeepsTheUpload:
    def test_neither_the_sweep_nor_a_delete_takes_one_being_installed(self, world):
        c, app, alice, _ = world
        up = _upload(c, alice, "My.ipa", b"x").json()["upload_id"]
        store = app.state.uploads
        with store.installing(up):
            assert store.sweep(lambda who: False) == []
            r = c.delete(f"/api/v1/uploads/{up}", headers=_h(alice))
            assert r.status_code == 409
        assert store.sweep(lambda who: False) == [up]

    def test_a_failed_install_names_the_upload_not_the_servers_path(self, world):
        c, app, alice, _ = world
        app.state.device_controller.fail = True
        app.state.device_controller.message_with_path = True
        up = _upload(c, alice, "My.ipa", b"x").json()["upload_id"]
        r = c.post("/api/v1/device/app/install", json={"upload_id": up, "udid": SIM},
                   headers=_h(alice))
        assert r.status_code >= 400
        assert str(app.state.uploads.root) not in r.text and "My.ipa" in r.text


class TestCleanupWaitsForTheDisk:
    def test_a_cancelled_upload_cleans_up_only_once_unpacking_has_stopped(self, store, monkeypatch):
        """Cancelled mid-unpack (the client went away), the directory was
        removed and the room freed while the worker still wrote (CodeRabbit)."""
        import threading
        import time

        import server.uploads as uploads

        started = threading.Event()

        def slow_unpack(archive, dest, room):
            started.set()
            time.sleep(0.3)
            dest.mkdir(parents=True, exist_ok=True)     # still writing after the cancel
            (dest / "late").write_bytes(b"x")
            return dest, 1

        monkeypatch.setattr(uploads, "_unpack_app", slow_unpack)
        zeros = _tar([("My.app", "dir", b""), ("My.app/f", "file", b"x")])

        async def go():
            task = asyncio.create_task(
                store.receive("client:alice", "My.app.tar.gz", _chunks(zeros)))
            deadline = time.monotonic() + 5
            while not started.is_set():
                # Bounded, and failing with the upload's own error if it ended
                # before unpacking -- not a hang (CodeRabbit).
                assert not task.done(), f"ended before unpacking: {task.exception()!r}"
                assert time.monotonic() < deadline, "never reached unpacking"
                await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(go())
        assert list(store.root.iterdir()) == [], "nothing written after the cleanup"
        assert store._inflight == {} and store.held("client:alice") == 0

    def test_swept_bytes_stay_counted_until_their_files_are_gone(self, tmp_path, monkeypatch):
        import server.uploads as uploads

        now = [1000.0]
        store = UploadStore(tmp_path / "u", max_bytes=1 << 20, clock=lambda: now[0])
        up = _receive(store, "client:alice", "Old.ipa", b"x" * 900_000)
        now[0] += TTL_SECONDS
        real = uploads.shutil.rmtree
        monkeypatch.setattr(uploads.shutil, "rmtree", lambda *a, **k: None)   # deleting fails
        assert store.sweep(lambda who: True) == []
        assert store.held("client:alice") == 900_000, "still on disk, still counted"
        with pytest.raises(UploadError):
            _receive(store, "client:alice", "New.ipa", b"x" * 200_000)
        monkeypatch.setattr(uploads.shutil, "rmtree", real)
        assert store.sweep(lambda who: True) == [up.id], "tried again"
        assert store.held("client:alice") == 0
