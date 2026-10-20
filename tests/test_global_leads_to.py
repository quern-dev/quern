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
        assert len(unusable) == 2 and all(u["category"] == "except" for u in unusable)

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
        assert globals_ == [] and unusable[0]["reason"] == "global_leads_to is not a list"

    def test_a_non_mapping_entry_is_reported_and_the_rest_kept(self):
        globals_, unusable = parse_global_leads_to(["oops", TABS[0]])
        assert [e.target for e, _ in globals_] == ["home"]
        assert unusable[0]["reason"] == "not a mapping"

    def test_an_entry_without_a_screen(self):
        globals_, unusable = parse_global_leads_to([
            {"action": 'tap_element label="X"'}, TABS[1]])
        assert [e.target for e, _ in globals_] == ["search"]
        assert unusable[0]["reason"] == "names no screen"

    @pytest.mark.parametrize("bad", ["", False, ["[[screens/login]]", 5]])
    def test_any_unusable_except_is_reported(self, bad):
        globals_, unusable = parse_global_leads_to([{**TABS[0], "except": bad}])
        assert globals_[0][1] == [] and unusable[0]["category"] == "except"

    def test_a_bad_timeout_keeps_its_except(self):
        globals_, unusable = parse_global_leads_to([
            {**TABS[0], "timeout": "slow", "except": ["[[screens/login]]"]}])
        assert globals_[0][1] == ["login"] and unusable[0]["category"] == "timeout"


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

    def test_one_edge_each_for_a_tab_with_a_stack(self):
        # The Settings tab shows whatever its stack was left on.
        screens = _screens("home", "settings", "settings_detail")
        globals_, _ = parse_global_leads_to([
            {"screen": "[[screens/settings]]", "action": 'tap_element identifier="tab_settings"'},
            {"screen": "[[screens/settings_detail]]",
             "action": 'tap_element identifier="tab_settings"'},
        ])
        apply_global_edges(screens, globals_)
        by = {s.screen: _targets(s) for s in screens}
        # Both apply from home: the second is not taken for home's own edge.
        assert by["home"] == [("settings", "tab_settings"), ("settings_detail", "tab_settings")]
        # Re-tapping the tab on any screen of its own stack is not this group's
        # navigation, so neither edge applies there.
        assert by["settings"] == [] and by["settings_detail"] == []

    @pytest.mark.parametrize("field", ["label_contains", "label_prefix"])
    def test_widened_own_edges_override_too(self, field):
        screens = _screens("home", "detail")
        screens[1].leads_to = [NavEdge(target="detail", **{field: "HOME"})]
        globals_, _ = parse_global_leads_to([
            {"screen": "[[screens/home]]", "action": f'tap_element {field}="Home"'}])
        apply_global_edges(screens, globals_)
        assert len(screens[1].leads_to) == 1 and not screens[1].leads_to[0].from_global

    @pytest.mark.parametrize("field", ["label_contains", "label_prefix"])
    def test_a_different_widened_own_edge_does_not_override(self, field):
        screens = _screens("home", "detail")
        screens[1].leads_to = [NavEdge(target="detail", **{field: "Settings"})]
        globals_, _ = parse_global_leads_to([
            {"screen": "[[screens/home]]", "action": f'tap_element {field}="Home"'}])
        apply_global_edges(screens, globals_)
        assert any(e.from_global for e in screens[1].leads_to)

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
        assert issues[0]["reason"] == "except names no loaded screen"
        assert issues[0]["category"] == "except"
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


class TestAppMdIsNeverFatal:
    """app.md was never read before #503: whatever an existing knowledge base
    keeps there must not fail its load."""

    def _load(self, tmp_path, raw: bytes):
        _screen_file(tmp_path, "home")
        (tmp_path / "app.md").write_bytes(raw)
        registry = LandmarkRegistry()
        count, _ = registry.load_from_path("probe", str(tmp_path))
        return count, registry.navigation_report("probe")

    def test_not_utf8(self, tmp_path):
        count, report = self._load(tmp_path, "---\napp_name: caf\xe9\n---\n".encode("latin-1"))
        assert count == 1 and "could not be read" in report["app_md"][0]["reason"]

    def test_binary(self, tmp_path):
        count, report = self._load(tmp_path, b"\xff\xfe\x00\x01\x02")
        assert count == 1 and report["app_md"]

    def test_unreadable(self, tmp_path):
        _screen_file(tmp_path, "home")
        (tmp_path / "app.md").mkdir()   # a directory: read_text raises an OSError
        registry = LandmarkRegistry()
        count, _ = registry.load_from_path("probe", str(tmp_path))
        assert count == 1 and registry.navigation_report("probe")["app_md"]

    def test_a_bom_is_not_a_reason_to_drop_globals(self, tmp_path):
        _screen_file(tmp_path, "search")
        count, report = self._load(tmp_path, ("\ufeff" + APP_MD).encode("utf-8"))
        assert report.get("global") == 2 and "app_md" not in report

    def test_prose_app_md_without_globals_is_none_of_our_business(self, tmp_path):
        count, report = self._load(tmp_path, b"# Just notes\n\nno frontmatter at all\n")
        assert report == {"edges": 0}
        count, report = self._load(tmp_path, b"---\nnot: [valid\n---\n")
        assert report == {"edges": 0}

    def test_globals_outside_frontmatter_are_reported(self, tmp_path):
        count, report = self._load(tmp_path, b"# notes\nglobal_leads_to: []\n")
        assert "frontmatter" in report["app_md"][0]["reason"]

    def test_non_mapping_frontmatter_with_globals_is_reported(self, tmp_path):
        count, report = self._load(tmp_path, b"---\n- global_leads_to\n---\n")
        assert "not a mapping" in report["app_md"][0]["reason"]

    def test_bad_yaml_with_globals_is_reported_apart_from_edges(self, tmp_path):
        count, report = self._load(tmp_path, b"---\nglobal_leads_to: [unclosed\n---\n")
        assert "not valid YAML" in report["app_md"][0]["reason"]
        assert "unusable" not in report and "note" not in report


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

    def test_an_unknown_target_in_app_md_is_reported(self):
        scan = self._kb(self._tmp, app_md=(
            "---\nglobal_leads_to:\n  - screen: \"[[screens/gone]]\"\n"
            "    action: 'tap_element label=\"Gone\"'\n---\n"))
        assert scan.edge_issues == [{"file": "app.md", "entry": {"screen": "gone"},
                                     "reason": "names no loaded screen"}]

    def test_file_names_that_differ_from_screen_names(self):
        _write(self._tmp / "screens" / "home.md",
               '---\nscreen: "Home Screen"\nlandmarks:\n  - { element: "Heading", label: "H" }\n'
               "---\n# H\n")
        _screen_file(self._tmp, "detail")
        _write(self._tmp / "app.md", (
            "---\nglobal_leads_to:\n  - screen: \"[[screens/home]]\"\n"
            "    action: 'tap_element identifier=\"tab_home\"'\n---\n"))
        scan = scan_knowledge_base(self._tmp)
        detail = next(s for s in scan.screens if s.screen == "detail")
        assert _targets(detail) == [("Home Screen", "tab_home")] and scan.edge_issues == []

    def test_unusable_globals_name_app_md(self):
        scan = self._kb(self._tmp, app_md=(
            "---\nglobal_leads_to:\n  - screen: \"[[screens/home]]\"\n"
            "    action: 'swipe left'\n---\n"))
        assert scan.edge_issues[0]["file"] == "app.md"

    def test_a_screens_dir_given_directly_reads_no_app_md(self):
        # An app.md inside screens/ would be read if the path were not checked.
        self._kb(self._tmp, app_md=None)
        _write(self._tmp / "screens" / "app.md", APP_MD)
        scan = scan_knowledge_base(self._tmp / "screens")
        assert all(not e.from_global for s in scan.screens for e in s.leads_to)

    def test_global_counts_declarations_not_applications(self):
        # Two edges that differ only in type; and one that applies nowhere.
        self._kb(self._tmp, app_md=(
            "---\nglobal_leads_to:\n"
            "  - screen: \"[[screens/home]]\"\n"
            "    action: 'tap_element label=\"Home\" element_type=\"Button\"'\n"
            "  - screen: \"[[screens/home]]\"\n"
            "    action: 'tap_element label=\"Home\" element_type=\"RadioButton\"'\n"
            "  - screen: \"[[screens/search]]\"\n    action: 'tap_element label=\"Go\"'\n"
            "    except: [\"[[screens/home]]\", \"[[screens/detail]]\", \"[[screens/login]]\"]\n"
            "---\n"))
        registry = LandmarkRegistry()
        registry.load_from_path("probe", str(self._tmp))
        assert registry.navigation_report("probe")["global"] == 3

    def test_own_and_global_edges_both_count_in_edges(self):
        self._kb(self._tmp)
        _screen_file(self._tmp, "detail", "leads_to:\n  - screen: \"[[screens/home]]\"\n"
                                          "    action: 'tap_element label=\"Back\"'\n")
        registry = LandmarkRegistry()
        registry.load_from_path("probe", str(self._tmp))
        report = registry.navigation_report("probe")
        assert report["edges"] == 6 and report["global"] == 2

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
    async def _load(self, payload):
        app = create_app(config=ServerConfig(api_key="k"), enable_oslog=False,
                         enable_crash=False, enable_proxy=False)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/v1/landmarks/load", json={"app": "probe", **payload},
                                     headers={"Authorization": "Bearer k"})
        return app, resp

    async def test_global_leads_to_inline(self):
        app, resp = await self._load({
            "landmarks": {n: [{"element": "Heading", "label": n}]
                          for n in ("home", "search", "detail")},
            "global_leads_to": [*TABS, {"screen": "gone", "action": 'tap_element label="Gone"'}],
        })
        nav = resp.json()["navigation"]
        # Three declared; the one to an unknown screen applies nowhere.
        assert nav["global"] == 3 and nav["edges"] == 4
        assert nav["unusable"][0]["file"] == "inline:global_leads_to"
        edges = app.state.landmark_registry.edges_from("probe", "detail")
        assert sorted(e.target for e in edges) == ["home", "search"]

    async def test_inline_problems_are_reported_not_refused(self):
        _, resp = await self._load({
            "landmarks": {n: [{"element": "Heading", "label": n}] for n in ("home", "a")},
            "global_leads_to": ["oops", {**TABS[0], "except": ["[[screens/nope]]"]}],
        })
        assert resp.status_code == 200
        nav = resp.json()["navigation"]
        assert nav["unusable"] == [{"file": "inline:global_leads_to", "entry": "oops",
                                    "reason": "not a mapping"}]
        assert nav["except_ignored"][0]["file"] == "inline:global_leads_to"
