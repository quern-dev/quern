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
from server.device.landmarks import LandmarkRegistry, load_remembered

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
        assert config_mod.get_knowledge_bases() == {APP: str(knowledge.resolve())}

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
        assert "not a directory" in outcomes["gone"]["error"]
        assert "no screens with landmarks" in outcomes["empty"]["error"]
        assert registry.list_sets()["good"] == 1 and "gone" not in registry.list_sets()

    def test_one_that_raises_never_stops_the_start(self, tmp_path, monkeypatch):
        good = _project(tmp_path / "good")
        registry = LandmarkRegistry()

        def boom(app, path):
            raise RuntimeError("parser exploded")
        monkeypatch.setattr(registry, "load_from_path", boom)
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
