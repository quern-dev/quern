"""Behaviour of the iOS scroll sweep, asserted as an ordered event log.

`test_scroll_to_find.py` drives the sweep against a screen that moves by a
fixed offset, which is right for "does it scroll" and cannot express the
questions that matter most here: *in what order* does it settle, fetch and
swipe, and *which way* does it go back when it loses a target it had found.
A review of #204 mutation-tested the sweep and found ten of eleven changes to
this code survived the existing suite — including moving the settle to after
the fetch, which undoes the #84 fix outright.

So this harness records every swipe, settle and fetch in order, serves
scripted UI reads, and drives the clock directly. Each test names the mutant it
exists to kill.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server.device.controller_ui import DeviceControllerUI
from server.models import UIElement

SCREEN = {"width": 393, "height": 852}


def _el(y: float, identifier: str = "target") -> UIElement:
    """An element whose top edge sits at `y`.

    With the harness screen, `_visible` accepts a top edge of at least 50 and a
    tap point no lower than 818 (852 minus the home-indicator inset).
    """
    return UIElement(
        type="Button", label=identifier, identifier=identifier,
        frame={"x": 40, "y": y, "width": 300, "height": 40},
    )


VISIBLE = 300.0     # comfortably on screen
BELOW = 900.0       # tap point past the bottom inset


class Recorder(DeviceControllerUI):
    """A sweep target that logs what the sweep does, in order.

    `respond(probe)` supplies each UI read. `moves_on` decides which swipe
    directions shift what the progress hit-test reports, which is how a test
    chooses whether the sweep sees a screen that scrolls or one that does not.
    """

    def __init__(self, respond, *, moves_on=("down",), clock=None):
        self.events: list[str] = []
        self.respond = respond
        self.moves_on = set(moves_on)
        self.position = 0.0
        self.clock = clock

    async def _get_screen_dimensions(self, _udid):
        return dict(SCREEN)

    async def resolve_udid(self, udid=None):
        return udid or "SIM"

    def _invalidate_ui_cache(self, _udid):
        pass

    async def wait_for_settle(self, udid=None, timeout=10.0):
        self.events.append("settle")
        return {"settled": True, "elapsed_ms": 0.0, "reason": None}

    async def get_ui_elements(self, *_a, probe_containers=True, **_k):
        self.events.append(f"fetch:{'probe' if probe_containers else 'plain'}")
        return self.respond(probe_containers), "SIM"

    def _ui_backend(self, _udid):
        backend = MagicMock()

        async def swipe(_udid, _x1, y1, _x2, y2, *_a, **_k):
            # The sweep's own convention: y1 > y2 is "down" (revealing content
            # below), and the reverse is "up".
            direction = "down" if y1 > y2 else "up"
            self.events.append(f"swipe:{direction}")
            if direction in self.moves_on:
                self.position += 100.0 if direction == "down" else -100.0

        async def describe_point(_udid, _x, y):
            self.events.append("hit-test")
            return {"identifier": "row", "frame": {"y": y + self.position}}

        backend.swipe = AsyncMock(side_effect=swipe)
        backend.describe_point = AsyncMock(side_effect=describe_point)
        return backend


def _script(*reads):
    """Serve `reads` in order, then nothing; ignores the probe flag."""
    queue = list(reads)

    def respond(_probe):
        return queue.pop(0) if queue else []
    return respond


def _flow(events, keep=("swipe", "settle", "fetch")):
    return [e for e in events if e.split(":")[0] in keep]


def _clock(start=0.0):
    """A clock the test advances by hand, so timing is not raced."""
    now = [start]
    return now, (lambda: now[0])


# -- M1: settle before looking -----------------------------------------------


@pytest.mark.asyncio
async def test_every_sweep_read_comes_after_a_settle():
    """M1 — the #84 fix itself.

    A search read taken mid-fling caught the target in flight and then lost it.
    The fix is ordering, not count: `test_the_sweep_settles_before_it_looks`
    compares the number of settles to the number of swipes, which is satisfied
    just as well by settling *after* the read. This checks the order.
    """
    ctrl = Recorder(_script([], [], [], []))
    await ctrl._ios_scroll_to_element("SIM", None, "target", max_swipes=1)

    flow = _flow(ctrl.events)
    sweep_reads = [i for i, e in enumerate(flow) if e == "fetch:plain"]
    assert sweep_reads, f"the sweep never read the screen: {flow}"
    for i in sweep_reads:
        assert flow[i - 1] == "settle", (
            f"a sweep read at position {i} followed {flow[i - 1]!r}, not a "
            f"settle — it looked while the list could still be moving: {flow}"
        )


# -- M2 / M2b: going back for a lost sighting --------------------------------


@pytest.mark.asyncio
async def test_a_lost_sighting_is_recovered_by_swiping_back():
    """M2 and M2b.

    The target is seen, the confirm read loses it, and the sweep must swipe
    back the *other* way before looking again — it is at most one swipe past.
    Removing the nudge still re-reads and can still recover by luck, so this
    asserts the swipe itself and its direction.
    """
    ctrl = Recorder(_script([], [_el(VISIBLE)], [], [_el(VISIBLE)]))
    found = await ctrl._ios_scroll_to_element("SIM", None, "target", max_swipes=3)

    assert found is not None, "the recovered target was not returned"
    swipes = [e for e in ctrl.events if e.startswith("swipe")]
    assert swipes == ["swipe:down", "swipe:up"], (
        f"expected a forward swipe then a swipe back, got {swipes}"
    )


@pytest.mark.asyncio
async def test_the_swipe_back_follows_a_settle_and_precedes_a_read():
    """The recovery read must not itself be mid-fling."""
    ctrl = Recorder(_script([], [_el(VISIBLE)], [], [_el(VISIBLE)]))
    await ctrl._ios_scroll_to_element("SIM", None, "target", max_swipes=3)

    flow = _flow(ctrl.events)
    back = flow.index("swipe:up")
    assert flow[back + 1:back + 3] == ["settle", "fetch:plain"], (
        f"after swiping back the sweep should settle and then look: {flow}"
    )


# -- M3 / M10: every success path reports when it is slow --------------------


@pytest.mark.asyncio
async def test_a_slow_confirmed_find_is_traced(caplog):
    """M3 — the confirm path, which is the common way a sweep succeeds."""
    now, clock = _clock()
    reads = iter([[], [_el(VISIBLE)], [_el(VISIBLE)]])

    def respond(_probe):
        items = next(reads, [])
        if items and now[0] == 0.0 and respond.seen:
            now[0] = 60.0            # the confirm read is where the time goes
        respond.seen = respond.seen or bool(items)
        return items
    respond.seen = False

    ctrl = Recorder(respond)
    with (
        caplog.at_level(logging.INFO, logger="quern-debug-server.device"),
        patch("server.device.controller_ui.time.perf_counter", clock),
    ):
        found = await ctrl._ios_scroll_to_element(
            "SIM", None, "target", max_swipes=3, deadline_s=100.0,
        )

    assert found is not None
    assert any("slower than expected" in r.message for r in caplog.records), (
        "a slow success through the confirm path logged nothing"
    )


@pytest.mark.asyncio
async def test_a_slow_find_on_the_reverse_path_is_traced(caplog):
    """M10 — the progress check's reverse branch has its own return."""
    now, clock = _clock()

    def respond(_probe):
        respond.calls += 1
        if respond.calls == 1:
            return []                 # cold lookup: not on screen
        now[0] = 60.0
        return [_el(VISIBLE)]         # found after the reverse swipe
    respond.calls = 0

    # A downward swipe changes nothing; only the reverse does. That is what
    # sends the sweep down its reverse branch.
    ctrl = Recorder(respond, moves_on=("up",))
    with (
        caplog.at_level(logging.INFO, logger="quern-debug-server.device"),
        patch("server.device.controller_ui.time.perf_counter", clock),
    ):
        found = await ctrl._ios_scroll_to_element(
            "SIM", None, "target", max_swipes=3, deadline_s=100.0,
        )

    assert found is not None, f"reverse path did not return: {ctrl.events}"
    assert any("slower than expected" in r.message for r in caplog.records), (
        "a slow success through the reverse branch logged nothing"
    )


# -- M7: the reverse branch settles too --------------------------------------


@pytest.mark.asyncio
async def test_the_reverse_progress_check_settles_before_hit_testing():
    """M7 — same defect as #84, on the branch that decides "does this scroll"."""
    ctrl = Recorder(_script([], [], [], []), moves_on=("up",))
    await ctrl._ios_scroll_to_element("SIM", None, "target", max_swipes=1)

    flow = _flow(ctrl.events, keep=("swipe", "settle", "hit-test"))
    back = flow.index("swipe:up")
    assert flow[back + 1] == "settle", (
        f"the reverse swipe was hit-tested without settling first: {flow}"
    )


# -- M4: the deadline is enforced inside the loop ----------------------------


@pytest.mark.asyncio
async def test_the_deadline_can_end_the_sweep_mid_run(caplog):
    """M4.

    The existing deadline test uses a deadline already past, which only ever
    exercises the check *before* the first lookup. Here the time runs out
    during that lookup, so only the in-loop check can stop the sweep.
    """
    now, clock = _clock()

    def respond(_probe):
        now[0] = 60.0                 # the cold lookup takes the budget
        return []

    ctrl = Recorder(respond)
    with (
        caplog.at_level(logging.INFO, logger="quern-debug-server.device"),
        patch("server.device.controller_ui.time.perf_counter", clock),
    ):
        found = await ctrl._ios_scroll_to_element(
            "SIM", None, "target", max_swipes=10, deadline_s=50.0,
        )

    assert found is None
    swipes = [e for e in ctrl.events if e.startswith("swipe")]
    assert swipes == [], f"the deadline had passed and the sweep still swiped: {swipes}"
    assert any("deadline of" in r.message for r in caplog.records), (
        "the in-loop deadline did not report why it stopped"
    )


# -- M13: a probe-only target keeps the sweep probing ------------------------


@pytest.mark.asyncio
async def test_a_probe_only_target_is_not_hunted_with_plain_reads():
    """M13.

    A tab-bar item is visible only to a probing read. If the sweep drops to
    plain reads it loses the target at once and hunts blindly for something
    no swipe reveals — 30 swipes in review, against 2 on main.
    """
    def respond(probe):
        # Present to a probing read, absent from a plain one, and never on
        # screen: chrome does not scroll.
        return [_el(BELOW)] if probe else []

    ctrl = Recorder(respond, moves_on=())
    found = await ctrl._ios_scroll_to_element("SIM", None, "target", max_swipes=10)

    assert found is None
    fetches = [e for e in ctrl.events if e.startswith("fetch")]
    assert fetches[:2] == ["fetch:probe", "fetch:plain"], (
        f"expected the cold probe and then one plain read to classify it: {fetches}"
    )
    assert all(f == "fetch:probe" for f in fetches[2:]), (
        f"the sweep dropped to plain reads for a probe-only target: {fetches}"
    )
    swipes = [e for e in ctrl.events if e.startswith("swipe")]
    assert len(swipes) <= 3, (
        f"{len(swipes)} swipes for a target scrolling cannot move; the stall "
        "check should end this after two"
    )
