"""Placing Web Inspector page geometry onto the screen.

The failure this module has to prevent is silent: a wrong offset still produces
well-formed elements with plausible frames, and the only symptom is taps landing
on the wrong pixels. So most of what follows is about refusing a correspondence
rather than finding one.

An earlier design searched for any DOM text matching any probe hit. It is gone,
and one of these tests records why: on joinmastodon.org a logo whose aria-label
is "Mastodon" matched the body paragraph "Mastodon is not a single website...",
and the search anchored the page 215pt from where it sat -- confidently, with no
error anywhere. Hypothesising an origin and confirming one predicted element
cannot make that mistake, because it asks "is this the element I expected here"
instead of "which element is this".
"""

from __future__ import annotations

from server.device.web_content import (
    Anchor,
    _texts_correspond,
    anchor_verification_targets,
    candidate_anchors,
    collect_web_content,
    confirm_anchor,
    normalise,
    project,
    to_screen,
)

SCREEN = {"x": 0, "y": 0, "width": 402, "height": 874}


def dom(text, x, y, w=100, h=20, **kw):
    return {"text": text, "x": x, "y": y, "width": w, "height": h, **kw}


def native_el(type_, label, x, y, w, h):
    return {"type": type_, "AXLabel": label,
            "frame": {"x": x, "y": y, "width": w, "height": h}}


def page(elements, vw=402, vh=746):
    return {"viewport": {"width": vw, "height": vh}, "scroll": {"x": 0, "y": 0},
            "elements": elements}


class FakeInspector:
    def __init__(self, apps, listing=None, contents=None):
        self.apps, self.listing, self.contents = apps, listing or [], contents or {}

    async def connected_applications(self):
        return self.apps

    async def pages(self, application_id):
        return self.listing

    async def page_contents(self, application_id, page_id):
        return self.contents.get(page_id)


class FakeScreen:
    """Elements answer hit-tests; whitespace answers with the nearest element,
    which is what the real AXPTranslator does and why containment is checked."""

    def __init__(self, elements):
        self.elements, self.probes = elements, []

    async def describe_point(self, udid, x, y):
        self.probes.append((x, y))
        for element in self.elements:
            f = element["frame"]
            if f["x"] <= x <= f["x"] + f["width"] and f["y"] <= y <= f["y"] + f["height"]:
                return element
        return self.elements[0] if self.elements else None


# ------------------------------------------------------------ correspondence

def test_an_exact_label_identifies_a_node():
    assert _texts_correspond("Servers", "Servers")


def test_a_truncated_label_identifies_a_node_by_prefix():
    """Accessibility clips long labels; the prefix still names the element."""
    assert _texts_correspond(
        "Mastodon is not a single website. ",
        "Mastodon is not a single website. To use it, you need an account",
    )


def test_a_short_prefix_identifies_nothing():
    """The measured failure: a logo's aria-label prefixes a body paragraph."""
    assert not _texts_correspond(
        "Mastodon", "Mastodon is not a single website. To use it")


def test_a_substring_that_is_not_a_prefix_is_refused():
    assert not _texts_correspond(
        "single website. To use", "Mastodon is not a single website. To use it")


def test_empty_text_never_corresponds():
    assert not _texts_correspond("", "Servers")
    assert not _texts_correspond("Servers", "")


# ------------------------------------------------------------ candidate origins

def test_candidate_origins_include_bottom_anchored_and_below_chrome():
    native = [native_el("Application", "App", 0, 0, 402, 874),
              native_el("Group", "", 0, 72, 402, 56)]
    ys = [a.dy for a in candidate_anchors(native, page([]), SCREEN)]
    assert 128 in ys       # 874 - 746, a view running to the foot of the screen
    assert 0 in ys


def test_the_host_view_is_not_mistaken_for_top_chrome():
    """A full-screen container's bottom edge is the bottom of the screen, which
    would put the page origin below everything and confirm nothing."""
    native = [native_el("Application", "App", 0, 0, 402, 874),
              native_el("Group", "", 0, 0, 402, 860)]
    assert 860 not in [a.dy for a in candidate_anchors(native, page([]), SCREEN)]


def test_a_narrower_viewport_offers_a_centred_origin():
    anchors = candidate_anchors([], page([], vw=354, vh=199), SCREEN)
    assert any(a.dx == (402 - 354) / 2 for a in anchors)
    assert any(a.dx == 0 for a in anchors)


def test_verification_targets_are_the_largest_text_elements():
    contents = page([
        dom("small", 0, 0, 10, 10), dom("huge", 0, 20, 300, 200),
        dom("", 0, 300, 400, 400), dom("medium", 0, 40, 100, 50),
    ])
    assert [t["text"] for t in anchor_verification_targets(contents)] == [
        "huge", "medium", "small",
    ]


# ------------------------------------------------------------ confirming

async def test_a_confirmed_hypothesis_yields_the_observed_offset_not_the_guess():
    """The hypothesis only has to land inside the element; the frame that comes
    back is what fixes the offset exactly."""
    screen = FakeScreen([native_el("Heading", "Servers", 24, 296, 144, 53)])
    anchor, probes = await confirm_anchor(
        "SIM", screen.describe_point, page([dom("Servers", 24, 170, 144, 53)]),
        [Anchor(dx=0, dy=128)], native_screen=SCREEN,
    )
    assert anchor is not None
    assert (anchor.dx, anchor.dy) == (0.0, 126.0), "refined from the hit, not the 128 guess"
    assert probes == 1


async def test_a_hypothesis_landing_in_whitespace_is_rejected():
    """The nearest-element answer looks like a hit and must not be treated as one."""
    screen = FakeScreen([native_el("Heading", "Servers", 24, 296, 144, 53)])
    anchor, _ = await confirm_anchor(
        "SIM", screen.describe_point, page([dom("Servers", 24, 170, 144, 53)]),
        [Anchor(dx=0, dy=600)], native_screen=SCREEN,
    )
    assert anchor is None


async def test_a_different_element_sitting_at_the_predicted_point_is_rejected():
    """The live failure, reconstructed: the prediction lands squarely inside a
    logo link whose label prefixes the paragraph's text. Containment alone would
    accept it; only the prefix-length rule refuses."""
    logo = native_el("Link", "Mastodon", 24, 154, 300, 120)
    screen = FakeScreen([logo])
    paragraph = dom("Mastodon is not a single website. To use it", 24, 240, 350, 148)
    # Chosen so the predicted centre falls inside the logo's frame.
    anchor, probes = await confirm_anchor(
        "SIM", screen.describe_point, page([paragraph]),
        [Anchor(dx=0, dy=-131)], native_screen=SCREEN,
    )
    assert probes == 1, "the point must actually have been probed"
    assert anchor is None


async def test_predictions_off_the_screen_cost_no_probe():
    screen = FakeScreen([native_el("Heading", "Servers", 24, 296, 144, 53)])
    _, probes = await confirm_anchor(
        "SIM", screen.describe_point, page([dom("Servers", 24, 170, 144, 53)]),
        [Anchor(dx=0, dy=5000)], native_screen=SCREEN,
    )
    assert probes == 0


async def test_predictions_beside_the_screen_cost_no_probe():
    """A centred-origin hypothesis for a wide element can put the predicted
    point past the right edge; probing there would query the wrong app."""
    screen = FakeScreen([native_el("Heading", "Servers", 24, 296, 144, 53)])
    _, probes = await confirm_anchor(
        "SIM", screen.describe_point, page([dom("Servers", 24, 170, 144, 53)]),
        [Anchor(dx=4000, dy=126)], native_screen=SCREEN,
    )
    assert probes == 0


async def test_confirmation_respects_its_probe_budget():
    screen = FakeScreen([native_el("Other", "nothing matches", 0, 0, 402, 874)])
    contents = page([dom(f"t{i}", 0, i * 30, 100, 20) for i in range(6)])
    _, probes = await confirm_anchor(
        "SIM", screen.describe_point, contents,
        [Anchor(dx=0, dy=d) for d in range(0, 400, 20)],
        native_screen=SCREEN, max_probes=3,
    )
    assert probes == 3


# ------------------------------------------------------------ projecting

def test_projection_places_the_page_on_screen():
    assert to_screen(dom("x", 24, 170, 354, 50), Anchor(dx=0, dy=126)) == {
        "x": 24, "y": 296, "width": 354, "height": 50}


def test_projection_honours_scale():
    assert to_screen(dom("x", 10, 10, 100, 20), Anchor(dx=5, dy=5, scale=2.0)) == {
        "x": 25, "y": 25, "width": 200, "height": 40}


def test_zero_area_elements_are_dropped():
    contents = page([dom("real", 0, 0), dom("collapsed", 0, 0, 0, 0)])
    assert [e["AXLabel"] for e in project(contents, Anchor(0, 0))] == ["real"]


def test_projected_elements_carry_their_provenance_and_dom_identity():
    contents = page([dom("Sign up", 10, 20, tag="button", id="cta", role=None)])
    element = project(contents, Anchor(dx=0, dy=100), page_id=3)[0]
    assert element["source"] == "web-inspector"
    assert element["dom_id"] == "cta"        # what the tree walk can never supply
    assert element["page_id"] == 3
    assert element["frame"]["y"] == 120


def test_dom_tags_map_onto_the_accessibility_vocabulary():
    """tap_element takes an element_type, so a web button must answer to the
    same name a native one does."""
    contents = page([
        dom("a", 0, 0, tag="button"), dom("b", 0, 20, tag="a"),
        dom("c", 0, 40, tag="h1"), dom("d", 0, 60, tag="input"),
        dom("e", 0, 80, tag="p"), dom("f", 0, 100, tag="div", role="button"),
    ])
    assert [e["type"] for e in project(contents, Anchor(0, 0))] == [
        "Button", "Link", "Heading", "TextField", "StaticText", "Button",
    ]


def test_labels_are_whitespace_normalised_to_match_accessibility():
    assert normalise("  Mastodon is\n  not one   site ") == "Mastodon is not one site"
    assert project(page([dom("Two\n\n  lines", 0, 0)]), Anchor(0, 0))[0]["AXLabel"] == "Two lines"


# ------------------------------------------------------------ collection

APP = {"application_id": "PID:1", "bundle_id": "com.example.app", "name": "Example"}


async def test_an_offscreen_page_cannot_starve_a_visible_one():
    """The live regression: an embedded video player elsewhere in the app spent
    the entire probe budget, so the page actually on screen was never tried."""
    inspector = FakeInspector(
        [APP],
        [{"page_id": 1, "title": "Video", "url": "https://v"},
         {"page_id": 3, "title": "Servers", "url": "https://s"}],
        {1: page([dom(f"hidden{i}", 0, i * 40, 300, 30) for i in range(5)], vw=354, vh=199),
         3: page([dom("Servers", 24, 170, 144, 53)])},
    )
    screen = FakeScreen([native_el("Heading", "Servers", 24, 296, 144, 53)])
    result = await collect_web_content(
        "SIM", "com.example.app", screen.describe_point, inspector,
        [native_el("Application", "App", 0, 0, 402, 874)],
    )
    assert result["anchored"] is True
    assert [e["AXLabel"] for e in result["elements"]] == ["Servers"]


async def test_a_page_whose_title_matches_native_chrome_is_probed_first():
    """A nav title bound to the page <title> identifies the frontmost page for
    free, so the visible page should confirm on the very first probe."""
    inspector = FakeInspector(
        [APP],
        [{"page_id": 1, "title": "Video", "url": "https://v"},
         {"page_id": 3, "title": "Servers - Mastodon", "url": "https://s"}],
        {1: page([dom("hidden", 0, 0, 300, 30)]),
         3: page([dom("Servers", 24, 170, 144, 53)])},
    )
    screen = FakeScreen([native_el("Heading", "Servers", 24, 296, 144, 53)])
    result = await collect_web_content(
        "SIM", "com.example.app", screen.describe_point, inspector,
        [native_el("Application", "App", 0, 0, 402, 874),
         native_el("Heading", "Servers - Mastodon", 123, 90, 156, 20)],
    )
    assert result["anchored"] is True
    # The very first probe is the prediction for the titled page's heading.
    # Other pages are still tried afterwards -- two web views can be on screen
    # at once -- so the total probe count does not show the ordering; this does.
    assert screen.probes[0] == (96.0, 324.5)
    assert [e["AXLabel"] for e in result["elements"]] == ["Servers"]


async def test_an_app_that_never_connected_says_why():
    result = await collect_web_content(
        "SIM", "com.example.app", FakeScreen([]).describe_point, FakeInspector([]), [])
    assert result["anchored"] is False
    assert "isInspectable" in result["reason"]


async def test_a_connected_app_with_no_inspectable_pages_says_why():
    result = await collect_web_content(
        "SIM", "com.example.app", FakeScreen([]).describe_point,
        FakeInspector([APP], []), [])
    assert "per WKWebView instance" in result["reason"]


async def test_pages_that_cannot_be_located_are_reported_not_guessed():
    """Elements with no anchor have no screen position. Emitting them at a
    guessed origin would put taps on arbitrary pixels."""
    inspector = FakeInspector(
        [APP], [{"page_id": 1, "title": "T", "url": "u"}],
        {1: page([dom("nowhere on screen", 0, 0, 300, 30)])})
    screen = FakeScreen([native_el("Other", "unrelated", 0, 0, 402, 874)])
    result = await collect_web_content(
        "SIM", "com.example.app", screen.describe_point, inspector,
        [native_el("Application", "App", 0, 0, 402, 874)])
    assert result["anchored"] is False
    assert result["elements"] == []
    assert "position on screen is unknown" in result["reason"]


# ------------------------------------------------- which simulator answered

async def test_content_from_another_simulator_is_refused():
    """The webinspectord socket carries no UDID, so a connection can belong to a
    different booted simulator. Returning its page as this device's would be
    wrong in a way no caller could detect."""
    inspector = FakeInspector(
        [APP], [{"page_id": 1, "title": "T", "url": "u"}],
        {1: page([dom("Servers", 24, 170, 144, 53)])})
    screen = FakeScreen([native_el("Heading", "Servers", 24, 296, 144, 53)])

    async def attribute(_application_id):
        return "SOME-OTHER-SIM"

    result = await collect_web_content(
        "SIM", "com.example.app", screen.describe_point, inspector,
        [native_el("Application", "App", 0, 0, 402, 874)],
        attribute_udid=attribute,
    )
    assert result["device_mismatch"] is True
    assert result["elements"] == []
    assert "SOME-OTHER-SIM" in result["reason"]


async def test_an_unattributable_connection_is_refused_when_it_matters():
    """With several simulators booted, "could not tell" has to fail closed."""
    inspector = FakeInspector(
        [APP], [{"page_id": 1, "title": "T", "url": "u"}],
        {1: page([dom("Servers", 24, 170, 144, 53)])})
    screen = FakeScreen([native_el("Heading", "Servers", 24, 296, 144, 53)])

    async def unknown(_application_id):
        return None

    result = await collect_web_content(
        "SIM", "com.example.app", screen.describe_point, inspector,
        [native_el("Application", "App", 0, 0, 402, 874)],
        attribute_udid=unknown, require_device_match=True,
    )
    assert result["device_mismatch"] is True


async def test_an_unattributable_connection_is_allowed_when_only_one_is_booted():
    """With a single simulator there is nothing to confuse it with, and failing
    would make the tool unusable on the ordinary setup."""
    inspector = FakeInspector(
        [APP], [{"page_id": 1, "title": "T", "url": "u"}],
        {1: page([dom("Servers", 24, 170, 144, 53)])})
    screen = FakeScreen([native_el("Heading", "Servers", 24, 296, 144, 53)])

    async def unknown(_application_id):
        return None

    result = await collect_web_content(
        "SIM", "com.example.app", screen.describe_point, inspector,
        [native_el("Application", "App", 0, 0, 402, 874)],
        attribute_udid=unknown, require_device_match=False,
    )
    assert not result.get("device_mismatch")
    assert result["anchored"] is True


async def test_a_matching_attribution_proceeds():
    inspector = FakeInspector(
        [APP], [{"page_id": 1, "title": "T", "url": "u"}],
        {1: page([dom("Servers", 24, 170, 144, 53)])})
    screen = FakeScreen([native_el("Heading", "Servers", 24, 296, 144, 53)])

    async def attribute(_application_id):
        return "SIM"

    result = await collect_web_content(
        "SIM", "com.example.app", screen.describe_point, inspector,
        [native_el("Application", "App", 0, 0, 402, 874)],
        attribute_udid=attribute, require_device_match=True,
    )
    assert result["device_verified"] is True
    assert [e["AXLabel"] for e in result["elements"]] == ["Servers"]
