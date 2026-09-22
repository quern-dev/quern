"""Autoscroll is opt-in, or recorded — never speculative.

`tap_element` used to default to `scroll_to_find=True`, so a miss swept the
screen: down until nothing moved, then up until nothing moved. On a screen that
cannot scroll that is two real gestures before it can conclude anything, and
the second is a downward drag from near the top -- the pull-to-refresh and
sheet-dismiss gesture.

Detecting scrollability first is not possible. Measured on a booted simulator,
Settings and Safari both scroll and both report zero scroll containers in
`type` and in `role`: the accessibility tree quern reads exposes interactive
leaves, not containers. So the fact is recorded per screen in the knowledge
base instead of rediscovered per tap. See #274.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from server.device.controller import DeviceController
from server.device.landmarks import LandmarkRegistry
from server.models import DeviceType, Landmark, ScreenLandmarks, UIElement


def _element(label: str) -> UIElement:
    return UIElement(
        type="Button", label=label, identifier=None, value=None,
        frame={"x": 0, "y": 0, "width": 10, "height": 10}, enabled=True,
    )


def _registry(scrollable: bool | None, *, screen: str = "Home") -> LandmarkRegistry:
    reg = LandmarkRegistry()
    reg.load("app", [ScreenLandmarks(
        screen=screen,
        landmarks=[Landmark(element="Button", label="Anchor")],
        scrollable=scrollable,
    )])
    return reg


def _controller(registry: LandmarkRegistry | None):
    ctrl = DeviceController()
    ctrl._device_type_cache["AAAA-1111"] = DeviceType.SIMULATOR
    ctrl.resolve_udid = AsyncMock(return_value="AAAA-1111")
    ctrl._invalidate_ui_cache = MagicMock()
    # The tap misses, and the full-tree read returns the anchor that identifies
    # the screen -- the two reads tap_element really makes on this path.
    ctrl.get_ui_elements = AsyncMock(return_value=([_element("Anchor")], "AAAA-1111"))
    ctrl._ios_scroll_to_element = AsyncMock(return_value=None)
    ctrl._landmarks = registry
    return ctrl


async def _tap(ctrl, **kwargs):
    with patch(
        "server.device.controller_ui._capture_screenshot", AsyncMock(return_value=None),
    ):
        return await ctrl.tap_element(label="Nope", **kwargs)


class TestTheDefaultNoLongerSweeps:
    """The whole point. A default that swipes is a default that changes the
    screen, on screens where it cannot possibly help."""

    async def test_with_no_knowledge_base_at_all(self):
        ctrl = _controller(None)

        result = await _tap(ctrl)

        ctrl._ios_scroll_to_element.assert_not_awaited()
        assert result["scroll"]["attempted"] is False
        assert result["scroll"]["reason"] == "scrollability_unknown"

    async def test_with_a_screen_that_says_nothing(self):
        """Loaded, recognised, silent on the question. Same as no knowledge."""
        ctrl = _controller(_registry(None))

        result = await _tap(ctrl)

        ctrl._ios_scroll_to_element.assert_not_awaited()
        assert result["scroll"]["reason"] == "scrollability_unknown"

    async def test_the_unknown_case_names_the_retry(self):
        """A dead end that does not say how to get past it is worse than the
        old behaviour: the agent cannot tell 'not here' from 'not looked for'."""
        ctrl = _controller(None)

        result = await _tap(ctrl)

        assert "scroll_to_find=true" in result["scroll"]["detail"]
        assert "scrollable: true" in result["scroll"]["detail"]


class TestTheKnowledgeBaseDecidesWhenItCan:
    async def test_a_scrollable_screen_sweeps_without_being_asked(self):
        """The long-list case keeps working with no change at the call site --
        that is what makes default-off affordable."""
        ctrl = _controller(_registry(True))

        await _tap(ctrl)

        ctrl._ios_scroll_to_element.assert_awaited_once()

    async def test_a_fixed_screen_does_not(self):
        ctrl = _controller(_registry(False))

        result = await _tap(ctrl)

        ctrl._ios_scroll_to_element.assert_not_awaited()
        assert result["scroll"]["reason"] == "screen_not_scrollable"

    async def test_a_fixed_screen_does_not_suggest_a_pointless_retry(self):
        """Why `scrollable` is tri-state rather than a boolean: `False` and
        unknown both skip the sweep, and only `False` can say the retry will
        not help."""
        ctrl = _controller(_registry(False))

        result = await _tap(ctrl)

        assert "scroll_to_find=true" not in result["scroll"]["detail"]
        assert result["scroll"]["screen"] == "Home"

    async def test_an_explicit_true_beats_a_fixed_screen(self):
        """The knowledge base is a hint, never a gate. A wrong entry must cost
        a slowdown, not an unreachable element."""
        ctrl = _controller(_registry(False))

        await _tap(ctrl, scroll_to_find=True)

        ctrl._ios_scroll_to_element.assert_awaited_once()

    async def test_an_explicit_false_beats_a_scrollable_screen(self):
        ctrl = _controller(_registry(True))

        result = await _tap(ctrl, scroll_to_find=False)

        ctrl._ios_scroll_to_element.assert_not_awaited()
        assert result["scroll"]["reason"] == "scroll_to_find=false"


class TestAnAmbiguousIdentificationIsNotKnowledge:
    async def test_two_matching_screens_read_as_unknown(self):
        """Two screens can disagree about scrolling. Taking either would be a
        guess presented as knowledge, which is what the knowledge base exists
        to avoid."""
        reg = LandmarkRegistry()
        reg.load("app", [
            ScreenLandmarks(
                screen="A", scrollable=True,
                landmarks=[Landmark(element="Button", label="Anchor")],
            ),
            ScreenLandmarks(
                screen="B", scrollable=True,
                landmarks=[Landmark(element="Button", label="Anchor")],
            ),
        ])
        ctrl = _controller(reg)

        result = await _tap(ctrl)

        ctrl._ios_scroll_to_element.assert_not_awaited()
        assert result["scroll"]["reason"] == "scrollability_unknown"


class TestASweepThatRanIsVisible:
    """A gesture the caller cannot see is the defect `{"status": "ok"}` into a
    device with no input services was. The upward sweep is the
    pull-to-refresh drag; an agent that does not know it happened acts next
    against a screen it has not read."""

    async def test_the_response_says_it_swiped(self):
        ctrl = _controller(_registry(True))

        async def _sweep(*a, report=None, **k):
            if report is not None:
                report["swipes"] = 2
                report["moved"] = False
            return None

        ctrl._ios_scroll_to_element = AsyncMock(side_effect=_sweep)

        result = await _tap(ctrl)

        assert result["scroll"]["attempted"] is True
        assert result["scroll"]["swipes"] == 2
        assert result["scroll"]["moved"] is False
        assert "may have moved" in result["scroll"]["detail"]

    async def test_it_is_reported_even_when_the_sweep_found_nothing(self):
        """The case that matters: found means the caller sees the element and
        knows the screen moved. Not-found is where the swipes are invisible."""
        ctrl = _controller(_registry(True))

        result = await _tap(ctrl)

        assert result["status"] == "not_found"
        assert result["scroll"]["attempted"] is True
