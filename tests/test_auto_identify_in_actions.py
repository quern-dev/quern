"""An action that returns screen context says which screen it landed on.

Identification used to need a second call: act, then ask
`get_screen_summary?identify=true` where you ended up. That made knowing your
own position an agent's responsibility to remember, and cost a second full
screen read to satisfy.

It is free where the context is already being built. The elements are in hand,
and `max_elements` truncates only the *summary* -- the list identification runs
against is the whole tree. With no landmarks loaded, nothing is added and
nothing extra is read.

See docs/screen-identification-in-actions.md, and
docs/proposals/kb-drift-measurement.md for what this enables.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from server.api.device import _capture_screen_context
from server.device.landmarks import LandmarkRegistry
from server.models import Landmark, ScreenLandmarks, UIElement


def _element(label: str) -> UIElement:
    return UIElement(
        type="Button", label=label, identifier=None, value=None,
        frame={"x": 0, "y": 0, "width": 10, "height": 10}, enabled=True,
    )


def _controller(elements):
    ctrl = MagicMock()
    ctrl.get_screen_summary = AsyncMock(return_value=(
        {"screen_title": "T", "summary": "S", "element_count": len(elements),
         "interactive_elements": []},
        elements,
        "SIM",
    ))
    ctrl.web_page_urls = AsyncMock(return_value=[])
    return ctrl


def _registry(*screens: ScreenLandmarks) -> LandmarkRegistry:
    reg = LandmarkRegistry()
    if screens:
        reg.load("app", list(screens))
    return reg


def _screen(name: str, label: str = "Anchor") -> ScreenLandmarks:
    return ScreenLandmarks(
        screen=name, landmarks=[Landmark(element="Button", label=label)],
    )


class TestIdentificationIsAddedWhenItCanBe:
    async def test_an_exact_match_names_the_screen(self):
        ctrl = _controller([_element("Anchor")])

        ctx = await _capture_screen_context(ctrl, "SIM", _registry(_screen("Home")))

        assert ctx["identified_as"] == "Home"
        assert ctx["confidence"] == "exact"

    async def test_the_existing_context_is_untouched(self):
        """Additive. Everything a caller reads today must still be there."""
        ctrl = _controller([_element("Anchor")])

        ctx = await _capture_screen_context(ctrl, "SIM", _registry(_screen("Home")))

        assert ctx["screen_title"] == "T"
        assert ctx["summary"] == "S"
        assert "interactive_elements" in ctx

    async def test_no_match_says_so_rather_than_omitting_the_field(self):
        """`identified_as: null` and a missing key mean different things: the
        first says quern looked, the second says it did not."""
        ctrl = _controller([_element("Something else")])

        ctx = await _capture_screen_context(ctrl, "SIM", _registry(_screen("Home")))

        assert ctx["identified_as"] is None
        assert ctx["confidence"] == "none"


class TestItCostsNothingWhenThereIsNoKnowledge:
    async def test_no_registry_adds_no_fields(self):
        """Every existing caller passes none of this until it is wired up."""
        ctrl = _controller([_element("Anchor")])

        ctx = await _capture_screen_context(ctrl, "SIM")

        assert "identified_as" not in ctx
        assert "confidence" not in ctx

    async def test_an_empty_registry_adds_no_fields(self):
        ctrl = _controller([_element("Anchor")])

        ctx = await _capture_screen_context(ctrl, "SIM", _registry())

        assert "identified_as" not in ctx

    async def test_the_web_page_listing_is_not_read_without_url_landmarks(self):
        """A knowledge base with no URL landmarks must cost nothing extra --
        the same rule `get_screen_summary?identify=true` follows. This is the
        one place identification could stop being free."""
        ctrl = _controller([_element("Anchor")])

        await _capture_screen_context(ctrl, "SIM", _registry(_screen("Home")))

        ctrl.web_page_urls.assert_not_awaited()

    async def test_it_is_read_when_a_landmark_needs_it(self):
        """And that the value reaches `identify`, not merely that it was
        fetched. Asserting only the await left `registry.identify(elements)`
        -- dropping `page_urls` -- passing the whole suite: quern would pay
        the Web Inspector read on every action and URL screens would never
        match, which is both costs and no benefit."""
        reg = LandmarkRegistry()
        reg.load("app", [ScreenLandmarks(
            screen="WebScreen",
            landmarks=[Landmark(web_url_contains="example.com")],
        )])
        ctrl = _controller([_element("Anchor")])
        ctrl.web_page_urls = AsyncMock(return_value=[{"url": "https://example.com/x"}])

        ctx = await _capture_screen_context(ctrl, "SIM", reg)

        ctrl.web_page_urls.assert_awaited_once()
        assert ctx.get("identified_as") == "WebScreen", (
            "the fetched page listing never reached identify()"
        )



class TestAKnowledgeBaseFailureCostsOnlyTheIdentification:
    """Losing `identified_as` is the intended degradation. Losing the screen
    is not -- and `all_screens()` raising did exactly that, because the guard
    sat above the `try` and the exception escaped into
    `_capture_screen_context`'s own handler, which returns `{}` for the whole
    context. The existing test makes `identify` raise, which proves the
    neighbour rather than the claim.
    """

    async def test_a_registry_that_cannot_be_listed_keeps_the_context(self):
        ctrl = _controller([_element("Anchor")])
        reg = MagicMock()
        reg.all_screens = MagicMock(side_effect=RuntimeError("kb unreadable"))

        ctx = await _capture_screen_context(ctrl, "SIM", reg)

        assert ctx.get("screen_title") == "T", (
            "a knowledge base failure discarded the screen context itself"
        )
        assert ctx.get("summary") == "S"
        assert "identified_as" not in ctx

class TestAmbiguityIsNotPresentedAsAnIdentification:
    def _two_matching(self):
        return _registry(_screen("A"), _screen("B"))

    async def test_candidates_are_listed(self):
        """Reporting only the first of several matches would present a guess
        as an identification."""
        ctrl = _controller([_element("Anchor")])

        ctx = await _capture_screen_context(ctrl, "SIM", self._two_matching())

        assert ctx["confidence"] == "ambiguous"
        assert sorted(ctx["candidates"]) == ["A", "B"]

    async def test_candidates_are_absent_on_an_exact_match(self):
        """Present only when it means something; an empty list on every exact
        match would read as "several, none named"."""
        ctrl = _controller([_element("Anchor")])

        ctx = await _capture_screen_context(ctrl, "SIM", _registry(_screen("Home")))

        assert "candidates" not in ctx


class TestAFailedIdentificationDoesNotFailTheAction:
    """Identification is an addition to an action's response. An action that
    worked must not report failure because a knowledge base could not be
    consulted."""

    async def test_a_broken_registry_leaves_the_context_intact(self):
        ctrl = _controller([_element("Anchor")])
        reg = MagicMock()
        reg.all_screens = MagicMock(return_value=[_screen("Home")])
        reg.identify = MagicMock(side_effect=RuntimeError("boom"))

        ctx = await _capture_screen_context(ctrl, "SIM", reg)

        assert ctx["summary"] == "S"
        assert "identified_as" not in ctx

    async def test_a_failing_page_listing_does_not_lose_the_context(self):
        reg = LandmarkRegistry()
        reg.load("app", [ScreenLandmarks(
            screen="WebScreen", landmarks=[Landmark(web_url_contains="x.com")],
        )])
        ctrl = _controller([_element("Anchor")])
        ctrl.web_page_urls = AsyncMock(side_effect=RuntimeError("inspector down"))

        ctx = await _capture_screen_context(ctrl, "SIM", reg)

        assert ctx["summary"] == "S"


# ---------------------------------------------------------------------------
# The wiring, through the real endpoints.
#
# Everything above exercises `_capture_screen_context` directly, which says
# nothing about whether any handler passes it a registry. Measured: removing
# `request.app.state.landmark_registry` from all four call sites left the whole
# suite green -- the feature could be deleted and nothing would notice. These
# drive the app so the argument itself is under test.
# ---------------------------------------------------------------------------


@pytest.fixture
def identifying_app():
    """The real app, with a registry that knows one screen and a controller
    that always reports being on it."""
    from server.config import ServerConfig
    from server.device.controller import DeviceController
    from server.main import create_app

    app = create_app(
        config=ServerConfig(api_key="test-key-12345"),
        enable_oslog=False, enable_crash=False, enable_proxy=False,
    )
    reg = LandmarkRegistry()
    reg.load("app", [ScreenLandmarks(
        screen="Home", landmarks=[Landmark(element="Button", label="Anchor")],
    )])
    app.state.landmark_registry = reg

    ctrl = DeviceController()
    ctrl._active_udid = "AAAA-1111"
    ctrl.resolve_udid = AsyncMock(return_value="AAAA-1111")
    ctrl.launch_app = AsyncMock(return_value="AAAA-1111")
    ctrl.open_url = AsyncMock(return_value="AAAA-1111")
    ctrl.get_screen_summary = AsyncMock(return_value=(
        {"screen_title": "T", "summary": "S", "element_count": 1,
         "interactive_elements": []},
        [_element("Anchor")],
        "AAAA-1111",
    ))
    ctrl.web_page_urls = AsyncMock(return_value=None)
    app.state.device_controller = ctrl
    return app


async def _post(app, path, body):
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        return await client.post(
            path, json=body, headers={"Authorization": "Bearer test-key-12345"},
        )


class TestTheHandlersActuallyPassTheRegistry:
    """One per wired endpoint. A handler that forgets the registry returns a
    screen context with no `identified_as`, which is indistinguishable from
    "nothing matched" -- so the caller cannot tell the feature is missing."""

    async def test_launch_app_identifies(self, identifying_app):
        r = await _post(identifying_app, "/api/v1/device/app/launch", {
            "bundle_id": "com.example.App", "include_screen_context": True,
        })

        assert r.status_code == 200, r.text
        ctx = r.json().get("screen_context") or {}
        assert ctx.get("identified_as") == "Home", ctx

    async def test_open_url_identifies(self, identifying_app):
        r = await _post(identifying_app, "/api/v1/device/open-url", {
            "url": "https://example.com", "include_screen_context": True,
        })

        assert r.status_code == 200, r.text
        ctx = r.json().get("screen_context") or {}
        assert ctx.get("identified_as") == "Home", ctx
