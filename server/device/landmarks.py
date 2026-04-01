"""Screen landmark matching, registry, and knowledge base parsing."""

from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml

from server.models import Landmark, ScreenLandmarks, UIElement

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Single-landmark matching
# ---------------------------------------------------------------------------


def match_landmark(elements: list[UIElement], landmark: Landmark) -> bool:
    """Check if a single landmark matches against a UI element list.

    Uses AND logic: all specified fields (element type, identifier, label)
    must match on the same element.  When ``absent=True``, the result is
    inverted — the landmark matches only if the element is NOT found.
    """
    candidates = elements

    # Filter by element type (always required)
    lower_type = landmark.element.lower()
    candidates = [e for e in candidates if e.type.lower() == lower_type]

    # Filter by identifier (primary, locale-independent)
    if landmark.identifier is not None:
        candidates = [e for e in candidates if e.identifier == landmark.identifier]

    # Filter by label (fallback, locale-dependent)
    if landmark.label is not None:
        lower_label = landmark.label.lower()
        candidates = [e for e in candidates if e.label.lower() == lower_label]
    elif landmark.label_contains is not None:
        lower_sub = landmark.label_contains.lower()
        candidates = [e for e in candidates if lower_sub in e.label.lower()]

    found = len(candidates) > 0
    return (not found) if landmark.absent else found


# ---------------------------------------------------------------------------
# Multi-landmark matching
# ---------------------------------------------------------------------------


def match_landmarks(
    elements: list[UIElement],
    landmarks: list[Landmark],
) -> tuple[bool, list[dict]]:
    """Check all landmarks against the UI element list (AND logic).

    Returns:
        (all_matched, per_landmark_results) where each result is
        ``{"landmark": {...}, "matched": bool}``.
    """
    results: list[dict] = []
    all_matched = True
    for lm in landmarks:
        matched = match_landmark(elements, lm)
        results.append({
            "landmark": lm.model_dump(exclude_none=True),
            "matched": matched,
        })
        if not matched:
            all_matched = False
    return all_matched, results


# ---------------------------------------------------------------------------
# Screen identification
# ---------------------------------------------------------------------------


def identify_screen(
    elements: list[UIElement],
    screens: list[ScreenLandmarks],
) -> dict:
    """Identify which screen matches the current UI state.

    Returns a dict with:
    - matched: screen name or None
    - confidence: "exact" (one match), "ambiguous" (multiple), "none"
    - matched_landmarks: per-landmark results for the matched screen
    - partial_matches: screens with some but not all landmarks matched
    """
    full_matches: list[tuple[str, list[dict]]] = []
    partial_matches: list[dict] = []

    for screen in screens:
        if not screen.landmarks:
            continue
        all_matched, results = match_landmarks(elements, screen.landmarks)
        matched_count = sum(1 for r in results if r["matched"])
        if all_matched:
            full_matches.append((screen.screen, results))
        elif matched_count > 0:
            partial_matches.append({
                "screen": screen.screen,
                "matched": matched_count,
                "total": len(screen.landmarks),
            })

    if len(full_matches) == 1:
        name, results = full_matches[0]
        return {
            "matched": name,
            "confidence": "exact",
            "matched_landmarks": results,
            "partial_matches": partial_matches,
        }
    elif len(full_matches) > 1:
        # Multiple screens matched — ambiguous
        return {
            "matched": full_matches[0][0],
            "confidence": "ambiguous",
            "matched_landmarks": full_matches[0][1],
            "ambiguous_with": [name for name, _ in full_matches[1:]],
            "partial_matches": partial_matches,
        }
    else:
        return {
            "matched": None,
            "confidence": "none",
            "matched_landmarks": [],
            "partial_matches": partial_matches,
        }


# ---------------------------------------------------------------------------
# Collision detection
# ---------------------------------------------------------------------------


def detect_collisions(screens: list[ScreenLandmarks]) -> dict:
    """Check for landmark collisions across screens.

    Two screens collide when one's landmarks are a subset of the other's
    — meaning both could match on the same UI state.

    Returns a dict with collisions, screens with no landmarks, and total count.
    """
    collisions: list[dict] = []
    no_landmarks: list[str] = []

    for screen in screens:
        if not screen.landmarks:
            no_landmarks.append(screen.screen)

    # Compare each pair of screens with landmarks
    with_landmarks = [s for s in screens if s.landmarks]
    for i, a in enumerate(with_landmarks):
        for b in with_landmarks[i + 1:]:
            a_keys = {_landmark_key(lm) for lm in a.landmarks}
            b_keys = {_landmark_key(lm) for lm in b.landmarks}
            if a_keys <= b_keys or b_keys <= a_keys:
                collisions.append({
                    "screens": [a.screen, b.screen],
                    "reason": "landmark subset overlap",
                    "shared": sorted(a_keys & b_keys),
                })

    return {
        "collisions": collisions,
        "no_landmarks": no_landmarks,
        "total_screens": len(screens),
    }


def _landmark_key(lm: Landmark) -> str:
    """Create a hashable key for a landmark for comparison."""
    parts = [lm.element.lower()]
    if lm.identifier:
        parts.append(f"id={lm.identifier}")
    if lm.label:
        parts.append(f"label={lm.label.lower()}")
    if lm.label_contains:
        parts.append(f"contains={lm.label_contains.lower()}")
    if lm.absent:
        parts.append("absent")
    return "|".join(parts)


# ---------------------------------------------------------------------------
# YAML frontmatter parsing
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)


def parse_screen_landmarks(file_path: Path) -> ScreenLandmarks | None:
    """Extract landmarks from a screen markdown file's YAML frontmatter.

    Returns None if the file has no landmarks field or empty landmarks.
    """
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError:
        logger.warning("Could not read %s", file_path)
        return None

    match = _FRONTMATTER_RE.match(text)
    if not match:
        return None

    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        logger.warning("Invalid YAML frontmatter in %s", file_path)
        return None

    if not isinstance(data, dict):
        return None

    screen_name = data.get("screen", "")
    if not screen_name:
        # Try to derive from filename
        screen_name = file_path.stem

    raw_landmarks = data.get("landmarks")
    if not raw_landmarks or not isinstance(raw_landmarks, list):
        return None

    landmarks: list[Landmark] = []
    for entry in raw_landmarks:
        if not isinstance(entry, dict) or "element" not in entry:
            continue
        landmarks.append(Landmark(**entry))

    if not landmarks:
        return None

    return ScreenLandmarks(screen=screen_name, landmarks=landmarks)


def scan_knowledge_base(path: Path) -> list[ScreenLandmarks]:
    """Scan a knowledge base directory for screen files with landmarks.

    Looks for ``screens/*.md`` files (excluding templates starting with _).
    """
    screens_dir = path / "screens"
    if not screens_dir.is_dir():
        # Try path directly if it already points to a screens dir
        if path.is_dir() and any(path.glob("*.md")):
            screens_dir = path
        else:
            return []

    results: list[ScreenLandmarks] = []
    for md_file in sorted(screens_dir.glob("*.md")):
        if md_file.name.startswith("_"):
            continue
        screen = parse_screen_landmarks(md_file)
        if screen:
            results.append(screen)

    return results


# ---------------------------------------------------------------------------
# Landmark registry
# ---------------------------------------------------------------------------


class LandmarkRegistry:
    """In-memory registry of screen landmarks, scoped by app identifier."""

    def __init__(self) -> None:
        self._sets: dict[str, list[ScreenLandmarks]] = {}

    def load(self, app: str, screens: list[ScreenLandmarks]) -> int:
        """Load landmarks for an app. Replaces any existing set for that app.

        Returns the number of screens loaded.
        """
        self._sets[app] = screens
        return len(screens)

    def load_from_path(self, app: str, path: str) -> int:
        """Scan a knowledge base path and load landmarks for an app.

        Returns the number of screens with landmarks found.
        """
        screens = scan_knowledge_base(Path(path))
        return self.load(app, screens)

    def unload(self, app: str | None = None) -> str:
        """Unload landmarks. If app is None, unload all.

        Returns what was unloaded: the app name or "all".
        """
        if app is None:
            self._sets.clear()
            return "all"
        self._sets.pop(app, None)
        return app

    def list_sets(self) -> dict[str, int]:
        """Return app -> screen count mapping."""
        return {app: len(screens) for app, screens in self._sets.items()}

    def all_screens(self, app: str | None = None) -> list[ScreenLandmarks]:
        """Get all screens, optionally filtered by app."""
        if app is not None:
            return list(self._sets.get(app, []))
        result: list[ScreenLandmarks] = []
        for screens in self._sets.values():
            result.extend(screens)
        return result

    def identify(
        self,
        elements: list[UIElement],
        app: str | None = None,
    ) -> dict:
        """Identify the current screen against loaded landmarks.

        If app is specified, only match against that app's screens.
        If app is None, match against all loaded screens.
        Returns a result dict with matched/confidence/partial_matches.
        """
        screens = self.all_screens(app)
        if not screens:
            return {
                "matched": None,
                "confidence": "none",
                "error": "no_landmarks_loaded",
                "matched_landmarks": [],
                "partial_matches": [],
            }
        return identify_screen(elements, screens)

    def validate(self, app: str | None = None) -> dict:
        """Check for collisions across loaded landmarks."""
        screens = self.all_screens(app)
        if not screens:
            return {
                "collisions": [],
                "no_landmarks": [],
                "total_screens": 0,
                "error": "no_landmarks_loaded",
            }
        return detect_collisions(screens)

    @property
    def is_empty(self) -> bool:
        """True if no landmarks are loaded."""
        return not self._sets
