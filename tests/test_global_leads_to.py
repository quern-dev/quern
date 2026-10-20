"""#503: navigation available from every screen, declared once in app.md.

A tab bar is the commonest navigation there is, and #499's checks only saw
the edges a screen lists itself -- so a real knowledge base, which will not
repeat eight tab edges on forty screens, got nothing checked on tab taps.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from server.config import ServerConfig
from server.knowledge.landmarks import LandmarkRegistry, scan_knowledge_base
from server.knowledge.navigation import (
    DocumentedTap,
    apply_global_edges,
    parse_global_leads_to,
    previous_tap_note,
)
from server.main import create_app
from server.models import Landmark, NavEdge, ScreenLandmarks

TABS = [
    {"screen": "[[screens/home]]", "action": 'tap_element identifier="tab_home"'},
    {"screen": "[[screens/search]]", "action": 'tap_element identifier="tab_search"'},
]


def _screens(*names):
    return [ScreenLandmarks(screen=n, landmarks=[Landmark(element="Heading", label=n)])
            for n in names]


def _targets(screen):
    return sorted((e.target, e.identifier or e.label) for e in screen.leads_to)


class TestParse:
    def test_edges_with_their_except(self):
        globals_, unusable = parse_global_leads_to([
            *TABS,
            {"screen": "[[screens/home]]", "action": 'tap_element label="Home"',
             "except": ["[[screens/login]]", "onboarding"]},
        ])
        assert unusable == []
        assert [(e.target, e.from_global, ex) for e, ex in globals_] == [
            ("home", True, []), ("search", True, []), ("home", True, ["login", "onboarding"]),
        ]

    def test_a_bad_except_is_reported_and_the_edge_applies_everywhere(self):
        globals_, unusable = parse_global_leads_to([
            {**TABS[0], "except": "login"},
            {**TABS[1], "except": ["[[screens/...]]"]},
        ])
        assert [ex for _e, ex in globals_] == [[], []]
        assert len(unusable) == 2 and all(u["edge_kept"] for u in unusable)

    def test_unusable_entries_are_reported_and_the_rest_kept_in_order(self):
        globals_, unusable = parse_global_leads_to([
            {"screen": "[[screens/x]]", "action": "tap the gear"},
            {**TABS[1], "except": ["[[screens/login]]"]},
        ])
        assert [(e.target, ex) for e, ex in globals_] == [("search", ["login"])]
        assert "tap_element" in unusable[0]["reason"]

    def test_condition_and_timeout_carry_over(self):
        globals_, _ = parse_global_leads_to([
            {**TABS[0], "condition": "signed in", "timeout": 8}])
        edge = globals_[0][0]
        assert edge.condition == "signed in" and edge.timeout == 8.0

    def test_not_a_list(self):
        globals_, unusable = parse_global_leads_to({"screen": "x"})
        assert globals_ == [] and unusable


class TestApply:
    def test_every_screen_but_the_target(self):
        screens = _screens("home", "search", "detail")
        globals_, _ = parse_global_leads_to(TABS)
        assert apply_global_edges(screens, globals_) == []
        by = {s.screen: _targets(s) for s in screens}
        assert by["home"] == [("search", "tab_search")]
        assert by["search"] == [("home", "tab_home")]
        assert by["detail"] == [("home", "tab_home"), ("search", "tab_search")]

    def test_except(self):
        screens = _screens("home", "search", "login")
        globals_, _ = parse_global_leads_to([{**TABS[0], "except": ["[[screens/login]]"]}])
        apply_global_edges(screens, globals_)
        assert _targets(screens[2]) == []
        assert _targets(screens[1]) == [("home", "tab_home")]

    def test_a_screens_own_edge_for_the_element_wins(self):
        # On "detail" the Home tab pops to its root rather than to "home".
        screens = _screens("home", "detail", "root")
        screens[1].leads_to = [NavEdge(target="root", identifier="tab_home")]
        globals_, _ = parse_global_leads_to([TABS[0]])
        apply_global_edges(screens, globals_)
        assert _targets(screens[1]) == [("root", "tab_home")]
        assert _targets(screens[2]) == [("home", "tab_home")]

    def test_labels_compare_without_case(self):
        screens = _screens("home", "detail")
        screens[1].leads_to = [NavEdge(target="detail", label="HOME")]
        globals_, _ = parse_global_leads_to([
            {"screen": "[[screens/home]]", "action": 'tap_element label="Home"'}])
        apply_global_edges(screens, globals_)
        assert _targets(screens[1]) == [("detail", "HOME")]

    def test_file_names_resolve_to_screen_names(self):
        screens = _screens("Home Screen", "Login", "Other")
        stems = {"home": "Home Screen", "login": "Login", "other": "Other"}
        globals_, _ = parse_global_leads_to([{**TABS[0], "except": ["[[screens/login]]"]}])
        assert apply_global_edges(screens, globals_, stems) == []
        assert _targets(screens[2]) == [("Home Screen", "tab_home")]
        assert _targets(screens[1]) == []

    def test_an_unknown_target_is_reported_once(self):
        screens = _screens("a", "b", "c")
        globals_, _ = parse_global_leads_to([
            {"screen": "[[screens/gone]]", "action": 'tap_element label="Gone"'}])
        issues = apply_global_edges(screens, globals_)
        assert [i["reason"] for i in issues] == ["names no loaded screen"]
        assert all(s.leads_to == [] for s in screens)

    def test_a_target_with_no_landmarks_is_reported(self):
        screens = [*_screens("a"), ScreenLandmarks(screen="stub", landmarks=[])]
        globals_, _ = parse_global_leads_to([
            {"screen": "[[screens/stub]]", "action": 'tap_element label="Stub"'}])
        issues = apply_global_edges(screens, globals_)
        assert "no landmarks" in issues[0]["reason"] and screens[0].leads_to == []

    def test_an_unknown_except_is_reported_and_the_edge_kept(self):
        screens = _screens("home", "a")
        globals_, _ = parse_global_leads_to([{**TABS[0], "except": ["[[screens/nope]]"]}])
        issues = apply_global_edges(screens, globals_)
        assert issues[0]["reason"] == "except names no loaded screen" and issues[0]["edge_kept"]
        assert _targets(screens[1]) == [("home", "tab_home")]


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _screen_file(root, name, extra=""):
    _write(root / "screens" / f"{name}.md",
           f'---\nscreen: "{name}"\nlandmarks:\n  - {{ element: "Heading", label: "{name}" }}\n'
           f"{extra}---\n# {name}\n")


APP_MD = '''---
app_name: "Probe"
global_leads_to:
  - screen: "[[screens/home]]"
    action: 'tap_element identifier="tab_home"'
    except: ["[[screens/login]]"]
  - screen: "[[screens/search]]"
    action: 'tap_element identifier="tab_search"'
---
# Probe
'''


class TestScan:
    def _kb(self, tmp_path, app_md=APP_MD):
        for n in ("home", "search", "detail", "login"):
            _screen_file(tmp_path, n)
        if app_md is not None:
            _write(tmp_path / "app.md", app_md)
        return scan_knowledge_base(tmp_path)

    def test_app_md_applies_to_the_screens(self):
        scan = self._kb(self._tmp)
        by = {s.screen: _targets(s) for s in scan.screens}
        assert by["detail"] == [("home", "tab_home"), ("search", "tab_search")]
        assert by["login"] == [("search", "tab_search")]
        assert scan.edge_issues == []

    @pytest.fixture(autouse=True)
    def _tmpdir(self, tmp_path):
        self._tmp = tmp_path

    def test_no_app_md(self):
        scan = self._kb(self._tmp, app_md=None)
        assert all(s.leads_to == [] for s in scan.screens) and scan.edge_issues == []

    def test_app_md_without_the_key(self):
        scan = self._kb(self._tmp, app_md='---\napp_name: "x"\n---\n# x\n')
        assert all(s.leads_to == [] for s in scan.screens) and scan.edge_issues == []

    def test_app_md_with_bad_yaml_is_reported(self):
        scan = self._kb(self._tmp, app_md="---\nglobal_leads_to: [unclosed\n---\n")
        assert scan.edge_issues[0]["file"] == "app.md"
        assert "not valid YAML" in scan.edge_issues[0]["reason"]

    def test_unusable_globals_name_app_md(self):
        scan = self._kb(self._tmp, app_md=(
            "---\nglobal_leads_to:\n  - screen: \"[[screens/home]]\"\n"
            "    action: 'swipe left'\n---\n"))
        assert scan.edge_issues[0]["file"] == "app.md"

    def test_a_screens_dir_given_directly_reads_no_app_md(self):
        self._kb(self._tmp)
        scan = scan_knowledge_base(self._tmp / "screens")
        assert all(s.leads_to == [] for s in scan.screens)

    def test_the_load_report_counts_globals_once(self):
        self._kb(self._tmp)
        registry = LandmarkRegistry()
        registry.load_from_path("probe", str(self._tmp))
        report = registry.navigation_report("probe")
        # home: search; search: home; detail: both; login: search only.
        assert report["edges"] == 5 and report["global"] == 2

    def test_a_tab_tap_from_any_screen_feeds_previous_tap(self):
        # The point of #503: a tab tap on "detail" that did not register is
        # named on the next miss, with no edge written on "detail" itself.
        self._kb(self._tmp)
        registry = LandmarkRegistry()
        registry.load_from_path("probe", str(self._tmp))
        tapped = {"label": "Home", "type": "Button", "identifier": "tab_home"}
        tap = DocumentedTap(tapped=tapped, edges=registry.edges_for_tapped(tapped),
                            at=0.0, stamp=1.0)
        note = previous_tap_note(
            tap, {"identified_as": "detail", "confidence": "exact"}, 5.0, stamp=1.0)
        assert note["expected"] == "home" and note["on"] == "detail"
        # And the tap that arrived is not blamed on its target.
        assert previous_tap_note(
            tap, {"identified_as": "home", "confidence": "exact"}, 5.0, stamp=1.0) is None


class TestInline:
    async def test_global_leads_to_inline(self):
        app = create_app(config=ServerConfig(api_key="k"), enable_oslog=False,
                         enable_crash=False, enable_proxy=False)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/v1/landmarks/load", json={
                "app": "probe",
                "landmarks": {n: [{"element": "Heading", "label": n}]
                              for n in ("home", "search", "detail")},
                "global_leads_to": [*TABS, {"screen": "gone",
                                            "action": 'tap_element label="Gone"'}],
            }, headers={"Authorization": "Bearer k"})
        body = resp.json()
        assert body["navigation"]["global"] == 2 and body["navigation"]["edges"] == 4
        assert body["navigation"]["unusable"][0]["file"] == "inline:global_leads_to"
        edges = app.state.landmark_registry.edges_from("probe", "detail")
        assert sorted(e.target for e in edges) == ["home", "search"]
