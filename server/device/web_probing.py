"""Find web content that the accessibility tree walk cannot see.

On iOS simulators `describe_all` does not descend into `WKWebView`, so an agent
looking at a screen with web content sees native chrome and nothing else. The
web view is not merely empty in the tree — it is absent entirely, so there is no
container to notice and nothing to bound a search with.

`describe_point` (AXPTranslator's `objectAtPoint:`) *does* reach web content,
with accurate frames in device points and useful types. Measured against a
remote page in an `SFSafariViewController` belonging to an app that never opted
into web inspection:

    (200, 200) -> Link       "Mastodon"
    (200, 380) -> StaticText "Mastodon is not a single website. To use it,"

So the content is reachable one point at a time. The problem this module solves
is *where to point*, cheaply.

Two behaviours make that harder than it sounds, both measured:

* A hit-test in whitespace does not return `None`. It returns the nearest
  element, whose frame does not contain the probe point. `hit_contains` is
  therefore the validity test, and without it a sweep records phantoms.
* A blind lattice is slow and lossy: 57 probes and 5.3s to find 2 of 4 elements
  on a simple page, because a fixed step jumps over short ones. Probes cost
  ~93ms, so the lattice is the wrong instrument.

A screenshot locates content far better. One capture, edge-filtered, yields
bands that map onto the real elements closely enough to aim at — around five
probes where the lattice needed 57.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from server.device import vision_ocr
from server.device.probing import DescribePointFn, frame_key

logger = logging.getLogger("quern-debug-server.device")

ScreenshotFn = Callable[[], Awaitable[bytes | None]]

# A native element only marks its area "already known" if it is a labelled or
# interactive leaf covering less than this share of the screen. The cap is load
# bearing: a labelled full-screen scroll view would otherwise mask the entire
# page, and the web view's own host view is an unlabelled container that must
# NOT contribute coverage, since its absence from the tree is the whole problem.
MAX_COVERAGE_SHARE = 0.6

# Content rows closer together than this are treated as one band, so a
# paragraph is one band rather than one per line of text. Kept small: bands are
# probed at more than one height, but a large gap merges genuinely separate
# elements and costs a probe to untangle.
_BAND_GAP_PT = 6.0

# How far to advance inside a band when a probe finds nothing.
_BAND_STEP_PT = 18.0

# The status bar renders above any web content, and probing it only rediscovers
# clock and cellular indicators the tree omits — real elements, but not what
# this is for, and each one costs a probe. Overridable for a genuinely
# fullscreen web view.
STATUS_BAR_PT = 60.0

INTERACTIVE_TYPES = frozenset({
    "Button", "Link", "TextField", "SecureTextField", "Switch", "Slider",
    "RadioButton", "CheckBox", "SearchField", "Stepper", "SegmentedControl",
})


@dataclass(slots=True)
class WebSweepResult:
    elements: list[dict] = field(default_factory=list)
    regions: list[dict] = field(default_factory=list)
    probes: int = 0
    elapsed_ms: float = 0.0
    truncated: bool = False
    reason: str | None = None


def hit_contains(frame: dict | None, x: float, y: float) -> bool:
    """Whether a returned frame genuinely contains the probed point.

    The hit-test answers a whitespace probe with the nearest element rather than
    nothing, so a result is only evidence of something *at* the point if its own
    frame says so.
    """
    if not frame:
        return False
    w = frame.get("width") or 0
    h = frame.get("height") or 0
    if w <= 0 or h <= 0:
        return False
    fx = frame.get("x") or 0
    fy = frame.get("y") or 0
    return fx <= x <= fx + w and fy <= y <= fy + h


def _rect(frame: dict) -> tuple[float, float, float, float]:
    return (
        float(frame.get("x") or 0), float(frame.get("y") or 0),
        float(frame.get("width") or 0), float(frame.get("height") or 0),
    )


def app_frame(native: list[dict]) -> dict | None:
    """The screen rect, taken from the Application element or the largest frame."""
    for el in native:
        if el.get("type") == "Application" and el.get("frame"):
            return el["frame"]
    framed = [el["frame"] for el in native if el.get("frame")]
    if not framed:
        return None
    return max(framed, key=lambda f: (f.get("width") or 0) * (f.get("height") or 0))


def coverage_rects(native: list[dict], screen: dict) -> list[tuple[float, float, float, float]]:
    """Areas whose contents the tree already reports, so probing them is waste."""
    screen_area = (screen.get("width") or 0) * (screen.get("height") or 0)
    covered: list[tuple[float, float, float, float]] = []
    for el in native:
        frame = el.get("frame")
        if not frame:
            continue
        x, y, w, h = _rect(frame)
        if w <= 0 or h <= 0:
            continue
        labelled = bool(el.get("AXLabel") or el.get("label"))
        if not labelled and el.get("type") not in INTERACTIVE_TYPES:
            continue
        if screen_area and (w * h) > MAX_COVERAGE_SHARE * screen_area:
            continue
        covered.append((x, y, w, h))
    return covered


def _covered(rects: list[tuple[float, float, float, float]], x: float, y: float) -> bool:
    return any(rx <= x <= rx + rw and ry <= y <= ry + rh for rx, ry, rw, rh in rects)


def content_bands(png: bytes, screen: dict, min_band_pt: float = 4.0) -> list[tuple[float, float]]:
    """Vertical bands that contain visible content, in device points.

    Rows are judged by how many pixels differ from the page background, not by
    edge energy. Edges were the first attempt and are wrong for this: an edge
    filter marks a shape's border, so a solid-filled button arrives as two
    hairlines as far apart as the button is tall, each too thin to survive a
    minimum-height filter. Text has dense edges and hid the flaw until a
    synthetic image exposed it.

    The background is taken as the most common value rather than assumed white,
    so a dark-mode page works the same way.
    """
    try:
        import io

        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is a declared dependency
        return []

    try:
        image = Image.open(io.BytesIO(png)).convert("L")
    except Exception:
        return []

    width_px, height_px = image.size
    screen_h = float(screen.get("height") or 0)
    if not width_px or not height_px or screen_h <= 0:
        return []
    scale = height_px / screen_h

    histogram = image.histogram()
    background = histogram.index(max(histogram))
    pixels = image.load()
    # Every 4th column is enough to answer "is there anything on this row" and
    # keeps the scan linear in height rather than in area.
    xs = range(0, width_px, 4)
    sampled = len(range(0, width_px, 4))
    min_differing = max(1, int(sampled * 0.01))

    rows = [
        sum(1 for x in xs if abs(pixels[x, y] - background) > 24) >= min_differing
        for y in range(height_px)
    ]

    runs: list[tuple[int, int]] = []
    start_y: int | None = None
    for y, has_content in enumerate(rows):
        if has_content and start_y is None:
            start_y = y
        elif not has_content and start_y is not None:
            runs.append((start_y, y))
            start_y = None
    if start_y is not None:
        runs.append((start_y, height_px))

    # Close hairline gaps from antialiasing and line spacing so a paragraph is
    # one band rather than one per line of text.
    gap_px = max(1, int(_BAND_GAP_PT * scale))
    merged: list[tuple[int, int]] = []
    for run_start, run_end in runs:
        if merged and run_start - merged[-1][1] <= gap_px:
            merged[-1] = (merged[-1][0], run_end)
        else:
            merged.append((run_start, run_end))

    return [
        (a / scale, b / scale) for a, b in merged
        if (b - a) / scale >= min_band_pt
    ]


async def sweep_web_content(
    udid: str,
    describe_point: DescribePointFn,
    screenshot: ScreenshotFn,
    native: list[dict],
    *,
    region: dict | None = None,
    top_inset: float = STATUS_BAR_PT,
    max_probes: int = 60,
    time_budget: float = 15.0,
    columns: tuple[float, ...] = (0.5, 0.25, 0.75),
) -> WebSweepResult:
    """Find content the tree walk missed, by aiming hit-tests with a screenshot.

    `describe_point` and `screenshot` are injected so this is testable without a
    device, the same way `probing.probe_container` takes its `describe_point`.
    """
    started = time.perf_counter()
    result = WebSweepResult()

    screen = region or app_frame(native)
    if not screen:
        result.reason = "no application frame to search within"
        return result

    covered = coverage_rects(native, screen)
    native_keys = {frame_key(el.get("frame")) for el in native if el.get("frame")}

    png = await screenshot()
    if not png:
        result.reason = "no screenshot available to aim with"
        return result

    # Vision names the text and boxes it, so each probe lands on something
    # rather than hunting along a row that merely has ink somewhere on it.
    # Measured on the same page: 10 boxes in 125ms against 75 probes and 6.9s
    # for the pixel heuristic. Bands remain the fallback for a screen Vision
    # cannot read, and for anything without text.
    targets: list[tuple[float, float]] = []
    for region in vision_ocr.text_regions(png, screen):
        targets.append((
            region["x"] + region["width"] / 2,
            region["y"] + region["height"] / 2,
        ))

    bands = [] if targets else content_bands(png, screen)
    if not targets and not bands:
        result.reason = "nothing on screen to aim at"
        return result

    top = float(screen.get("y") or 0) + top_inset
    bottom = top + float(screen.get("height") or 0)
    left = float(screen.get("x") or 0)
    width = float(screen.get("width") or 0)

    seen: set[tuple] = set()
    deadline = started + time_budget

    # Vision targets first: one probe each, at a known box centre.
    for tx, ty in targets:
        if result.probes >= max_probes or time.perf_counter() > deadline:
            result.truncated = True
            break
        if not (top <= ty <= bottom) or _covered(covered, tx, ty):
            continue
        try:
            hit = await describe_point(udid, tx, ty)
        except Exception as exc:
            logger.debug("web sweep: probe at (%.0f, %.0f) failed: %s", tx, ty, exc)
            continue
        result.probes += 1
        frame = hit.get("frame") if hit else None
        if not hit_contains(frame, tx, ty):
            continue
        covered.append(_rect(frame))
        key = frame_key(frame)
        if key in native_keys:
            continue
        dedupe = (key, hit.get("type"), hit.get("AXLabel") or "")
        if dedupe in seen:
            continue
        seen.add(dedupe)
        enriched = dict(hit)
        attrs = dict(enriched.get("extra_attrs") or {})
        attrs.update({"source": "web-probe",
                      "probe_x": f"{tx:.0f}", "probe_y": f"{ty:.0f}"})
        enriched["extra_attrs"] = attrs
        result.elements.append(enriched)

    for band_top, band_bottom in bands:
        # Walk down the band rather than sampling its middle once. Bands merge
        # when elements sit close together, and a single sample then finds
        # whichever of them happens to be under it and silently drops the rest.
        y = max(band_top + 1, top)
        band_end = min(band_bottom, bottom)
        while y <= band_end:
            advanced_to: float | None = None
            for fraction in columns:
                if result.probes >= max_probes or time.perf_counter() > deadline:
                    result.truncated = True
                    break
                x = left + width * fraction
                # Anything already accounted for costs nothing to skip, and the
                # skip list grows with every hit, so a wide element is probed
                # once however many bands or columns cross it.
                if _covered(covered, x, y):
                    continue
                try:
                    hit = await describe_point(udid, x, y)
                except Exception as exc:
                    logger.debug("web sweep: probe at (%.0f, %.0f) failed: %s", x, y, exc)
                    continue
                result.probes += 1
                frame = hit.get("frame") if hit else None
                if not hit_contains(frame, x, y):
                    continue  # whitespace: the nearest element, not one here

                covered.append(_rect(frame))
                fy, fh = float(frame.get("y") or 0), float(frame.get("height") or 0)
                advanced_to = max(advanced_to or 0.0, fy + fh + 1)

                key = frame_key(frame)
                if key in native_keys:
                    continue  # native chrome the tree already reported
                dedupe = (key, hit.get("type"), hit.get("AXLabel") or "")
                if dedupe in seen:
                    continue
                seen.add(dedupe)

                enriched = dict(hit)
                attrs = dict(enriched.get("extra_attrs") or {})
                attrs.update({"source": "web-probe",
                              "probe_x": f"{x:.0f}", "probe_y": f"{y:.0f}"})
                enriched["extra_attrs"] = attrs
                result.elements.append(enriched)

            if result.truncated:
                break
            # Step past whatever was found here; if nothing was, move on by a
            # line's worth so a tall blank band cannot spin.
            y = advanced_to if advanced_to and advanced_to > y else y + _BAND_STEP_PT
        if result.truncated:
            break

    result.regions = [dict(screen)]
    result.elapsed_ms = (time.perf_counter() - started) * 1000
    return result
