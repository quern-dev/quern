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
from server.device.landmarks import (
    CURRENT_LANDMARK_CONVENTIONS,
    LandmarkRegistry,
    check_conventions,
    conventions_report,
    detect_collisions,
    identify_screen,
    match_landmark_via,
    scan_knowledge_base,
)
from server.device.ui_elements import find_element, parse_elements
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
