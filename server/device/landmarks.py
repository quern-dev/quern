"""Screen landmark matching, registry, and knowledge base parsing."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from server.models import (
    Landmark,
    ScreenLandmarks,
    UIElement,
    WebContentHint,
)

logger = logging.getLogger(__name__)


@dataclass
class SkippedFile:
    """A screen file that the loader could not turn into landmarks.

    Surfaced in load/validate responses so an agent (or human) can act on it.

    Reasons:
        legacy_format    — file has identify_by: but no usable landmarks:.
                           identify_by is included verbatim (entries may be
                           dicts ready for mechanical migration, or strings /
                           freeform prose that need agent reinterpretation).
        no_landmarks     — file has neither field. Likely a stub.
        no_frontmatter   — file has no '---' YAML block.
        yaml_error       — frontmatter failed to parse. error is set.
        invalid_entries  — landmarks: present but all entries are malformed
                           (missing the required 'element' field).
        read_error       — couldn't read the file. error is set.
    """

    file: str
    reason: str
    screen: str | None = None
    identify_by: list[Any] | None = None
    error: str | None = None


@dataclass
class ParseResult:
    """Result of parsing a single screen markdown file.

    Either ``screen`` is set (successful parse) or ``skip`` is set
    (could not extract landmarks; reason in ``skip.reason``).
    """

    screen: ScreenLandmarks | None = None
    skip: SkippedFile | None = None
    web_content: list[WebContentHint] = field(default_factory=list)
    """Carried on both paths deliberately. The screens that most need a web
    content hint -- an OAuth view, a settings page behind
    SFSafariViewController -- are exactly the ones with no native landmarks, so
    a hint attached only to successful parses would never reach the cases it
    exists for."""


@dataclass
class KnowledgeBaseScan:
    """Result of scanning a knowledge base directory."""

    screens: list[ScreenLandmarks] = field(default_factory=list)
    skipped: list[SkippedFile] = field(default_factory=list)
    web_content: list[WebContentHint] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Single-landmark matching
# ---------------------------------------------------------------------------


def match_landmark(elements: list[UIElement], landmark: Landmark) -> bool:
    """Check if a single landmark matches against a UI element list.

    Uses AND logic: all specified fields (element type, identifier, label,
    selection state) must match on the same element.  When ``absent=True``,
    the result is inverted — the landmark matches only if the element is
    NOT found.
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

    # Filter by selection state (for tabs, switches, radios, checkboxes).
    # Both iOS and Android backends serialize selection state as UIElement
    # value = "1" (selected) / "0" (not selected).
    if landmark.selected is not None:
        if landmark.selected:
            candidates = [e for e in candidates if e.value == "1"]
        else:
            candidates = [e for e in candidates if e.value != "1"]

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
    - partial_matches: every evaluated screen that did NOT fully match,
      including zero-match screens, sorted by descending match count.
      Each entry includes per-landmark results so callers can see which
      selectors hit and which missed without re-running identification.
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
        else:
            # Surface every non-fully-matched screen, including zero-match,
            # so that "none" responses still tell the caller what was
            # evaluated and how each landmark fared.
            partial_matches.append({
                "screen": screen.screen,
                "matched": matched_count,
                "total": len(screen.landmarks),
                "landmarks": results,
            })

    # Best candidate first; deterministic tie-break by screen name.
    partial_matches.sort(key=lambda p: (-p["matched"], p["screen"]))

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


def _relative_file_label(file_path: Path, base_path: Path) -> str:
    """Best-effort relative path from base_path for skipped[] entries."""
    try:
        return str(file_path.relative_to(base_path))
    except ValueError:
        return file_path.name


def parse_screen_landmarks(
    file_path: Path, *, base_path: Path | None = None,
) -> ParseResult:
    """Extract landmarks from a screen markdown file's YAML frontmatter.

    Returns a :class:`ParseResult` — either ``screen`` is populated on
    success, or ``skip`` is populated with a categorized reason so callers
    can surface what went wrong (legacy format, malformed YAML, etc.).
    """
    label = (
        _relative_file_label(file_path, base_path)
        if base_path is not None
        else file_path.name
    )

    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("Could not read %s", file_path)
        return ParseResult(skip=SkippedFile(
            file=label, reason="read_error", error=str(e),
        ))

    match = _FRONTMATTER_RE.match(text)
    if not match:
        return ParseResult(skip=SkippedFile(
            file=label, reason="no_frontmatter",
        ))

    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError as e:
        logger.warning("Invalid YAML frontmatter in %s", file_path)
        return ParseResult(skip=SkippedFile(
            file=label, reason="yaml_error", error=str(e),
        ))

    if not isinstance(data, dict):
        return ParseResult(skip=SkippedFile(
            file=label, reason="yaml_error",
            error="frontmatter is not a YAML mapping",
        ))

    screen_name = data.get("screen", "") or file_path.stem
    hints = _parse_web_content(data.get("web_content"), screen_name)

    raw_landmarks = data.get("landmarks")
    if not raw_landmarks or not isinstance(raw_landmarks, list):
        # No usable landmarks. Distinguish legacy format (file has
        # identify_by:) from genuine no-landmarks (stub or unannotated).
        identify_by = data.get("identify_by")
        if isinstance(identify_by, list) and identify_by:
            # Pass entries through verbatim — dict entries can be migrated
            # mechanically, but strings / freeform prose are also legitimate
            # legacy content that an agent should see and reinterpret.
            return ParseResult(skip=SkippedFile(
                file=label, screen=screen_name, reason="legacy_format",
                identify_by=list(identify_by),
            ), web_content=hints)
        return ParseResult(skip=SkippedFile(
            file=label, screen=screen_name, reason="no_landmarks",
        ), web_content=hints)

    landmarks: list[Landmark] = []
    for entry in raw_landmarks:
        if not isinstance(entry, dict) or "element" not in entry:
            continue
        landmarks.append(Landmark(**entry))

    if not landmarks:
        return ParseResult(skip=SkippedFile(
            file=label, screen=screen_name, reason="invalid_entries",
        ), web_content=hints)

    return ParseResult(
        screen=ScreenLandmarks(screen=screen_name, landmarks=landmarks),
        web_content=hints,
    )


def _parse_web_content(raw: object, screen: str) -> list[WebContentHint]:
    """Read a screen's web_content: block, skipping anything malformed.

    A bad hint must never stop a knowledge base loading: it is an optimisation,
    and every value in it is verified before use.
    """
    if not isinstance(raw, list):
        return []
    hints: list[WebContentHint] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            hints.append(WebContentHint(screen=screen, **entry))
        except ValidationError:
            logger.warning("Ignoring malformed web_content entry in %s", screen)
    return hints


def scan_knowledge_base(path: Path) -> KnowledgeBaseScan:
    """Scan a knowledge base directory for screen files with landmarks.

    Looks for ``screens/*.md`` files (excluding templates starting with _).
    Returns a :class:`KnowledgeBaseScan` with both successfully parsed
    screens and a list of skipped files (with categorized reasons).
    """
    screens_dir = path / "screens"
    if not screens_dir.is_dir():
        # Try path directly if it already points to a screens dir
        if path.is_dir() and any(path.glob("*.md")):
            screens_dir = path
        else:
            return KnowledgeBaseScan()

    scan = KnowledgeBaseScan()
    for md_file in sorted(screens_dir.glob("*.md")):
        if md_file.name.startswith("_"):
            continue
        result = parse_screen_landmarks(md_file, base_path=path)
        scan.web_content.extend(result.web_content)
        if result.screen is not None:
            scan.screens.append(result.screen)
        elif result.skip is not None:
            scan.skipped.append(result.skip)
    return scan


# ---------------------------------------------------------------------------
# Landmark registry
# ---------------------------------------------------------------------------


class LandmarkRegistry:
    """In-memory registry of screen landmarks, scoped by app identifier."""

    def __init__(self) -> None:
        self._sets: dict[str, list[ScreenLandmarks]] = {}
        self._web_content: dict[str, list[WebContentHint]] = {}

    def load(self, app: str, screens: list[ScreenLandmarks]) -> int:
        """Load landmarks for an app. Replaces any existing set for that app.

        Returns the number of screens loaded.
        """
        self._sets[app] = screens
        return len(screens)

    def load_from_path(
        self, app: str, path: str,
    ) -> tuple[int, list[SkippedFile]]:
        """Scan a knowledge base path and load landmarks for an app.

        Returns a tuple of (count, skipped) — the number of screens loaded
        and the list of files that could not be turned into landmarks
        (with categorized reasons in each entry).
        """
        scan = scan_knowledge_base(Path(path))
        count = self.load(app, scan.screens)
        self._web_content[app] = scan.web_content
        return count, scan.skipped

    def web_content(self, app: str | None = None) -> list[WebContentHint]:
        """Recorded web view facts, for one app or all of them."""
        if app is not None:
            return list(self._web_content.get(app, []))
        return [hint for hints in self._web_content.values() for hint in hints]

    def unload(self, app: str | None = None) -> str:
        """Unload landmarks. If app is None, unload all.

        Returns what was unloaded: the app name or "all".
        """
        if app is None:
            self._sets.clear()
            self._web_content.clear()
            return "all"
        self._sets.pop(app, None)
        self._web_content.pop(app, None)
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
