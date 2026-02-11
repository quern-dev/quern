"""Tests for UI element parsing, search, and screen summary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from server.device.ui_elements import (
    find_by_identifier,
    find_by_label,
    find_by_type,
    generate_screen_summary,
    get_center,
    parse_elements,
)
from server.models import UIElement

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def raw_fixture():
    """Load the idb describe-all fixture data."""
    return json.loads((FIXTURES / "idb_describe_all_output.json").read_text())


@pytest.fixture
def elements(raw_fixture):
    """Parse fixture data into UIElement models."""
    return parse_elements(raw_fixture)


# ---------------------------------------------------------------------------
# parse_elements
# ---------------------------------------------------------------------------


class TestParseElements:
    def test_count(self, raw_fixture):
        elements = parse_elements(raw_fixture)
        assert len(elements) == 14

    def test_application_element(self, raw_fixture):
        elements = parse_elements(raw_fixture)
        app = elements[0]
        assert app.type == "Application"
        assert app.label == "Springboard"
        assert app.identifier is None
        assert app.role == "AXApplication"
        assert app.frame == {"x": 0, "y": 0, "width": 393, "height": 852}

    def test_button_element(self, raw_fixture):
        elements = parse_elements(raw_fixture)
        maps = elements[1]
        assert maps.type == "Button"
        assert maps.label == "Maps"
        assert maps.identifier == "Maps"
        assert maps.role == "AXButton"
        assert maps.help == "Double tap to open"
        assert maps.enabled is True

    def test_slider_element(self, raw_fixture):
        elements = parse_elements(raw_fixture)
        slider = elements[12]
        assert slider.type == "Slider"
        assert slider.label == "Search"
        assert slider.value == "Page 1 of 2"
        assert slider.identifier == "SearchSlider"

    def test_frame_parsing(self, raw_fixture):
        elements = parse_elements(raw_fixture)
        maps = elements[1]
        assert maps.frame["x"] == 27.0
        assert maps.frame["y"] == 382.0
        assert maps.frame["width"] == 68.0
        assert maps.frame["height"] == 86.0

    def test_null_fields(self, raw_fixture):
        elements = parse_elements(raw_fixture)
        app = elements[0]
        assert app.identifier is None
        assert app.value is None
        assert app.help is None

    def test_empty_input(self):
        assert parse_elements([]) == []

    def test_minimal_element(self):
        raw = [{"type": "Button"}]
        elements = parse_elements(raw)
        assert len(elements) == 1
        assert elements[0].type == "Button"
        assert elements[0].label == ""
        assert elements[0].identifier is None
        assert elements[0].frame is None

    def test_missing_type_defaults(self):
        raw = [{"AXLabel": "something"}]
        elements = parse_elements(raw)
        assert elements[0].type == "Unknown"

    def test_non_dict_frame_becomes_none(self):
        raw = [{"type": "X", "frame": "not a dict"}]
        elements = parse_elements(raw)
        assert elements[0].frame is None


# ---------------------------------------------------------------------------
# find_by_label
# ---------------------------------------------------------------------------


class TestFindByLabel:
    def test_single_match(self, elements):
        results = find_by_label(elements, "Maps")
        assert len(results) == 1
        assert results[0].label == "Maps"

    def test_multiple_matches(self, elements):
        results = find_by_label(elements, "Calendar")
        assert len(results) == 2
        assert all(r.label == "Calendar" for r in results)

    def test_case_insensitive(self, elements):
        results = find_by_label(elements, "maps")
        assert len(results) == 1
        assert results[0].label == "Maps"

    def test_no_match(self, elements):
        results = find_by_label(elements, "Nonexistent")
        assert results == []

    def test_no_substring_match(self, elements):
        """'Search' should NOT match element with label 'SearchSlider' — but
        in our fixture, the Slider's label is 'Search', so search for a
        partial to confirm no substring matching occurs."""
        results = find_by_label(elements, "Sear")
        assert results == []


# ---------------------------------------------------------------------------
# find_by_identifier
# ---------------------------------------------------------------------------


class TestFindByIdentifier:
    def test_exact_match(self, elements):
        results = find_by_identifier(elements, "Maps")
        assert len(results) == 1
        assert results[0].identifier == "Maps"

    def test_case_sensitive(self, elements):
        results = find_by_identifier(elements, "maps")
        assert results == []

    def test_no_match(self, elements):
        results = find_by_identifier(elements, "DoesNotExist")
        assert results == []

    def test_distinguishes_calendar_variants(self, elements):
        r1 = find_by_identifier(elements, "Calendar-1")
        r2 = find_by_identifier(elements, "Calendar-2")
        assert len(r1) == 1
        assert len(r2) == 1
        assert r1[0].identifier != r2[0].identifier


# ---------------------------------------------------------------------------
# find_by_type
# ---------------------------------------------------------------------------


class TestFindByType:
    def test_buttons(self, elements):
        results = find_by_type(elements, "Button")
        assert len(results) == 11  # 11 buttons in fixture

    def test_case_insensitive(self, elements):
        results = find_by_type(elements, "button")
        assert len(results) == 11

    def test_slider(self, elements):
        results = find_by_type(elements, "Slider")
        assert len(results) == 1
        assert results[0].label == "Search"

    def test_no_match(self, elements):
        results = find_by_type(elements, "TextField")
        assert results == []


# ---------------------------------------------------------------------------
# get_center
# ---------------------------------------------------------------------------


class TestGetCenter:
    def test_center_calculation(self):
        el = UIElement(type="Button", frame={"x": 100, "y": 200, "width": 50, "height": 30})
        cx, cy = get_center(el)
        assert cx == 125.0
        assert cy == 215.0

    def test_center_from_fixture(self, elements):
        maps = elements[1]
        cx, cy = get_center(maps)
        assert cx == 61.0  # 27 + 68/2
        assert cy == 425.0  # 382 + 86/2

    def test_no_frame_raises(self):
        el = UIElement(type="Button", label="NoFrame")
        with pytest.raises(ValueError, match="has no frame"):
            get_center(el)

    def test_zero_size_element(self):
        el = UIElement(type="Button", frame={"x": 50, "y": 50, "width": 0, "height": 0})
        cx, cy = get_center(el)
        assert cx == 50.0
        assert cy == 50.0


# ---------------------------------------------------------------------------
# generate_screen_summary
# ---------------------------------------------------------------------------


class TestGenerateScreenSummary:
    def test_summary_structure(self, elements):
        result = generate_screen_summary(elements)
        assert "summary" in result
        assert "element_count" in result
        assert "element_types" in result
        assert "interactive_elements" in result

    def test_element_count(self, elements):
        result = generate_screen_summary(elements)
        assert result["element_count"] == 14

    def test_element_types(self, elements):
        result = generate_screen_summary(elements)
        types = result["element_types"]
        assert types["Button"] == 11
        assert types["Slider"] == 1
        assert types["Application"] == 1
        assert types["StaticText"] == 1

    def test_interactive_elements(self, elements):
        result = generate_screen_summary(elements)
        interactive = result["interactive_elements"]
        # 11 buttons + 1 slider = 12 interactive elements
        assert len(interactive) == 12
        labels = [e["label"] for e in interactive]
        assert "Maps" in labels
        assert "Settings" in labels
        assert "Search" in labels

    def test_summary_text_mentions_app(self, elements):
        result = generate_screen_summary(elements)
        assert "Springboard" in result["summary"]

    def test_summary_text_mentions_buttons(self, elements):
        result = generate_screen_summary(elements)
        assert "button" in result["summary"].lower()

    def test_interactive_elements_include_values(self, elements):
        result = generate_screen_summary(elements)
        slider = [e for e in result["interactive_elements"] if e["type"] == "Slider"]
        assert len(slider) == 1
        assert slider[0]["value"] == "Page 1 of 2"

    def test_empty_elements(self):
        result = generate_screen_summary([])
        assert result["element_count"] == 0
        assert result["element_types"] == {}
        assert result["interactive_elements"] == []
        assert "Screen" in result["summary"]
