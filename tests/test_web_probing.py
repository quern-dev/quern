"""Finding web content the accessibility tree cannot see.

Every behaviour asserted here was measured on a simulator first; the fixture
exists to reproduce it without one. The two that matter most are easy to get
wrong from first principles:

* A hit-test in whitespace returns the *nearest* element, not None. Treating
  that as a find records elements that are not where they are said to be.
* Probes cost ~93ms, so where you aim decides whether this is a second or a
  minute. A blind lattice took 57 probes to find 2 of 4 elements; aiming with a
  screenshot took 9 to find 4 on a harder page.
"""

from __future__ import annotations

import io

import pytest

from server.device import web_probing

SIM = "F5AF3736-C05F-493F-AA52-CA883B13B18C"
SCREEN = {"x": 0.0, "y": 0.0, "width": 400.0, "height": 800.0}


def el(type_, label, x, y, w, h, **extra):
    return {"type": type_, "AXLabel": label, "AXUniqueId": None,
            "frame": {"x": x, "y": y, "width": w, "height": h}, **extra}


class FakeScreen:
    """A screen of rects that answers hit-tests the way a simulator does.

    Crucially it never returns None for a miss: it returns the nearest rect,
    which is what makes the containment check load bearing rather than
    decorative.
    """

    def __init__(self, rects, *, strict_misses=False):
        self.rects = rects
        self.strict_misses = strict_misses
        self.calls: list[tuple[float, float]] = []

    async def describe_point(self, _udid, x, y):
        self.calls.append((x, y))
        for r in self.rects:
            f = r["frame"]
            if f["x"] <= x <= f["x"] + f["width"] and f["y"] <= y <= f["y"] + f["height"]:
                return r
        if self.strict_misses or not self.rects:
            return None
        return min(self.rects, key=lambda r: abs(
            (r["frame"]["y"] + r["frame"]["height"] / 2) - y))


def png_with_bands(bands, size=(400, 800)):
    """A PNG whose only content is dark stripes at the given DEVICE-POINT ranges.

    Screenshots come back at a device scale factor, so the fixture draws in
    pixels from point coordinates exactly as a real capture would.
    """
    from PIL import Image, ImageDraw
    image = Image.new("L", size, 255)
    draw = ImageDraw.Draw(image)
    scale = size[1] / SCREEN["height"]
    for top, bottom in bands:
        draw.rectangle([40, top * scale, size[0] - 40, bottom * scale], fill=0)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def shot_of(bands):
    async def _shot():
        return png_with_bands(bands)
    return _shot


# --------------------------------------------------------------------------
# The validity test
# --------------------------------------------------------------------------

def test_a_frame_that_does_not_contain_the_point_is_not_a_hit():
    frame = {"x": 0, "y": 100, "width": 100, "height": 20}
    assert web_probing.hit_contains(frame, 50, 110)
    assert not web_probing.hit_contains(frame, 50, 300)
    assert not web_probing.hit_contains(None, 50, 110)
    assert not web_probing.hit_contains({"x": 0, "y": 0, "width": 0, "height": 0}, 0, 0)


async def test_whitespace_answers_are_rejected():
    """The failure this guards against is silent and plausible-looking.

    A probe into blank space comes back with a real element that is simply
    somewhere else. Recording it produces an element list that looks right and
    places things where they are not, which is worse than finding nothing.
    """
    content = el("StaticText", "far away", 0, 700, 400, 20)
    screen = FakeScreen([content])
    # Aim at a band nowhere near the only element.
    result = await web_probing.sweep_web_content(
        SIM, screen.describe_point, shot_of([(100, 130)]), [el("Application", "", 0, 0, 400, 800)],
    )
    assert screen.calls, "it should have probed"
    assert result.elements == [], "a nearest-element answer must not be recorded"


# --------------------------------------------------------------------------
# Finding content
# --------------------------------------------------------------------------

async def test_finds_content_the_native_tree_omits():
    web = [
        el("Heading", "Probe Web Heading", 20, 124, 360, 40),
        el("StaticText", "Deterministic paragraph", 20, 184, 360, 22),
        el("StaticText", "unclicked", 20, 240, 100, 22),
        el("StaticText", "nested-span-target", 24, 260, 143, 22),
    ]
    screen = FakeScreen(web)
    native = [el("Application", "", 0, 0, 400, 800)]
    result = await web_probing.sweep_web_content(
        SIM, screen.describe_point,
        shot_of([(130, 155), (188, 202), (245, 258), (265, 278)]), native,
    )
    labels = {e["AXLabel"] for e in result.elements}
    assert labels == {r["AXLabel"] for r in web}, f"found {labels}"


async def test_native_elements_are_not_reported_as_web_content():
    """The tree already reports these; repeating them would inflate every
    screen summary with duplicates."""
    chrome = el("Button", "Done", 20, 60, 60, 30)
    screen = FakeScreen([chrome])
    result = await web_probing.sweep_web_content(
        SIM, screen.describe_point, shot_of([(60, 90)]),
        [el("Application", "", 0, 0, 400, 800), chrome],
    )
    assert result.elements == []


async def test_found_elements_are_tagged_with_their_source():
    """tap_element needs to know which elements came from a probe so it can
    re-verify one before acting on it."""
    web = el("Link", "Mastodon", 20, 150, 200, 24)
    screen = FakeScreen([web])
    result = await web_probing.sweep_web_content(
        SIM, screen.describe_point, shot_of([(150, 172)]),
        [el("Application", "", 0, 0, 400, 800)],
    )
    assert result.elements[0]["extra_attrs"]["source"] == "web-probe"


# --------------------------------------------------------------------------
# Cost
# --------------------------------------------------------------------------

async def test_a_wide_element_is_probed_once_not_once_per_column():
    """Coverage grows with each hit, so crossing bands and columns are free.
    Without this the probe count multiplies by the column count."""
    wide = el("StaticText", "spans the screen", 0, 300, 400, 60)
    screen = FakeScreen([wide])
    await web_probing.sweep_web_content(
        SIM, screen.describe_point, shot_of([(305, 355)]),
        [el("Application", "", 0, 0, 400, 800)],
    )
    assert len(screen.calls) == 1, f"probed {len(screen.calls)} times for one element"


async def test_the_probe_budget_is_honoured():
    rects = [el("StaticText", f"row {i}", 0, 100 + i * 30, 60, 20) for i in range(20)]
    screen = FakeScreen(rects)
    bands = [(100 + i * 30, 118 + i * 30) for i in range(20)]
    result = await web_probing.sweep_web_content(
        SIM, screen.describe_point, shot_of(bands),
        [el("Application", "", 0, 0, 400, 800)], max_probes=5,
    )
    assert result.truncated
    assert result.probes <= 5


# --------------------------------------------------------------------------
# Degrading honestly
# --------------------------------------------------------------------------

async def test_no_screenshot_reports_why_rather_than_sweeping_blindly():
    async def no_shot():
        return None
    screen = FakeScreen([el("StaticText", "x", 0, 100, 50, 20)])
    result = await web_probing.sweep_web_content(
        SIM, screen.describe_point, no_shot, [el("Application", "", 0, 0, 400, 800)],
    )
    assert result.elements == []
    assert result.reason and "screenshot" in result.reason
    assert not screen.calls, "it must not fall back to probing at random"


async def test_a_failing_probe_does_not_abort_the_sweep():
    calls = {"n": 0}

    async def flaky(_udid, x, y):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("sim-bridge: transient")
        return el("StaticText", "second band", 0, 300, 400, 30)

    result = await web_probing.sweep_web_content(
        SIM, flaky, shot_of([(100, 130), (305, 325)]),
        [el("Application", "", 0, 0, 400, 800)],
    )
    assert [e["AXLabel"] for e in result.elements] == ["second band"]


def test_a_full_screen_container_does_not_mask_the_page():
    """The cap that makes region detection work.

    A labelled scroll view covering the screen would otherwise mark everything
    as already-known and the sweep would probe nothing at all.
    """
    screen_rect = {"x": 0, "y": 0, "width": 400, "height": 800}
    covered = web_probing.coverage_rects([
        el("ScrollView", "Content", 0, 0, 400, 800),
        el("Button", "Done", 20, 60, 60, 30),
    ], screen_rect)
    assert covered == [(20.0, 60.0, 60.0, 30.0)]


def test_unlabelled_containers_contribute_no_coverage():
    """The web view's host view is an unlabelled Group. If containers counted,
    it would hide the very region this exists to search."""
    screen_rect = {"x": 0, "y": 0, "width": 400, "height": 800}
    assert web_probing.coverage_rects([el("Group", "", 0, 100, 400, 500)], screen_rect) == []


@pytest.mark.parametrize("bands", [[(100, 140)], [(100, 140), (300, 340)]])
def test_bands_are_returned_in_device_points(bands):
    png = png_with_bands(bands, size=(800, 1600))     # 2x the point size
    found = web_probing.content_bands(png, SCREEN)
    assert found, "the detector found nothing in an image with obvious content"
    for (want_top, want_bottom), (got_top, got_bottom) in zip(bands, found, strict=False):
        assert abs(got_top - want_top) < 12, f"{got_top} vs {want_top}"
        assert abs(got_bottom - want_bottom) < 12
