"""The scroll-to-find loop must notice when nothing is scrolling.

Reported symptom: on screens with no scrollable content, `tap_element` swiped
repeatedly before reporting not-found — visibly thrashing, and slow enough to
look like a hung server. Measured at 16.6s and 30 swipes on a static screen.

The stall guard already existed, but only on the branch where the target is in
the tree yet off-screen. When the target is absent entirely — a typo, a wrong
label, an element on another screen — the loop fell into a blind sweep that
reset the guard on every iteration and spent the whole budget.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server.models import UIElement


def _screen(offset: float = 0.0) -> list[UIElement]:
    """A screen of three elements, shifted vertically by `offset`."""
    return [
        UIElement(type="StaticText", label=f"item_{i}", identifier=f"item_{i}",
                  frame={"x": 0, "y": 100 * i + offset, "width": 300, "height": 40})
        for i in range(3)
    ]


@pytest.fixture
def controller(monkeypatch):
    from server.device.controller_ui import DeviceControllerUI

    class Harness(DeviceControllerUI):
        def __init__(self):
            self.swipes = 0
            self.settles: list[int] = []
            self.offset = 0.0
            self.scrolls = False        # does the screen move when swiped?
            self.only_upward = False    # already at the bottom: only a reverse
                                        # swipe reveals anything
            self.controlled = True      # False models idb, whose swipes fling
            self.holds: list[float] = []

        def __post_init__(self):  # pragma: no cover - not a dataclass
            pass

        async def _get_screen_dimensions(self, _udid):
            return {"width": 393, "height": 852}

        async def resolve_udid(self, udid=None):
            return udid or "SIM"

        async def wait_for_settle(self, udid=None, timeout=10.0):
            """Stubbed: the real one screenshots, which this harness has no
            device for. Recorded rather than ignored so a test can assert the
            sweep settles *before* it looks -- reading the tree mid-fling is
            what lost an already-located row in #84."""
            self.settles.append(self.swipes)
            return {"settled": True, "elapsed_ms": 0.0, "reason": None}

        async def get_ui_elements(self, *_a, **_k):
            if _k.get("filter_type") == "Application":
                # How the sweep asks for the viewport; see test_scroll_sweep.
                from server.models import UIElement as _UIElement
                return ([_UIElement(
                    type="Application", identifier="", label="App",
                    frame={"x": 0, "y": 0, "width": 393, "height": 852},
                )], "SIM")
            return _screen(self.offset), "SIM"

        def _invalidate_ui_cache(self, _udid):
            pass

        def _ui_backend(self, _udid):
            backend = MagicMock()
            if not self.controlled:
                backend.swipe_is_controlled = False

            async def swipe(_udid, _x1, y1, _x2, y2, *_a, hold=0.0, **_k):
                self.swipes += 1
                self.holds.append(hold)
                reverse = y2 > y1        # near -> far reveals content above
                if self.only_upward:
                    if reverse:
                        self.offset += 50.0
                elif self.scrolls:
                    self.offset -= 50.0   # content moves under the finger
            backend.swipe = AsyncMock(side_effect=swipe)

            async def describe_point(_udid, _x, y):
                # The progress check hit-tests instead of reading the tree:
                # ~85ms versus ~1.8s. Whatever sits under the point moves with
                # the content, so its reported y is the signal.
                return {"identifier": "row", "frame": {"y": y + self.offset}}
            backend.describe_point = AsyncMock(side_effect=describe_point)
            return backend

    return Harness()


async def test_a_static_screen_stops_after_a_couple_of_swipes(controller):
    """The reported bug. A screen that cannot scroll must not consume the budget.

    Before the fix this ran the full blind sweep — max_swipes * 3 — because the
    per-iteration check looks for the target, which is absent either way, and
    nothing compared the screen against itself.
    """
    controller.scrolls = False
    found = await controller._ios_scroll_to_element(
        "SIM", label=None, identifier="never_exists", max_swipes=10,
    )
    assert found is None
    assert controller.swipes <= 3, (
        f"swiped {controller.swipes} times on a screen that never moved"
    )


async def test_the_budget_is_still_spent_when_the_screen_does_move(controller):
    """The other half: an absent target on a genuinely scrollable screen still
    gets a real search. Aborting early here would break scroll-to-element for
    lazy lists, which is the feature this loop exists for."""
    controller.scrolls = True
    found = await controller._ios_scroll_to_element(
        "SIM", label=None, identifier="never_exists", max_swipes=10,
    )
    assert found is None
    assert controller.swipes > 3, (
        f"only swiped {controller.swipes} times on a screen that was scrolling"
    )


async def test_a_target_that_is_present_is_returned_without_swiping(controller):
    controller.scrolls = False
    found = await controller._ios_scroll_to_element(
        "SIM", label=None, identifier="item_1", max_swipes=10,
    )
    assert found is not None
    assert found.identifier == "item_1"
    assert controller.swipes == 0


async def test_the_static_check_costs_no_extra_reads(controller):
    """Whether the list moved comes from the sweep's own reads.

    The check it replaced hit-tested three points before and after the first
    swipe, and on a physical device each hit-test is a full tree read -- six
    extra reads, measured at ~25s on an iPhone 11, before the sweep had
    looked for anything.
    """
    controller.scrolls = False
    backend = controller._ui_backend("SIM")
    controller._ui_backend = lambda _udid: backend
    await controller._ios_scroll_to_element(
        "SIM", label=None, identifier="never_exists", max_swipes=10,
    )
    backend.describe_point.assert_not_called()


async def test_a_list_at_the_bottom_is_not_mistaken_for_a_static_screen(controller):
    """The false-abort the reverse probe exists to prevent.

    A container already scrolled to the end cannot move further in the sweep's
    first direction, so one downward probe looks exactly like a screen with
    nothing scrollable. Content above it is still reachable, and calling it
    static there makes tap_element report not_found for targets the user can
    plainly see by swiping up.
    """
    controller.only_upward = True
    found = await controller._ios_scroll_to_element(
        "SIM", label=None, identifier="never_exists", max_swipes=10,
    )
    assert found is None
    assert controller.swipes > 3, (
        f"gave up after {controller.swipes} swipes; the reverse direction still moved"
    )


@pytest.mark.asyncio
async def test_a_controlled_sweep_holds_every_swipe_and_never_settles(controller):
    """The sweep's swipes stop dead, so there is nothing to wait for.

    A 105s sweep on iOS 18.6 settled after each of its 32 swipes and every
    settle timed out: the list was still flinging, 1020pt for a 389pt drag.
    Holding the swipe stops the fling, and the per-step settle that was
    compensating for it becomes 2.5s of nothing per step.
    """
    controller.scrolls = True
    await controller._ios_scroll_to_element(
        "SIM", label=None, identifier="never_exists", max_swipes=3,
    )

    assert controller.swipes > 0, "the sweep never swiped"
    assert all(h > 0 for h in controller.holds), (
        f"a sweep swipe was not held, so the list can fling past rows: {controller.holds}"
    )
    assert controller.settles == [], (
        f"settled {len(controller.settles)} time(s) after swipes that cannot fling"
    )


@pytest.mark.asyncio
async def test_a_sweep_whose_swipes_fling_settles_before_it_looks(controller):
    """idb cannot hold a swipe, so there the #84 fix still applies.

    The search read used to run mid-fling: it caught the target in flight, the
    fling carried it out of view, and the confirm rejected a sighting that had
    been real when it was taken. Counted rather than merely called, so one
    settle somewhere in the loop cannot satisfy it.
    """
    controller.scrolls = True
    controller.controlled = False
    await controller._ios_scroll_to_element(
        "SIM", label=None, identifier="never_exists", max_swipes=3,
    )

    assert controller.swipes > 0, "the sweep never swiped; nothing to settle after"
    assert len(controller.settles) == controller.swipes, (
        f"{controller.swipes} swipe(s) but {len(controller.settles)} settle(s) — "
        "some step read the tree while the list was still moving"
    )


@pytest.mark.asyncio
async def test_the_sweep_gives_up_when_its_deadline_passes(controller):
    """A swipe budget is not a time bound.

    The same 75 steps cost 25s or 500s depending on what a tree read costs that
    day; #84 produced sweeps of 413s and 523s against a caller that had given
    up at 180s. With a deadline already in the past, the sweep must return
    without swiping at all rather than spending its budget first.
    """
    controller.scrolls = True
    result = await controller._ios_scroll_to_element(
        "SIM", label=None, identifier="never_exists", max_swipes=25,
        deadline_s=-1.0,
    )

    assert result is None
    assert controller.swipes == 0, (
        f"the deadline had already passed but the sweep still made "
        f"{controller.swipes} swipe(s)"
    )


@pytest.mark.asyncio
async def test_every_backend_accepts_the_probe_keyword():
    """`describe_all(probe=...)` must exist on all four, not just sim-bridge.

    `_native_ui_elements` passes `probe=` on every call, and `_ui_backend`
    returns `U2Backend` for Android, `WdaBackend` for physical iOS, and
    `IdbBackend` when sim-bridge is unavailable — which is the state a machine
    is in whenever SimulatorKit cannot be found. A keyword only one backend
    accepted raised `TypeError` before fetching anything, so `scroll_to_element`
    crashed outright on three of the four.

    Checked by signature rather than by calling, so it covers the backends this
    suite has no device for.
    """
    import inspect

    from server.device.idb import IdbBackend
    from server.device.sim_bridge import SimBridgeBackend
    from server.device.u2_client import U2Backend
    from server.device.wda_client import WdaBackend

    for backend in (SimBridgeBackend, IdbBackend, WdaBackend, U2Backend):
        params = inspect.signature(backend.describe_all).parameters
        assert "probe" in params, (
            f"{backend.__name__}.describe_all has no `probe` parameter; "
            "_native_ui_elements passes it unconditionally, so this raises "
            "TypeError before any UI is read"
        )
        assert params["probe"].default is True, (
            f"{backend.__name__}.describe_all defaults probe to "
            f"{params['probe'].default!r}; every existing caller expects the "
            "probing behaviour it had before the keyword existed"
        )


@pytest.mark.asyncio
async def test_the_first_lookup_probes_even_though_the_sweep_does_not(controller):
    """A caller can ask to scroll to something only probing can see.

    Tab-bar items are exactly that on an iOS simulator: absent from the static
    tree, recovered by probing. Skipping the probe on the cold read would hide
    the target from the one lookup that could have found it without scrolling,
    and send the sweep hunting something no amount of swiping reveals.
    """
    seen: list[bool] = []

    async def _spy(*_a, **kw):
        # The viewport query that opens the sweep is not a lookup: it asks for
        # the root element to size the screen, and deliberately does not probe.
        if kw.get("filter_type") != "Application":
            seen.append(kw.get("probe_containers", True))
        return ([], "SIM")

    controller.get_ui_elements = _spy  # type: ignore[method-assign]
    controller.scrolls = True
    await controller._ios_scroll_to_element(
        "SIM", label=None, identifier="never_exists", max_swipes=1,
    )

    assert seen, "no lookup happened at all"
    assert seen[0] is True, "the cold lookup skipped probing"
    assert all(p is False for p in seen[1:]), (
        f"a sweep lookup probed: {seen} — that is the 3.5s-per-swipe cost this "
        "fix exists to remove"
    )


@pytest.mark.asyncio
async def test_a_slow_success_is_traced_not_just_a_failure(controller, caplog):
    """A sweep that works but takes far too long must still say why.

    The trace was originally emitted only by the give-up path, which left the
    most diagnosable case silent. Measured on iOS 18.6: the same scroll passed
    in 24s, 24s and 113s, and nothing in the log distinguished them — the cause
    had to be reconstructed from scattered [PERF] lines and a count of settle
    timeouts.

    `item_1` is on screen at rest, so this returns on the pre-loop lookup and
    the only variable under test is the timing.

    The clock is driven rather than raced. A tiny real `deadline_s` trips the
    pre-lookup deadline guard before anything can be slow, and a large one needs
    a genuinely slow sweep to cross the threshold; neither tests what this is
    for. Stepping `perf_counter` 20s per call puts the elapsed time well past a
    quarter of a 100s budget while staying inside the budget itself.
    """
    import itertools
    import logging

    ticks = itertools.count(0.0, 20.0)

    with (
        caplog.at_level(logging.INFO, logger="server.device"),
        patch("server.device.controller_ui.time.perf_counter", lambda: next(ticks)),
    ):
        found = await controller._ios_scroll_to_element(
            "SIM", label=None, identifier="item_1", max_swipes=5,
            deadline_s=100.0,
        )

    assert found is not None, "the target was on screen and should have been returned"
    assert any("slower than expected" in r.message for r in caplog.records), (
        "a slow success logged nothing; the trace still only fires on failure"
    )
    assert any("FOUND, but slowly" in r.message for r in caplog.records), (
        "the trace was logged without the line saying why it was emitted"
    )


@pytest.mark.asyncio
async def test_a_fast_success_stays_quiet(controller, caplog):
    """Silence on the common path is the point; a line per scroll is noise."""
    import logging

    with caplog.at_level(logging.INFO, logger="server.device"):
        found = await controller._ios_scroll_to_element(
            "SIM", label=None, identifier="item_1", max_swipes=5,
        )

    assert found is not None
    assert not any("slower than expected" in r.message for r in caplog.records), (
        "a fast sweep logged its trace; only slow ones should"
    )


async def test_the_sweep_reports_what_it_did_to_the_screen(controller):
    """Every swipe is a real gesture, and the caller has to be able to see it.

    `tap_element` puts this in its `not_found` response: the upward half of the
    sweep is the pull-to-refresh and sheet-dismiss drag, so an agent told only
    "not found" acts next against a screen it does not know has moved (#274).

    Asserted against the harness's own count rather than a constant. A test
    that fixed the number would have to be rewritten whenever the budget
    changes, and would pass while reporting a number that had stopped tracking
    the swipes actually made -- which is the failure this report exists to
    prevent, one level up.
    """
    controller.scrolls = False
    report: dict = {}

    await controller._ios_scroll_to_element(
        "SIM", label=None, identifier="never_exists", max_swipes=10,
        report=report,
    )

    assert report["swipes"] == controller.swipes
    assert report["swipes"] > 0, "the sweep swiped but reported nothing"
    assert report["moved"] is False


async def test_the_report_says_when_the_screen_moved(controller):
    """The other half. 'Swiped and nothing happened' and 'swiped and the list
    scrolled' leave the caller's screen in very different states."""
    controller.scrolls = True
    report: dict = {}

    await controller._ios_scroll_to_element(
        "SIM", label=None, identifier="never_exists", max_swipes=10,
        report=report,
    )

    assert report["moved"] is True


async def test_a_report_is_filled_in_even_when_nothing_is_found(controller):
    """The seeding, not the incrementing. Every exit from the sweep has to
    leave the caller able to say what happened -- including the ones that
    return before any swipe."""
    controller.scrolls = False
    report: dict = {}

    await controller._ios_scroll_to_element(
        "SIM", label=None, identifier="never_exists", max_swipes=0, report=report,
    )

    assert "swipes" in report and "moved" in report


async def test_the_report_says_the_screen_moved_even_when_the_target_is_found(
    controller,
):
    """`moved` was recorded only in the branch taken when the target was still
    absent, so a sweep that located it and kept scrolling toward it reported
    "nothing moved". Measured before the fix: 3 swipes, 150pt of travel,
    `{'swipes': 3, 'moved': False}`.

    Telling a caller the screen did not move when it did is worse than saying
    nothing -- they act next against a screen they believe is unchanged."""
    controller.scrolls = True
    report: dict = {}

    await controller._ios_scroll_to_element(
        "SIM", label=None, identifier="_Target", max_swipes=1, report=report,
    )

    assert report["swipes"] > 0
    assert report["moved"] is True, (
        f"swiped {report['swipes']} times and reported the screen unmoved"
    )
