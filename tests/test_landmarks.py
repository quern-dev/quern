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
        assert result is not None
        assert result.screen == "Login"
        assert len(result.landmarks) == 2
        assert result.landmarks[0].element == "navigationBar"
        assert result.landmarks[1].identifier == "email_field"

    def test_no_landmarks_field(self, tmp_path):
        md = tmp_path / "home.md"
        md.write_text(textwrap.dedent("""\
            ---
            screen: "Home"
            status: documented
            ---

            # Home
        """))
        assert parse_screen_landmarks(md) is None

    def test_empty_landmarks(self, tmp_path):
        md = tmp_path / "empty.md"
        md.write_text(textwrap.dedent("""\
            ---
            screen: "Empty"
            landmarks: []
            ---

            # Empty
        """))
        assert parse_screen_landmarks(md) is None

    def test_no_frontmatter(self, tmp_path):
        md = tmp_path / "plain.md"
        md.write_text("# Just a markdown file\n\nNo frontmatter here.\n")
        assert parse_screen_landmarks(md) is None

    def test_malformed_yaml(self, tmp_path):
        md = tmp_path / "bad.md"
        md.write_text("---\n[invalid yaml: {{{\n---\n")
        assert parse_screen_landmarks(md) is None

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
        assert result is not None
        assert result.screen == "settings"

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
        assert result is not None
        assert result.landmarks[1].absent is True


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

        results = scan_knowledge_base(tmp_path)
        assert len(results) == 2
        names = {s.screen for s in results}
        assert names == {"Login", "Home"}

    def test_scan_empty_dir(self, tmp_path):
        (tmp_path / "screens").mkdir()
        assert scan_knowledge_base(tmp_path) == []

    def test_scan_no_screens_dir(self, tmp_path):
        assert scan_knowledge_base(tmp_path) == []


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
        count = registry.load_from_path("com.example", str(tmp_path))
        assert count == 1
        assert registry.list_sets() == {"com.example": 1}
