"""Screen landmark matching, registry, and knowledge base parsing."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, NamedTuple

import yaml
from pydantic import ValidationError

from server.device.element_types import (
    element_rule,
    equivalence,
    portability_finding,
    rule_for,
    type_matches,
)
from server.models import (
    Landmark,
    ScreenLandmarks,
    UIElement,
    WebContentHint,
)

logger = logging.getLogger(__name__)


class ScrollHint(NamedTuple):
    """What the knowledge base can say about a screen's scrollability.

    `reason` exists so the caller can tell four different silences apart:
    nothing loaded, nothing matched, two things matched, or a match that says
    nothing about scrolling. They all produce `scrollable=None` and they ask
    the reader to do different things about it.
    """

    scrollable: bool | None
    screen: str | None
    reason: str
    candidates: list[str] | None = None


@dataclass
class SkippedFile:
    """A screen file that the loader could not turn into landmarks.

    Surfaced in load/validate responses so an agent (or human) can act on it.

    Reasons:
        legacy_format    — file has identify_by: but no usable landmarks:.
                           identify_by was the field before landmarks (April
                           2026) and the loader has never evaluated it. Kept as
                           a diagnostic, not a migration path: without it such a
                           file reports no_landmarks, which tells the reader
                           nothing. The entries are echoed verbatim so the
                           rename can be done from the response alone.
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


#: The landmark conventions this quern checks against. See
#: `docs/proposals/landmark-conventions.md` §3: v1 is everything written before
#: that spec, v2 adds the cross-backend portability checks.
CURRENT_LANDMARK_CONVENTIONS = 2


@dataclass
class FileConventions:
    """How one screen file stands against the landmark conventions.

    `declared` is what the file says it is written for -- a target, never a
    claim of compliance, and never written by quern. `findings` is what quern
    computed. The two are reported side by side because they answer different
    questions: `grep -L "landmark_conventions: 2"` finds the files nobody has
    migrated without loading anything, and the findings say which files
    actually comply. A stamp claiming compliance would go stale on the first
    edit after the audit that wrote it (ADR 3).
    """

    file: str
    screen: str | None
    declared: int | None
    findings: list[dict] = field(default_factory=list)
    app: str | None = None
    """Set by the registry, so entries from several loaded apps that share a
    file name (every app has a `screens/home.md`) can be told apart."""

    @property
    def state(self) -> str:
        if self.declared is None:
            return "undeclared"
        if self.declared < CURRENT_LANDMARK_CONVENTIONS:
            return "behind"
        if self.declared > CURRENT_LANDMARK_CONVENTIONS:
            # Written for a quern newer than this one. Checked against what
            # this one knows, and said so, rather than read as compliant.
            return "newer"
        return "failing" if self.findings else "current"

    def to_dict(self) -> dict:
        out: dict = {"file": self.file}
        if self.app is not None:
            out["app"] = self.app
        if self.screen is not None:
            out["screen"] = self.screen
        out["declared"] = self.declared
        out["state"] = self.state
        if self.findings:
            out["findings"] = self.findings
        return out


def _declared_conventions(raw: object) -> tuple[int | None, dict | None]:
    """The declared target, and a finding when the declaration is unusable.

    Anything but a positive integer reads as undeclared, and says why: a typo
    must not silently claim a target, and must not silently vanish either.
    `bool` is excluded explicitly because `True` is an `int` in Python, and
    `landmark_conventions: yes` would otherwise declare v1.
    """
    if raw is None:
        return None, None
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 1:
        return raw, None
    return None, {
        "code": "invalid_declaration",
        "message": (
            f"landmark_conventions is {raw!r}; it must be a positive integer, "
            f"such as {CURRENT_LANDMARK_CONVENTIONS}. Read as undeclared."
        ),
    }


#: Why a file loaded no screen, as a finding. A file that identifies nothing
#: must never read as `current` -- a declared target over an empty or
#: unreadable file satisfied "reload until it reports current" while matching
#: no screen at all, which is a failed check reading as a passing one.
_UNLOADED = {
    "no_landmarks": "The file has no landmarks, so it identifies no screen. "
                    "Add landmarks, or leave it as a stub without a declaration.",
    "invalid_entries": "Every landmark in the file is invalid, so it identifies "
                       "no screen. See this file's skipped[] entry.",
    "no_frontmatter": "The file has no '---' frontmatter block, so it was not read.",
    "yaml_error": "The frontmatter is not valid YAML, so it was not read. See "
                  "this file's skipped[] entry.",
    "read_error": "The file could not be read.",
}


def unloaded_finding(reason: str) -> dict:
    """The finding for a file skipped for `reason`."""
    return {
        "code": reason,
        "message": _UNLOADED.get(reason, "The file loaded no screen."),
    }


def check_conventions(
    file: str,
    screen: str | None,
    raw_declared: object,
    landmarks: Sequence[Landmark],
    *,
    identify_by: bool = False,
    unloaded: str | None = None,
    invalid_entries: Sequence[dict] = (),
) -> FileConventions:
    """Check one screen's landmarks against the current conventions (§3.3).

    Every file is checked, whatever it declares: an undeclared or older file
    gets the same findings, which are exactly what migrating it would change.

    `unloaded` is the skip reason when the file loaded no screen;
    `invalid_entries` lists landmarks the loader dropped, which would
    otherwise vanish while the rest of the file reads as compliant.
    """
    declared, invalid = _declared_conventions(raw_declared)
    findings: list[dict] = []
    if invalid is not None:
        findings.append(invalid)
    if unloaded is not None:
        findings.append(unloaded_finding(unloaded))
    findings.extend(invalid_entries)
    if identify_by:
        findings.append({
            "code": "legacy_format",
            "message": (
                "identify_by: is the field from before landmarks: and has never "
                "been evaluated; rename it to landmarks:."
            ),
        })
    for lm in landmarks:
        if lm.element is None:
            continue  # URL landmarks are backend-independent
        found = portability_finding(
            lm.element, identifier=lm.identifier, label=lm.label or lm.label_contains,
        )
        if found is not None:
            code, message = found
            findings.append({
                "landmark": lm.model_dump(exclude_none=True, exclude_defaults=True),
                "code": code,
                "message": message,
            })
    return FileConventions(file=file, screen=screen, declared=declared, findings=findings)


def conventions_report(entries: Sequence[FileConventions]) -> dict:
    """The `conventions` block on load and validate responses.

    Counts cover every file; `files` lists only those not reported current, so
    a two-hundred-screen knowledge base in good order costs a few lines rather
    than two hundred.
    """
    counts = {"current": 0, "failing": 0, "behind": 0, "undeclared": 0, "newer": 0}
    for entry in entries:
        counts[entry.state] += 1
    report: dict = {
        "current_version": CURRENT_LANDMARK_CONVENTIONS,
        "counts": {k: v for k, v in counts.items() if v or k != "newer"},
        "files": [e.to_dict() for e in entries if e.state != "current"],
    }
    if report["files"]:
        report["how_to_migrate"] = (
            "Fix each file's findings, then set "
            f"`landmark_conventions: {CURRENT_LANDMARK_CONVENTIONS}` in its "
            "frontmatter and reload to confirm it reports current. Undeclared "
            "files are read as v1 and matched exactly as declared ones are. "
            "See 'Auditing a knowledge base' in docs/screen-landmarks.md."
        )
    return report


@dataclass
class ParseResult:
    """Result of parsing a single screen markdown file.

    Either ``screen`` is set (successful parse) or ``skip`` is set
    (could not extract landmarks; reason in ``skip.reason``).
    """

    screen: ScreenLandmarks | None = None
    skip: SkippedFile | None = None
    conventions: FileConventions | None = None
    """Set for every file whose frontmatter parsed, landmarks or not."""
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
    conventions: list[FileConventions] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Single-landmark matching
# ---------------------------------------------------------------------------


def match_landmark(
    elements: list[UIElement],
    landmark: Landmark,
    page_urls: Sequence[Mapping[str, str | None]] | None = None,
) -> bool:
    """Check if a single landmark matches against a UI element list.

    See `match_landmark_via`, which also says how the type matched.
    """
    return match_landmark_via(elements, landmark, page_urls)[0]


def match_landmark_via(
    elements: list[UIElement],
    landmark: Landmark,
    page_urls: Sequence[Mapping[str, str | None]] | None = None,
) -> tuple[bool, str | None]:
    """Whether a landmark matches, and the type equivalence it matched through.

    The second value is `"RadioButton≈Button"`-style when the element that
    satisfied the landmark has a different type that stands for the same
    element on another backend, and None for an exact match, a URL landmark,
    an absent landmark, or no match. An exact candidate is preferred when there
    is one, so a landmark that matches exactly never reports an equivalence.

    The element type is compared under `element_types.rule_for`: widened to the
    type's family when the landmark carries an identifier, to its safe pair
    when it carries a label, and not at all when it carries neither. That
    widening is what lets a landmark written on a simulator's accessibility
    tree match the same screen read through WDA (#336).

    Uses AND logic: all specified fields (element type, identifier, label,
    selection state) must match on the same element.  When ``absent=True``,
    the result is inverted — the landmark matches only if the element is
    NOT found.

    A landmark naming ``web_url_contains`` is matched against ``page_urls``
    instead, for screens whose identity is entirely web. ``page_urls`` of
    ``None`` means the listing could not be obtained, which is different from an
    empty list: an empty list is evidence that no page is open, and no listing
    at all is no evidence of anything.
    """
    if landmark.web_url_contains is not None:
        if page_urls is None:
            # Before the absent inversion, deliberately. Inverting "could not
            # look" would let an absent-URL landmark identify a screen on the
            # strength of a failed query.
            return False, None
        needle = landmark.web_url_contains.lower()
        found = any(
            needle in (page.get("url") or "").lower()
            and (landmark.web_process is None
                 or page.get("process") == landmark.web_process)
            for page in page_urls
        )
        return ((not found) if landmark.absent else found), None

    candidates = elements

    # Filter by element type (always required), widened across backends as far
    # as the landmark's other fields make safe.
    wanted = landmark.element or ""
    # Truthiness, not `is not None`: an empty label is a filter that matches
    # every unlabelled element, and it pins nothing down.
    rule = rule_for(
        identifier=landmark.identifier,
        label=landmark.label or landmark.label_contains,
    )
    candidates = [
        e for e in candidates
        if type_matches(wanted, e.type, element_rule(rule, landmark.identifier, e.label))
    ]

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
    if landmark.absent:
        return not found, None
    if not found:
        return False, None
    lower_type = wanted.lower()
    if any(e.type.lower() == lower_type for e in candidates):
        return True, None
    return True, equivalence(wanted, candidates[0].type)


# ---------------------------------------------------------------------------
# Multi-landmark matching
# ---------------------------------------------------------------------------


def match_landmarks(
    elements: list[UIElement],
    landmarks: list[Landmark],
    page_urls: Sequence[Mapping[str, str | None]] | None = None,
) -> tuple[bool, list[dict]]:
    """Check all landmarks against the UI element list (AND logic).

    Returns:
        (all_matched, per_landmark_results) where each result is
        ``{"landmark": {...}, "matched": bool}``, plus ``"matched_via"`` when
        the landmark matched only through a type equivalence.
    """
    results: list[dict] = []
    all_matched = True
    for lm in landmarks:
        matched, via = match_landmark_via(elements, lm, page_urls)
        entry: dict = {
            "landmark": lm.model_dump(exclude_none=True),
            "matched": matched,
        }
        if via:
            entry["matched_via"] = via
        results.append(entry)
        if not matched:
            all_matched = False
    return all_matched, results


# ---------------------------------------------------------------------------
# Screen identification
# ---------------------------------------------------------------------------


def identify_screen(
    elements: list[UIElement],
    screens: list[ScreenLandmarks],
    page_urls: Sequence[Mapping[str, str | None]] | None = None,
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
        all_matched, results = match_landmarks(elements, screen.landmarks, page_urls)
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


def needs_page_urls(screens: list[ScreenLandmarks]) -> bool:
    """Whether identifying against these screens requires the page listing.

    Asked before the Web Inspector is contacted at all, so a knowledge base that
    uses no URL landmarks -- which is nearly all of them -- pays nothing.
    """
    return any(
        lm.web_url_contains is not None
        for screen in screens for lm in screen.landmarks
    )


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
            a_in_b = _covered(a.landmarks, b.landmarks)
            b_in_a = _covered(b.landmarks, a.landmarks)
            if len(a_in_b) == len(a.landmarks) or len(b_in_a) == len(b.landmarks):
                collisions.append({
                    "screens": [a.screen, b.screen],
                    "reason": "landmark subset overlap",
                    "shared": sorted({_landmark_key(lm) for lm in a_in_b}),
                })

    return {
        "collisions": collisions,
        "no_landmarks": no_landmarks,
        "total_screens": len(screens),
    }


def _covered(landmarks: list[Landmark], others: list[Landmark]) -> list[Landmark]:
    """The landmarks in `landmarks` that some landmark in `others` duplicates.

    Duplicates under the matching rule, not by spelling: `RadioButton` and
    `Button` with the same label match the same element on either backend, so
    two screens anchored on them collide as surely as two spelling it the same.
    Comparing keys missed exactly the collisions a knowledge base half-migrated
    between the two vocabularies would have.
    """
    return [lm for lm in landmarks if any(_same_selector(lm, o) for o in others)]


def _same_selector(a: Landmark, b: Landmark) -> bool:
    """Whether two landmarks select the same element on some backend."""
    if (
        (a.identifier or None) != (b.identifier or None)
        or (a.label or "").lower() != (b.label or "").lower()
        or (a.label_contains or "").lower() != (b.label_contains or "").lower()
        or a.absent != b.absent
    ):
        return False
    if a.element is None or b.element is None:
        # URL landmarks: compared by their key, as before.
        return _landmark_key(a) == _landmark_key(b)
    rule = rule_for(identifier=a.identifier, label=a.label or a.label_contains)
    return type_matches(a.element, b.element, rule)


def _landmark_key(lm: Landmark) -> str:
    """Create a hashable key for a landmark for comparison.

    `element` is None on a URL landmark, and `.lower()` on it made
    `validate_landmarks` raise for any knowledge base that had one.
    """
    if lm.web_url_contains is not None:
        parts = [f"url~{lm.web_url_contains.lower()}"]
        if lm.web_process:
            parts.append(f"process={lm.web_process}")
        if lm.absent:
            parts.append("absent")
        return "|".join(parts)
    parts = [(lm.element or "").lower()]
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
    raw_declared = data.get("landmark_conventions")

    raw_landmarks = data.get("landmarks")
    if not raw_landmarks or not isinstance(raw_landmarks, list):
        # No usable landmarks. Say which of the two it is: a file still using
        # the pre-landmarks identify_by: field, or one that never had any.
        identify_by = data.get("identify_by")
        if isinstance(identify_by, list) and identify_by:
            # Pass entries through verbatim — dict entries can be migrated
            # mechanically, but strings / freeform prose are also legitimate
            # legacy content that an agent should see and reinterpret.
            return ParseResult(skip=SkippedFile(
                file=label, screen=screen_name, reason="legacy_format",
                identify_by=list(identify_by),
            ), web_content=hints, conventions=check_conventions(
                label, screen_name, raw_declared, [], identify_by=True,
            ))
        return ParseResult(skip=SkippedFile(
            file=label, screen=screen_name, reason="no_landmarks",
        ), web_content=hints, conventions=check_conventions(
            label, screen_name, raw_declared, [], unloaded="no_landmarks",
        ))

    landmarks: list[Landmark] = []
    dropped: list[dict] = []
    for index, entry in enumerate(raw_landmarks):
        if not isinstance(entry, dict):
            dropped.append(_invalid_landmark(index, entry, "not a mapping"))
            continue
        try:
            landmarks.append(Landmark(**entry))
        except (ValidationError, TypeError) as e:
            # Enforced by the model: a landmark naming neither an element nor a
            # URL can match nothing, and treating it as satisfied would make its
            # screen match everything.
            reason = (
                "; ".join(err["msg"] for err in e.errors())
                if isinstance(e, ValidationError) else str(e)
            )
            dropped.append(_invalid_landmark(index, entry, reason))

    conventions = check_conventions(
        label, screen_name, raw_declared, landmarks,
        unloaded=None if landmarks else "invalid_entries",
        invalid_entries=dropped,
    )

    if not landmarks:
        return ParseResult(skip=SkippedFile(
            file=label, screen=screen_name, reason="invalid_entries",
        ), web_content=hints, conventions=conventions)

    # Anything other than a literal bool reads as unset. A typo must mean
    # "nobody has said" rather than silently asserting one of the two answers
    # -- the same rule `auto_install_cert` follows for the same reason.
    raw_scrollable = data.get("scrollable")
    scrollable = raw_scrollable if isinstance(raw_scrollable, bool) else None

    return ParseResult(
        screen=ScreenLandmarks(
            screen=screen_name, landmarks=landmarks, scrollable=scrollable,
        ),
        web_content=hints,
        conventions=conventions,
    )


def _invalid_landmark(index: int, entry: object, reason: str) -> dict:
    """A finding for a landmark entry the loader dropped.

    Dropped entries used to vanish: a misspelt `elemnt:` beside a valid
    landmark left the file loading, checked and reported compliant on the
    landmarks that survived, with nothing saying one had been discarded.
    """
    return {
        "code": "invalid_landmark",
        "landmark": entry if isinstance(entry, dict) else repr(entry),
        "message": f"Landmark {index} was ignored: {reason}.",
    }


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
        # The screen name comes from the file, not the entry. Passing both
        # raises TypeError for a duplicate keyword before Pydantic ever runs,
        # and that is not a ValidationError -- one entry carrying a stray
        # `screen:` would abort the whole scan rather than be skipped.
        fields = {k: v for k, v in entry.items() if k != "screen"}
        try:
            hints.append(WebContentHint(screen=screen, **fields))
        except (ValidationError, TypeError):
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
        if result.conventions is not None:
            scan.conventions.append(result.conventions)
        elif result.skip is not None:
            # Failed before the frontmatter parsed. Still a file in the
            # knowledge base, so it is counted -- `counts` claims every file.
            scan.conventions.append(FileConventions(
                file=result.skip.file, screen=result.skip.screen, declared=None,
                findings=[unloaded_finding(result.skip.reason)],
            ))
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
        self._conventions: dict[str, list[FileConventions]] = {}
        #: Where each app's set came from: a directory, or "inline".
        self._sources: dict[str, str] = {}

    def load(
        self,
        app: str,
        screens: list[ScreenLandmarks],
        conventions: list[FileConventions] | None = None,
    ) -> int:
        """Load landmarks for an app. Replaces any existing set for that app.

        `conventions` is replaced along with the screens, so a reload never
        reports a previous load's findings. Returns the number of screens
        loaded.
        """
        self._sets[app] = screens
        self._sources[app] = "inline"
        self._conventions[app] = [
            replace(entry, app=app) for entry in conventions or []
        ]
        return len(screens)

    def conventions(self, app: str | None = None) -> list[FileConventions]:
        """The conventions computed at load, for one app or all of them."""
        if app is not None:
            return list(self._conventions.get(app, []))
        return [c for entries in self._conventions.values() for c in entries]

    def load_from_path(
        self, app: str, path: str,
    ) -> tuple[int, list[SkippedFile]]:
        """Scan a knowledge base path and load landmarks for an app.

        Returns a tuple of (count, skipped) — the number of screens loaded
        and the list of files that could not be turned into landmarks
        (with categorized reasons in each entry).
        """
        return self.load_scan(app, scan_knowledge_base(Path(path)), path)

    def load_scan(
        self, app: str, scan: KnowledgeBaseScan, path: str,
    ) -> tuple[int, list[SkippedFile]]:
        """Load a scan already made -- so a caller can look at it first and
        refuse it without replacing what is loaded."""
        count = self.load(app, scan.screens, scan.conventions)
        self._web_content[app] = scan.web_content
        self._sources[app] = path
        return count, scan.skipped

    def source(self, app: str) -> str | None:
        """Where `app`'s loaded set came from, or None if none is loaded."""
        return self._sources.get(app) if app in self._sets else None

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
            self._conventions.clear()
            self._sources.clear()
            return "all"
        self._sets.pop(app, None)
        self._web_content.pop(app, None)
        self._conventions.pop(app, None)
        self._sources.pop(app, None)
        return app

    def list_sets(self) -> dict[str, int]:
        """Return app -> screen count mapping."""
        return {app: len(screens) for app, screens in self._sets.items()}

    def scrollable_for(
        self,
        elements: list[UIElement],
        app: str | None = None,
        page_urls: Sequence[Mapping[str, str | None]] | None = None,
    ) -> ScrollHint:
        """Does the screen these elements came from scroll? And which screen?

        **Only an exact identification counts.** `ambiguous` means two screens
        matched, and they can disagree about scrolling; taking either would be
        a guess presented as knowledge, which is the failure the whole
        knowledge base exists to avoid.

        But ambiguous is reported as itself rather than folded into "nobody has
        said", because the two need different things from the reader. Unknown
        means *record it*; ambiguous means *the recording is there and cannot
        be used*, and the usual cause is mundane -- landmarks loaded for two
        apps at once, where one screen matches both. Measured: load a second
        app whose screen shares a landmark and a working `scrollable: true`
        silently stops being consulted. Scope with `app` to avoid it.

        Pure: no device read. `identify_screen` works off the element list the
        caller already has, at ~0.26ms against a 200-screen base.
        """
        screens = self.all_screens(app)
        if not screens:
            return ScrollHint(None, None, "no_knowledge")
        result = identify_screen(elements, screens, page_urls=page_urls)
        confidence = result.get("confidence")
        if confidence == "none" and needs_page_urls(screens) and page_urls is None:
            # Only when nothing matched, and only then.
            #
            # A `web_url_contains` landmark matches nothing when the page
            # listing is absent (`match_landmark` returns False outright), so a
            # screen identified by URL is unrecognisable here and its recorded
            # `scrollable` would read as "nobody has said" -- the "told to
            # record what you already recorded" failure this type exists to
            # avoid. Saying so needs its own reason.
            #
            # Checking it *before* identifying was a regression: the lookup
            # passes no `app`, so `all_screens(None)` spans every loaded app,
            # and one URL-identified screen anywhere turned off recorded
            # scrollability for all of them. A native screen that identifies
            # perfectly well must not be refused because some other app has a
            # web screen.
            return ScrollHint(None, None, "needs_page_urls")
        if confidence == "ambiguous":
            # `matched` plus `ambiguous_with`, which is where identify_screen
            # puts the rest. `partial_matches` holds the screens that did *not*
            # fully match, so reading candidates from it returned an empty list
            # -- which says "no candidates" rather than "several", and is the
            # one answer that is certainly wrong.
            candidates = [result.get("matched"), *result.get("ambiguous_with", [])]
            return ScrollHint(
                None, None, "ambiguous", candidates=[c for c in candidates if c],
            )
        # No second "is it exact?" test. Ambiguous has already returned above,
        # and a no-match leaves `matched` None, so the lookup below answers
        # both. Two earlier shapes each carried a redundant guard that mutation
        # testing showed to be equivalent -- a rule spelled twice is a pair
        # that drifts apart later, and an unkillable mutant is how you find it.
        # The screen that *matched*, not the last one loaded under that name.
        # `identify_screen` returns a name, and a name is not unique across
        # loaded apps -- `{s.screen: s}` kept whichever app loaded last, so the
        # hint could come from a screen that did not match, arriving with
        # `reason="recorded"`, the most confident thing this type says. That is
        # the same collision `_url_rival_in_same_app` is built to avoid, one
        # line below. `Home`, `Login` and `Settings` repeat across apps.
        #
        # Exactly one screen fully matched -- `ambiguous` has already returned
        # -- so this finds it or nothing. The extra `match_landmarks` pass runs
        # only over screens sharing the matched name, and reads no device.
        #
        # That exactness also makes `screen.screen == matched_name` redundant,
        # and mutation testing duly cannot kill it: the match test alone picks
        # the same screen. It stays because it says which screen we are looking
        # for, where the match test only says how we recognise it -- and it is
        # what keeps this honest if `identify_screen` ever reports a best
        # candidate rather than a sole one.
        matched_name = result.get("matched")
        found = next(
            (
                screen for screen in screens
                if screen.screen == matched_name and screen.landmarks
                and match_landmarks(elements, screen.landmarks, page_urls)[0]
            ),
            None,
        )
        if found is None:
            return ScrollHint(None, None, "no_match")
        if page_urls is None and self._url_rival_in_same_app(found, elements):
            # A screen in the *same* app whose only unmet landmark is its URL.
            # Without the page listing that landmark cannot match, so
            # `identify_screen` reports the native screen as "exact" when the
            # honest answer is "one of two". Using its `scrollable` would be a
            # guess wearing an exact match's clothes.
            #
            # Same app only: a URL screen belonging to a different app is not a
            # rival for this one, and treating it as one is the regression that
            # disabled recorded scrollability everywhere.
            return ScrollHint(None, None, "needs_page_urls")
        reason = "recorded" if found.scrollable is not None else "screen_silent"
        return ScrollHint(found.scrollable, found.screen, reason)

    def _url_rival_in_same_app(
        self, matched: ScreenLandmarks, elements: list[UIElement],
    ) -> bool:
        """Could a URL-identified screen beside `matched` also be on screen?

        Only its app's screens are considered, and only those whose *non-URL*
        landmarks all matched -- a screen that failed on something native is
        not a rival, whatever its URL says.

        This walks the app's own `ScreenLandmarks` rather than filtering
        `identify_screen`'s `partial_matches`, because those carry a screen
        name and no app. Joining on the name let `Login` in another app count
        as a rival for `Login` here, which is the precise regression this
        method exists to prevent -- and the names that repeat across apps are
        exactly the common ones. The first test written for this used the name
        `OtherWeb`, which collides with nothing, so it passed against the bug.

        Pure: `match_landmark` reads the element list the caller already has,
        and the pass is over one app's screens.
        """
        app = next(
            (name for name, screens in self._sets.items() if matched in screens),
            None,
        )
        if app is None:
            return False
        for sibling in self._sets[app]:
            # `sibling is matched` is unreachable today and kept deliberately:
            # this runs only when `page_urls is None`, a URL landmark cannot
            # match without the listing, so an exactly-matched screen holds no
            # URL landmark and the `needs_page_urls` filter already excludes
            # it. Mutation testing cannot kill it, which by this repo's usual
            # rule argues for deleting it -- but the rule it encodes is "a
            # screen is not its own rival", and it stops being redundant the
            # moment this is called with a listing in hand.
            if sibling is matched or not needs_page_urls([sibling]):
                continue
            # `page_urls` is None on this path by construction, so a URL
            # landmark cannot match and is excluded rather than evaluated.
            native = [lm for lm in sibling.landmarks if lm.web_url_contains is None]
            if all(match_landmark(elements, lm) for lm in native):
                return True
        return False

    async def identify_for_context(self, elements, fetch_page_urls) -> dict:
        """`identified_as` / `confidence` for a screen context, or nothing.

        One implementation, two callers: the action responses in
        `server/api/device.py` and the miss paths in
        `server/device/controller_ui.py`. Those miss paths -- `tap_element`
        finding nothing, `wait_for_element` timing out -- are where a caller
        most needs to know which screen it is actually on, and they returned a
        context without identification, so one endpoint answered in two shapes
        and nobody could tell "nothing matched" from "this path does not ask".

        It lives here rather than being written twice because the rule it
        encodes is not obvious -- reach for the page listing only when a loaded
        landmark needs one -- and a rule spelled in two places is a pair that
        drifts.

        `fetch_page_urls` is an awaitable-returning callable rather than a
        controller and a udid: the registry has no business knowing what a
        device is, and this way the one caller that already has both can supply
        them without the knowledge base learning about either.

        Mirrors `get_screen_summary?identify=true` rather than inventing a
        second shape for the same fact -- the same field names, and the same
        string confidence: "exact", "ambiguous", "none". `candidates` is added
        on an ambiguous match, which that endpoint does not do, because
        reporting the first of several matches presents a guess as an
        identification.

        Silent on failure. Identification is an addition to a response; an
        action that worked must not report failure because a knowledge base
        could not be consulted, or because the Web Inspector is down.
        """
        try:
            # Inside the try. `all_screens()` raising would otherwise escape
            # into the caller's own handler, which discards the *whole* screen
            # context -- losing title, summary and elements too. Losing the
            # identification is the intended degradation; losing the screen is
            # not.
            screens = self.all_screens()
            if not screens:
                return {}
            # Only reach for the page listing when a loaded landmark needs it,
            # so a knowledge base with no URL landmarks costs nothing extra.
            page_urls = await fetch_page_urls() if needs_page_urls(screens) else None
            result = self.identify(elements, page_urls=page_urls)
        except Exception:
            logger.debug("screen identification failed", exc_info=True)
            return {}
        identified = {
            "identified_as": result.get("matched"),
            "confidence": result.get("confidence"),
        }
        if result.get("confidence") == "ambiguous":
            identified["candidates"] = [
                result.get("matched"), *result.get("ambiguous_with", []),
            ]
        return identified

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
        page_urls: Sequence[Mapping[str, str | None]] | None = None,
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
        return identify_screen(elements, screens, page_urls)

    def validate(self, app: str | None = None) -> dict:
        """Check for collisions across loaded landmarks."""
        screens = self.all_screens(app)
        conventions = self.conventions(app)
        if not screens:
            result = {
                "collisions": [],
                "no_landmarks": [],
                "total_screens": 0,
                "error": "no_landmarks_loaded",
            }
        else:
            result = detect_collisions(screens)
        # Even with no screens: a knowledge base of nothing but identify_by
        # files loads zero screens, and its conventions are the explanation.
        if conventions:
            result["conventions"] = conventions_report(conventions)
        return result

    @property
    def is_empty(self) -> bool:
        """True if no landmarks are loaded."""
        return not self._sets


def load_remembered(
    registry: LandmarkRegistry, knowledge_bases: Mapping[str, str],
) -> dict[str, dict]:
    """Load each remembered knowledge base, and say how each one went: the
    answer is what `list_landmarks` shows, because a knowledge base that did
    not load -- a checkout moved, a volume not mounted -- reported only in the
    server log is one nobody driving quern will see.

    Never raises: a knowledge base that will not load must not stop the
    server starting.
    """
    outcomes: dict[str, dict] = {}
    for app, path in knowledge_bases.items():
        outcome: dict = {"path": path}
        try:
            # A hand-edited entry may say ~ or name the project root.
            directory = Path(path).expanduser()
            if (directory / ".quern" / "knowledge").is_dir():
                directory = directory / ".quern" / "knowledge"
            if not directory.is_dir():
                outcome["error"] = f"{path} is not a directory: moved, deleted, or not mounted"
            else:
                scan = scan_knowledge_base(directory)
                outcome["skipped"] = len(scan.skipped)
                if not scan.screens:
                    # Not registered: an empty set would list as loaded.
                    outcome["screens"] = 0
                    outcome["error"] = f"{path} has no screens with landmarks"
                else:
                    outcome["screens"], _ = registry.load_scan(app, scan, path)
        except Exception as e:  # noqa: BLE001 -- said, never fatal to start
            outcome["error"] = f"could not be loaded: {e}"
            logger.exception("Remembered knowledge base for %s at %s did not load", app, path)
        if outcome.get("error"):
            logger.warning("Remembered knowledge base for %s: %s", app, outcome["error"])
        outcomes[app] = outcome
    return outcomes

