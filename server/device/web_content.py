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
            "page_id": page_id,
        })
    return projected


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

# Enough probes to cross a screen at roughly one per text block, and few enough
# that a screen with no reachable web content costs well under a second.
MAX_ANCHOR_PROBES = 10

# A per-page ceiling, so one page that is not on screen cannot spend the whole
# budget before a page that is has been tried at all.
MAX_PROBES_PER_PAGE = 4

# A truncated accessibility label has to be this long before it is allowed to
# identify a DOM node by prefix alone. Short prefixes match far too much.
MIN_PREFIX_MATCH = 12

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
    describe_point,
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


# The Web Inspector reports WebKit's own helper processes alongside real apps.
# They are never what a caller means by "the app".
_HELPER_PREFIXES = ("process-", "com.apple.WebKit")


def _is_app(application: dict) -> bool:
    bundle = str(application.get("bundle_id") or "")
    return bool(bundle) and not bundle.startswith(_HELPER_PREFIXES)


async def collect_web_content(
    udid: str,
    bundle_id: str | None,
    describe_point,
    inspector,
    native: list[dict],
    *,
    max_probes: int = MAX_ANCHOR_PROBES,
    attribute_udid=None,
    require_device_match: bool = False,
) -> dict:
    """Read every inspectable page on screen and place it in screen coordinates.

    `inspector` and `describe_point` are injected so this is testable without a
    simulator, the same contract `probing.probe_container` uses.
    """
    from server.device.web_probing import app_frame

    result: dict = {
        "elements": [], "pages": [], "probes": 0, "anchored": False, "reason": None,
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
    # the other. A page whose content is scrolled out of view simply fails to
    # confirm and is reported rather than guessed at.
    screen = app_frame(native) or {"x": 0, "y": 0, "width": 0, "height": 0}
    budget = max_probes
    for page, page_contents in contents:
        if budget <= 0:
            break
        anchor, probes = await confirm_anchor(
            udid, describe_point, page_contents,
            candidate_anchors(native, page_contents, screen),
            native_screen=screen, max_probes=min(budget, MAX_PROBES_PER_PAGE),
        )
        result["probes"] += probes
        budget -= probes
        if anchor is None:
            continue
        result["anchored"] = True
        result["elements"].extend(project(page_contents, anchor, page_id=page["page_id"]))

    if not result["anchored"]:
        result["reason"] = (
            f"found {sum(len(c.get('elements') or []) for _, c in contents)} web "
            f"elements across {len(contents)} page(s), but {result['probes']} probes "
            "matched none of their text, so their position on screen is unknown. "
            "The page may be scrolled away, covered, or off screen."
        )
    return result
