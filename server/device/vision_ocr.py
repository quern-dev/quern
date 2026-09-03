"""Locate on-screen text with Apple's Vision framework.

Used to aim accessibility hit-tests at web content. `describe_point` reaches
inside a `WKWebView` where the tree walk cannot, but each probe costs ~93ms, so
the question is where to point. Vision answers it directly: text with precise
bounding boxes, in one pass over a screenshot we already take.

Measured against a remote page in an `SFSafariViewController`: 10 observations
in 125ms. The pixel heuristic it replaces needed 75 probes and 6.9s to find
four elements, because it could only say "something is on this row" and then
had to hunt along it.

Called through pyobjc rather than a Swift helper deliberately — it runs
in-process, needs no compile step, and adds nothing that has to be signed or
shipped. The call is short enough to keep here rather than take a wrapper
library for it.

macOS only, like the rest of the simulator path. A missing framework degrades to
an empty result rather than raising, so callers fall back instead of failing.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("quern-debug-server.device")

# Vision's accurate path. Slower than .fast and worth it: .fast misses small
# type, which is most of a web page.
_RECOGNITION_ACCURATE = 1

_unavailable_logged = False


def available() -> bool:
    try:
        import Quartz  # noqa: F401
        import Vision  # noqa: F401
    except ImportError:
        return False
    return True


def text_regions(png: bytes, screen: dict) -> list[dict]:
    """Text found in a screenshot, as rects in device points.

    Returns `{"text", "x", "y", "width", "height", "confidence"}` per run, with
    coordinates in the same space as the accessibility tree so a caller can hand
    them straight to a hit-test.
    """
    global _unavailable_logged
    try:
        import Quartz
        import Vision
        from Foundation import NSData
    except ImportError:
        if not _unavailable_logged:
            logger.info(
                "Vision framework unavailable (pyobjc-framework-Vision not "
                "installed) — falling back to pixel analysis for web content",
            )
            _unavailable_logged = True
        return []

    screen_height = float(screen.get("height") or 0)
    if not png or screen_height <= 0:
        return []

    try:
        data = NSData.dataWithBytes_length_(png, len(png))
        source = Quartz.CGImageSourceCreateWithData(data, None)
        if source is None:
            return []
        image = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None)
        if image is None:
            return []
        width_px = Quartz.CGImageGetWidth(image)
        height_px = Quartz.CGImageGetHeight(image)
        if not width_px or not height_px:
            return []

        request = Vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(_RECOGNITION_ACCURATE)
        # Language correction rewrites UI strings into dictionary words, and the
        # text is used to match against accessibility labels, so it has to stay
        # verbatim.
        request.setUsesLanguageCorrection_(False)

        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, None)
        ok, _ = handler.performRequests_error_([request], None)
        if not ok:
            return []
        observations = request.results() or []
    except Exception as exc:  # pragma: no cover - defensive around a C bridge
        logger.debug("vision OCR failed: %s", exc)
        return []

    scale = height_px / screen_height
    if scale <= 0:
        return []

    regions: list[dict] = []
    for observation in observations:
        candidates = observation.topCandidates_(1)
        if not candidates:
            continue
        box = observation.boundingBox()
        # Vision normalises to 0..1 with the origin at the bottom left; the
        # accessibility tree measures points from the top left.
        regions.append({
            "text": str(candidates[0].string()),
            "confidence": float(candidates[0].confidence()),
            "x": box.origin.x * width_px / scale,
            "y": (1.0 - box.origin.y - box.size.height) * height_px / scale,
            "width": box.size.width * width_px / scale,
            "height": box.size.height * height_px / scale,
        })
    return regions
