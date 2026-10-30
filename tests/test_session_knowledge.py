"""A knowledge base uploaded for one held session (#560).

From another machine `load_landmarks(path=...)` names nothing, and loading was
the owner's because sets are server-wide. Now a client uploads the project's
`.quern/knowledge` and loads it for its own held session: for that app the
session sees only its own screens, everyone else sees what they saw, and the
set ends with the session.
"""

from __future__ import annotations

import io
import tarfile
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from server import config
from server.api import landmarks as api
from server.knowledge.landmarks import LandmarkRegistry, ScreenLandmarks, scan_knowledge_base
from server.knowledge.session_landmarks import SessionLandmarks
from server.models import LoadLandmarksRequest
from server.uploads import UploadError, UploadStore

APP = "com.example.app"
OTHER = "com.example.other"


def _screen(name: str, label: str) -> str:
    return (f'---\nscreen: "{name}"\nlandmarks:\n  - {{ element: "Button", '
            f'label: "{label}" }}\n---\n\nbody\n')


def _tarball(files: dict[str, str | bytes], *, links: dict[str, str] | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            data = content.encode() if isinstance(content, str) else content
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for name, target in (links or {}).items():
            info = tarfile.TarInfo(name)
            info.type, info.linkname = tarfile.SYMTYPE, target
            tar.addfile(info)
    return buf.getvalue()


async def _chunks(data: bytes):
    yield data


def _store(tmp_path, **kw) -> UploadStore:
    return UploadStore(tmp_path / "uploads", 64 * 1024 * 1024, **kw)


async def _upload(store, data: bytes, principal="client:a", name="app.knowledge.tar.gz"):
    return await store.receive(principal, name, _chunks(data), len(data))


KB = {"app.md": "# App\n", "screens/home.md": _screen("home", "Feed"),
      "screens/settings.md": _screen("settings", "Log out")}


# ---------------------------------------------------------------------------
# The upload
# ---------------------------------------------------------------------------


async def test_a_knowledge_upload_unpacks_to_a_directory(tmp_path):
    up = await _upload(_store(tmp_path), _tarball(KB))
    assert up.kind == "knowledge"
    assert (up.path / "screens" / "home.md").is_file()
    assert [s.screen for s in scan_knowledge_base(up.path).screens] == ["home", "settings"]


async def test_files_the_loader_does_not_read_are_kept_not_refused(tmp_path):
    """Screenshots are likely later: the server ignores what it does not read."""
    up = await _upload(_store(tmp_path), _tarball({**KB, "screens/home.png": b"\x89PNG"}))
    assert (up.path / "screens" / "home.png").is_file()


@pytest.mark.parametrize("target", ["home.md", "/etc/passwd"])
async def test_a_link_is_refused(tmp_path, target):
    """Even one that stays inside: a knowledge base has no use for links, so
    none is followed (the safety check alone would allow an inside one)."""
    with pytest.raises(UploadError, match="link"):
        await _upload(_store(tmp_path), _tarball(KB, links={"screens/x.md": target}))


@pytest.mark.parametrize("name", ["../escape.md", "/abs.md"])
async def test_a_name_outside_the_directory_is_refused(tmp_path, name):
    with pytest.raises(UploadError):
        await _upload(_store(tmp_path), _tarball({**KB, name: "x"}))


async def test_too_many_files_is_413(tmp_path):
    files = {f"screens/s{i}.md": _screen(f"s{i}", "x") for i in range(6)}
    with pytest.raises(UploadError) as e:
        await _upload(_store(tmp_path, knowledge_max_files=5), _tarball(files))
    assert e.value.status == 413 and "knowledge_max_files" in str(e.value)


async def test_too_many_bytes_is_413(tmp_path):
    with pytest.raises(UploadError) as e:
        await _upload(_store(tmp_path, knowledge_max_bytes=1000),
                      _tarball({**KB, "screens/big.md": "x" * 2000}))
    assert e.value.status == 413 and "knowledge_max_mb" in str(e.value)


async def test_a_refused_upload_leaves_nothing_behind(tmp_path):
    store = _store(tmp_path, knowledge_max_files=1)
    with pytest.raises(UploadError):
        await _upload(store, _tarball(KB))
    assert store.list("client:a") == []
    assert not any((tmp_path / "uploads").iterdir())


@pytest.mark.parametrize("uploads, mb, files", [
    ({}, 5, 2000),
    ({"knowledge_max_mb": 20, "knowledge_max_files": 9000}, 20, 9000),
    ({"knowledge_max_mb": "big", "knowledge_max_files": 0}, 5, 2000),   # defaults, with a note
])
def test_the_limits_are_settings(monkeypatch, uploads, mb, files):
    monkeypatch.setattr(config, "read_user_config", lambda **kw: {"uploads": uploads})
    got = config.upload_settings()
    assert got["knowledge_max_bytes"] == mb * 1024 * 1024
    assert got["knowledge_max_files"] == files


# ---------------------------------------------------------------------------
# The session's view
# ---------------------------------------------------------------------------


def _screens(*names):
    return [ScreenLandmarks(screen=n, landmarks=[]) for n in names]


@pytest.fixture
def as_session(monkeypatch):
    from server.knowledge import session_landmarks as mod

    def set_(session, principal="client:a"):
        caller = SimpleNamespace(session=session, principal=principal,
                                 is_owner=principal == "owner")
        monkeypatch.setattr(mod, "current_caller", lambda: caller)
        monkeypatch.setattr(api.visibility, "current_caller", lambda: caller)
        return caller
    return set_


def _proxy():
    shared = LandmarkRegistry()
    shared.load(APP, _screens("server-home", "server-gone"))
    shared.load(OTHER, _screens("other-home"))
    return SessionLandmarks(shared)


def test_an_uploaded_app_replaces_the_server_set_for_that_session_only(as_session):
    lm = _proxy()
    lm.own("s1").load(APP, _screens("home"))

    as_session("s1")
    assert [s.screen for s in lm.all_screens(APP)] == ["home"]          # replaced, not merged
    assert [s.screen for s in lm.all_screens(OTHER)] == ["other-home"]   # others fall back
    as_session("s2")
    assert [s.screen for s in lm.all_screens(APP)] == ["server-home", "server-gone"]
    as_session("")
    assert lm.list_sets() == {APP: 2, OTHER: 1}


def test_the_servers_web_content_does_not_show_through_an_uploaded_app(as_session):
    """An inline load records no web content; the server's for that app must
    not appear in the session's view (review)."""
    lm = _proxy()
    lm.shared._web_content[APP] = ["server hint"]
    lm.own("s1").load(APP, _screens("home"))
    as_session("s1")
    assert lm.web_content(APP) == []


def test_writes_through_the_proxy_are_the_servers(as_session):
    lm = _proxy()
    lm.own("s1").load(APP, _screens("home"))
    as_session("s1")
    lm.load("com.new", _screens("x"))
    assert "com.new" in lm.shared.list_sets()
    assert "com.new" not in lm.own("s1").list_sets()


def test_a_method_handed_out_once_answers_for_whoever_calls(as_session):
    """The controller is given `all_screens`-style lookups at start-up; they
    must see the calling session, not the one present at start-up."""
    lm = _proxy()
    lm.own("s1").load(APP, _screens("home"))
    as_session("")
    bound = lm.all_screens
    assert bound.__self__ is lm
    as_session("s1")
    assert [s.screen for s in bound(APP)] == ["home"]


def test_dropping_a_session_forgets_its_sets(as_session):
    lm = _proxy()
    lm.own("s1").load(APP, _screens("home"))
    assert lm.drop("s1") == [APP]
    as_session("s1")
    assert [s.screen for s in lm.all_screens(APP)] == ["server-home", "server-gone"]
    assert lm.drop("s1") == []


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------


def _request(tmp_path, lm, store, *, held=("s1", "client:a")):
    req = MagicMock()
    req.app.state.landmark_registry = lm
    req.app.state.uploads = store
    sessions = {held[0]: SimpleNamespace(id=held[0], principal=held[1])} if held else {}
    req.app.state.sessions = SimpleNamespace(get=sessions.get)
    return req


async def _load(req, **body):
    return await api.load_landmarks.__wrapped__(req, LoadLandmarksRequest(**body))


async def test_an_upload_loads_for_the_session_and_is_discarded(tmp_path, as_session):
    as_session("s1")
    lm, store = _proxy(), _store(tmp_path)
    up = await _upload(store, _tarball(KB))
    out = await _load(_request(tmp_path, lm, store), upload_id=up.id, app=APP)
    assert out["scope"] == "session" and out["screens"] == 2
    assert lm.own_apps("s1") == [APP]
    assert lm.shared.list_sets()[APP] == 2 and \
        [s.screen for s in lm.shared.all_screens(APP)] == ["server-home", "server-gone"]
    with pytest.raises(UploadError):
        store.get(up.id, "client:a")                       # used up


async def test_a_wrapped_directory_is_found(tmp_path, as_session):
    as_session("s1")
    lm, store = _proxy(), _store(tmp_path)
    up = await _upload(store, _tarball({f"knowledge/{k}": v for k, v in KB.items()}))
    out = await _load(_request(tmp_path, lm, store), upload_id=up.id, app=APP)
    assert out["screens"] == 2


async def test_without_a_held_session_it_is_refused(tmp_path, as_session):
    """A set for a session nobody holds would never end."""
    as_session("s1")
    lm, store = _proxy(), _store(tmp_path)
    up = await _upload(store, _tarball(KB))
    with pytest.raises(HTTPException) as e:
        await _load(_request(tmp_path, lm, store, held=None), upload_id=up.id, app=APP)
    assert e.value.status_code == 409 and lm.own_apps("s1") == []


async def test_a_session_held_by_another_key_is_refused(tmp_path, as_session):
    as_session("s1", principal="client:a")
    lm, store = _proxy(), _store(tmp_path)
    up = await _upload(store, _tarball(KB))
    with pytest.raises(HTTPException) as e:
        await _load(_request(tmp_path, lm, store, held=("s1", "client:b")),
                    upload_id=up.id, app=APP)
    assert e.value.status_code == 409


async def test_another_keys_upload_reads_as_missing(tmp_path, as_session):
    as_session("s1", principal="client:a")
    lm, store = _proxy(), _store(tmp_path)
    up = await _upload(store, _tarball(KB), principal="client:b")
    with pytest.raises(HTTPException) as e:
        await _load(_request(tmp_path, lm, store), upload_id=up.id, app=APP)
    assert e.value.status_code == 404


async def test_an_app_upload_is_not_a_knowledge_base(tmp_path, as_session):
    as_session("s1")
    lm, store = _proxy(), _store(tmp_path)
    up = await store.receive("client:a", "My.ipa", _chunks(b"ipa bytes"), 9)
    with pytest.raises(HTTPException) as e:
        await _load(_request(tmp_path, lm, store), upload_id=up.id, app=APP)
    assert e.value.status_code == 400 and "knowledge.tar.gz" in e.value.detail


async def test_an_empty_upload_does_not_replace_what_the_session_has(tmp_path, as_session):
    as_session("s1")
    lm, store = _proxy(), _store(tmp_path)
    lm.own("s1").load(APP, _screens("home"))
    up = await _upload(store, _tarball({"app.md": "# x", "screens/_template.md": "no"}))
    with pytest.raises(HTTPException) as e:
        await _load(_request(tmp_path, lm, store), upload_id=up.id, app=APP)
    assert e.value.status_code == 400
    assert [s.screen for s in lm.own("s1").all_screens(APP)] == ["home"]


@pytest.mark.parametrize("extra", [{"source": "/x"}, {"remember": True},
                                   {"landmarks": {"a": []}}])
async def test_upload_id_is_used_alone(tmp_path, as_session, extra):
    as_session("s1")
    with pytest.raises(HTTPException) as e:
        await _load(_request(tmp_path, _proxy(), _store(tmp_path)), upload_id="up_x",
                    app=APP, **extra)
    assert e.value.status_code == 400


async def test_app_is_required(tmp_path, as_session):
    as_session("s1")
    with pytest.raises(HTTPException) as e:
        await _load(_request(tmp_path, _proxy(), _store(tmp_path)), upload_id="up_x")
    assert e.value.status_code == 400


# -- unloading -----------------------------------------------------------------


async def _unload(req, app=None, forget=False):
    return await api.unload_landmarks.__wrapped__(req, app=app, forget=forget)


async def test_a_client_unloads_its_own_and_only_its_own(tmp_path, as_session):
    as_session("s1")
    lm = _proxy()
    lm.own("s1").load(APP, _screens("home"))
    out = await _unload(_request(tmp_path, lm, _store(tmp_path)), app=APP)
    assert out == {"unloaded": APP, "scope": "session"}
    assert lm.own_apps("s1") == [] and APP in lm.shared.list_sets()
    with pytest.raises(HTTPException) as e:                # nothing of its own left
        await _unload(_request(tmp_path, lm, _store(tmp_path)), app=APP)
    assert e.value.status_code == 404 and APP in lm.shared.list_sets()


async def test_forget_does_not_apply_to_a_session_set(tmp_path, as_session):
    as_session("s1")
    lm = _proxy()
    lm.own("s1").load(APP, _screens("home"))
    with pytest.raises(HTTPException) as e:
        await _unload(_request(tmp_path, lm, _store(tmp_path)), app=APP, forget=True)
    assert e.value.status_code == 400 and lm.own_apps("s1") == [APP]


async def test_the_owner_still_unloads_the_server_sets(tmp_path, as_session):
    as_session("", principal="owner")
    lm = _proxy()
    out = await _unload(_request(tmp_path, lm, _store(tmp_path)), app=OTHER)
    assert out == {"unloaded": OTHER} and OTHER not in lm.shared.list_sets()


# -- the session ending ------------------------------------------------------------


async def test_the_set_ends_with_the_session():
    from server.api import sessions as sessions_api

    lm = _proxy()
    lm.own("client:a/s1").load(APP, _screens("home"))
    app = SimpleNamespace(state=SimpleNamespace(
        landmark_registry=lm, proxy_adapter=None, recordings=None, mock_owners={}))
    session = SimpleNamespace(id="client:a/s1", principal="client:a", reservations=[])
    actions = await sessions_api._end_client_work(app, SimpleNamespace(holder_of=lambda u: None),
                                                  session)
    assert {"action": "knowledge_dropped", "ok": True, "apps": [APP]} in actions
    assert lm.own_apps("client:a/s1") == []
