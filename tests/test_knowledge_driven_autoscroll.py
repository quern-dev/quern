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
    ctrl._scrollable_lookup = registry.scrollable_for if registry else None
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
    """Two screens matching is not a guess to be broken; it is a state to be
    reported. They can disagree about scrolling, so taking either would be a
    guess presented as knowledge -- but the caller still has to be told which
    of the two silences they are in, because the fixes differ.

    The cause is mundane and was found by measurement, not imagination:
    landmarks loaded for two apps at once, where one screen matches both. Then
    a `scrollable: true` that is correctly recorded silently stops being
    consulted, and folding that into "nobody has said" tells the caller to
    record something they already recorded.
    """

    def _two_apps(self):
        reg = LandmarkRegistry()
        reg.load("app.one", [ScreenLandmarks(
            screen="A", scrollable=True,
            landmarks=[Landmark(element="Button", label="Anchor")],
        )])
        reg.load("app.two", [ScreenLandmarks(
            screen="B", scrollable=True,
            landmarks=[Landmark(element="Button", label="Anchor")],
        )])
        return reg

    async def test_it_does_not_sweep_on_a_guess(self):
        ctrl = _controller(self._two_apps())

        await _tap(ctrl)

        ctrl._ios_scroll_to_element.assert_not_awaited()

    async def test_it_says_ambiguous_rather_than_unknown(self):
        ctrl = _controller(self._two_apps())

        result = await _tap(ctrl)

        assert result["scroll"]["reason"] == "screen_ambiguous"

    async def test_it_does_not_tell_you_to_record_what_you_recorded(self):
        """The whole point of separating this from unknown."""
        ctrl = _controller(self._two_apps())

        result = await _tap(ctrl)

        assert "scrollable: true" not in result["scroll"]["detail"]
        assert "one app at a time" in result["scroll"]["detail"]

    async def test_scoping_to_one_app_resolves_it(self):
        """Not a test of tap_element, but of the advice the message gives. A
        remedy that does not work is worse than none."""
        reg = self._two_apps()

        assert reg.scrollable_for([_element("Anchor")], app="app.one").scrollable is True


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


class TestTheProductionWiringIsPinned:
    """Deleting the injection in `main.py` left the whole suite green.

    Every test here sets the lookups on its own controller, so nothing asserted
    that the running server ever connects the knowledge base to the device
    layer -- the same shape as the registry that was never attached at all and
    was found only by running the server. Read out of the source, because
    building the app needs a device stack this cannot have.
    """

    def _main_source(self) -> str:
        import pathlib

        return (
            pathlib.Path(__file__).resolve().parents[1] / "server" / "main.py"
        ).read_text()

    def test_the_scrollable_lookup_is_wired(self):
        assert "_scrollable_lookup = (" in self._main_source()
        assert "landmark_registry.scrollable_for" in self._main_source()

    def test_they_are_wired_where_the_controller_exists(self):
        """Attaching these beside the registry's construction raised
        AttributeError on boot: that runs in `create_app`, where
        `app.state.device_controller` is still None."""
        source = self._main_source()
        made = source.index("device_controller = DeviceController()")
        wired = source.index("_scrollable_lookup = (")

        assert wired > made, "wired before the controller is built"


class TestTheReportSurvivesTheDetailsThatWereWrong:
    async def test_an_ambiguous_report_names_the_screens(self):
        reg = LandmarkRegistry()
        reg.load("one", [ScreenLandmarks(
            screen="A", scrollable=True,
            landmarks=[Landmark(element="Button", label="Anchor")],
        )])
        reg.load("two", [ScreenLandmarks(
            screen="B", scrollable=True,
            landmarks=[Landmark(element="Button", label="Anchor")],
        )])
        ctrl = _controller(reg)

        result = await _tap(ctrl)

        assert sorted(result["scroll"]["candidates"]) == ["A", "B"]
        assert "'A'" in result["scroll"]["detail"]

    async def test_a_url_identified_app_says_it_needs_the_page_listing(self):
        """`web_url_contains` matches nothing without the page listing, so such
        a screen could never be recognised and its recorded `scrollable` was
        invisible. Saying 'unknown' told the caller to record what they had."""
        reg = LandmarkRegistry()
        reg.load("web", [ScreenLandmarks(
            screen="WebScreen", scrollable=True,
            landmarks=[Landmark(web_url_contains="example.com")],
        )])
        ctrl = _controller(reg)

        result = await _tap(ctrl)

        assert result["scroll"]["reason"] == "needs_page_urls"
        assert "record" not in result["scroll"]["detail"]

    async def test_an_explicit_true_that_cannot_be_honoured_says_why(self):
        """`label_contains` is not searchable by the scroll loop, so an
        explicit True is silently not done. Telling that caller to 'retry with
        scroll_to_find=true' is advice they have already taken."""
        ctrl = _controller(None)

        with patch(
            "server.device.controller_ui._capture_screenshot",
            AsyncMock(return_value=None),
        ):
            result = await ctrl.tap_element(
                label_contains="Nope", scroll_to_find=True,
            )

        assert result["scroll"]["reason"] == "not_searchable"
        assert "scroll_to_find=true" not in result["scroll"]["detail"]


class TestAndroidSaysWhenItSwept:
    """Android's fast path calls `scroll_into_view`, a real swipe loop, and the
    response said `attempted: False` -- then offered two remedies that are both
    no-ops there: unset already sweeps on Android, and no Android path reads
    `scrollable`. The exact defect the `scroll` object exists to prevent,
    reintroduced on the other platform."""

    def _android(self, backend):
        ctrl = DeviceController()
        ctrl._device_type_cache["emulator-5554"] = DeviceType.ANDROID_EMULATOR
        ctrl.resolve_udid = AsyncMock(return_value="emulator-5554")
        ctrl._invalidate_ui_cache = MagicMock()
        ctrl._ui_backend = MagicMock(return_value=backend)
        ctrl.get_ui_elements = AsyncMock(return_value=([], "emulator-5554"))
        return ctrl

    async def test_a_swept_android_screen_is_reported(self):
        backend = MagicMock()
        backend.tap_by_selector = AsyncMock(return_value=None)
        backend.scroll_into_view = AsyncMock(return_value=None)
        ctrl = self._android(backend)

        with patch(
            "server.device.controller_ui._capture_screenshot",
            AsyncMock(return_value=None),
        ):
            result = await ctrl.tap_element(identifier="nope")

        backend.scroll_into_view.assert_awaited_once()
        assert result["scroll"]["attempted"] is True, (
            "the device was swiped and the response says it was not"
        )

    async def test_it_does_not_offer_ios_remedies_to_android(self):
        backend = MagicMock()
        backend.tap_by_selector = AsyncMock(return_value=None)
        backend.scroll_into_view = AsyncMock(return_value=None)
        ctrl = self._android(backend)

        with patch(
            "server.device.controller_ui._capture_screenshot",
            AsyncMock(return_value=None),
        ):
            result = await ctrl.tap_element(identifier="nope")

        assert "scrollable: true" not in (result["scroll"].get("detail") or "")


class TestASuccessfulTapAlsoReportsTheSweep:
    """A tap that succeeded only *because* the screen scrolled has moved the
    screen, and `status: ok` alone does not say so. The easiest case to forget,
    because nothing went wrong -- and the same defect as the Android branch
    reporting `attempted: False`."""

    async def test_a_tap_after_a_sweep_says_it_scrolled(self):
        found = _element("Target")
        ctrl = _controller(_registry(True))
        ctrl._ios_scroll_to_element = AsyncMock(return_value=found)
        ctrl._ui_backend = MagicMock(return_value=MagicMock(tap=AsyncMock()))

        with patch(
            "server.device.controller_ui._capture_screenshot",
            AsyncMock(return_value=None),
        ):
            result = await ctrl.tap_element(label="Target", skip_stability_check=True)

        assert result["status"] == "ok"
        assert result["scroll"]["attempted"] is True

    async def test_a_plain_tap_carries_no_scroll_key(self):
        """Omitted rather than reported empty: a `scroll` object on every tap
        would make the one that means something invisible."""
        found = _element("Here")
        ctrl = _controller(_registry(True))
        ctrl.get_ui_elements = AsyncMock(return_value=([found], "AAAA-1111"))
        ctrl._ui_backend = MagicMock(return_value=MagicMock(tap=AsyncMock()))

        with patch(
            "server.device.controller_ui._capture_screenshot",
            AsyncMock(return_value=None),
        ):
            result = await ctrl.tap_element(label="Here", skip_stability_check=True)

        assert result["status"] == "ok"
        assert "scroll" not in result


class TestOneWebAppDoesNotDisableTheOthers:
    """The lookup passes no `app`, so `all_screens(None)` spans every loaded
    app. Checking `needs_page_urls` *before* identifying therefore let a single
    URL-identified screen anywhere turn off recorded scrollability everywhere
    -- a native screen that identifies perfectly well refused because some
    other app has a web screen.

    The check belongs after identification, and only when nothing matched."""

    def _both_apps(self):
        reg = LandmarkRegistry()
        reg.load("native", [ScreenLandmarks(
            screen="NativeHome", scrollable=True,
            landmarks=[Landmark(element="Button", label="Anchor")],
        )])
        reg.load("web", [ScreenLandmarks(
            screen="WebScreen",
            landmarks=[Landmark(web_url_contains="example.com")],
        )])
        return reg

    async def test_the_native_screen_is_still_identified(self):
        ctrl = _controller(self._both_apps())

        result = await _tap(ctrl)

        assert result["scroll"]["attempted"] is True, (
            "a web-identified screen in another app disabled this one"
        )

    async def test_a_genuinely_unmatched_screen_still_says_needs_page_urls(self):
        """The case the early return was added for must keep working."""
        reg = LandmarkRegistry()
        reg.load("web", [ScreenLandmarks(
            screen="WebScreen", scrollable=True,
            landmarks=[Landmark(web_url_contains="example.com")],
        )])
        ctrl = _controller(reg)

        result = await _tap(ctrl)

        assert result["scroll"]["reason"] == "needs_page_urls"


class TestAndroidGetsAdviceThatWorksOnAndroid:
    """An Android `tap_element` that skips the selector fast path -- `value`
    set, or `identifier` plus `element_type` -- is then skipped by the iOS
    block too, so it produced a report with nothing recorded and fell through
    to the iOS advice: retry with `scroll_to_find=true`, or add `scrollable:
    true` to the knowledge base.

    Both are no-ops on Android. Unset already sweeps there, and no Android path
    reads `scrollable`. Advice that cannot help is worse than none: it sends
    the caller somewhere that will not fix it."""

    def _android(self):
        ctrl = DeviceController()
        ctrl._device_type_cache["emulator-5554"] = DeviceType.ANDROID_EMULATOR
        ctrl.resolve_udid = AsyncMock(return_value="emulator-5554")
        ctrl._invalidate_ui_cache = MagicMock()
        ctrl.get_ui_elements = AsyncMock(return_value=([], "emulator-5554"))
        ctrl._ui_backend = MagicMock(return_value=MagicMock(
            tap_by_selector=AsyncMock(return_value=None),
        ))
        return ctrl

    async def _miss_off_the_fast_path(self, ctrl):
        with patch(
            "server.device.controller_ui._capture_screenshot",
            AsyncMock(return_value=None),
        ):
            # element_type keeps it off the selector path
            return await ctrl.tap_element(identifier="nope", element_type="Button")

    async def test_it_is_not_told_to_record_scrollable(self):
        result = await self._miss_off_the_fast_path(self._android())

        assert "scrollable: true" not in (result["scroll"].get("detail") or "")

    async def test_it_is_not_told_to_retry_with_scroll_to_find(self):
        """Already the effective default on Android."""
        result = await self._miss_off_the_fast_path(self._android())

        assert "scroll_to_find=true" not in (result["scroll"].get("detail") or "")

    async def test_it_is_told_what_would_actually_work(self):
        result = await self._miss_off_the_fast_path(self._android())

        assert result["scroll"]["reason"] == "not_searchable"
        assert "selector path" in result["scroll"]["detail"]


class TestASameAppUrlRivalIsNotResolvedByGuessing:
    """Without the page listing a `web_url_contains` landmark cannot match, so
    `identify_screen` reports a native sibling as "exact" when the honest
    answer is "one of two". Using the native screen's `scrollable` would be a
    guess wearing an exact match's clothes.

    Same app only. A URL screen in a *different* app is not a rival, and
    treating it as one is the regression that disabled recorded scrollability
    everywhere."""

    def _app_with_a_url_sibling(self):
        reg = LandmarkRegistry()
        reg.load("one", [
            ScreenLandmarks(
                screen="NativeHome", scrollable=True,
                landmarks=[Landmark(element="Button", label="Anchor")],
            ),
            # Same anchor, plus a URL it cannot check without the page listing.
            ScreenLandmarks(
                screen="WebVariant", scrollable=False,
                landmarks=[
                    Landmark(element="Button", label="Anchor"),
                    Landmark(web_url_contains="example.com"),
                ],
            ),
        ])
        return reg

    async def test_it_refuses_to_use_the_native_screens_value(self):
        ctrl = _controller(self._app_with_a_url_sibling())

        result = await _tap(ctrl)

        assert result["scroll"]["attempted"] is False
        assert result["scroll"]["reason"] == "needs_page_urls"

    async def test_a_url_screen_in_another_app_is_not_a_rival(self):
        """The distinction the whole check rests on."""
        reg = LandmarkRegistry()
        reg.load("one", [ScreenLandmarks(
            screen="NativeHome", scrollable=True,
            landmarks=[Landmark(element="Button", label="Anchor")],
        )])
        reg.load("two", [ScreenLandmarks(
            screen="OtherWeb",
            landmarks=[Landmark(web_url_contains="example.com")],
        )])
        ctrl = _controller(reg)

        result = await _tap(ctrl)

        assert result["scroll"]["attempted"] is True

    async def test_a_sibling_failing_on_something_native_is_not_a_rival(self):
        """Only a screen whose *sole* unmet landmark is the URL counts. One
        that failed on a native selector is simply not this screen."""
        reg = LandmarkRegistry()
        reg.load("one", [
            ScreenLandmarks(
                screen="NativeHome", scrollable=True,
                landmarks=[Landmark(element="Button", label="Anchor")],
            ),
            ScreenLandmarks(
                screen="Unrelated",
                landmarks=[
                    Landmark(element="Button", label="NotOnScreen"),
                    Landmark(web_url_contains="example.com"),
                ],
            ),
        ])
        ctrl = _controller(reg)

        result = await _tap(ctrl)

        assert result["scroll"]["attempted"] is True
