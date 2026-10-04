"""Remembered knowledge bases: load_landmarks(remember=true) and the reload
at every start.

Landmarks live in the server's memory, and every restart -- an update, a
merge, a crash -- emptied them, so screen identification quietly stopped
working until someone loaded the knowledge base again from wherever it was.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import config as config_mod
from server.api.landmarks import router
from server.knowledge.landmarks import LandmarkRegistry, load_remembered

APP = "com.example.app"

SCREEN = textwrap.dedent("""\
    ---
    screen: "Login"
    landmarks:
      - { element: "navigationBar", label: "Login" }
    ---
    # Login
""")


@pytest.fixture(autouse=True)
def _config(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "USER_CONFIG_FILE", tmp_path / "state" / "config.json")
    (tmp_path / "state").mkdir()


def _project(root: Path, bundle: str | None = APP, screens: bool = True) -> Path:
    """A project with a `.quern/knowledge` the way init_app_knowledge lays it out."""
    knowledge = root / ".quern" / "knowledge"
    (knowledge / "screens").mkdir(parents=True)
    if screens:
        (knowledge / "screens" / "login.md").write_text(SCREEN)
    if bundle is not None:
        (root / ".quern" / "config.json").write_text(json.dumps({"bundle_id": bundle}))
    return knowledge


def _client(registry: LandmarkRegistry | None = None, startup: dict | None = None):
    app = FastAPI()
    app.include_router(router)
    app.state.landmark_registry = registry or LandmarkRegistry()
    app.state.landmark_startup = startup or {}
    return TestClient(app), app


class TestConfig:
    def test_remember_and_forget(self):
        config_mod.remember_knowledge_base("a", "/kb/a")
        config_mod.remember_knowledge_base("b", "/kb/b")
        assert config_mod.get_knowledge_bases() == {"a": "/kb/a", "b": "/kb/b"}
        assert config_mod.forget_knowledge_base("a") == ["a"]
        assert config_mod.forget_knowledge_base("a") == [], "forgetting twice forgets nothing"
        assert config_mod.forget_knowledge_base(None) == ["b"]
        assert config_mod.get_knowledge_bases() == {}

    def test_entries_of_the_wrong_shape_are_left_out(self):
        config_mod.USER_CONFIG_FILE.write_text(json.dumps(
            {"auto_install_cert": True,
             "knowledge_bases": {"ok": "/kb", "empty": "", "num": 3, "list": ["/x"]}}))
        assert config_mod.get_knowledge_bases() == {"ok": "/kb"}
        config_mod.USER_CONFIG_FILE.write_text(json.dumps({"knowledge_bases": ["/kb"]}))
        assert config_mod.get_knowledge_bases() == {}

    def test_other_settings_survive(self):
        config_mod.USER_CONFIG_FILE.write_text(json.dumps({"auto_install_cert": True}))
        config_mod.remember_knowledge_base("a", "/kb/a")
        assert json.loads(config_mod.USER_CONFIG_FILE.read_text())["auto_install_cert"] is True


class TestLoad:
    def test_remember_keeps_the_absolute_path(self, tmp_path):
        knowledge = _project(tmp_path / "proj")
        client, _ = _client()
        r = client.post("/api/v1/landmarks/load",
                        json={"app": APP, "source": str(knowledge), "remember": True})
        assert r.status_code == 200, r.text
        assert r.json()["remembered"] is True and r.json()["screens"] == 1
        assert config_mod.get_knowledge_bases() == {APP: str(knowledge.absolute())}

    def test_a_symlinked_path_is_kept_as_given(self, tmp_path):
        """~/src -> /Volumes/... here: the link, not where it points today."""
        _project(tmp_path / "real")
        (tmp_path / "link").symlink_to(tmp_path / "real")
        client, _ = _client()
        client.post("/api/v1/landmarks/load", json={
            "app": APP, "source": str(tmp_path / "link"), "remember": True})
        assert config_mod.get_knowledge_bases()[APP] == str(
            tmp_path / "link" / ".quern" / "knowledge")

    def test_without_remember_nothing_is_kept(self, tmp_path):
        knowledge = _project(tmp_path / "proj")
        client, _ = _client()
        r = client.post("/api/v1/landmarks/load", json={"app": APP, "source": str(knowledge)})
        assert r.json()["remembered"] is False
        assert config_mod.get_knowledge_bases() == {}

    def test_the_project_names_the_app_and_the_root_finds_the_knowledge(self, tmp_path):
        """`init_app_knowledge` writes `.quern/config.json` with the bundle id;
        asking for it again is a chance to get it wrong."""
        _project(tmp_path / "proj", bundle="com.from.config")
        client, app = _client()
        for source in (tmp_path / "proj", tmp_path / "proj" / ".quern" / "knowledge"):
            r = client.post("/api/v1/landmarks/load", json={"source": str(source)})
            assert r.status_code == 200, r.text
            assert r.json()["loaded"] == "com.from.config" and r.json()["screens"] == 1
        assert app.state.landmark_registry.list_sets() == {"com.from.config": 1}

    def test_no_app_and_no_project_config_is_refused(self, tmp_path):
        knowledge = _project(tmp_path / "proj", bundle=None)
        client, _ = _client()
        r = client.post("/api/v1/landmarks/load", json={"source": str(knowledge)})
        assert r.status_code == 400 and "app is required" in r.json()["detail"]

    def test_a_path_that_is_not_there_is_refused_not_loaded_empty(self, tmp_path):
        """It used to load zero screens and say nothing -- how a moved
        checkout would look at every start."""
        client, _ = _client()
        r = client.post("/api/v1/landmarks/load",
                        json={"app": APP, "source": str(tmp_path / "gone"), "remember": True})
        assert r.status_code == 400 and "is not a directory" in r.json()["detail"]
        assert config_mod.get_knowledge_bases() == {}

    def test_an_empty_knowledge_base_is_not_remembered(self, tmp_path):
        knowledge = _project(tmp_path / "proj", screens=False)
        client, _ = _client()
        r = client.post("/api/v1/landmarks/load",
                        json={"app": APP, "source": str(knowledge), "remember": True})
        assert r.status_code == 400 and "no screens with landmarks" in r.json()["detail"]
        assert config_mod.get_knowledge_bases() == {}

    def test_inline_landmarks_cannot_be_remembered(self):
        client, _ = _client()
        r = client.post("/api/v1/landmarks/load", json={
            "app": APP, "remember": True,
            "landmarks": {"Home": [{"element": "Button", "label": "OK"}]}})
        assert r.status_code == 400 and "needs a source path" in r.json()["detail"]
        assert config_mod.get_knowledge_bases() == {}
        r = client.post("/api/v1/landmarks/load", json={
            "landmarks": {"Home": [{"element": "Button", "label": "OK"}]}})
        assert r.status_code == 400 and "app is required" in r.json()["detail"]

    def test_a_config_that_cannot_be_written_is_said(self, tmp_path, monkeypatch):
        knowledge = _project(tmp_path / "proj")

        def broken(app, path):
            raise PermissionError(13, "Permission denied")
        monkeypatch.setattr(config_mod, "remember_knowledge_base", broken)
        client, _ = _client()
        r = client.post("/api/v1/landmarks/load",
                        json={"app": APP, "source": str(knowledge), "remember": True})
        assert r.status_code == 500 and "could not remember" in r.json()["detail"]


class TestStart:
    def test_each_remembered_knowledge_base_loads_and_says_how(self, tmp_path):
        good = _project(tmp_path / "good")
        empty = _project(tmp_path / "empty", screens=False)
        registry = LandmarkRegistry()
        outcomes = load_remembered(registry, {
            "good": str(good), "gone": str(tmp_path / "gone"), "empty": str(empty)})
        assert outcomes["good"]["screens"] == 1 and "error" not in outcomes["good"]
        assert outcomes["good"]["skipped"] == 0
        assert "not a directory" in outcomes["gone"]["error"]
        assert "no screens with landmarks" in outcomes["empty"]["error"]
        # An empty one is not registered: it would list as loaded (review).
        assert registry.list_sets() == {"good": 1}

    def test_one_that_raises_never_stops_the_start(self, tmp_path, monkeypatch):
        good = _project(tmp_path / "good")
        registry = LandmarkRegistry()

        def boom(app, scan, path):
            raise RuntimeError("parser exploded")
        monkeypatch.setattr(registry, "load_scan", boom)
        outcomes = load_remembered(registry, {"good": str(good)})
        assert "parser exploded" in outcomes["good"]["error"]

    def test_create_app_loads_them_and_list_landmarks_says_so(self, tmp_path):
        """The whole path: remembered in config, loaded by create_app, shown
        by the route -- including the one that did not load."""
        from server.config import ServerConfig
        from server.main import create_app
        config_mod.remember_knowledge_base(APP, str(_project(tmp_path / "proj")))
        config_mod.remember_knowledge_base("com.gone", str(tmp_path / "gone"))
        app = create_app(config=ServerConfig(api_key="k"), enable_oslog=False)
        assert app.state.landmark_registry.list_sets().get(APP) == 1
        listing = TestClient(app).get(
            "/api/v1/landmarks/", headers={"Authorization": "Bearer k"}).json()
        rows = {r["app"]: r for r in listing["remembered"]}
        assert rows[APP]["loaded"] is True and rows[APP]["at_start"]["screens"] == 1
        assert rows["com.gone"]["loaded"] is False
        assert "not a directory" in rows["com.gone"]["at_start"]["error"]


class TestUnload:
    def test_forget_stops_the_reload_and_plain_unload_does_not(self, tmp_path):
        knowledge = _project(tmp_path / "proj")
        client, _ = _client()
        client.post("/api/v1/landmarks/load",
                    json={"app": APP, "source": str(knowledge), "remember": True})
        assert client.delete("/api/v1/landmarks/", params={"app": APP}).json() == {
            "unloaded": APP}
        assert APP in config_mod.get_knowledge_bases()
        r = client.delete("/api/v1/landmarks/", params={"app": APP, "forget": "true"}).json()
        assert r == {"unloaded": APP, "forgotten": [APP]}
        assert config_mod.get_knowledge_bases() == {}


class TestReview:
    def test_a_refused_remember_leaves_what_was_loaded(self, tmp_path):
        """It used to load the empty directory first and refuse after: a 400
        that had silently emptied the set (review)."""
        good = _project(tmp_path / "good")
        empty = tmp_path / "empty"
        empty.mkdir()
        client, app = _client()
        client.post("/api/v1/landmarks/load",
                    json={"app": APP, "source": str(good), "remember": True})
        r = client.post("/api/v1/landmarks/load",
                        json={"app": APP, "source": str(empty), "remember": True})
        assert r.status_code == 400
        assert f"stays remembered at {good.absolute()}" in r.json()["detail"]
        assert app.state.landmark_registry.list_sets() == {APP: 1}
        assert config_mod.get_knowledge_bases() == {APP: str(good.absolute())}

    def test_an_empty_load_never_empties_a_loaded_set(self, tmp_path):
        """Without remember too: loading nothing over a working set used to
        replace it and answer 200, so identification went dead quietly."""
        good = _project(tmp_path / "good")
        empty = _project(tmp_path / "empty", screens=False)
        client, app = _client()
        client.post("/api/v1/landmarks/load", json={"app": APP, "source": str(good)})
        r = client.post("/api/v1/landmarks/load", json={"app": APP, "source": str(empty)})
        assert r.status_code == 400
        assert f"loaded set from {good} was left in place" in r.json()["detail"]
        assert app.state.landmark_registry.list_sets() == {APP: 1}
        assert app.state.landmark_registry.source(APP) == str(good)

    def test_an_empty_load_with_nothing_loaded_still_answers(self, tmp_path):
        """Nothing to protect, so a started-but-empty knowledge base loads as
        zero screens, as it did -- the refusal is about the set it would
        replace, not the emptiness."""
        empty = _project(tmp_path / "empty", screens=False)
        client, _ = _client()
        r = client.post("/api/v1/landmarks/load", json={"app": APP, "source": str(empty)})
        assert r.status_code == 200 and r.json()["screens"] == 0

    def test_list_says_where_each_set_came_from(self, tmp_path):
        a = _project(tmp_path / "a")
        client, _ = _client()
        client.post("/api/v1/landmarks/load", json={"app": APP, "source": str(a)})
        client.post("/api/v1/landmarks/load", json={
            "app": "other", "landmarks": {"Home": [{"element": "Button", "label": "OK"}]}})
        assert client.get("/api/v1/landmarks/").json()["sources"] == {
            APP: str(a), "other": "inline"}

    def test_loaded_means_the_remembered_path_is_loaded(self, tmp_path):
        """Another directory's set for the same app is not the remembered
        one, and an inline load is not either (review)."""
        a = _project(tmp_path / "a")
        b = _project(tmp_path / "b")
        client, _ = _client()
        client.post("/api/v1/landmarks/load", json={"app": APP, "source": str(a), "remember": True})
        row = client.get("/api/v1/landmarks/").json()["remembered"][0]
        assert row["loaded"] is True and row["path"] == str(a.absolute())
        assert row["screens"] == 1
        client.post("/api/v1/landmarks/load", json={"app": APP, "source": str(b)})
        row = client.get("/api/v1/landmarks/").json()["remembered"][0]
        assert row["loaded"] is False and row["loaded_from"] == str(b)
        client.post("/api/v1/landmarks/load", json={
            "app": APP, "landmarks": {"Home": [{"element": "Button", "label": "OK"}]}})
        row = client.get("/api/v1/landmarks/").json()["remembered"][0]
        assert row["loaded"] is False and row["loaded_from"] == "inline"

    def test_a_config_that_cannot_be_read_is_never_overwritten_or_read_as_empty(self):
        config_mod.USER_CONFIG_FILE.write_text('{"auto_install_cert": true, ')
        before = config_mod.USER_CONFIG_FILE.read_text()
        with pytest.raises(config_mod.ConfigUnreadable):
            config_mod.remember_knowledge_base(APP, "/kb")
        with pytest.raises(config_mod.ConfigUnreadable):
            config_mod.forget_knowledge_base(APP)
        assert config_mod.USER_CONFIG_FILE.read_text() == before
        client, _ = _client()
        listing = client.get("/api/v1/landmarks/").json()
        assert listing["remembered"] is None and "cannot be read" in listing["remembered_error"]
        r = client.delete("/api/v1/landmarks/", params={"app": APP, "forget": "true"})
        assert r.status_code == 500 and "could not forget" in r.json()["detail"]

    def test_forget_keeps_other_settings(self):
        config_mod.USER_CONFIG_FILE.write_text(json.dumps(
            {"auto_install_cert": True, "knowledge_bases": {APP: "/kb"}}))
        config_mod.forget_knowledge_base(APP)
        assert json.loads(config_mod.USER_CONFIG_FILE.read_text())["auto_install_cert"] is True

    @pytest.mark.parametrize("config_text, expected", [
        ('{"bundle_id": "com.ok"}', "com.ok"),
        ('{"bundle_id": 42}', None),
        ('{"bundle_id": ""}', None),
        ('{"bundle_id": "com.ok", ', None),          # malformed
        ('["com.ok"]', None),                       # not an object
    ])
    def test_the_project_config_is_read_carefully(self, tmp_path, config_text, expected):
        from server.api.landmarks import _app_from_project
        knowledge = _project(tmp_path / "p", bundle=None)
        (tmp_path / "p" / ".quern" / "config.json").write_text(config_text)
        assert _app_from_project(knowledge) == expected
        assert _app_from_project(knowledge / "screens") == expected

    def test_only_a_quern_directory_names_the_app(self, tmp_path):
        """A config.json beside some other knowledge directory is not one."""
        from server.api.landmarks import _app_from_project
        other = tmp_path / "notes" / "knowledge"
        other.mkdir(parents=True)
        (tmp_path / "notes" / "config.json").write_text('{"bundle_id": "com.wrong"}')
        assert _app_from_project(other) is None

    def test_every_project_path_finds_the_knowledge_and_the_app(self, tmp_path):
        _project(tmp_path / "proj", bundle="com.from.config")
        client, _ = _client()
        for source in ("proj", "proj/.quern", "proj/.quern/knowledge",
                       "proj/.quern/knowledge/screens"):
            r = client.post("/api/v1/landmarks/load", json={"source": str(tmp_path / source)})
            assert r.status_code == 200 and r.json()["loaded"] == "com.from.config", source
            assert r.json()["screens"] == 1, source

    def test_the_answer_names_the_directory_loaded(self, tmp_path):
        knowledge = _project(tmp_path / "proj")
        client, _ = _client()
        r = client.post("/api/v1/landmarks/load",
                        json={"app": APP, "source": str(tmp_path / "proj")})
        assert r.json()["source"] == str(knowledge)

    def test_a_relative_or_home_path_is_remembered_absolute(self, tmp_path, monkeypatch):
        _project(tmp_path / "home" / "proj")
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        client, _ = _client()
        r = client.post("/api/v1/landmarks/load",
                        json={"app": APP, "source": "~/proj", "remember": True})
        assert r.status_code == 200, r.text
        assert config_mod.get_knowledge_bases()[APP] == str(
            tmp_path / "home" / "proj" / ".quern" / "knowledge")
        monkeypatch.chdir(tmp_path / "home")
        client.post("/api/v1/landmarks/load",
                    json={"app": "rel", "source": "proj", "remember": True})
        assert config_mod.get_knowledge_bases()["rel"] == str(
            tmp_path / "home" / "proj" / ".quern" / "knowledge")

    def test_a_hand_edited_entry_with_home_and_a_root_still_loads(self, tmp_path, monkeypatch):
        _project(tmp_path / "home" / "proj")
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        registry = LandmarkRegistry()
        outcomes = load_remembered(registry, {APP: "~/proj"})
        assert outcomes[APP]["screens"] == 1 and registry.list_sets() == {APP: 1}



def test_the_locked_read_is_the_strict_one(monkeypatch):
    """A check before the lock can be outrun by an edit; the read inside
    it decides (CodeRabbit). `change` never sees an unreadable file."""
    config_mod.USER_CONFIG_FILE.write_text("[1, 2]")
    seen = []
    with pytest.raises(config_mod.ConfigUnreadable):
        config_mod.update_user_config(seen.append, strict=True)
    assert seen == [] and config_mod.USER_CONFIG_FILE.read_text() == "[1, 2]"
    # The default stays lenient for every other writer.
    config_mod.update_user_config(lambda c: c.__setitem__("x", 1))
    assert json.loads(config_mod.USER_CONFIG_FILE.read_text()) == {"x": 1}
