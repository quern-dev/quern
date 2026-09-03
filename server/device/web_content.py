"""Turn Web Inspector page contents into screen-addressable elements.

The Inspector reports geometry in page space -- CSS pixels relative to the
viewport -- while every other part of Quern speaks device points relative to the
screen. Nothing in the Inspector protocol says where a `WKWebView` sits on
screen, and the view is absent from the accessibility tree entirely, so the
offset cannot be read off the native element list either.

It can be *measured*. An accessibility hit-test does reach web content, so one
confirmed hit whose label matches a DOM element's text pins the two coordinate
spaces together: the difference between the reported screen frame and the known
page rect is the offset. Everything else on the page follows from it.

Measured on joinmastodon.org/servers in a `WKWebView` pushed into a
`UINavigationController`: a page-space rect plus a constant (0, 126) offset at
scale 1 predicted a probe point inside the correct accessibility frame for 4 of
4 visible elements. The offset is derived per page rather than assumed -- a
second web view on the same screen (an embedded video player, viewport 354x199)
sits at a different origin.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from server.device.probing import DescribePointFn, frame_key


@dataclass(frozen=True)
class Anchor:
    """Maps page space to screen space."""

    dx: float
    dy: float
    scale: float = 1.0
    matched: int = 0
    """How many probe hits agreed on this offset. 1 is usable; more is better."""


def normalise(text: str | None) -> str:
    """Collapse whitespace so a DOM text node and an AXLabel compare equal.

    The accessibility label for a paragraph arrives with its internal newlines
    and runs of spaces already collapsed; the DOM text node does not.
    """
    return " ".join((text or "").split()).strip()


def to_screen(element: dict, anchor: Anchor) -> dict:
    """Project one page-space element into a screen-space frame."""
    return {
        "x": element.get("x", 0) * anchor.scale + anchor.dx,
        "y": element.get("y", 0) * anchor.scale + anchor.dy,
        "width": element.get("width", 0) * anchor.scale,
        "height": element.get("height", 0) * anchor.scale,
    }


def _element_type(element: dict) -> str:
    """Map a DOM node onto the accessibility vocabulary the rest of Quern uses.

    Callers address elements by `element_type` through `tap_element`, so a web
    button has to answer to "Button" like a native one.
    """
    role = (element.get("role") or "").lower()
    tag = (element.get("tag") or "").lower()
    if role in ("button", "link", "heading", "textbox", "checkbox", "radio", "tab"):
        return {
            "button": "Button", "link": "Link", "heading": "Heading",
            "textbox": "TextField", "checkbox": "Switch",
            "radio": "Button", "tab": "Button",
        }[role]
    if tag == "button":
        return "Button"
    if tag == "a":
        return "Link"
    if tag in ("input", "textarea", "select"):
        return "TextField"
    if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
        return "Heading"
    return "StaticText"


def project(contents: dict, anchor: Anchor, *, page_id: int | None = None) -> list[dict]:
    """Every element of a page as a Quern element dict in screen coordinates."""
    projected: list[dict] = []
    for element in contents.get("elements") or []:
        frame = to_screen(element, anchor)
        if frame["width"] <= 0 or frame["height"] <= 0:
            continue
        projected.append({
            "type": _element_type(element),
            "AXLabel": normalise(element.get("text")),
            "frame": frame,
            "enabled": True,
            "source": "web-inspector",
            "dom_id": element.get("id"),
            "tag": element.get("tag"),
            "href": element.get("href"),
            "interactive": bool(element.get("interactive")),
            "value": element.get("value"),
            "page_id": page_id,
        })
    return projected


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

# Enough probes to cross a screen at roughly one per text block, and few enough
# that a screen with no reachable web content costs well under a second.
MAX_ANCHOR_PROBES = 26

# A per-page ceiling, so one page that is not on screen cannot spend the whole
# budget before a page that is has been tried at all.
MAX_PROBES_PER_PAGE = 4

# A truncated accessibility label has to be this long before it is allowed to
# identify a DOM node by prefix alone. Short prefixes match far too much.
MIN_PREFIX_MATCH = 12

# Probes spent looking for an origin that geometry could not suggest. Only paid
# when the cheap path has already failed.
MAX_FALLBACK_PROBES = 14

# Independent probes that must agree before a probed offset is believed.
MIN_PROBE_AGREEMENT = 2

# Accessibility frames are rounded, so agreement is approximate.
ANCHOR_TOLERANCE_PT = 2.0

# Fractions of the screen width to sweep. Centre first because body text lives
# there, then the gutters: a short left-aligned heading has an accessibility
# frame only as wide as its glyphs, so a centre probe falls outside it and is
# correctly rejected.
ANCHOR_COLUMNS = (0.5, 0.25, 0.75)

# Two agreeing probes is the point of diminishing returns: the first establishes
# the offset and the second rules out a coincidental text match.
# The status bar and a nav bar, past which page content will not begin. Probing
# into system chrome only ever returns native elements.
DEFAULT_TOP_INSET_PT = 100.0

def candidate_anchors(native: list[dict], contents: dict, screen: dict) -> list[Anchor]:
    """Plausible page origins, best first, derived from geometry alone.

    A `WKWebView` is invisible to the accessibility tree, but the chrome around
    it is not: the view starts where the top chrome stops, and it is as tall as
    its own viewport. Both give a candidate origin for nothing more than
    arithmetic, and one probe then decides between them.
    """
    viewport = contents.get("viewport") or {}
    vw = float(viewport.get("width") or 0)
    vh = float(viewport.get("height") or 0)
    sx, sy = float(screen.get("x", 0)), float(screen.get("y", 0))
    sw, sh = float(screen.get("width", 0)), float(screen.get("height", 0))

    xs = [sx]
    if vw and abs(vw - sw) > 1:
        xs.append(sx + (sw - vw) / 2)

    ys: list[float] = []
    if vh:
        # Bottom-anchored: the view runs to the foot of the screen.
        ys.append(sy + sh - vh)
    for element in native:
        frame = element.get("frame") or {}
        bottom = float(frame.get("y", 0)) + float(frame.get("height", 0))
        # Top chrome only. A container occupying most of the screen is the
        # host view, and its bottom edge says nothing about where content began.
        if sy < bottom < sy + sh / 2 and float(frame.get("height", 0)) < sh / 2:
            ys.append(bottom)
    ys.append(sy)

    seen: set[tuple[float, float]] = set()
    ordered: list[Anchor] = []
    for y in ys:
        for x in xs:
            if (x, y) not in seen:
                seen.add((x, y))
                ordered.append(Anchor(dx=x, dy=y))
    return ordered


def _texts_correspond(label: str, text: str) -> bool:
    """Whether an accessibility label and a DOM text describe the same node.

    Accessibility truncates a long label, so a prefix still identifies the node.
    Anything shorter than `MIN_PREFIX_MATCH` is refused even so: measured on
    joinmastodon.org, a logo whose aria-label is "Mastodon" is a prefix of the
    body paragraph "Mastodon is not a single website...", and accepting that
    pairing anchored the page 215pt away from where it actually sat.
    """
    if not label or not text:
        return False
    if label == text:
        return True
    shorter, longer = sorted((label, text), key=len)
    return len(shorter) >= MIN_PREFIX_MATCH and longer.startswith(shorter)


def anchor_verification_targets(contents: dict, limit: int = 3) -> list[dict]:
    """DOM elements worth probing to confirm an offset.

    Largest first: a big element tolerates an imprecise hypothesis, because the
    predicted centre still lands inside it when the guess is off by a few points.
    """
    with_text = [
        e for e in (contents.get("elements") or [])
        if normalise(e.get("text")) and e.get("width") and e.get("height")
    ]
    with_text.sort(key=lambda e: e["width"] * e["height"], reverse=True)
    return with_text[:limit]


async def confirm_anchor(
    udid: str,
    describe_point: DescribePointFn,
    contents: dict,
    candidates: list[Anchor],
    *,
    native_screen: dict,
    max_probes: int = MAX_ANCHOR_PROBES,
) -> tuple[Anchor | None, int]:
    """Probe predicted positions until one confirms a candidate offset.

    Returns the anchor recomputed from the confirming hit -- the hypothesis only
    has to be close enough to land inside the element, and the observed frame
    then supplies the exact offset.
    """
    from server.device.web_probing import hit_contains

    probes = 0
    targets = anchor_verification_targets(contents)
    # Candidates are ordered by plausibility and targets only by size, so the
    # outer loop is over candidates: an unlikely origin should not consume the
    # budget before a likely one has been tried against every target. Measured
    # with an open nav menu, the reverse order spent all four probes on the
    # largest element and never reached the one that would have confirmed.
    for candidate in candidates:
        for element in targets:
            if probes >= max_probes:
                return None, probes
            frame = to_screen(element, candidate)
            x = frame["x"] + frame["width"] / 2
            y = frame["y"] + frame["height"] / 2
            left, top = native_screen.get("x", 0), native_screen.get("y", 0)
            if not left <= x <= left + native_screen.get("width", 0):
                continue
            if not top <= y <= top + native_screen.get("height", 0):
                continue
            hit = await describe_point(udid, x, y)
            probes += 1
            if not hit or not hit_contains(hit.get("frame"), x, y):
                continue
            if not _texts_correspond(normalise(hit.get("AXLabel")), normalise(element.get("text"))):
                continue
            observed = hit["frame"]
            return Anchor(
                dx=float(observed.get("x", 0)) - float(element.get("x", 0)),
                dy=float(observed.get("y", 0)) - float(element.get("y", 0)),
                matched=1,
            ), probes
    return None, probes


class InspectorLike(Protocol):
    """What collection needs from a Web Inspector connection.

    A Protocol rather than the concrete class, because every test here drives
    this with a fake -- the real one needs a booted simulator and a live socket.
    """

    async def connected_applications(self) -> list[dict]: ...

    async def pages(self, application_id: str) -> list[dict]: ...

    async def page_contents(self, application_id: str, page_id: int) -> dict | None: ...


class AttributeUdidFn(Protocol):
    """Maps an inspector application id to the simulator hosting it."""

    async def __call__(self, application_id: str) -> str | None: ...


# The Web Inspector reports WebKit's own helper processes alongside real apps.
# They are never what a caller means by "the app".
_HELPER_PREFIXES = ("process-", "com.apple.WebKit")


def _is_app(application: dict) -> bool:
    bundle = str(application.get("bundle_id") or "")
    return bool(bundle) and not bundle.startswith(_HELPER_PREFIXES)


def _unique_texts(dom: list[dict]) -> dict[str, dict]:
    """DOM elements indexed by text, dropping any text that appears twice.

    An ambiguous label identifies nothing: two elements sharing text sit at
    different page rects, so an offset derived from one may be wrong by the
    distance between them.
    """
    seen: dict[str, dict | None] = {}
    for element in dom:
        key = normalise(element.get("text"))
        if not key:
            continue
        seen[key] = None if key in seen else element
    return {text: el for text, el in seen.items() if el is not None}


async def anchor_by_probe(
    udid: str,
    describe_point: DescribePointFn,
    contents: dict,
    screen: dict,
    *,
    max_probes: int = MAX_FALLBACK_PROBES,
    min_agreement: int = MIN_PROBE_AGREEMENT,
) -> tuple[Anchor | None, int]:
    """Find an origin by sweeping, when geometry cannot suggest one.

    An out-of-process web view carries its own chrome -- an address bar above, a
    toolbar below -- and none of it appears in the native tree, so neither
    "below the top chrome" nor "as tall as its viewport, anchored to the foot of
    the screen" describes where the page starts. Measured on GoToSocial's
    settings page inside SFSafariViewController: a 402x685 viewport on a 402x874
    screen sits at y=106, while bottom-anchoring predicts 189.

    Matching here is **exact only**. An earlier design accepted containment and
    paired a logo labelled "Mastodon" with a paragraph beginning "Mastodon is
    not a single website...", anchoring the page 215pt from where it sat. Two
    independent probes must also agree, so a single freak match cannot decide
    the offset on its own.
    """
    index = _unique_texts(contents.get("elements") or [])
    if not index:
        return None, 0

    left, top = screen.get("x", 0), screen.get("y", 0)
    width, height = screen.get("width", 0), screen.get("height", 0)
    if width <= 0 or height <= 0 or max_probes < 1:
        return None, 0

    step = height / (max_probes + 1)
    # Offset plus the frame it came from. Two sweep rows can land inside one
    # tall element and yield identical offsets; counting those as two agreeing
    # probes would be counting the same evidence twice, which is exactly what
    # the agreement rule exists to prevent.
    candidates: list[tuple[tuple[float, float], tuple[int, int, int, int]]] = []
    probes = 0
    for i in range(max_probes):
        x = left + width * ANCHOR_COLUMNS[i % len(ANCHOR_COLUMNS)]
        y = top + step * (i + 1)
        hit = await describe_point(udid, x, y)
        probes += 1
        if not hit or not _contains(hit.get("frame"), x, y):
            continue
        element = index.get(normalise(hit.get("AXLabel")))
        if element is None:
            continue
        frame = hit["frame"]
        key = frame_key(frame)
        if key is None:
            continue
        candidates.append(((
            float(frame.get("x", 0)) - float(element.get("x", 0)),
            float(frame.get("y", 0)) - float(element.get("y", 0)),
        ), key))
        best = _largest_cluster(candidates)
        if len({key for _, key in best}) >= min_agreement:
            offsets = [offset for offset, _ in best]
            return Anchor(
                dx=sum(o[0] for o in offsets) / len(offsets),
                dy=sum(o[1] for o in offsets) / len(offsets),
                matched=len({key for _, key in best}),
            ), probes
    return None, probes


def _largest_cluster(
    candidates: list[tuple[tuple[float, float], tuple[int, int, int, int]]],
    tolerance: float = ANCHOR_TOLERANCE_PT,
) -> list[tuple[tuple[float, float], tuple[int, int, int, int]]]:
    """The biggest group of mutually-agreeing offsets, ranked by distinct hits.

    Averaging every candidate would land the page between the truth and any
    outlier -- wrong for every element rather than wrong for one. Ranking by
    distinct source frames rather than sample count keeps a single tall element
    sampled twice from outvoting two genuinely different ones.
    """
    best: list[tuple[tuple[float, float], tuple[int, int, int, int]]] = []
    for (pivot, _) in candidates:
        group = [
            (offset, key) for offset, key in candidates
            if abs(offset[0] - pivot[0]) <= tolerance
            and abs(offset[1] - pivot[1]) <= tolerance
        ]
        if len({k for _, k in group}) > len({k for _, k in best}):
            best = group
    return best


def _contains(frame: dict | None, x: float, y: float) -> bool:
    from server.device.web_probing import hit_contains
    return hit_contains(frame, x, y)


# Accessibility types that answer a tap. A probed element carries no DOM, so
# interactivity has to be read off the type the platform reports.
_INTERACTIVE_TYPES = frozenset({
    "Button", "Link", "TextField", "SearchField", "TextArea", "Switch",
    "Slider", "RadioButton", "CheckBox", "SegmentedControl", "Cell", "MenuItem",
})


def from_probe(hits: list[dict]) -> list[dict]:
    """Probed hits in the shape projected pages already use.

    A sweep returns accessibility elements in screen coordinates, so there is
    nothing to anchor -- but the caller should not have to care which route
    found an element, so both arrive looking the same. What differs is what is
    knowable: a probe has no DOM, so there is no id, tag or href to report.
    """
    elements: list[dict] = []
    for hit in hits:
        frame = hit.get("frame") or {}
        if not frame.get("width") or not frame.get("height"):
            continue
        element_type = hit.get("type") or "Other"
        elements.append({
            "type": element_type,
            "AXLabel": normalise(hit.get("AXLabel")),
            "frame": frame,
            "enabled": bool(hit.get("enabled", True)),
            "source": "web-probe",
            "dom_id": None,
            "tag": None,
            "href": None,
            "interactive": element_type in _INTERACTIVE_TYPES,
            "page_id": None,
            "value": hit.get("AXValue"),
        })
    return elements


def _anchor_hint_for(hints: list | None, page: dict) -> Anchor | None:
    """A recorded origin for this page, matched by URL.

    Matching on the URL rather than the screen name is what keeps this
    non-circular: the pages a hint could help locate are largely the ones with
    no native identity, so requiring the screen to be identified first would
    rule out every case worth optimising.
    """
    if not hints:
        return None
    url = str(page.get("url") or "")
    if not url:
        return None
    for hint in hints:
        recorded = getattr(hint, "url", None)
        anchor = getattr(hint, "anchor", None)
        if not recorded or anchor is None or not getattr(anchor, "origin", None):
            continue
        if recorded not in url and url not in recorded:
            continue
        origin = anchor.origin
        if len(origin) < 2:
            continue
        return Anchor(dx=float(origin[0]), dy=float(origin[1]))
    return None


async def collect_web_content(
    udid: str,
    bundle_id: str | None,
    describe_point: DescribePointFn,
    inspector: InspectorLike,
    native: list[dict],
    *,
    max_probes: int = MAX_ANCHOR_PROBES,
    attribute_udid: AttributeUdidFn | None = None,
    require_device_match: bool = False,
    hints: list | None = None,
) -> dict:
    """Read every inspectable page on screen and place it in screen coordinates.

    `inspector` and `describe_point` are injected so this is testable without a
    simulator, the same contract `probing.probe_container` uses.
    """
    from server.device.web_probing import app_frame

    result: dict = {
        "elements": [], "pages": [], "probes": 0, "anchored": False, "reason": None,
        "anchors": [],
    }

    applications = await inspector.connected_applications()
    if bundle_id:
        application = next(
            (a for a in applications if str(a.get("bundle_id") or "") == bundle_id), None)
        if application is None:
            result["reason"] = (
                f"{bundle_id} is not connected to the Web Inspector. The app must be "
                "running, and each WKWebView must set isInspectable = true (it is a "
                "per-instance property, so setting it on one web view says nothing "
                "about another)."
            )
            return result
    else:
        apps = [a for a in applications if _is_app(a)]
        if not apps:
            result["reason"] = (
                "no app is connected to the Web Inspector. The app must be running, "
                "and each WKWebView must set isInspectable = true."
            )
            return result
        if len(apps) > 1:
            result["reason"] = (
                "several apps are connected to the Web Inspector "
                f"({', '.join(str(a.get('bundle_id')) for a in apps)}); "
                "pass bundle_id to choose one."
            )
            return result
        application = apps[0]
    result["bundle_id"] = application.get("bundle_id")
    result["application_id"] = application.get("application_id")

    # The webinspectord socket is not labelled with a UDID, so a connection can
    # belong to a different booted simulator than the one asked about. Returning
    # that simulator's page as this one's would be wrong in a way no caller
    # could detect, so it is refused rather than reported.
    if attribute_udid is not None:
        owner = await attribute_udid(application.get("application_id"))
        result["device_verified"] = owner == udid
        if owner is not None and owner != udid:
            result["device_mismatch"] = True
            result["reason"] = (
                f"the Web Inspector connection belongs to simulator {owner}, not "
                f"{udid}. The webinspectord socket carries no UDID, so with more "
                "than one simulator booted the connection cannot be steered; shut "
                "the others down."
            )
            return result
        if owner is None and require_device_match:
            result["device_mismatch"] = True
            result["reason"] = (
                "more than one simulator is booted and this Web Inspector "
                f"connection could not be attributed to {udid}, so its content "
                "may belong to another device. Shut the others down."
            )
            return result

    application_id = application.get("application_id")
    listing = await inspector.pages(application_id)
    if not listing:
        result["reason"] = (
            "the app is connected but exposes no inspectable pages. isInspectable "
            "is per WKWebView instance, not per app, so a web view built elsewhere "
            "in the code may still be opted out."
        )
        return result

    contents: list[tuple[dict, dict]] = []
    for page in listing:
        page_contents = await inspector.page_contents(application_id, page["page_id"])
        if page_contents and page_contents.get("elements"):
            contents.append((page, page_contents))
    result["pages"] = [
        {"page_id": p["page_id"], "title": p.get("title"), "url": p.get("url"),
         "elements": len(c.get("elements") or [])}
        for p, c in contents
    ]
    if not contents:
        result["reason"] = "inspectable pages exist but none reported any visible elements."
        return result

    # A web view pushed into a navigation controller commonly binds the nav
    # title to the page's <title>, so a native label matching a page title says
    # that page is the frontmost one. Free, and it decides which page gets the
    # probe budget first.
    native_labels = {normalise(e.get("AXLabel")) for e in native if e.get("AXLabel")}
    native_labels.discard("")
    contents.sort(key=lambda pc: normalise(pc[0].get("title")) not in native_labels)

    # Each page is anchored on its own: two web views on one screen have
    # different origins, and an offset confirmed for one says nothing about
    # the other.
    #
    # Three passes, cheapest first, because they cost very differently. A
    # recorded origin is one probe. Geometry is up to four. Sweeping is a dozen
    # or more, so no page pays for it until every page has failed the cheaper
    # passes -- otherwise a page that is not on screen sweeps away the budget
    # before a page that is has been looked at at all.
    screen = app_frame(native) or {"x": 0, "y": 0, "width": 0, "height": 0}
    budget = max_probes

    async def attempt(page, page_contents, candidates, limit, strategy) -> bool:
        nonlocal budget
        if budget <= 0 or not candidates:
            return False
        anchor, probes = await confirm_anchor(
            udid, describe_point, page_contents, candidates,
            native_screen=screen, max_probes=min(budget, limit),
        )
        result["probes"] += probes
        budget -= probes
        if anchor is None:
            return False
        result["anchored"] = True
        result["elements"].extend(project(page_contents, anchor, page_id=page["page_id"]))
        viewport = page_contents.get("viewport") or {}
        # What an agent writes back into the knowledge base, so the next run
        # starts from a one-probe confirmation instead of a search.
        result["anchors"].append({
            "page_id": page["page_id"],
            "url": page.get("url"),
            "origin": [round(anchor.dx, 1), round(anchor.dy, 1)],
            "viewport": [viewport.get("width"), viewport.get("height")],
            "strategy": strategy,
        })
        return True

    located: set = set()
    for page, page_contents in contents:
        hint = _anchor_hint_for(hints, page)
        if hint is not None and await attempt(page, page_contents, [hint], 3, "hint"):
            located.add(page["page_id"])

    for page, page_contents in contents:
        if page["page_id"] in located:
            continue
        if await attempt(page, page_contents,
                         candidate_anchors(native, page_contents, screen),
                         MAX_PROBES_PER_PAGE, "geometry"):
            located.add(page["page_id"])

    # Sweep whatever is still unlocated. Gated on the page rather than on
    # whether anything anchored at all: two web views can be on screen together,
    # and one of them being found cheaply is no reason to leave the other
    # unreachable. This is the only route for a view whose chrome the native
    # tree cannot see, which is every out-of-process one.
    for page, page_contents in contents:
        if page["page_id"] in located:
            continue
        if budget <= 0:
            break
        anchor, extra = await anchor_by_probe(
            udid, describe_point, page_contents, screen,
            max_probes=min(budget, MAX_FALLBACK_PROBES),
        )
        result["probes"] += extra
        budget -= extra
        if anchor is None:
            continue
        result["anchored"] = True
        result["elements"].extend(
            project(page_contents, anchor, page_id=page["page_id"]))
        viewport = page_contents.get("viewport") or {}
        result["anchors"].append({
            "page_id": page["page_id"],
            "url": page.get("url"),
            "origin": [round(anchor.dx, 1), round(anchor.dy, 1)],
            "viewport": [viewport.get("width"), viewport.get("height")],
            "strategy": "sweep",
        })


    if not result["anchored"]:
        result["reason"] = (
            f"found {sum(len(c.get('elements') or []) for _, c in contents)} web "
            f"elements across {len(contents)} page(s), but {result['probes']} probes "
            "matched none of their text, so their position on screen is unknown. "
            "The page may be scrolled away, covered, or off screen."
        )
    return result
