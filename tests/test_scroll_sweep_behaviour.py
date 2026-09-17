"""Behaviour of the iOS scroll sweep, against a modelled list.

`test_scroll_to_find.py` drives the sweep against three rows that shift by a
fixed offset, which is right for "does it stop on a static screen" and cannot
express the questions that matter most here: can any row be passed over, which
way does the sweep go at an end, and how many reads does each swipe cost.

History, because each part of the harness exists for a measured failure:

- A review of #204 mutation-tested the sweep and ten of eleven changes to it
  survived the suite, so every test here names the mutant it exists to kill.
- A 105s sweep on iOS 18.6 turned out to be flings: 1020pt of travel for a
  389pt drag, so rows were passed over. Sweep swipes are now held and 75% of
  the screen, and the model moves the list by exactly the drag.
- A drag past the end of a list snaps back on release; a read straight after
  it was 91pt off. `bounce` models that.
- idb cannot hold a swipe (`controlled=False`); WDA returns only once the app
  is idle (`at_rest=True`).

The model's physics follow measurement, not the sweep's assumptions: a
controlled swipe travels 0.9x its drag (625pt for 694pt), an idb swipe flings
2.6x (1020pt for 389pt), and a bounce decays over several reads, as a real
snap-back animates for a few tenths of a second. A model that moved exactly by
the drag let the review of this rebuild show idb passing over 87 of 200 rows
with every test green.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server.device.controller_ui import DeviceControllerUI
from server.models import UIElement

SCREEN = {"width": 393, "height": 852}
ROW = 44.0
TOP = 100.0          # where row 0 sits at offset 0, just under the nav bar
DRAG = 0.75          # the sweep's swipe, as a fraction of the screen
TRAVEL = 0.9         # a controlled swipe's travel, per unit of drag
FLING = 2.6          # an unheld swipe's travel, per unit of drag


class ListScreen(DeviceControllerUI):
    """A lazy list of `rows` rows under a fixed screen, and a log of the sweep.

    Only rows on screen are in the tree, as in a UITableView. A swipe moves the
    list by the drag, clamped to its ends. Every swipe, settle and read is
    recorded in order.
    """

    def __init__(self, rows: int = 200, *, offset: float = 0.0, row: float = ROW,
                 scrolls: bool = True, controlled: bool = True,
                 at_rest: bool = False, bounce: float = 0.0,
                 ids: bool = True, ticking: bool = False):
        self.ids = ids              # False: rows told apart only by label
        self.ticking = ticking      # a clock label that changes every read
        self.ticks = 0
        self.phantom: set[str] = set()   # seen by a filtered read only
        self.rows = rows
        self.row = row
        self.offset = offset
        self.scrolls = scrolls
        self.controlled = controlled
        self.at_rest = at_rest
        self.bounce = bounce
        self.bouncing = 0           # reads left that still show the bounce
        self.probe_only: set[str] = set()
        self.script: list[list[UIElement]] | None = None
        self.events: list[str] = []
        self.holds: list[float] = []
        self.drags: list[float] = []

    @property
    def max_offset(self) -> float:
        content = TOP + self.rows * self.row
        return max(0.0, content - (SCREEN["height"] - 34))

    def at_bottom(self) -> ListScreen:
        self.offset = self.max_offset
        return self

    def resting_y(self, index: int) -> float:
        return TOP + index * self.row - self.offset

    async def _get_screen_dimensions(self, _udid):
        return dict(SCREEN)

    async def resolve_udid(self, udid=None):
        return udid or "SIM"

    def _invalidate_ui_cache(self, _udid):
        pass

    async def wait_for_settle(self, udid=None, timeout=10.0):
        self.events.append("settle")
        return {"settled": True, "elapsed_ms": 0.0, "reason": None}

    async def get_ui_elements(self, *_a, probe_containers=True,
                              filter_label=None, filter_identifier=None, **_k):
        filtered = bool(filter_label or filter_identifier)
        self.events.append(
            f"read:{'probe' if probe_containers else 'plain'}"
            f":{'filtered' if filtered else 'full'}"
        )
        if self.script is not None and not filtered:
            els = self.script.pop(0) if self.script else []
        else:
            # Decays over three reads: bounce, half, a quarter, then rest.
            shift = self.offset
            if self.bouncing:
                shift += self.bounce / 2 ** (3 - self.bouncing)
                self.bouncing -= 1
            els = [UIElement(
                type="NavigationBar", identifier="nav", label="List",
                frame={"x": 0, "y": 50, "width": SCREEN["width"], "height": 44},
            )]
            for i in range(self.rows):
                y = TOP + i * self.row - shift
                if -self.row < y < SCREEN["height"]:
                    els.append(UIElement(
                        type="Cell", identifier=f"row_{i}" if self.ids else "",
                        label=f"Row {i}",
                        frame={"x": 0, "y": y, "width": SCREEN["width"],
                               "height": self.row},
                    ))
        if self.ticking:
            self.ticks += 1
            els.append(UIElement(
                type="StaticText", identifier="clock", label=f"0:{self.ticks:02d}",
                frame={"x": 300, "y": 60, "width": 60, "height": 20},
            ))
        if filtered:
            for ident in sorted(self.phantom):
                els.append(UIElement(
                    type="Cell", identifier=ident, label=ident,
                    frame={"x": 0, "y": 900, "width": 393, "height": ROW},
                ))
        if probe_containers:
            # Chrome only a probing read can see: a tab-bar item, whose tap
            # point sits below the home-indicator inset.
            for ident in sorted(self.probe_only):
                els.append(UIElement(
                    type="Button", identifier=ident, label=ident,
                    frame={"x": 0, "y": 800, "width": 80, "height": 49},
                ))
        if filter_identifier:
            els = [e for e in els if e.identifier == filter_identifier]
        if filter_label:
            els = [e for e in els if e.label == filter_label]
        return els, "SIM"

    def _ui_backend(self, _udid):
        backend = MagicMock()
        if not self.controlled:
            backend.swipe_is_controlled = False
        if self.at_rest:
            backend.swipe_returns_at_rest = True

        async def swipe(_udid, _x1, y1, _x2, y2, *_a, hold=0.0, **_k):
            # The sweep's convention: y1 > y2 is "down", revealing content below.
            self.events.append(f"swipe:{'down' if y1 > y2 else 'up'}")
            self.holds.append(hold)
            self.drags.append(abs(y1 - y2) / SCREEN["height"])
            if not self.scrolls:
                self.bouncing = 3 if self.bounce else 0   # short lists bounce too
                return
            travel = (TRAVEL if self.controlled else FLING) * (y1 - y2)
            wanted = self.offset + travel
            landed = min(max(wanted, 0.0), self.max_offset)
            self.bouncing = 3 if self.bounce and landed != wanted else 0
            self.offset = landed

        async def no_hit_tests(*_a, **_k):
            # Recorded, not raised: the check this replaced swallowed its own
            # exceptions, so raising would pass against it.
            self.events.append("hit-test")
            return None

        backend.swipe = AsyncMock(side_effect=swipe)
        backend.describe_point = AsyncMock(side_effect=no_hit_tests)
        return backend

    # -- reading the log ---------------------------------------------------

    def swipes(self) -> list[str]:
        return [e for e in self.events if e.startswith("swipe")]

    def after_each_swipe(self) -> list[list[str]]:
        """The events between each swipe and the next."""
        groups: list[list[str]] = []
        for event in self.events:
            if event.startswith("swipe"):
                groups.append([])
            elif groups:
                groups[-1].append(event)
        return groups


async def _sweep(screen: ListScreen, target: str, max_swipes: int = 10, **kw):
    return await screen._ios_scroll_to_element(
        "SIM", None, target, max_swipes=max_swipes, **kw,
    )


def _clock(start: float = 0.0):
    """A clock the test advances by hand, so timing is not raced."""
    now = [start]
    return now, (lambda: now[0])


# -- reach: no row is passed over --------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("index", [0, 15, 16, 17, 30, 31, 45, 99, 150, 198, 199])
async def test_every_row_below_is_reached_from_the_top(index):
    """M18, M27.

    With a swipe shorter than the visible area no row can be stepped over, and
    the downward budget has to cover the list: 200 rows need 13 swipes, more
    than the 10 an even split of the default budget gave. The old geometry,
    a 42% drag, needs 23.
    """
    screen = ListScreen()
    found = await _sweep(screen, f"row_{index}")

    assert found is not None, (
        f"row_{index} not reached in {len(screen.swipes())} swipes"
    )
    assert set(screen.swipes()) <= {"swipe:down"}


@pytest.mark.asyncio
async def test_the_sweep_drags_three_quarters_of_the_screen():
    """M18 — the step is sized to the viewport, not to a guess at a fling."""
    screen = ListScreen()
    await _sweep(screen, "row_60")
    assert screen.drags and all(abs(d - DRAG) < 0.01 for d in screen.drags), (
        screen.drags
    )


@pytest.mark.asyncio
async def test_a_found_row_is_returned_with_its_resting_frame():
    screen = ListScreen()
    found = await _sweep(screen, "row_60")
    assert found is not None
    assert found.frame["y"] == screen.resting_y(60)


# -- ends: turn around at the bottom, stop at the top ------------------------


@pytest.mark.asyncio
async def test_a_row_above_turns_the_sweep_around_at_the_bottom():
    """M25.

    The trace behind this: from the bottom of a Settings list, a sweep for a
    row above spent its whole downward budget -- ten swipes, ~60s on an
    iPhone 11 -- swiping at an end that could not move.
    """
    screen = ListScreen().at_bottom()
    found = await _sweep(screen, "row_3")

    assert found is not None
    swipes = screen.swipes()
    assert swipes[0] == "swipe:down" and set(swipes[1:]) == {"swipe:up"}, (
        f"expected one downward swipe to find the end, then up: {swipes}"
    )


@pytest.mark.asyncio
async def test_an_absent_target_is_swept_to_both_ends_and_no_further():
    """M26 — the top end ends the search; the budget need not.

    60 rows is four swipes of travel. Down to the end and one that does not
    move, then the same upward: ten swipes, against a budget of thirty.
    """
    screen = ListScreen(rows=60)
    found = await _sweep(screen, "row_missing")

    assert found is None
    assert screen.swipes() == ["swipe:down"] * 5 + ["swipe:up"] * 5, (
        screen.swipes()
    )


@pytest.mark.asyncio
async def test_the_downward_share_of_the_budget_is_two_thirds():
    """M27 — a list longer than the budget: down for 2x, then up for the rest."""
    screen = ListScreen(rows=5000)
    await _sweep(screen, "row_missing", max_swipes=3)

    assert screen.swipes() == ["swipe:down"] * 6 + ["swipe:up"] * 3, (
        screen.swipes()
    )


@pytest.mark.asyncio
async def test_a_static_screen_gives_up_after_one_swipe_each_way():
    screen = ListScreen(scrolls=False)
    found = await _sweep(screen, "row_missing")

    assert found is None
    assert screen.swipes() == ["swipe:down", "swipe:up"]


@pytest.mark.asyncio
async def test_a_list_moved_by_whole_rows_is_still_seen_to_move():
    """M30 — identity is part of the fingerprint.

    When a swipe moves the list by an exact number of rows, different rows land
    on the same positions. Compared on positions alone, that reads as nothing
    having moved, and the sweep turns back at an end it has not reached.
    """
    row = SCREEN["height"] * DRAG * TRAVEL / 10
    screen = ListScreen(rows=100, row=row)
    found = await _sweep(screen, "row_80")

    assert found is not None, f"gave up after {screen.swipes()}"


# -- reading at rest ---------------------------------------------------------


@pytest.mark.asyncio
async def test_a_bounce_on_a_static_screen_is_not_read_as_scrolling():
    """M16 — a list too short to scroll still bounces when dragged.

    Judged on the first read after the swipe, the bounce looks like movement
    and the sweep spends its whole budget on a screen that cannot scroll.
    """
    screen = ListScreen(scrolls=False, bounce=40.0)
    found = await _sweep(screen, "row_missing")

    assert found is None
    assert screen.swipes() == ["swipe:down", "swipe:up"], screen.swipes()


@pytest.mark.asyncio
async def test_a_row_sighted_mid_bounce_is_returned_where_it_rests():
    """M16 — measured: 91pt between the first read and the resting position."""
    screen = ListScreen(bounce=91.0)
    found = await _sweep(screen, "row_199")

    assert found is not None
    assert found.frame["y"] == screen.resting_y(199), (
        f"returned y={found.frame['y']}, resting y={screen.resting_y(199)}"
    )


@pytest.mark.asyncio
async def test_controlled_swipes_are_held_and_never_settled():
    """M15 — the hold is what stops the fling, and the settle it replaces
    timed out on every one of 32 swipes in the trace that found this."""
    screen = ListScreen()
    await _sweep(screen, "row_150")

    assert screen.holds and all(h > 0 for h in screen.holds), screen.holds
    assert "settle" not in screen.events


@pytest.mark.asyncio
async def test_a_simulator_confirms_each_read():
    """M22b — two agreeing reads per swipe where a bounce is possible."""
    screen = ListScreen()
    await _sweep(screen, "row_150")

    for group in screen.after_each_swipe():
        assert group.count("read:plain:full") >= 2, group


@pytest.mark.asyncio
async def test_where_a_swipe_returns_at_rest_each_read_is_taken_once():
    """M22 — on WDA a read is a /source, 3-7s on a physical device."""
    screen = ListScreen(at_rest=True)
    await _sweep(screen, "row_150")

    for group in screen.after_each_swipe():
        assert group == ["read:plain:full"], group


@pytest.mark.asyncio
async def test_where_swipes_fling_every_read_follows_a_settle():
    """M1 — idb cannot hold a swipe, so the #84 fix still applies there.

    Order, not count: a settle after the read would satisfy a count.
    """
    screen = ListScreen(controlled=False)
    await _sweep(screen, "row_150")

    groups = screen.after_each_swipe()
    assert groups
    for group in groups:
        assert group[0] == "settle", f"read before settling: {group}"


# -- direction, probing, the cold lookup -------------------------------------


@pytest.mark.asyncio
async def test_a_target_lost_on_the_way_up_is_followed_up():
    """M28 — the sweep keeps the direction the target was in.

    Restarting downward after losing a row it had seen above is how the 105s
    sweep swiped away from its target for 25 steps.
    """
    def row(y):
        return UIElement(type="Cell", identifier="row_5", label="Row 5",
                         frame={"x": 0, "y": y, "width": 393, "height": ROW})

    def filler(tag):
        return UIElement(type="Cell", identifier=tag, label=tag,
                         frame={"x": 0, "y": 300, "width": 393, "height": ROW})

    screen = ListScreen()
    screen.script = [
        [row(-20)],                    # baseline: located, under the nav bar
        [filler("a")], [filler("a")],  # after swiping up: gone, and moved
        [filler("b")], [filler("b")],  # the next swipe must go up again
        [row(300)], [row(300)],
    ]
    found = await _sweep(screen, "row_5", target_known_absent=True)

    assert screen.swipes()[:2] == ["swipe:up", "swipe:up"], screen.swipes()
    assert found is not None


@pytest.mark.asyncio
async def test_a_probe_only_target_is_not_hunted_with_plain_reads():
    """M13.

    A tab-bar item is visible only to a probing read. If the sweep drops to
    plain reads it loses the target at once and hunts blindly for something no
    swipe reveals -- 30 swipes in review, against 2 on main.
    """
    screen = ListScreen()
    screen.probe_only = {"tab_x"}
    found = await _sweep(screen, "tab_x")

    assert found is None
    reads = [e for e in screen.events if e.startswith("read")]
    assert reads[:2] == ["read:probe:filtered", "read:plain:full"], reads
    sweep_reads = [
        e for group in screen.after_each_swipe() for e in group
        if e.startswith("read")
    ]
    assert sweep_reads and all(e == "read:probe:full" for e in sweep_reads), (
        f"the sweep dropped to plain reads for a probe-only target: {sweep_reads}"
    )
    assert len(screen.swipes()) == 2, (
        f"{len(screen.swipes())} swipes for a target scrolling cannot move; "
        "the stall check should end it after two"
    )


@pytest.mark.asyncio
async def test_a_visible_target_costs_one_filtered_read_and_no_swipes():
    """On a physical device a filtered read is a predicate query, ~0.3s,
    where the full tree is a /source of 3-7s."""
    screen = ListScreen()
    found = await _sweep(screen, "row_3")

    assert found is not None
    assert screen.events == ["read:probe:filtered"]


@pytest.mark.asyncio
async def test_the_sweep_never_hit_tests():
    """Movement comes from the sweep's own reads.

    The hit-test check it replaced cost six full tree reads on WDA, ~25s on an
    iPhone 11, and on iOS 26 Settings it resolved every point to a full-screen
    overlay and called a scrollable list static.
    """
    screen = ListScreen(scrolls=False)
    await _sweep(screen, "row_missing")
    assert "hit-test" not in screen.events


# -- time --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_deadline_can_end_the_sweep_mid_run(caplog):
    """M4 — time runs out during the first reads, so only the in-loop check
    can stop the sweep."""
    now, clock = _clock()

    class Slow(ListScreen):
        async def get_ui_elements(self, *a, **k):
            now[0] = 60.0
            return await super().get_ui_elements(*a, **k)

    screen = Slow()
    with (
        caplog.at_level(logging.INFO, logger="quern-debug-server.device"),
        patch("server.device.controller_ui.time.perf_counter", clock),
    ):
        found = await _sweep(screen, "row_150", deadline_s=50.0)

    assert found is None
    assert screen.swipes() == [], screen.swipes()
    assert any("deadline of" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_a_slow_success_is_traced(caplog):
    """M3 — a sweep that succeeds slowly says how it spent the time."""
    now, clock = _clock()

    class Slow(ListScreen):
        async def get_ui_elements(self, *a, **k):
            now[0] += 1.5
            return await super().get_ui_elements(*a, **k)

    screen = Slow()
    with (
        caplog.at_level(logging.INFO, logger="quern-debug-server.device"),
        patch("server.device.controller_ui.time.perf_counter", clock),
    ):
        found = await _sweep(screen, "row_150", deadline_s=100.0)

    assert found is not None
    assert any("slower than expected" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_a_fast_success_is_not_traced(caplog):
    screen = ListScreen()
    with caplog.at_level(logging.INFO, logger="quern-debug-server.device"):
        found = await _sweep(screen, "row_150")

    assert found is not None
    assert not any("slower than expected" in r.message for r in caplog.records)


# -- the backends' declarations ----------------------------------------------


def test_the_backends_declare_how_their_swipes_end():
    """The sweep reads these; losing one changes its behaviour silently.

    idb cannot hold a swipe, so without its flag the #84 failure returns on
    every simulator that falls back to idb. WDA returns at rest, so without its
    flag a physical device pays for every read twice.
    """
    from server.device.idb import IdbBackend
    from server.device.sim_bridge import SimBridgeBackend
    from server.device.wda_client import WdaBackend

    assert IdbBackend.swipe_is_controlled is False
    assert getattr(SimBridgeBackend, "swipe_is_controlled", True) is not False
    assert getattr(WdaBackend, "swipe_is_controlled", True) is not False

    assert WdaBackend.swipe_returns_at_rest is True
    assert getattr(SimBridgeBackend, "swipe_returns_at_rest", False) is not True
    assert getattr(IdbBackend, "swipe_returns_at_rest", False) is not True


# -- from the review of the rebuild ------------------------------------------


@pytest.mark.asyncio
async def test_where_swipes_fling_no_row_is_passed_over():
    """M32 — idb flings, so its step must leave room for the fling.

    With the controlled backends' 75% drag, a 2.6x fling passed over 87 of 200
    rows from the top.
    """
    missed = []
    for index in range(200):
        screen = ListScreen(controlled=False)
        if await _sweep(screen, f"row_{index}") is None:
            missed.append(index)
    assert not missed, f"{len(missed)} rows passed over: {missed[:10]}"


@pytest.mark.asyncio
@pytest.mark.parametrize("row", [300.0, 400.0, 500.0, 700.0])
async def test_tall_rows_are_brought_into_view(row):
    """M33 — a located row is swiped toward by what it needs, not a full step.

    A full step toward a row taller than the window it must land in carried
    it straight past, back and forth, until the budget ran out.
    """
    missed = []
    for index in range(30):
        screen = ListScreen(rows=30, row=row)
        if await _sweep(screen, f"row_{index}", max_swipes=25) is None:
            missed.append(index)
    assert not missed, f"rows of {row}pt not brought into view: {missed}"


@pytest.mark.asyncio
async def test_a_label_changing_in_place_does_not_hide_the_bottom():
    """M34 — a running clock must not read as the list moving.

    Counted as movement it stopped end detection outright: from the bottom, a
    search for a row above swiped down 20 times first.
    """
    screen = ListScreen(ticking=True).at_bottom()
    found = await _sweep(screen, "row_3")

    assert found is not None
    swipes = screen.swipes()
    assert swipes[0] == "swipe:down" and set(swipes[1:]) == {"swipe:up"}, swipes


@pytest.mark.asyncio
async def test_a_label_changing_in_place_does_not_keep_the_reads_going():
    """M34 — nor stop two reads from agreeing: 182 reads for one sweep."""
    screen = ListScreen(rows=60, ticking=True)
    await _sweep(screen, "row_missing")

    assert screen.swipes() == ["swipe:down"] * 5 + ["swipe:up"] * 5, screen.swipes()
    for group in screen.after_each_swipe():
        assert len(group) == 2, f"reads did not agree at once: {group}"


@pytest.mark.asyncio
async def test_rows_told_apart_only_by_label_are_seen_to_move():
    """M30b — the label is part of an element's identity.

    Without ids, rows differ only in label. Dropping it makes every row the
    same element, and a scroll by whole rows reads as nothing moving.
    """
    row = SCREEN["height"] * DRAG * TRAVEL / 10
    screen = ListScreen(rows=100, row=row, ids=False)
    found = await screen._ios_scroll_to_element(
        "SIM", "Row 80", None, max_swipes=10,
    )
    assert found is not None, f"gave up after {screen.swipes()}"


@pytest.mark.asyncio
async def test_a_bounce_is_read_through_to_rest():
    """M16b — a snap-back animates over several reads, not one."""
    screen = ListScreen(bounce=91.0)
    found = await _sweep(screen, "row_199")

    assert found is not None
    assert found.frame["y"] == screen.resting_y(199)


@pytest.mark.asyncio
async def test_a_target_without_a_frame_ends_the_sweep_at_once():
    """M35 — it matched, but no swipe can bring it into view; sweeping on
    spent all 30 swipes finding that out."""
    class Frameless(ListScreen):
        async def get_ui_elements(self, *a, **k):
            els, udid = await super().get_ui_elements(*a, **k)
            els.append(UIElement(type="Cell", identifier="ghost", label="ghost",
                                 frame=None))
            return els, udid

    screen = Frameless()
    found = await _sweep(screen, "ghost")

    assert found is None
    assert screen.swipes() == [], screen.swipes()


@pytest.mark.asyncio
async def test_a_failure_mid_sweep_still_logs_the_steps_before_it(caplog):
    """M36 — the trace is most wanted exactly when something broke."""
    class Breaks(ListScreen):
        reads = 0

        async def get_ui_elements(self, *a, **k):
            Breaks.reads += 1
            if Breaks.reads == 5:
                raise RuntimeError("sim-bridge exited while running describe")
            return await super().get_ui_elements(*a, **k)

    screen = Breaks()
    with (
        caplog.at_level(logging.INFO, logger="quern-debug-server.device"),
        pytest.raises(RuntimeError),
    ):
        await _sweep(screen, "row_150")

    traces = [r.message for r in caplog.records if "stopped by RuntimeError" in r.message]
    assert traces, "the failure logged no trace"
    assert "swipe 1/" in traces[0], traces[0]


@pytest.mark.asyncio
async def test_a_target_only_the_filtered_lookup_sees_does_not_run_to_the_deadline():
    """WDA answers a filtered lookup with a predicate query and a full read
    with /source, and the two can disagree. The target is then treated as
    probe-only; the sweep must still end at the list's ends."""
    screen = ListScreen(rows=60, at_rest=True)
    screen.phantom = {"phantom"}
    found = await _sweep(screen, "phantom")

    assert found is None
    assert len(screen.swipes()) <= 10, screen.swipes()
