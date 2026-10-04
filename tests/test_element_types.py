"""Element types across backends, and versioned landmark conventions (#336).

The spec is docs/proposals/landmark-conventions.md. Every behaviour here was
measured on QuernProbe: the same tab item is a `RadioButton` on the
accessibility tree and a `Button` through WDA, so a landmark or `element_type`
written on one backend has to find its element on the other -- without letting
generic types (`Group`, `GenericElement`) or a label landing on a *different*
element (`Button`↔`StaticText`, spec §1.3) match things they should not.
"""

from __future__ import annotations

import textwrap
import time
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from server.config import ServerConfig
from server.device.controller import DeviceController
from server.device.element_types import (
    equivalence,
    portability_finding,
    related_type_names,
    rule_for,
    type_matches,
)
from server.device.ui_elements import find_element, parse_elements
from server.knowledge.landmarks import (
    CURRENT_LANDMARK_CONVENTIONS,
    LandmarkRegistry,
    check_conventions,
    conventions_report,
    detect_collisions,
    identify_screen,
    match_landmark_via,
    scan_knowledge_base,
)
from server.main import create_app
from server.models import DeviceType, Landmark, ScreenLandmarks, UIElement


def _el(type: str, label: str = "", identifier: str | None = None, value=None) -> UIElement:
    return UIElement(
        type=type, label=label, identifier=identifier, value=value,
        frame={"x": 0, "y": 0, "width": 10, "height": 10},
    )


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


class TestTheRule:
    def test_exact_needs_nothing(self):
        assert type_matches("Button", "button", "exact")

    def test_type_only_is_never_widened(self):
        assert not type_matches("RadioButton", "Button", "exact")

    def test_a_label_earns_the_safe_pair(self):
        assert type_matches("RadioButton", "Button", "label")
        assert type_matches("Button", "RadioButton", "label")

    def test_a_label_does_not_earn_the_family(self):
        # §1.3: by label, a Button landmark would land on the text inside a
        # More-list row. Only an identifier makes rows safe.
        assert not type_matches("Button", "StaticText", "label")
        assert not type_matches("Group", "TabBar", "label")

    def test_an_identifier_earns_the_family(self):
        assert type_matches("Button", "Cell", "identifier")
        assert type_matches("Group", "TabBar", "identifier")
        assert type_matches("GenericElement", "ProgressIndicator", "identifier")

    def test_an_identifier_also_earns_the_safe_pair(self):
        assert type_matches("CheckBox", "Switch", "identifier")

    def test_unrelated_types_never_match(self):
        assert not type_matches("Button", "Slider", "identifier")
        assert not type_matches("Image", "Button", "identifier")

    def test_identifier_wins_over_label(self):
        assert rule_for(identifier="x", label="y") == "identifier"
        assert rule_for(identifier=None, label="y") == "label"
        assert rule_for(identifier="", label="") == "exact"

    def test_equivalence_is_reported_only_when_the_types_differ(self):
        assert equivalence("RadioButton", "Button") == "RadioButton≈Button"
        assert equivalence("Button", "BUTTON") is None

    def test_wda_names_keep_their_case_and_add_no_lowercase_twin(self):
        assert related_type_names("Image") == ["Image"]
        assert related_type_names("button") == [
            "Button", "Cell", "RadioButton", "StaticText", "button",
        ]


# ---------------------------------------------------------------------------
# Element filters (tap_element, get_element, wait_for_element)
# ---------------------------------------------------------------------------


class TestFindElement:
    def test_label_finds_the_other_backends_type(self):
        els = [_el("Button", "Home")]
        assert find_element(els, label="Home", element_type="RadioButton") == els

    def test_label_contains_and_prefix_earn_the_pair_too(self):
        els = [_el("Button", "Home tab")]
        assert find_element(els, label_contains="home", element_type="RadioButton") == els
        assert find_element(els, label_prefix="home", element_type="RadioButton") == els

    def test_identifier_finds_the_row_whatever_it_is_called(self):
        els = [_el("Cell", "Detail row", identifier="lists_row_2")]
        assert find_element(els, identifier="lists_row_2", element_type="Button") == els

    def test_label_does_not_reach_into_the_family(self):
        els = [_el("StaticText", "Home")]
        assert find_element(els, label="Home", element_type="Button") == []

    def test_type_only_stays_exact(self):
        els = [_el("Button", "Home")]
        assert find_element(els, element_type="RadioButton") == []

    def test_an_exact_match_hides_the_equivalents(self):
        """Otherwise a tap that was unambiguous becomes ambiguous."""
        tab = _el("RadioButton", "Home")
        button = _el("Button", "Home")
        assert find_element([tab, button], label="Home", element_type="RadioButton") == [tab]

    def test_a_prefilter_keeps_every_related_type(self):
        els = [_el("RadioButton", "a"), _el("Cell", "b"), _el("Slider", "c")]
        kept = find_element(els, element_type="Button", prefilter=True)
        assert [e.type for e in kept] == ["RadioButton", "Cell"]

    def test_parse_time_filter_keeps_the_equivalent(self):
        raw = [
            {"type": "Button", "AXLabel": "Home"},
            {"type": "Slider", "AXLabel": "Home"},
        ]
        parsed = parse_elements(raw, filter_type="RadioButton")
        assert [e.type for e in parsed] == ["Button"]


class TestTheReadPathDoesNotDropTheEquivalent:
    """A `label_contains` reaches `get_ui_elements` as a type-only filter, since
    `_effective_filter_label` passes exact labels only. Applying the rule there
    -- exact, for type-only -- dropped the `Button` before the caller's own
    `find_element` could accept it under the label rule."""

    async def test_cached_tree_filtered_by_type_alone(self):
        ctrl = DeviceController()
        ctrl._active_udid = "SIM-1"
        ctrl._device_type_cache["SIM-1"] = DeviceType.SIMULATOR
        ctrl._ui_cache["SIM-1"] = ([_el("Button", "Home tab"), _el("Slider", "x")], time.time())

        elements, _ = await ctrl.get_ui_elements("SIM-1", filter_type="RadioButton")
        assert find_element(
            elements, label_contains="home", element_type="RadioButton",
        ) == [elements[0]]


class TestResponsesSayHowTheTypeMatched:
    @staticmethod
    def _ctrl(elements):
        ctrl = DeviceController()
        ctrl._is_android = lambda udid: False
        ctrl._is_physical = lambda udid: False
        ctrl.get_ui_elements = AsyncMock(return_value=(elements, "SIM-1"))
        return ctrl

    async def test_get_element_reports_an_equivalence(self):
        ctrl = self._ctrl([_el("Button", "Home")])
        result, _ = await ctrl.get_element(label="Home", element_type="RadioButton", udid="SIM-1")
        assert result["matched_via"] == "RadioButton≈Button"

    async def test_an_exact_match_carries_nothing(self):
        ctrl = self._ctrl([_el("Button", "Home")])
        result, _ = await ctrl.get_element(label="Home", element_type="button", udid="SIM-1")
        assert "matched_via" not in result

    async def test_wait_for_element_reports_an_equivalence(self):
        from server.models import WaitCondition

        ctrl = self._ctrl([_el("Button", "Home")])
        ctrl.resolve_udid = AsyncMock(return_value="SIM-1")
        result, _ = await ctrl.wait_for_element(
            WaitCondition.EXISTS, label="Home", element_type="RadioButton",
            udid="SIM-1", timeout=0.1,
        )
        assert result["matched"] is True
        assert result["matched_via"] == "RadioButton≈Button"


# ---------------------------------------------------------------------------
# Landmarks
# ---------------------------------------------------------------------------


class TestLandmarkMatching:
    def test_a_simulator_landmark_matches_the_wda_tree(self):
        lm = Landmark(element="RadioButton", label="Home", selected=True)
        assert match_landmark_via([_el("Button", "Home", value="1")], lm) == (
            True, "RadioButton≈Button",
        )

    def test_selection_still_applies_after_widening(self):
        lm = Landmark(element="RadioButton", label="Home", selected=True)
        assert match_landmark_via([_el("Button", "Home", value="0")], lm) == (False, None)

    def test_a_type_only_landmark_is_not_widened(self):
        lm = Landmark(element="RadioButton")
        assert match_landmark_via([_el("Button", "Home")], lm) == (False, None)

    def test_an_empty_label_pins_nothing(self):
        lm = Landmark(element="RadioButton", label="")
        assert match_landmark_via([_el("Button", "")], lm) == (False, None)

    def test_a_generic_type_needs_an_identifier(self):
        assert not match_landmark_via([_el("TabBar", "Tab Bar")],
                                      Landmark(element="Group", label="Tab Bar"))[0]
        assert match_landmark_via(
            [_el("TabBar", "", identifier="tabs")],
            Landmark(element="Group", identifier="tabs"),
        ) == (True, "Group≈TabBar")

    def test_exact_is_preferred_when_both_are_present(self):
        lm = Landmark(element="Button", label="Home")
        els = [_el("RadioButton", "Home"), _el("Button", "Home")]
        assert match_landmark_via(els, lm) == (True, None)

    def test_an_absent_landmark_sees_the_equivalent_as_present(self):
        lm = Landmark(element="RadioButton", label="Home", absent=True)
        assert match_landmark_via([_el("Button", "Home")], lm) == (False, None)

    def test_identification_reports_the_equivalence_per_landmark(self):
        screens = [ScreenLandmarks(screen="Home", landmarks=[
            Landmark(element="RadioButton", label="Home"),
            Landmark(element="StaticText", label="Welcome"),
        ])]
        result = identify_screen([_el("Button", "Home"), _el("StaticText", "Welcome")], screens)
        assert result["matched"] == "Home"
        vias = [r.get("matched_via") for r in result["matched_landmarks"]]
        assert vias == ["RadioButton≈Button", None]


class TestCollisions:
    def test_two_vocabularies_for_one_element_collide(self):
        a = ScreenLandmarks(screen="A", landmarks=[Landmark(element="RadioButton", label="Home")])
        b = ScreenLandmarks(screen="B", landmarks=[
            Landmark(element="Button", label="Home"), Landmark(element="StaticText", label="x"),
        ])
        result = detect_collisions([a, b])
        assert [c["screens"] for c in result["collisions"]] == [["A", "B"]]

    def test_a_label_does_not_make_generic_types_collide(self):
        a = ScreenLandmarks(screen="A", landmarks=[Landmark(element="Group", label="Bar")])
        b = ScreenLandmarks(screen="B", landmarks=[Landmark(element="TabBar", label="Bar")])
        assert detect_collisions([a, b])["collisions"] == []

    def test_a_url_landmark_does_not_crash_validation(self):
        """`_landmark_key` called `.lower()` on a URL landmark's None element."""
        a = ScreenLandmarks(screen="A", landmarks=[Landmark(web_url_contains="/login")])
        b = ScreenLandmarks(screen="B", landmarks=[Landmark(web_url_contains="/LOGIN")])
        result = detect_collisions([a, b])
        assert result["collisions"][0]["shared"] == ["url~/login"]


# ---------------------------------------------------------------------------
# Conventions
# ---------------------------------------------------------------------------


def _codes(entry) -> list[str]:
    return [f["code"] for f in entry.findings]


class TestConventionChecks:
    def test_a_type_only_paired_landmark_is_flagged(self):
        entry = check_conventions("f", "s", 2, [Landmark(element="RadioButton")])
        assert _codes(entry) == ["needs_identifier_or_label"]
        assert "Button" in entry.findings[0]["message"]

    def test_a_labelled_paired_landmark_is_portable(self):
        entry = check_conventions("f", "s", 2, [Landmark(element="Button", label="OK")])
        assert entry.findings == [] and entry.state == "current"

    def test_a_generic_type_needs_an_identifier_even_with_a_label(self):
        entry = check_conventions("f", "s", 2, [Landmark(element="Group", label="Bar")])
        assert _codes(entry) == ["needs_identifier"]

    def test_a_generic_type_with_an_identifier_is_portable(self):
        entry = check_conventions("f", "s", 2, [Landmark(element="Group", identifier="tabs")])
        assert entry.findings == []

    def test_a_type_with_no_counterpart_is_flagged_even_with_an_identifier(self):
        lm = Landmark(element="NavigationBar", identifier="Home")
        entry = check_conventions("f", "s", 2, [lm])
        assert _codes(entry) == ["no_portable_counterpart"]

    def test_unpaired_types_and_url_landmarks_pass(self):
        entry = check_conventions("f", "s", 2, [
            Landmark(element="Image"), Landmark(web_url_contains="/x"),
        ])
        assert entry.findings == []

    def test_the_finding_names_the_landmark(self):
        entry = check_conventions("f", "s", 2, [Landmark(element="Group", label="Bar")])
        assert entry.findings[0]["landmark"] == {"element": "Group", "label": "Bar"}


class TestConventionStates:
    @pytest.mark.parametrize(("declared", "state"), [
        (None, "undeclared"), (1, "behind"), (2, "current"), (3, "newer"),
    ])
    def test_states_for_a_clean_file(self, declared, state):
        assert check_conventions("f", "s", declared, []).state == state

    def test_current_with_findings_is_failing(self):
        entry = check_conventions("f", "s", 2, [Landmark(element="RadioButton")])
        assert entry.state == "failing"

    def test_older_and_undeclared_files_still_get_findings(self):
        for declared in (None, 1):
            entry = check_conventions("f", "s", declared, [Landmark(element="RadioButton")])
            assert _codes(entry) == ["needs_identifier_or_label"]

    @pytest.mark.parametrize("raw", [True, "2", 0, -1, 2.0])
    def test_an_unusable_declaration_reads_as_undeclared_and_says_so(self, raw):
        entry = check_conventions("f", "s", raw, [])
        assert entry.declared is None and entry.state == "undeclared"
        assert _codes(entry) == ["invalid_declaration"]

    def test_the_report_counts_every_file_and_lists_the_rest(self):
        report = conventions_report([
            check_conventions("ok.md", "ok", 2, []),
            check_conventions("old.md", "old", None, []),
        ])
        assert report["current_version"] == CURRENT_LANDMARK_CONVENTIONS
        assert report["counts"] == {"current": 1, "failing": 0, "behind": 0, "undeclared": 1}
        assert [f["file"] for f in report["files"]] == ["old.md"]
        assert "how_to_migrate" in report

    def test_an_all_current_report_has_nothing_to_migrate(self):
        report = conventions_report([check_conventions("ok.md", "ok", 2, [])])
        assert report["files"] == [] and "how_to_migrate" not in report


def _write(dir, name, body):
    (dir / name).write_text(textwrap.dedent(body).lstrip())


@pytest.fixture
def kb(tmp_path):
    screens = tmp_path / "screens"
    screens.mkdir()
    _write(screens, "home.md", """
        ---
        screen: home
        landmark_conventions: 2
        landmarks:
          - element: RadioButton
            label: Home
        ---
    """)
    _write(screens, "list.md", """
        ---
        screen: list
        landmarks:
          - element: Group
            label: Toolbar
        ---
    """)
    _write(screens, "legacy.md", """
        ---
        screen: legacy
        landmark_conventions: 2
        identify_by:
          - element: Button
        ---
    """)
    _write(screens, "_template.md", """
        ---
        screen: template
        ---
    """)
    return tmp_path


class TestScanning:
    def test_every_parsed_file_is_checked(self, kb):
        scan = scan_knowledge_base(kb)
        by_file = {c.file: c for c in scan.conventions}
        assert set(by_file) == {"screens/home.md", "screens/list.md", "screens/legacy.md"}
        assert by_file["screens/home.md"].state == "current"
        assert by_file["screens/list.md"].state == "undeclared"
        assert _codes(by_file["screens/list.md"]) == ["needs_identifier"]
        # identify_by is folded in: the file still loads no screen.
        assert by_file["screens/legacy.md"].state == "failing"
        assert _codes(by_file["screens/legacy.md"]) == ["legacy_format"]

    def test_a_reload_replaces_the_previous_findings(self, kb):
        registry = LandmarkRegistry()
        registry.load_from_path("app", str(kb))
        assert len(registry.conventions("app")) == 3
        registry.load("app", [], [])
        assert registry.conventions("app") == []

    def test_unload_forgets_them(self, kb):
        registry = LandmarkRegistry()
        registry.load_from_path("app", str(kb))
        registry.unload("app")
        assert registry.conventions() == []

    def test_a_load_of_only_legacy_files_still_explains_itself(self, tmp_path):
        """Zero screens load, so validate takes its no-landmarks branch -- and
        the conventions are the only thing that says why."""
        (tmp_path / "screens").mkdir()
        _write(tmp_path / "screens", "old.md", """
            ---
            identify_by:
              - element: Button
            ---
        """)
        registry = LandmarkRegistry()
        registry.load_from_path("app", str(tmp_path))
        result = registry.validate("app")
        assert result["error"] == "no_landmarks_loaded"
        assert result["conventions"]["files"][0]["findings"][0]["code"] == "legacy_format"

    def test_validating_the_loaded_set_reports_them(self, kb):
        registry = LandmarkRegistry()
        registry.load_from_path("app", str(kb))
        report = registry.validate("app")["conventions"]
        assert report["counts"]["current"] == 1


# ---------------------------------------------------------------------------
# The API surfaces
# ---------------------------------------------------------------------------


@pytest.fixture
async def client():
    app = create_app(
        config=ServerConfig(api_key="k"), enable_oslog=False, enable_crash=False,
        enable_proxy=False,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        headers={"Authorization": "Bearer k"},
    ) as c:
        yield c


class TestApi:
    async def test_load_from_a_path_reports_conventions(self, client, kb):
        r = await client.post("/api/v1/landmarks/load", json={"app": "a", "source": str(kb)})
        report = r.json()["conventions"]
        assert report["counts"] == {"current": 1, "failing": 1, "behind": 0, "undeclared": 1}

    async def test_inline_load_can_declare_a_target(self, client):
        r = await client.post("/api/v1/landmarks/load", json={"app": "a", "landmarks": {
            "Home": {"landmark_conventions": 2, "landmarks": [{"element": "RadioButton"}]},
            "Other": [{"element": "Button", "label": "OK"}],
        }})
        report = r.json()["conventions"]
        assert {f["file"]: f["state"] for f in report["files"]} == {
            "inline:Home": "failing", "inline:Other": "undeclared",
        }

    @pytest.mark.parametrize(("screens", "fragment"), [
        ({"Bad": [{"label": "no element"}]}, "landmark 0: "),
        ({"Bad": [{"web_url_contains": "/x", "label": "both"}]}, "cannot be combined"),
        ({"Bad": {"landmarks": {"element": "Button"}}}, "must be a list"),
        ({"Bad": {"landmarks": ["Button"]}}, "must be an object"),
    ])
    async def test_an_invalid_inline_landmark_is_a_400_naming_it(self, client, screens, fragment):
        """It was a bare 500, hidden over MCP only while the schema there
        required `element` -- which URL landmarks cannot have."""
        r = await client.post("/api/v1/landmarks/load", json={"app": "a", "landmarks": screens})
        assert r.status_code == 400
        assert "'Bad'" in r.json()["detail"] and fragment in r.json()["detail"]

    async def test_an_invalid_screen_loads_nothing(self, client):
        await client.post("/api/v1/landmarks/load", json={"app": "a", "landmarks": {
            "Good": [{"element": "Button", "label": "OK"}], "Bad": [{"label": "x"}],
        }})
        assert (await client.get("/api/v1/landmarks/")).json()["sets"] == {}

    async def test_validate_a_path_reports_conventions(self, client, kb):
        r = await client.post("/api/v1/landmarks/validate", params={"source": str(kb)})
        assert r.json()["conventions"]["counts"]["undeclared"] == 1

    async def test_validate_a_path_of_only_legacy_files_still_explains(self, client, tmp_path):
        (tmp_path / "screens").mkdir()
        _write(tmp_path / "screens", "old.md", """
            ---
            identify_by:
              - element: Button
            ---
        """)
        r = await client.post("/api/v1/landmarks/validate", params={"source": str(tmp_path)})
        body = r.json()
        assert body["error"] == "no_screens_found"
        assert body["conventions"]["files"][0]["findings"][0]["code"] == "legacy_format"


def test_portability_finding_is_none_for_a_pinned_pair():
    assert portability_finding("TextField", identifier="email", label=None) is None


# ---------------------------------------------------------------------------
# From the independent review of the branch
# ---------------------------------------------------------------------------


class TestAnIdentifierThatRepeatsTheLabelIsALabel:
    """WDA reports `name`: the identifier, or the label when there is none.
    Measured on a simulator: `name == 'More'` matches the More tab, which has
    no identifier, and the full tree reports the title `StaticText "Text"` with
    identifier "Text". Treating that as an identifier widened `Button` to the
    rows family and landed on the title -- the label match §1.3 forbids."""

    def test_filter_does_not_reach_the_family_through_a_label_in_disguise(self):
        title = _el("StaticText", "Settings", identifier="Settings")
        assert find_element([title], identifier="Settings", element_type="Button") == []

    def test_a_real_identifier_still_earns_the_family(self):
        row = _el("Cell", "Detail row", identifier="lists_row_2")
        assert find_element([row], identifier="lists_row_2", element_type="Button") == [row]

    def test_the_safe_pair_still_holds(self):
        tab = _el("Button", "More", identifier="More")
        assert find_element([tab], identifier="More", element_type="RadioButton") == [tab]

    def test_landmarks_follow_the_same_rule(self):
        lm = Landmark(element="Button", identifier="Settings")
        assert match_landmark_via([_el("StaticText", "Settings", identifier="Settings")], lm) == (
            False, None,
        )
        assert match_landmark_via([_el("Cell", "Row", identifier="Settings")], lm) == (
            True, "Button≈Cell",
        )


class TestTheScrollSweepPassesTheTypeRule:
    """The sweep finds by label or identifier alone, and its result replaced
    the matches unchecked: a Button request tapped a StaticText, and
    matched_via reported the forbidden pairing as an equivalence."""

    @staticmethod
    def _ctrl(swept: UIElement):
        from unittest.mock import MagicMock

        ctrl = DeviceController()
        ctrl.resolve_udid = AsyncMock(return_value="SIM-1")
        ctrl._warn_if_input_is_suppressed = AsyncMock()
        ctrl._is_android = lambda udid: False
        ctrl.get_ui_elements = AsyncMock(return_value=([], "SIM-1"))
        ctrl._ios_scroll_to_element = AsyncMock(return_value=swept)
        ctrl._identify_for_miss = AsyncMock(return_value={})
        backend = MagicMock()
        backend.tap = AsyncMock()
        ctrl._ui_backend = lambda udid: backend
        return ctrl, backend

    async def test_a_swept_element_of_a_forbidden_type_is_not_tapped(self, monkeypatch):
        import server.device.controller_ui as cui

        monkeypatch.setattr(cui, "_capture_screenshot", AsyncMock(return_value=None))
        ctrl, backend = self._ctrl(_el("StaticText", "Wi-Fi"))
        result = await ctrl.tap_element(
            label="Wi-Fi", element_type="Button", udid="SIM-1",
            scroll_to_find=True, skip_stability_check=True,
        )
        assert result["status"] == "not_found" and "matched_via" not in result
        assert result["scroll"]["attempted"] is True
        ctrl._ios_scroll_to_element.assert_awaited_once()
        backend.tap.assert_not_called()

    async def test_a_swept_equivalent_is_tapped_and_reported(self, monkeypatch):
        import server.device.controller_ui as cui

        monkeypatch.setattr(cui, "_capture_screenshot", AsyncMock(return_value=None))
        ctrl, backend = self._ctrl(_el("Button", "Wi-Fi"))
        result = await ctrl.tap_element(
            label="Wi-Fi", element_type="RadioButton", udid="SIM-1",
            scroll_to_find=True, skip_stability_check=True,
        )
        assert result["status"] == "ok"
        assert result["matched_via"] == "RadioButton≈Button"
        backend.tap.assert_awaited_once()


class TestTapElementReportsTheEquivalence:
    async def test_main_path(self):
        from unittest.mock import MagicMock

        ctrl = DeviceController()
        ctrl.resolve_udid = AsyncMock(return_value="SIM-1")
        ctrl._warn_if_input_is_suppressed = AsyncMock()
        ctrl._is_android = lambda udid: False
        ctrl.get_ui_elements = AsyncMock(return_value=([_el("Button", "Home")], "SIM-1"))
        backend = MagicMock()
        backend.tap = AsyncMock()
        ctrl._ui_backend = lambda udid: backend
        result = await ctrl.tap_element(
            label="Home", element_type="RadioButton", udid="SIM-1", skip_stability_check=True,
        )
        assert result["status"] == "ok"
        assert result["matched_via"] == "RadioButton≈Button"


class TestTextFieldsIncludeWdasTextView:
    def test_text_view_is_a_text_field(self):
        ctrl = DeviceController()
        notes = _el("TextView", "Notes")
        assert ctrl._matching_fields([notes], "Notes", None) == [notes]

    def test_the_comparison_ignores_case(self):
        ctrl = DeviceController()
        field = _el("textField", "Email")
        assert ctrl._matching_fields([field], "Email", None) == [field]


class TestEveryReadPathKeepsTheEquivalent:
    async def test_cold_read_filtered_by_type_alone(self):
        from unittest.mock import MagicMock

        ctrl = DeviceController()
        ctrl.resolve_udid = AsyncMock(return_value="SIM-1")
        ctrl._is_android = lambda udid: False
        ctrl._served_by_wda = lambda udid: False
        backend = MagicMock()
        backend.describe_all = AsyncMock(return_value=[
            {"type": "Button", "AXLabel": "Home tab"}, {"type": "Slider", "AXLabel": "x"},
        ])
        ctrl._ui_backend = lambda udid: backend
        # use_cache=True with a cold cache: the full tree is parsed, cached,
        # and then filtered in memory -- the post-fetch path.
        elements, _ = await ctrl.get_ui_elements("SIM-1", filter_type="RadioButton")
        assert [e.type for e in elements] == ["Button"]

    def test_the_web_overlay_merge(self):
        ctrl = DeviceController()
        ctrl._web_overlay["SIM-1"] = ([_el("Button", "Submit")], time.time())
        merged = ctrl._merge_web_overlay("SIM-1", [], filter_type="RadioButton")
        assert [e.type for e in merged] == ["Button"]

    def test_narrowing_after_a_label_also_widens_as_a_prefilter(self):
        els = [_el("Cell", "Row", identifier="r1")]
        assert find_element(els, label="Row", element_type="Button", prefilter=True) == els

    async def test_the_predicate_retry_keeps_the_identifier(self):
        ctrl = DeviceController()
        ctrl._active_udid = "PHYS-0001"
        ctrl._device_type_cache["PHYS-0001"] = DeviceType.DEVICE
        ctrl.wda_client.find_elements_by_query = AsyncMock(side_effect=[[], [{
            "type": "Button", "AXUniqueId": "", "AXLabel": "Controls",
            "frame": {"x": 0, "y": 0, "width": 10, "height": 10},
        }]])
        elements, _ = await ctrl._wda_direct_query("PHYS-0001", identifier="tab_controls")
        assert [e.identifier for e in elements] == ["tab_controls"]


class TestLandmarkGuardsTheReviewFoundUncovered:
    def test_label_contains_earns_the_pair(self):
        lm = Landmark(element="RadioButton", label_contains="Hom")
        assert match_landmark_via([_el("Button", "Home")], lm) == (True, "RadioButton≈Button")

    def test_an_absent_landmark_reports_no_equivalence(self):
        lm = Landmark(element="RadioButton", label="Gone", absent=True)
        assert match_landmark_via([_el("Button", "Home")], lm) == (True, None)

    @pytest.mark.parametrize("other", [
        Landmark(element="Button", identifier="x", label="Home"),
        Landmark(element="Button", label_contains="Home"),
        Landmark(element="RadioButton", label_contains="Set"),
        Landmark(element="Button", label="Home", absent=True),
    ])
    def test_selectors_differing_in_another_field_do_not_collide(self, other):
        mine = (
            Landmark(element="RadioButton", label_contains="Hom")
            if other.label_contains == "Set"
            else Landmark(element="RadioButton", label="Home")
        )
        a = ScreenLandmarks(screen="A", landmarks=[mine])
        b = ScreenLandmarks(screen="B", landmarks=[other])
        assert detect_collisions([a, b])["collisions"] == []

    def test_different_urls_do_not_collide(self):
        a = ScreenLandmarks(screen="A", landmarks=[Landmark(web_url_contains="/a")])
        b = ScreenLandmarks(screen="B", landmarks=[Landmark(web_url_contains="/b")])
        assert detect_collisions([a, b])["collisions"] == []

    def test_the_subset_can_be_either_side(self):
        big = ScreenLandmarks(screen="A", landmarks=[
            Landmark(element="Button", label="Home"), Landmark(element="StaticText", label="x"),
        ])
        small = ScreenLandmarks(
            screen="B", landmarks=[Landmark(element="RadioButton", label="Home")],
        )
        assert detect_collisions([big, small])["collisions"]

    def test_label_contains_counts_as_a_label_in_the_conventions_check(self):
        lm = Landmark(element="RadioButton", label_contains="Ho")
        assert check_conventions("f", "s", 2, [lm]).findings == []


class TestAFileThatLoadsNothingIsNeverCurrent:
    def test_all_landmarks_invalid(self, tmp_path):
        (tmp_path / "screens").mkdir()
        _write(tmp_path / "screens", "typo.md", """
            ---
            screen: typo
            landmark_conventions: 2
            landmarks:
              - elemnt: Button
                label: OK
            ---
        """)
        entry = scan_knowledge_base(tmp_path).conventions[0]
        assert entry.state == "failing"
        assert _codes(entry) == ["invalid_entries", "invalid_landmark"]

    def test_a_stub(self, tmp_path):
        (tmp_path / "screens").mkdir()
        _write(tmp_path / "screens", "stub.md", """
            ---
            screen: stub
            landmark_conventions: 2
            ---
        """)
        entry = scan_knowledge_base(tmp_path).conventions[0]
        assert entry.state == "failing" and _codes(entry) == ["no_landmarks"]

    def test_a_dropped_entry_beside_valid_ones_is_reported(self, tmp_path):
        (tmp_path / "screens").mkdir()
        _write(tmp_path / "screens", "partly.md", """
            ---
            screen: partly
            landmark_conventions: 2
            landmarks:
              - {element: Button, label: OK}
              - {elemnt: Button, label: Cancel}
              - just a string
            ---
        """)
        entry = scan_knowledge_base(tmp_path).conventions[0]
        assert entry.state == "failing"
        assert _codes(entry) == ["invalid_landmark", "invalid_landmark"]
        assert "Landmark 1 was ignored" in entry.findings[0]["message"]
        assert entry.findings[1]["landmark"] == "'just a string'"

    def test_an_unreadable_file_is_counted(self, tmp_path):
        (tmp_path / "screens").mkdir()
        _write(tmp_path / "screens", "ok.md", """
            ---
            screen: ok
            landmarks:
              - {element: Button, label: OK}
            ---
        """)
        (tmp_path / "screens" / "broken.md").write_text("---\nscreen: [\n---\n")
        (tmp_path / "screens" / "bare.md").write_text("no frontmatter here\n")
        report = conventions_report(scan_knowledge_base(tmp_path).conventions)
        assert sum(report["counts"].values()) == 3
        unread = {f["file"]: f["findings"][0]["code"] for f in report["files"] if "findings" in f}
        assert unread == {"screens/broken.md": "yaml_error", "screens/bare.md": "no_frontmatter"}


class TestSeveralApps:
    @staticmethod
    def _two(tmp_path):
        for app in ("one", "two"):
            (tmp_path / app / "screens").mkdir(parents=True)
            _write(tmp_path / app / "screens", "home.md", """
                ---
                screen: home
                landmarks:
                  - {element: Button, label: OK}
                ---
            """)
        registry = LandmarkRegistry()
        registry.load_from_path("one", str(tmp_path / "one"))
        registry.load_from_path("two", str(tmp_path / "two"))
        return registry

    def test_entries_name_their_app(self, tmp_path):
        files = self._two(tmp_path).validate()["conventions"]["files"]
        assert sorted(f["app"] for f in files) == ["one", "two"]

    def test_validate_for_one_app_reports_only_that_app(self, tmp_path):
        files = self._two(tmp_path).validate("one")["conventions"]["files"]
        assert [f["app"] for f in files] == ["one"]

    def test_unloading_everything_forgets_every_app(self, tmp_path):
        registry = self._two(tmp_path)
        registry.unload()
        assert registry.conventions() == []


class TestInlineObjectForm:
    async def test_missing_landmarks_is_refused(self, client):
        r = await client.post("/api/v1/landmarks/load", json={"app": "a", "landmarks": {
            "Home": {"landmark_conventions": 2, "scrollable": True},
        }})
        assert r.status_code == 400 and "'Home'" in r.json()["detail"]

    async def test_an_empty_screen_is_not_current(self, client):
        r = await client.post("/api/v1/landmarks/load", json={"app": "a", "landmarks": {
            "Home": {"landmark_conventions": 2, "landmarks": []},
        }})
        files = r.json()["conventions"]["files"]
        assert [(f["state"], f["findings"][0]["code"]) for f in files] == [
            ("failing", "no_landmarks"),
        ]
