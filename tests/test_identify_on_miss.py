"""A response that reports a miss says which screen it is reporting about.

`tap_element` finding nothing and `wait_for_element` timing out are the two
responses where a caller most needs to know where it actually is: an action
that succeeded implies you are roughly where you meant to be, while a miss is
exactly the moment you are lost. #278 gave the success paths `identified_as`
and left these two alone, so one endpoint answered in two shapes -- and a
caller could not tell "nothing matched" from "this path does not ask".

See #288.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from server.device.controller import DeviceController
from server.device.landmarks import LandmarkRegistry
from server.models import DeviceType, Landmark, ScreenLandmarks, UIElement


def _element(label: str, y: int = 0) -> UIElement:
    return UIElement(
        type="Button", label=label, identifier=None, value=None,
        frame={"x": 0, "y": y, "width": 10, "height": 10}, enabled=True,
    )


def _registry(*screens: ScreenLandmarks) -> LandmarkRegistry:
    reg = LandmarkRegistry()
    reg.load("app", list(screens))
    return reg


def _home() -> ScreenLandmarks:
    return ScreenLandmarks(
        screen="Home", landmarks=[Landmark(element="Button", label="Anchor")],
    )


def _controller(registry: LandmarkRegistry | None) -> DeviceController:
    ctrl = DeviceController()
    ctrl._invalidate_ui_cache = MagicMock()
    ctrl.resolve_udid = AsyncMock(return_value="SIM")
    ctrl.web_page_urls = AsyncMock(return_value=None)
    if registry is not None:
        ctrl._identify_lookup = registry.identify_for_context
    return ctrl


class TestTapElementNotFound:
    async def test_the_miss_names_the_screen(self):
        ctrl = _controller(_registry(_home()))

        ctx = await ctrl._identify_for_miss("SIM", [_element("Anchor")])

        assert ctx.get("identified_as") == "Home"
        assert ctx.get("confidence") == "exact"

    async def test_an_unrecognised_screen_says_so_rather_than_guessing(self):
        ctrl = _controller(_registry(_home()))

        ctx = await ctrl._identify_for_miss("SIM", [_element("Somewhere else")])

        assert ctx.get("identified_as") is None
        assert ctx.get("confidence") == "none"

    async def test_ambiguity_lists_candidates(self):
        reg = _registry(
            _home(),
            ScreenLandmarks(
                screen="Other", landmarks=[Landmark(element="Button", label="Anchor")],
            ),
        )
        ctrl = _controller(reg)

        ctx = await ctrl._identify_for_miss("SIM", [_element("Anchor")])

        assert ctx.get("confidence") == "ambiguous"
        assert sorted(ctx.get("candidates") or []) == ["Home", "Other"]


class TestItNeverTurnsAMissIntoAFailure:
    """The identification is an addition. The miss report is the answer the
    caller actually asked for, and must survive a knowledge base that cannot
    be consulted."""

    async def test_no_lookup_attached_adds_nothing(self):
        ctrl = _controller(None)

        assert await ctrl._identify_for_miss("SIM", [_element("Anchor")]) == {}

    async def test_a_lookup_that_raises_adds_nothing(self):
        ctrl = _controller(None)
        ctrl._identify_lookup = AsyncMock(side_effect=RuntimeError("kb gone"))

        assert await ctrl._identify_for_miss("SIM", [_element("Anchor")]) == {}

    async def test_an_empty_registry_adds_nothing(self):
        ctrl = _controller(LandmarkRegistry())

        assert await ctrl._identify_for_miss("SIM", [_element("Anchor")]) == {}


class TestThePageListingRule:
    """Read only when a loaded landmark needs it -- that is what keeps this
    free on a knowledge base without URL landmarks."""

    async def test_it_is_not_read_without_url_landmarks(self):
        ctrl = _controller(_registry(_home()))

        await ctrl._identify_for_miss("SIM", [_element("Anchor")])

        ctrl.web_page_urls.assert_not_awaited()

    async def test_it_is_read_and_used_when_one_needs_it(self):
        reg = _registry(ScreenLandmarks(
            screen="WebScreen", landmarks=[Landmark(web_url_contains="example.com")],
        ))
        ctrl = _controller(reg)
        ctrl.web_page_urls = AsyncMock(return_value=[{"url": "https://example.com/x"}])

        ctx = await ctrl._identify_for_miss("SIM", [_element("Anchor")])

        ctrl.web_page_urls.assert_awaited_once()
        assert ctx.get("identified_as") == "WebScreen", (
            "the listing was fetched but never reached identify()"
        )


class TestTheProductionWiringIsPinned:
    """#278 shipped this feature with its wiring entirely unheld: deleting the
    registry argument from every handler left 3699 tests green.

    The first version of this pinned it with `"_identify_lookup" in source`,
    which a typo satisfies -- renaming the attribute to `_identify_lookupX`
    left the suite green while identification was dead in production on both
    miss paths, because the controller reads it with `getattr(..., None)`.
    And the companion assertion about *where* it is injected got **easier**
    when the injection was moved into `create_app`, which is the failure it
    claimed to prevent.

    So this runs the real lifespan and asks the controller what it actually
    got, which is the only thing that cannot be satisfied by a near miss.
    """

    async def test_the_lifespan_binds_the_registry_to_the_controller(
        self, monkeypatch,
    ):
        from server.config import ServerConfig
        from server.lifecycle import network_monitor, update_check
        from server.main import create_app, lifespan

        # Entering the real lifespan runs startup, and startup reads the
        # machine: `update_network_state` establishes its baseline
        # synchronously, which measured at six `networksetup` spawns per run.
        # The subject here is one attribute binding, so the collaborators are
        # stubbed rather than left to answer from the developer's Wi-Fi.
        async def _never(*a, **k):
            return None

        monkeypatch.setattr(network_monitor, "update_network_state", lambda *a: None)
        monkeypatch.setattr(network_monitor, "network_monitor_loop", _never)
        monkeypatch.setattr(update_check, "periodic_update_check", _never)

        app = create_app(
            config=ServerConfig(api_key="k"),
            enable_oslog=False, enable_crash=False, enable_proxy=False,
        )
        app.state.device_controller = None  # replaced by the lifespan
        async with lifespan(app):
            ctrl = app.state.device_controller
            lookup = getattr(ctrl, "_identify_lookup", None)
            assert lookup is not None, (
                "the controller never received a lookup, so both miss paths "
                "identify nothing in production"
            )
            assert lookup.__self__ is app.state.landmark_registry, (
                "the lookup is bound to something other than the registry the "
                "API layer uses, so the two would answer differently"
            )

    def test_both_miss_paths_ask(self):
        """Source-level, and deliberately so: it guards against a call site
        being deleted, which the behavioural tests above already catch, but
        names the two sites explicitly so a third one added later is an
        obvious omission rather than a silent one."""
        import inspect

        from server.device import controller_ui

        src = inspect.getsource(controller_ui)
        assert src.count("_identify_for_miss(") >= 3, (
            "expected the definition plus both call sites -- tap_element's "
            "not_found and wait_for_element's timeout"
        )


class TestTheRealMissResponseCarriesIt:
    """Through `tap_element` itself, not the helper.

    #278 shipped this feature with 178 lines of green tests that all called
    the helper directly -- deleting the wiring from every handler left 3699
    tests passing. Asserting on the helper says nothing about whether the
    response a caller receives contains the field.
    """

    def _tapping_controller(self, registry):
        ctrl = DeviceController()
        ctrl._device_type_cache["AAAA-1111"] = DeviceType.SIMULATOR
        ctrl.resolve_udid = AsyncMock(return_value="AAAA-1111")
        ctrl._invalidate_ui_cache = MagicMock()
        # The tap misses; the full-tree read returns the anchor that identifies
        # the screen -- the two reads tap_element really makes on this path.
        ctrl.get_ui_elements = AsyncMock(
            return_value=([_element("Anchor")], "AAAA-1111"),
        )
        ctrl._ios_scroll_to_element = AsyncMock(return_value=None)
        ctrl.web_page_urls = AsyncMock(return_value=None)
        if registry is not None:
            ctrl._identify_lookup = registry.identify_for_context
        return ctrl

    async def _tap_that_misses(self, ctrl):
        with patch(
            "server.device.controller_ui._capture_screenshot",
            AsyncMock(return_value=None),
        ):
            return await ctrl.tap_element(label="NotOnThisScreen")

    async def test_a_not_found_response_names_the_screen(self):
        ctrl = self._tapping_controller(_registry(_home()))

        result = await self._tap_that_misses(ctrl)

        assert result["status"] == "not_found"
        assert result["screen_context"].get("identified_as") == "Home", (
            "the miss reported a screen context without saying which screen"
        )

    async def test_it_still_reports_the_miss_without_a_knowledge_base(self):
        """The identification is the addition; the miss is the answer."""
        ctrl = self._tapping_controller(None)

        result = await self._tap_that_misses(ctrl)

        assert result["status"] == "not_found"
        assert result["screen_context"].get("screen_title") is not None
        assert "identified_as" not in result["screen_context"]


class TestItRefusesToIdentifyAgainstAPartialTree:
    """`_all_elements_for_context` falls back to the *filtered* list when the
    full-tree read fails, and says so with `complete=False`. Identifying
    against that list compares landmarks with the target's own matches: at
    best it answers "recognised nothing" when nothing was read, and at worst
    it names a screen defined by an absent landmark, because everything is
    absent from a list of one.

    The scrollability path in the same function already refuses on this flag
    and explains why; this path discarded it.
    """

    def _controller_with_failed_full_read(self, registry):
        ctrl = DeviceController()
        ctrl._device_type_cache["AAAA-1111"] = DeviceType.SIMULATOR
        ctrl.resolve_udid = AsyncMock(return_value="AAAA-1111")
        ctrl._invalidate_ui_cache = MagicMock()
        ctrl.get_ui_elements = AsyncMock(return_value=([], "AAAA-1111"))
        ctrl._ios_scroll_to_element = AsyncMock(return_value=None)
        ctrl.web_page_urls = AsyncMock(return_value=None)
        ctrl._identify_lookup = registry.identify_for_context
        # The full read failed: the filtered list, and complete=False.
        ctrl._all_elements_for_context = AsyncMock(
            return_value=([_element("Anchor")], False),
        )
        return ctrl

    async def test_a_failed_full_read_names_no_screen(self):
        reg = _registry(
            _home(),
            # Matches a *partial* list precisely because its second landmark
            # is an absence -- the shape that turns a short list into a
            # confident wrong answer.
            ScreenLandmarks(screen="Login", landmarks=[
                Landmark(element="Button", label="Anchor"),
                Landmark(element="Button", label="Title", absent=True),
            ]),
        )
        ctrl = self._controller_with_failed_full_read(reg)

        with patch(
            "server.device.controller_ui._capture_screenshot",
            AsyncMock(return_value=None),
        ):
            result = await ctrl.tap_element(label="NotOnThisScreen")

        assert result["status"] == "not_found"
        ctx = result["screen_context"]
        assert "identified_as" not in ctx, (
            f"identified a screen from a partial tree: {ctx.get('identified_as')!r}"
        )
        assert "confidence" not in ctx, (
            "'confidence: none' from an unread screen says 'I looked and "
            "recognised nothing' when nothing was looked at"
        )


class TestTheTimeoutPathIdentifiesForReal:
    """Driven through `wait_for_element`, not counted in the source.

    Replacing the element list at that call site with `[]` -- every timeout
    reporting `confidence: none` forever -- left the whole suite green: the
    only thing watching was a `src.count(...)` assertion, which the mutation
    keeps satisfied. That is the exact shape this commit was written to fix
    on the other half.
    """

    def _timing_out_controller(self, registry):
        ctrl = DeviceController()
        ctrl._device_type_cache["AAAA-1111"] = DeviceType.SIMULATOR
        ctrl.resolve_udid = AsyncMock(return_value="AAAA-1111")
        ctrl._invalidate_ui_cache = MagicMock()
        # Never finds the target; the unfiltered re-read returns the anchor.
        ctrl.get_ui_elements = AsyncMock(
            return_value=([_element("Anchor")], "AAAA-1111"),
        )
        ctrl.web_page_urls = AsyncMock(return_value=None)
        ctrl._identify_lookup = registry.identify_for_context
        return ctrl

    async def test_a_timeout_names_the_screen(self):
        ctrl = self._timing_out_controller(_registry(_home()))

        with patch(
            "server.device.controller_ui._capture_screenshot",
            AsyncMock(return_value=None),
        ):
            result = await ctrl.wait_for_element(
                label="NeverAppears", condition="exists", timeout=0,
            )

        # Returns `(payload, resolved_udid)`, and reports `matched`, not `found`.
        payload, _ = result
        assert payload["matched"] is False
        assert payload["screen_context"].get("identified_as") == "Home", (
            "a timeout reported a screen context without saying which screen"
        )


class TestTheFlagIsProducedAsWellAsConsumed:
    """The tests above mock `_all_elements_for_context` and so hold only the
    *consumer* of `complete`. The line that turns a raising tree read into
    `complete=False` was held by nothing: flipping it to `True` left the whole
    suite green while reinstating both the wrong-screen identification and
    #274's "tell them to record a screen quern never read".

    So this drives the real producer -- `get_ui_elements` raises -- and
    asserts both consumers at once.
    """

    def _controller_whose_full_read_fails(self, registry):
        ctrl = DeviceController()
        ctrl._device_type_cache["AAAA-1111"] = DeviceType.SIMULATOR
        ctrl.resolve_udid = AsyncMock(return_value="AAAA-1111")
        ctrl._invalidate_ui_cache = MagicMock()
        ctrl.web_page_urls = AsyncMock(return_value=None)
        ctrl._identify_lookup = registry.identify_for_context
        ctrl._scrollable_lookup = registry.scrollable_for

        calls = {"n": 0}

        async def _reads(resolved, **kwargs):
            # The filtered read (the loop's own) succeeds; the unfiltered
            # re-read that `_all_elements_for_context` makes for context is
            # the one that fails -- which is the real shape, not a device
            # that is wholly unreachable.
            calls["n"] += 1
            if kwargs.get("filter_label") or kwargs.get("filter_identifier"):
                return [], "AAAA-1111"
            if calls["n"] > 1:
                raise RuntimeError("tree read failed")
            return [], "AAAA-1111"

        ctrl.get_ui_elements = AsyncMock(side_effect=_reads)
        ctrl._ios_scroll_to_element = AsyncMock(return_value=None)
        return ctrl

    async def test_a_raising_tree_read_identifies_nothing_and_says_why(self):
        ctrl = self._controller_whose_full_read_fails(_registry(_home()))

        with patch(
            "server.device.controller_ui._capture_screenshot",
            AsyncMock(return_value=None),
        ):
            result = await ctrl.tap_element(label="NotOnThisScreen")

        assert result["status"] == "not_found"
        ctx = result["screen_context"]
        assert "identified_as" not in ctx, (
            f"identified {ctx.get('identified_as')!r} from a tree it could not read"
        )
        # The #274 consumer of the same flag, asserted here because renaming
        # this reason also left the suite green.
        assert result["scroll"]["reason"] == "screen_unreadable", (
            "the scroll report must say the screen could not be read, not that "
            "nobody recorded whether it scrolls"
        )
