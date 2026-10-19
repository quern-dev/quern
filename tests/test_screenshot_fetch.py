"""Screenshots the server saved, fetched by the session that caused them
(#254, phase 3b): an answer's screenshot path names nothing on another
machine, so the file is served -- to that session, or the owner, and for
nothing that was not recorded.
"""

from __future__ import annotations

import asyncio
import contextvars

import pytest
from fastapi.testclient import TestClient

from server import device_access, principal, screenshots
from server.session import _SESSION
from server.session_registry import SessionRegistry

KEY = "k" * 32


class Shooter:
    def __init__(self, fail=False):
        self.fail = fail

    async def screenshot(self, udid, scale):
        if self.fail:
            raise RuntimeError("no screen")
        return b"\x89PNG" + udid.encode(), "image/png"


def _as(who: str, session: str, coro_fn):
    """Run `coro_fn()` as `who` in `session`, as a request's context would."""
    def run():
        principal.set_principal(who)
        _SESSION.set(session)
        return asyncio.run(coro_fn())
    return contextvars.copy_context().run(run)


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(screenshots, "ROOT", tmp_path / "quern")
    monkeypatch.setattr(screenshots, "DIR", tmp_path / "quern" / "shots")
    monkeypatch.setattr(screenshots, "_recorded", type(screenshots._recorded)())


def test_each_capture_is_its_own_file_and_recorded_to_its_caller():
    paths = [_as("client:alice", "alice:s1",
                 lambda: screenshots.capture(Shooter(), "SIM", "tap_before")) for _ in range(3)]
    assert len(set(paths)) == 3, "the same second and label wrote one file"
    assert all(screenshots.owner_of(p) == ("client:alice", "alice:s1") for p in paths)


def test_a_failed_capture_is_none_and_records_nothing():
    assert _as("owner", "x", lambda: screenshots.capture(Shooter(fail=True), "SIM", "l")) is None
    assert not screenshots._recorded


def test_the_record_is_bounded(monkeypatch):
    monkeypatch.setattr(screenshots, "MAX_RECORDED", 3)
    for i in range(5):
        screenshots.record(f"/tmp/quern/screenshots/{i}.png")
    assert list(screenshots._recorded) == [f"/tmp/quern/screenshots/{i}.png" for i in (2, 3, 4)]


@pytest.fixture
def world(tmp_path):
    import server.main as m
    from server.config import ServerConfig

    app = m.create_app(ServerConfig(api_key=KEY))
    app.state.clients.path = tmp_path / "clients.json"
    app.state.clients.seen_path = tmp_path / "clients-seen.json"
    registry = SessionRegistry(grace_seconds=180)
    app.state.sessions = registry
    device_access.install(registry)
    alice, bob = app.state.clients.add("alice"), app.state.clients.add("bob")
    shot = _as("client:alice", "alice:s1", lambda: screenshots.capture(Shooter(), "SIM", "tap"))
    try:
        yield TestClient(app), alice, bob, shot
    finally:
        device_access.install(None)


def _get(c, key, path, session="s1"):
    headers = {"Authorization": f"Bearer {key}"}
    if session:
        headers["X-Quern-Session"] = session
    return c.get("/api/v1/screenshots", params={"path": path}, headers=headers)


def test_the_session_that_caused_it_gets_it(world):
    c, alice, _, shot = world
    r = _get(c, alice, shot)
    assert r.status_code == 200 and r.content == b"\x89PNGSIM"
    assert r.headers["content-type"] == "image/png"


def test_another_session_or_key_gets_404(world):
    c, alice, bob, shot = world
    assert _get(c, alice, shot, session="s2").status_code == 404, "another session of alice's"
    assert _get(c, bob, shot).status_code == 404


def test_the_owner_gets_any(world):
    c, _, _, shot = world
    assert _get(c, KEY, shot, session=None).status_code == 200


def test_a_path_never_recorded_is_404_even_for_the_owner(world, tmp_path):
    """The route serves what it recorded, never a path it is handed."""
    c, *_ = world
    secret = tmp_path / "api-key"
    secret.write_text("secret")
    for path in (str(secret), "/etc/passwd"):
        r = _get(c, KEY, path, session=None)
        assert r.status_code == 404 and b"secret" not in r.content


def test_a_recorded_file_since_deleted_says_so(world):
    from pathlib import Path

    c, alice, _, shot = world
    Path(shot).unlink()
    r = _get(c, alice, shot)
    assert r.status_code == 404 and "gone" in r.json()["detail"]


def test_the_timelines_shots_are_the_owners(tmp_path, monkeypatch):
    """It films its own device whatever the request acted on: a client's tap
    on its own device must not credit it with this one's screen (review)."""
    import server.screenshot_timeline as tl

    monkeypatch.setattr(tl, "_TIMELINE_BASE", tmp_path / "quern" / "timeline")
    timeline = tl.ScreenshotTimeline(udid="OWNERS-SIM")
    entry = _as("client:alice", "alice:s1",
                lambda: timeline.capture(Shooter(), "tap", "OWNERS-SIM"))
    assert screenshots.owner_of(entry.screenshot) == ("owner", "")


def test_a_screenshot_swapped_for_a_symlink_is_not_served(world, tmp_path):
    """/tmp is shared: a recorded png replaced by a link to the API key must
    not hand the key out (review)."""
    import os

    c, alice, _, shot = world
    secret = tmp_path / "api-key"
    secret.write_text("the api key")
    os.unlink(shot)
    os.symlink(secret, shot)
    for key, session in ((alice, "s1"), (KEY, None)):
        r = _get(c, key, shot, session=session)
        assert r.status_code == 404 and b"the api key" not in r.content


def test_a_directory_someone_else_owns_is_not_used(monkeypatch):
    import os

    real = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real + 1)     # every dir reads as another's
    assert _as("owner", "x", lambda: screenshots.capture(Shooter(), "SIM", "l")) is None


def test_the_directories_are_kept_private(tmp_path):
    import os
    import stat

    (tmp_path / "quern").mkdir(mode=0o755)
    screenshots.private_dir(screenshots.DIR)
    for d in (tmp_path / "quern", screenshots.DIR):
        assert stat.S_IMODE(os.lstat(d).st_mode) == 0o700, d
