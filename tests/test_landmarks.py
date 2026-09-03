"""Tests for screen landmark matching, registry, and parsing."""

from __future__ import annotations

import textwrap

from server.device.landmarks import (
    LandmarkRegistry,
    detect_collisions,
    identify_screen,
    match_landmark,
    match_landmarks,
    parse_screen_landmarks,
    scan_knowledge_base,
)
from server.models import Landmark, ScreenLandmarks, UIElement

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _el(
    type: str = "Button",
    label: str = "",
    identifier: str | None = None,
    value: str | None = None,
) -> UIElement:
    return UIElement(type=type, label=label, identifier=identifier, value=value)


# ---------------------------------------------------------------------------
# match_landmark
# ---------------------------------------------------------------------------


class TestMatchLandmark:
    def test_match_by_identifier(self):
        elements = [_el("Button", "OK", identifier="btn_ok")]
        lm = Landmark(element="Button", identifier="btn_ok")
        assert match_landmark(elements, lm) is True

    def test_no_match_identifier(self):
        elements = [_el("Button", "OK", identifier="btn_ok")]
        lm = Landmark(element="Button", identifier="btn_cancel")
        assert match_landmark(elements, lm) is False

    def test_match_by_label(self):
        elements = [_el("Button", "Sign In")]
        lm = Landmark(element="Button", label="Sign In")
        assert match_landmark(elements, lm) is True

    def test_label_case_insensitive(self):
        elements = [_el("Button", "Sign In")]
        lm = Landmark(element="Button", label="sign in")
        assert match_landmark(elements, lm) is True

    def test_match_by_type_only(self):
        """'A navigationBar exists' — type-only landmark."""
        elements = [_el("navigationBar", "Settings")]
        lm = Landmark(element="navigationBar")
        assert match_landmark(elements, lm) is True

    def test_type_case_insensitive(self):
        elements = [_el("NavigationBar", "Settings")]
        lm = Landmark(element="navigationbar")
        assert match_landmark(elements, lm) is True

    def test_match_identifier_and_type(self):
        elements = [_el("Button", "OK", identifier="btn_ok")]
        lm = Landmark(element="Button", identifier="btn_ok")
        assert match_landmark(elements, lm) is True

    def test_match_identifier_and_label(self):
        """Both identifier AND label must match on the same element."""
        elements = [_el("Button", "OK", identifier="btn_ok")]
        lm = Landmark(element="Button", identifier="btn_ok", label="OK")
        assert match_landmark(elements, lm) is True

    def test_fail_identifier_and_label_mismatch(self):
        """Identifier matches but label doesn't — should fail."""
        elements = [_el("Button", "Cancel", identifier="btn_ok")]
        lm = Landmark(element="Button", identifier="btn_ok", label="OK")
        assert match_landmark(elements, lm) is False

    def test_absent_element_not_present(self):
        elements = [_el("Button", "OK")]
        lm = Landmark(element="TextField", absent=True)
        assert match_landmark(elements, lm) is True

    def test_absent_element_present(self):
        elements = [_el("TextField", "Email")]
        lm = Landmark(element="TextField", absent=True)
        assert match_landmark(elements, lm) is False

    def test_label_contains(self):
        elements = [_el("staticText", "Welcome to Geocaching")]
        lm = Landmark(element="staticText", label_contains="Geocaching")
        assert match_landmark(elements, lm) is True

    def test_label_contains_no_match(self):
        elements = [_el("staticText", "Welcome to Maps")]
        lm = Landmark(element="staticText", label_contains="Geocaching")
        assert match_landmark(elements, lm) is False

    def test_no_match_wrong_type(self):
        elements = [_el("Button", "OK", identifier="btn_ok")]
        lm = Landmark(element="TextField", identifier="btn_ok")
        assert match_landmark(elements, lm) is False

    def test_empty_elements(self):
        lm = Landmark(element="Button", identifier="anything")
        assert match_landmark([], lm) is False

    def test_absent_on_empty_elements(self):
        lm = Landmark(element="Button", absent=True)
        assert match_landmark([], lm) is True

    # selected: true / false — for tabs, switches, radios, checkboxes.
    # Both backends serialize selection state as UIElement.value "1"/"0".

    def test_selected_true_matches_value_1(self):
        elements = [
            _el("RadioButton", identifier="tab.timelines", value="1"),
            _el("RadioButton", identifier="tab.explore", value="0"),
        ]
        lm = Landmark(element="RadioButton", identifier="tab.timelines", selected=True)
        assert match_landmark(elements, lm) is True

    def test_selected_true_rejects_unselected(self):
        elements = [_el("RadioButton", identifier="tab.timelines", value="0")]
        lm = Landmark(element="RadioButton", identifier="tab.timelines", selected=True)
        assert match_landmark(elements, lm) is False

    def test_selected_false_matches_unselected(self):
        elements = [_el("RadioButton", identifier="tab.timelines", value="0")]
        lm = Landmark(element="RadioButton", identifier="tab.timelines", selected=False)
        assert match_landmark(elements, lm) is True

    def test_selected_false_rejects_selected(self):
        elements = [_el("RadioButton", identifier="tab.timelines", value="1")]
        lm = Landmark(element="RadioButton", identifier="tab.timelines", selected=False)
        assert match_landmark(elements, lm) is False

    def test_selected_disambiguates_two_radios_with_same_id(self):
        """The motivating use case: tabs that all expose tab.* identifiers
        but only one is selected. Without `selected`, all four tabs would
        match every screen's tab landmarks — defeating identification."""
        elements = [
            _el("RadioButton", identifier="tab.timelines", value="1"),
            _el("RadioButton", identifier="tab.explore", value="0"),
            _el("RadioButton", identifier="tab.notifications", value="0"),
            _el("RadioButton", identifier="tab.messages", value="0"),
        ]
        # Authored for the timelines screen
        lm = Landmark(element="RadioButton", identifier="tab.timelines", selected=True)
        assert match_landmark(elements, lm) is True
        # Authored for the explore screen — should fail on this UI tree
        lm_wrong = Landmark(element="RadioButton", identifier="tab.explore", selected=True)
        assert match_landmark(elements, lm_wrong) is False

    def test_selected_omitted_ignores_state(self):
        """Backwards-compatible: existing landmarks without `selected` set
        should match regardless of value."""
        elements = [_el("RadioButton", identifier="tab.timelines", value="0")]
        lm = Landmark(element="RadioButton", identifier="tab.timelines")
        assert match_landmark(elements, lm) is True


# ---------------------------------------------------------------------------
# match_landmarks
# ---------------------------------------------------------------------------


class TestMatchLandmarks:
    def test_all_match(self):
        elements = [
            _el("navigationBar", "Settings"),
            _el("Button", "Account", identifier="account_btn"),
        ]
        landmarks = [
            Landmark(element="navigationBar", label="Settings"),
            Landmark(element="Button", identifier="account_btn"),
        ]
        matched, results = match_landmarks(elements, landmarks)
        assert matched is True
        assert all(r["matched"] for r in results)

    def test_one_fails(self):
        elements = [_el("navigationBar", "Settings")]
        landmarks = [
            Landmark(element="navigationBar", label="Settings"),
            Landmark(element="Button", identifier="missing_btn"),
        ]
        matched, results = match_landmarks(elements, landmarks)
        assert matched is False
        assert results[0]["matched"] is True
        assert results[1]["matched"] is False

    def test_empty_landmarks(self):
        """Empty landmarks list is vacuously true."""
        matched, results = match_landmarks([_el("Button", "OK")], [])
        assert matched is True
        assert results == []


# ---------------------------------------------------------------------------
# identify_screen
# ---------------------------------------------------------------------------


class TestIdentifyScreen:
    def test_exact_match(self):
        elements = [
            _el("navigationBar", "Login"),
            _el("TextField", "Email", identifier="email_field"),
        ]
        screens = [
            ScreenLandmarks(
                screen="Login",
                landmarks=[
                    Landmark(element="navigationBar", label="Login"),
                    Landmark(element="TextField", identifier="email_field"),
                ],
            ),
            ScreenLandmarks(
                screen="Home",
                landmarks=[Landmark(element="navigationBar", label="Home")],
            ),
        ]
        result = identify_screen(elements, screens)
        assert result["matched"] == "Login"
        assert result["confidence"] == "exact"

    def test_ambiguous(self):
        elements = [_el("navigationBar", "Settings")]
        screens = [
            ScreenLandmarks(
                screen="Settings",
                landmarks=[Landmark(element="navigationBar", label="Settings")],
            ),
            ScreenLandmarks(
                screen="Settings Modal",
                landmarks=[Landmark(element="navigationBar", label="Settings")],
            ),
        ]
        result = identify_screen(elements, screens)
        assert result["confidence"] == "ambiguous"
        assert result["matched"] is not None
        assert "ambiguous_with" in result

    def test_no_match(self):
        elements = [_el("navigationBar", "Unknown")]
        screens = [
            ScreenLandmarks(
                screen="Login",
                landmarks=[Landmark(element="navigationBar", label="Login")],
            ),
        ]
        result = identify_screen(elements, screens)
        assert result["matched"] is None
        assert result["confidence"] == "none"

    def test_partial_matches_reported(self):
        elements = [
            _el("navigationBar", "Login"),
            _el("staticText", "Forgot Password?"),
        ]
        screens = [
            ScreenLandmarks(
                screen="Login",
                landmarks=[
                    Landmark(element="navigationBar", label="Login"),
                    Landmark(element="TextField", identifier="email_field"),
                ],
            ),
        ]
        result = identify_screen(elements, screens)
        assert result["matched"] is None
        assert len(result["partial_matches"]) == 1
        assert result["partial_matches"][0]["screen"] == "Login"
        assert result["partial_matches"][0]["matched"] == 1
        # Per-landmark detail is included so callers can debug without
        # a follow-up call.
        landmarks = result["partial_matches"][0]["landmarks"]
        assert len(landmarks) == 2
        # First landmark (navigationBar/Login) hit; second (TextField) didn't.
        assert landmarks[0]["matched"] is True
        assert landmarks[1]["matched"] is False

    def test_partial_matches_includes_zero_match_screens(self):
        """When ALL of a screen's landmarks miss, the screen should still
        appear in partial_matches — that's the case an agent most needs to
        debug ('I authored these landmarks, why didn't anything match?').
        Previously these were silently dropped."""
        elements = [_el("navigationBar", "Home")]
        screens = [
            ScreenLandmarks(
                screen="Login",
                landmarks=[
                    Landmark(element="Button", label="Sign In"),
                    Landmark(element="TextField", identifier="email"),
                ],
            ),
        ]
        result = identify_screen(elements, screens)
        assert result["matched"] is None
        assert len(result["partial_matches"]) == 1
        assert result["partial_matches"][0]["screen"] == "Login"
        assert result["partial_matches"][0]["matched"] == 0
        assert result["partial_matches"][0]["total"] == 2
        # Per-landmark results show both as not matched
        landmarks = result["partial_matches"][0]["landmarks"]
        assert all(lm["matched"] is False for lm in landmarks)

    def test_partial_matches_sorted_by_match_count_desc(self):
        """Best candidate should appear first so the most likely intended
        screen is easy to spot in a long list."""
        elements = [
            _el("navigationBar", "Home"),
            _el("Button", "Compose"),
        ]
        screens = [
            # 0 matches — should sort last
            ScreenLandmarks(
                screen="ZeroMatch",
                landmarks=[Landmark(element="navigationBar", label="Settings")],
            ),
            # 2 matches — should sort first
            ScreenLandmarks(
                screen="TwoMatches",
                landmarks=[
                    Landmark(element="navigationBar", label="Home"),
                    Landmark(element="Button", label="Compose"),
                    Landmark(element="TextField", identifier="search"),
                ],
            ),
            # 1 match — should sort middle
            ScreenLandmarks(
                screen="OneMatch",
                landmarks=[
                    Landmark(element="navigationBar", label="Home"),
                    Landmark(element="TextField", identifier="search"),
                ],
            ),
        ]
        result = identify_screen(elements, screens)
        names = [p["screen"] for p in result["partial_matches"]]
        assert names == ["TwoMatches", "OneMatch", "ZeroMatch"]

    def test_empty_screens(self):
        result = identify_screen([_el("Button", "OK")], [])
        assert result["matched"] is None
        assert result["confidence"] == "none"


# ---------------------------------------------------------------------------
# detect_collisions
# ---------------------------------------------------------------------------


class TestDetectCollisions:
    def test_identical_landmarks_collide(self):
        screens = [
            ScreenLandmarks(
                screen="A",
                landmarks=[Landmark(element="navigationBar", label="Settings")],
            ),
            ScreenLandmarks(
                screen="B",
                landmarks=[Landmark(element="navigationBar", label="Settings")],
            ),
        ]
        result = detect_collisions(screens)
        assert len(result["collisions"]) == 1
        assert set(result["collisions"][0]["screens"]) == {"A", "B"}

    def test_subset_collides(self):
        screens = [
            ScreenLandmarks(
                screen="A",
                landmarks=[Landmark(element="navigationBar", label="Settings")],
            ),
            ScreenLandmarks(
                screen="B",
                landmarks=[
                    Landmark(element="navigationBar", label="Settings"),
                    Landmark(element="Button", identifier="extra"),
                ],
            ),
        ]
        result = detect_collisions(screens)
        assert len(result["collisions"]) == 1

    def test_distinct_no_collision(self):
        screens = [
            ScreenLandmarks(
                screen="Login",
                landmarks=[Landmark(element="navigationBar", label="Login")],
            ),
            ScreenLandmarks(
                screen="Home",
                landmarks=[Landmark(element="navigationBar", label="Home")],
            ),
        ]
        result = detect_collisions(screens)
        assert result["collisions"] == []

    def test_no_landmarks_reported(self):
        screens = [
            ScreenLandmarks(screen="Empty", landmarks=[]),
            ScreenLandmarks(
                screen="OK",
                landmarks=[Landmark(element="Button", label="OK")],
            ),
        ]
        result = detect_collisions(screens)
        assert "Empty" in result["no_landmarks"]
        assert result["total_screens"] == 2


# ---------------------------------------------------------------------------
# YAML frontmatter parsing
# ---------------------------------------------------------------------------


class TestParseScreenLandmarks:
    def test_valid_landmarks(self, tmp_path):
        md = tmp_path / "login.md"
        md.write_text(textwrap.dedent("""\
            ---
            screen: "Login"
            status: documented
            landmarks:
              - { element: "navigationBar", label: "Login" }
              - { element: "TextField", identifier: "email_field" }
            ---

            # Login Screen
        """))
        result = parse_screen_landmarks(md)
        assert result.screen is not None
        assert result.skip is None
        assert result.screen.screen == "Login"
        assert len(result.screen.landmarks) == 2
        assert result.screen.landmarks[0].element == "navigationBar"
        assert result.screen.landmarks[1].identifier == "email_field"

    def test_no_landmarks_field(self, tmp_path):
        md = tmp_path / "home.md"
        md.write_text(textwrap.dedent("""\
            ---
            screen: "Home"
            status: documented
            ---

            # Home
        """))
        result = parse_screen_landmarks(md)
        assert result.screen is None
        assert result.skip is not None
        assert result.skip.reason == "no_landmarks"
        assert result.skip.screen == "Home"

    def test_empty_landmarks(self, tmp_path):
        md = tmp_path / "empty.md"
        md.write_text(textwrap.dedent("""\
            ---
            screen: "Empty"
            landmarks: []
            ---

            # Empty
        """))
        result = parse_screen_landmarks(md)
        assert result.screen is None
        assert result.skip is not None
        assert result.skip.reason == "no_landmarks"

    def test_legacy_format_preserves_freeform_entries(self, tmp_path):
        """Some legacy KBs use prose-style identify_by entries (strings, not
        dicts). The skip should preserve them verbatim so an agent can see
        what's there and reinterpret rather than silently zeroing them."""
        md = tmp_path / "account-settings.md"
        md.write_text(textwrap.dedent("""\
            ---
            screen: "account-settings"
            identify_by:
              - "SFSafariViewController showing server settings page"
            ---
        """))
        result = parse_screen_landmarks(md)
        assert result.skip is not None
        assert result.skip.reason == "legacy_format"
        assert result.skip.identify_by == [
            "SFSafariViewController showing server settings page",
        ]

    def test_legacy_format_with_identify_by(self, tmp_path):
        """File from a pre-landmarks knowledge base — has identify_by but no
        landmarks. The skip should categorize it as legacy_format and
        include the identify_by entries so an agent can propose a migration."""
        md = tmp_path / "timelines.md"
        md.write_text(textwrap.dedent("""\
            ---
            screen: "timelines"
            identify_by:
              - { element: "TabGroup", identifier: "timelines.segment-control" }
              - { element: "RadioButton", identifier: "tab.timelines", value: "1" }
            ---
        """))
        result = parse_screen_landmarks(md)
        assert result.screen is None
        assert result.skip is not None
        assert result.skip.reason == "legacy_format"
        assert result.skip.screen == "timelines"
        assert result.skip.identify_by is not None
        assert len(result.skip.identify_by) == 2
        assert result.skip.identify_by[1]["value"] == "1"

    def test_legacy_format_with_both_fields_uses_landmarks(self, tmp_path):
        """When both identify_by and landmarks are present (the documented
        coexistence pattern), landmarks wins — file is parsed normally,
        not flagged as legacy."""
        md = tmp_path / "settings.md"
        md.write_text(textwrap.dedent("""\
            ---
            screen: "Settings"
            landmarks:
              - { element: "navigationBar", label: "Settings" }
            identify_by:
              - { element: "navigationBar", label: "Settings" }
            ---
        """))
        result = parse_screen_landmarks(md)
        assert result.screen is not None
        assert result.skip is None

    def test_no_frontmatter(self, tmp_path):
        md = tmp_path / "plain.md"
        md.write_text("# Just a markdown file\n\nNo frontmatter here.\n")
        result = parse_screen_landmarks(md)
        assert result.screen is None
        assert result.skip is not None
        assert result.skip.reason == "no_frontmatter"

    def test_malformed_yaml(self, tmp_path):
        md = tmp_path / "bad.md"
        md.write_text("---\n[invalid yaml: {{{\n---\n")
        result = parse_screen_landmarks(md)
        assert result.screen is None
        assert result.skip is not None
        assert result.skip.reason == "yaml_error"
        assert result.skip.error  # error message populated

    def test_invalid_entries(self, tmp_path):
        """All landmark entries malformed (missing required 'element')."""
        md = tmp_path / "bogus.md"
        md.write_text(textwrap.dedent("""\
            ---
            screen: "Bogus"
            landmarks:
              - { label: "missing element type" }
              - { identifier: "also missing element" }
            ---
        """))
        result = parse_screen_landmarks(md)
        assert result.screen is None
        assert result.skip is not None
        assert result.skip.reason == "invalid_entries"

    def test_derives_screen_name_from_filename(self, tmp_path):
        md = tmp_path / "settings.md"
        md.write_text(textwrap.dedent("""\
            ---
            screen: ""
            landmarks:
              - { element: "navigationBar", identifier: "settings_nav" }
            ---
        """))
        result = parse_screen_landmarks(md)
        assert result.screen is not None
        assert result.screen.screen == "settings"

    def test_absent_landmark(self, tmp_path):
        md = tmp_path / "logged-out.md"
        md.write_text(textwrap.dedent("""\
            ---
            screen: "Logged Out"
            landmarks:
              - { element: "Button", label: "Sign In" }
              - { element: "tabBar", absent: true }
            ---
        """))
        result = parse_screen_landmarks(md)
        assert result.screen is not None
        assert result.screen.landmarks[1].absent is True

    def test_selected_landmark_parses(self, tmp_path):
        """The new `selected: true|false` field round-trips through YAML."""
        md = tmp_path / "timelines.md"
        md.write_text(textwrap.dedent("""\
            ---
            screen: "timelines"
            landmarks:
              - { element: "TabGroup", identifier: "timelines.segment-control" }
              - { element: "RadioButton", identifier: "tab.timelines", selected: true }
            ---
        """))
        result = parse_screen_landmarks(md)
        assert result.screen is not None
        assert result.screen.landmarks[1].selected is True


# ---------------------------------------------------------------------------
# scan_knowledge_base
# ---------------------------------------------------------------------------


class TestScanKnowledgeBase:
    def test_scan_finds_screens(self, tmp_path):
        screens_dir = tmp_path / "screens"
        screens_dir.mkdir()
        (screens_dir / "login.md").write_text(textwrap.dedent("""\
            ---
            screen: "Login"
            landmarks:
              - { element: "TextField", identifier: "email" }
            ---
        """))
        (screens_dir / "home.md").write_text(textwrap.dedent("""\
            ---
            screen: "Home"
            landmarks:
              - { element: "navigationBar", label: "Home" }
            ---
        """))
        # Template files should be skipped
        (screens_dir / "_template.md").write_text(textwrap.dedent("""\
            ---
            screen: ""
            landmarks:
              - { element: "", identifier: "" }
            ---
        """))

        scan = scan_knowledge_base(tmp_path)
        assert len(scan.screens) == 2
        names = {s.screen for s in scan.screens}
        assert names == {"Login", "Home"}
        assert scan.skipped == []

    def test_scan_empty_dir(self, tmp_path):
        (tmp_path / "screens").mkdir()
        scan = scan_knowledge_base(tmp_path)
        assert scan.screens == []
        assert scan.skipped == []

    def test_scan_no_screens_dir(self, tmp_path):
        scan = scan_knowledge_base(tmp_path)
        assert scan.screens == []
        assert scan.skipped == []

    def test_scan_categorizes_skipped_files(self, tmp_path):
        """Mixed knowledge base — some valid, some legacy, some stubs.
        The scan returns categorized skips alongside the parsed screens
        so agents and operators can act on the gaps."""
        screens_dir = tmp_path / "screens"
        screens_dir.mkdir()
        # Valid (new format)
        (screens_dir / "login.md").write_text(textwrap.dedent("""\
            ---
            screen: "Login"
            landmarks:
              - { element: "Button", label: "Sign In" }
            ---
        """))
        # Legacy (identify_by only — typical of pre-landmarks knowledge bases)
        (screens_dir / "timelines.md").write_text(textwrap.dedent("""\
            ---
            screen: "timelines"
            identify_by:
              - { element: "TabGroup", identifier: "timelines.segment-control" }
            ---
        """))
        # Stub (neither field)
        (screens_dir / "about.md").write_text(textwrap.dedent("""\
            ---
            screen: "About"
            status: stub
            ---
        """))

        scan = scan_knowledge_base(tmp_path)
        assert len(scan.screens) == 1
        assert scan.screens[0].screen == "Login"

        reasons = {s.reason: s for s in scan.skipped}
        assert set(reasons) == {"legacy_format", "no_landmarks"}
        assert reasons["legacy_format"].screen == "timelines"
        assert reasons["legacy_format"].identify_by is not None
        assert reasons["no_landmarks"].screen == "About"

    def test_scan_skipped_paths_are_relative(self, tmp_path):
        """The 'file' field on each skipped entry should be a relative path
        from the knowledge base root, not absolute or just the basename —
        agents can pass it to read/write tools without further resolution."""
        screens_dir = tmp_path / "screens"
        screens_dir.mkdir()
        (screens_dir / "stub.md").write_text(textwrap.dedent("""\
            ---
            screen: "Stub"
            ---
        """))
        scan = scan_knowledge_base(tmp_path)
        assert len(scan.skipped) == 1
        assert scan.skipped[0].file == "screens/stub.md"


# ---------------------------------------------------------------------------
# LandmarkRegistry
# ---------------------------------------------------------------------------


class TestLandmarkRegistry:
    def test_load_and_identify(self):
        registry = LandmarkRegistry()
        screens = [
            ScreenLandmarks(
                screen="Login",
                landmarks=[Landmark(element="navigationBar", label="Login")],
            ),
        ]
        registry.load("com.example.app", screens)

        elements = [_el("navigationBar", "Login")]
        result = registry.identify(elements, app="com.example.app")
        assert result["matched"] == "Login"
        assert result["confidence"] == "exact"

    def test_identify_no_landmarks_loaded(self):
        registry = LandmarkRegistry()
        result = registry.identify([_el("Button", "OK")])
        assert result["matched"] is None
        assert result["confidence"] == "none"
        assert result["error"] == "no_landmarks_loaded"

    def test_app_scoping(self):
        registry = LandmarkRegistry()
        registry.load("app1", [
            ScreenLandmarks(
                screen="Home",
                landmarks=[Landmark(element="navigationBar", label="Home")],
            ),
        ])
        registry.load("app2", [
            ScreenLandmarks(
                screen="Dashboard",
                landmarks=[Landmark(element="navigationBar", label="Home")],
            ),
        ])

        elements = [_el("navigationBar", "Home")]

        # Scoped to app1
        result = registry.identify(elements, app="app1")
        assert result["matched"] == "Home"

        # Scoped to app2
        result = registry.identify(elements, app="app2")
        assert result["matched"] == "Dashboard"

        # Unscoped — ambiguous
        result = registry.identify(elements)
        assert result["confidence"] == "ambiguous"

    def test_unload_specific_app(self):
        registry = LandmarkRegistry()
        registry.load("app1", [
            ScreenLandmarks(screen="A", landmarks=[Landmark(element="Button")]),
        ])
        registry.load("app2", [
            ScreenLandmarks(screen="B", landmarks=[Landmark(element="Button")]),
        ])
        registry.unload("app1")
        assert "app1" not in registry.list_sets()
        assert "app2" in registry.list_sets()

    def test_unload_all(self):
        registry = LandmarkRegistry()
        registry.load("app1", [
            ScreenLandmarks(screen="A", landmarks=[Landmark(element="Button")]),
        ])
        registry.unload()
        assert registry.is_empty

    def test_list_sets(self):
        registry = LandmarkRegistry()
        registry.load("app1", [
            ScreenLandmarks(screen="A", landmarks=[Landmark(element="Button")]),
            ScreenLandmarks(screen="B", landmarks=[Landmark(element="Button")]),
        ])
        assert registry.list_sets() == {"app1": 2}

    def test_validate(self):
        registry = LandmarkRegistry()
        registry.load("app", [
            ScreenLandmarks(
                screen="A",
                landmarks=[Landmark(element="navigationBar", label="X")],
            ),
            ScreenLandmarks(
                screen="B",
                landmarks=[Landmark(element="navigationBar", label="X")],
            ),
        ])
        result = registry.validate(app="app")
        assert len(result["collisions"]) == 1

    def test_load_from_path(self, tmp_path):
        screens_dir = tmp_path / "screens"
        screens_dir.mkdir()
        (screens_dir / "login.md").write_text(textwrap.dedent("""\
            ---
            screen: "Login"
            landmarks:
              - { element: "TextField", identifier: "email" }
            ---
        """))
        registry = LandmarkRegistry()
        count, skipped = registry.load_from_path("com.example", str(tmp_path))
        assert count == 1
        assert skipped == []
        assert registry.list_sets() == {"com.example": 1}

    def test_load_from_path_returns_skipped(self, tmp_path):
        """A knowledge base with mixed valid + legacy files should load the
        valid screens AND surface the legacy ones to the caller."""
        screens_dir = tmp_path / "screens"
        screens_dir.mkdir()
        (screens_dir / "login.md").write_text(textwrap.dedent("""\
            ---
            screen: "Login"
            landmarks:
              - { element: "Button", label: "Sign In" }
            ---
        """))
        (screens_dir / "legacy.md").write_text(textwrap.dedent("""\
            ---
            screen: "Legacy"
            identify_by:
              - { element: "navigationBar", label: "Legacy" }
            ---
        """))
        registry = LandmarkRegistry()
        count, skipped = registry.load_from_path("com.example", str(tmp_path))
        assert count == 1
        assert len(skipped) == 1
        assert skipped[0].reason == "legacy_format"
        assert skipped[0].screen == "Legacy"


# ------------------------------------------------- recorded web view facts

def _write(tmp_path, name, frontmatter):
    screens = tmp_path / "screens"
    screens.mkdir(exist_ok=True)
    (screens / name).write_text(f"---\n{frontmatter}\n---\n\nbody\n")
    return tmp_path


def test_web_content_is_read_from_a_screen_with_no_landmarks(tmp_path):
    """The screens that most need a recorded web view -- an OAuth view, a
    settings page behind SFSafariViewController -- are exactly the ones with no
    native identity. A hint reachable only through a successful landmark parse
    would never reach the cases it exists for."""
    from server.device.landmarks import scan_knowledge_base
    _write(tmp_path, "settings.md", '''screen: "settings"
landmarks: []
web_content:
  - host: "SFSafariViewController"
    process: "com.apple.SafariViewService"
    reachable_by: [inspector, hit_test]
    url: "https://example.test/settings"
    anchor:
      origin: [0, 106]
      viewport: [402, 685]''')
    scan = scan_knowledge_base(tmp_path)
    assert scan.screens == []                      # no landmarks, as expected
    assert len(scan.web_content) == 1
    hint = scan.web_content[0]
    assert hint.screen == "settings"
    assert hint.process == "com.apple.SafariViewService"
    assert hint.anchor.origin == [0, 106]
    assert "hit_test" in hint.reachable_by


def test_web_content_is_also_read_from_a_screen_that_has_landmarks(tmp_path):
    from server.device.landmarks import scan_knowledge_base
    _write(tmp_path, "picker.md", '''screen: "picker"
landmarks:
  - { element: "Button", label: "Done" }
web_content:
  - host: "WKWebView"
    url: "https://example.test/servers"''')
    scan = scan_knowledge_base(tmp_path)
    assert [s.screen for s in scan.screens] == ["picker"]
    assert [h.host for h in scan.web_content] == ["WKWebView"]


def test_a_malformed_web_content_entry_does_not_stop_the_load(tmp_path):
    """A hint is an optimisation and every value in it is verified before use;
    a bad one must never cost the whole knowledge base."""
    from server.device.landmarks import scan_knowledge_base
    _write(tmp_path, "broken.md", '''screen: "broken"
landmarks:
  - { element: "Button", label: "Done" }
web_content:
  - anchor: "not a mapping"
  - "a bare string"
  - host: "WKWebView"
    url: "https://ok.test/"''')
    scan = scan_knowledge_base(tmp_path)
    assert [s.screen for s in scan.screens] == ["broken"]
    assert [h.url for h in scan.web_content] == ["https://ok.test/"]


def test_unloading_an_app_forgets_its_web_content(tmp_path):
    from server.device.landmarks import LandmarkRegistry
    _write(tmp_path, "s.md", '''screen: "s"
landmarks: []
web_content:
  - url: "https://example.test/"''')
    registry = LandmarkRegistry()
    registry.load_from_path("App", str(tmp_path))
    assert len(registry.web_content("App")) == 1
    registry.unload("App")
    assert registry.web_content("App") == []


def test_a_web_content_entry_carrying_its_own_screen_key_is_skipped(tmp_path):
    """A duplicate keyword raises TypeError before Pydantic validates, which is
    not a ValidationError -- so one stray key would abort the whole scan."""
    from server.device.landmarks import scan_knowledge_base
    _write(tmp_path, "dup.md", '''screen: "dup"
landmarks:
  - { element: "Button", label: "Done" }
web_content:
  - screen: "somewhere-else"
    url: "https://example.test/"
  - url: "https://ok.test/"''')
    scan = scan_knowledge_base(tmp_path)
    assert [s.screen for s in scan.screens] == ["dup"]
    # The entry is kept, with the file's screen name winning over the stray key.
    assert [(h.screen, h.url) for h in scan.web_content] == [
        ("dup", "https://example.test/"), ("dup", "https://ok.test/"),
    ]
