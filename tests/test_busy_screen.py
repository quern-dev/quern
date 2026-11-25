"""A screen too busy to read over the push in time (#769).

Measured on an iPhone 12, a map: with ~119 pins the device sent its first
InitialDump 4.4 s after subscribing and its complete tree 7.9 s after the
newest, three of three. The per-element budget gave up at 5.6 s and
resubscribed, which started the phone over, so no read of it ever finished;
the fallback then started a WDA that could not read it either. Zoomed out to
415 elements, no complete tree came in 45 s.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from server.api import device as device_api
from server.device.ios import coredevice_ax
from server.device.ios.coredevice_ax import (
    PUSH_SELECTOR,
    PushStalled,
    ScreenTooBusy,
    Tree,
    read_tree,
)
from server.models import DeviceError
from tests.test_coredevice_ax import PHONE, SETTINGS, _as, _phone_controller

INITIAL = _as("InitialDump", SETTINGS)


def _queue_later(*timed):
    """A queue each (delay, push) lands on that long after the call."""
    # The production queue: it stamps when each push came (#772).
    q: asyncio.Queue = coredevice_ax._ArrivalQueue()

    async def feed():
        for delay, push in timed:
            await asyncio.sleep(delay)
            q.put_nowait((PUSH_SELECTOR, [push]))

    asyncio.get_running_loop().create_task(feed())
    return q


@pytest.fixture
def fast_budget(monkeypatch):
    """The per-element budget and its floor made tiny, so the device's own
    pace is what the window comes from."""
    monkeypatch.setattr(coredevice_ax, "STALL_MIN_S", 0.1)
    monkeypatch.setattr(coredevice_ax, "PAIR_BASE_S", 0.02)
    monkeypatch.setattr(coredevice_ax, "PAIR_PER_ELEMENT_S", 0.0)


@pytest.fixture
def paces(monkeypatch):
    """Every pace `_stall_window` is given, in order: what the code decided,
    rather than a wall-clock time a loaded CI runner stretches (CodeRabbit on
    #772: 1.006 s against a 0.8 s limit on Python 3.14)."""
    seen: list[float] = []
    real = coredevice_ax._stall_window

    def record(tree, pace):
        seen.append(pace)
        return real(tree, pace)

    monkeypatch.setattr(coredevice_ax, "_stall_window", record)
    return seen


class TestTheDevicesOwnPace:
    async def test_a_slow_screens_tree_is_waited_for_at_its_pace(self, fast_budget, paces):
        """Its InitialDump took 0.8 s to come, so its pair gets 2.5 x that."""
        loop = asyncio.get_running_loop()
        since = loop.time() - 0.8           # subscribed 0.8 s before this InitialDump
        q = _queue_later((0.0, INITIAL), (0.8, SETTINGS))
        tree, _, _ = await asyncio.wait_for(
            coredevice_ax._newest_tree(q, {}, since=since), 5)
        assert tree.dump_type == "AdditionalData"
        assert paces[0] >= 0.7

    async def test_a_684_stall_is_still_given_up_on_quickly(self, fast_budget, paces):
        """InitialDumps that come at once and then nothing: the window is the
        floor, as before, not stretched."""
        loop = asyncio.get_running_loop()
        q = _queue_later((0.0, INITIAL))
        with pytest.raises(PushStalled):
            await asyncio.wait_for(
                coredevice_ax._newest_tree(q, {}, since=loop.time()), 5)
        assert paces[0] < 0.3

    async def test_a_quick_initial_dump_does_not_shorten_a_slow_ones_wait(
            self, fast_budget, paces):
        """A slow first InitialDump (0.8 s), a second at once, then the
        complete tree: the slow pace still counts. Timed from the quick one
        alone, the map stalled as before (review)."""
        loop = asyncio.get_running_loop()
        since = loop.time() - 0.8
        q = _queue_later((0.0, INITIAL), (0.05, INITIAL), (1.0, SETTINGS))
        tree, _, _ = await asyncio.wait_for(
            coredevice_ax._newest_tree(q, {}, since=since), 5)
        assert tree.dump_type == "AdditionalData"
        assert len(paces) == 2 and paces[1] >= 0.7, paces

    async def test_each_gap_is_its_own_not_the_time_since_subscribing(
            self, fast_budget, paces):
        """InitialDumps every 0.3 s, then nothing: the pace is 0.3 s. Timed
        from the subscription each time, it grew with every dump and a stall
        like #684's was given longer and longer. A slow runner reading the
        pushes late is test_pushes_already_queued_are_paced_by_when_they_came:
        a parse that blocks the loop here delays this fake device's pushes as
        well, which is real lateness, not the bug (#772 CI)."""
        loop = asyncio.get_running_loop()
        q = _queue_later((0.0, INITIAL), (0.3, INITIAL), (0.3, INITIAL))
        with pytest.raises(PushStalled):
            await asyncio.wait_for(
                coredevice_ax._newest_tree(q, {}, since=loop.time() - 0.3), 10)
        # Steady, not growing: however slow the runner, the slowest gap is
        # what counts, where timed from subscribing it rose 0.3 s a dump.
        assert len(paces) == 3 and paces[-1] - paces[0] < 0.25, paces

    async def test_pushes_already_queued_are_paced_by_when_they_came(
            self, fast_budget, paces):
        """The runner read three pushes late, all at once: timed when they
        were taken off the queue, the first gap was the whole delay, and the
        later ones nothing (#772 CI)."""
        loop = asyncio.get_running_loop()
        q = coredevice_ax._ArrivalQueue()
        since = loop.time() - 0.3
        for i in range(3):
            if i:
                await asyncio.sleep(0.3)
            q.put_nowait((PUSH_SELECTOR, [INITIAL]))
        await asyncio.sleep(0.6)                  # nobody reading yet
        with pytest.raises(PushStalled):
            await asyncio.wait_for(coredevice_ax._newest_tree(q, {}, since=since), 10)
        assert paces and max(paces) < 0.5, paces

    async def test_a_slow_parse_is_not_the_devices_pace(self, fast_budget, paces,
                                                         monkeypatch):
        """Parsing took about 1 s on a CI runner, and timed after it, that was
        counted as the phone's pace and kept (#772)."""
        import time as clock
        real = coredevice_ax.parse_tree

        def slow_parse(data):
            clock.sleep(0.6)
            return real(data)

        monkeypatch.setattr(coredevice_ax, "parse_tree", slow_parse)
        loop = asyncio.get_running_loop()
        q = _queue_later((0.0, INITIAL))
        with pytest.raises(PushStalled):
            await asyncio.wait_for(
                coredevice_ax._newest_tree(q, {}, since=loop.time()), 10)
        assert paces[0] < 0.3, paces

    async def test_a_slow_parse_does_not_throw_away_a_queued_tree(
            self, fast_budget, monkeypatch):
        """Timed from its arrival, a parse longer than the window expired it
        before the queue was read, and the complete tree already waiting was
        taken for a stall (CodeRabbit on #772)."""
        import time as clock
        real = coredevice_ax.parse_tree

        def slow_parse(data):
            clock.sleep(0.4)                # longer than the 0.1 s window
            return real(data)

        monkeypatch.setattr(coredevice_ax, "parse_tree", slow_parse)
        loop = asyncio.get_running_loop()
        q = coredevice_ax._ArrivalQueue()
        q.put_nowait((PUSH_SELECTOR, [INITIAL]))
        q.put_nowait((PUSH_SELECTOR, [SETTINGS]))       # already there
        tree, _, _ = await asyncio.wait_for(
            coredevice_ax._newest_tree(q, {}, since=loop.time()), 10)
        assert tree.dump_type == "AdditionalData"

    def test_the_arrival_queue_stamps_each_item(self):
        async def run():
            q = coredevice_ax._ArrivalQueue()
            loop = asyncio.get_running_loop()
            q.put_nowait("a")
            put_at = loop.time()
            await asyncio.sleep(0.2)
            assert await q.get() == "a"
            return q.arrived, put_at
        arrived, put_at = asyncio.run(run())
        assert abs(arrived - put_at) < 0.05

    def test_the_pace_is_capped(self):
        """A late first push must not hold off recovering a real stall for the
        whole deadline (review)."""
        small = Tree(dump_type="InitialDump", app_pid=1, elements=[{"AXLabel": "a"}])
        assert coredevice_ax._stall_window(small, 100.0) == coredevice_ax.STALL_PACE_MAX_S
        assert coredevice_ax._stall_window(small, 2.0) == pytest.approx(
            coredevice_ax.STALL_PACE * 2.0)
        assert coredevice_ax._stall_window(small, 0.0) == coredevice_ax.STALL_MIN_S


class TestTheWiring:
    """The pace tests above call `_newest_tree` directly; a `_settled` that
    stopped passing the subscription's time passed them all (review)."""

    @staticmethod
    def _subscribe_slow(first_after: float, complete_after: float):
        async def subscribe(lib, address, conn, uuid, *, spare=False):
            loop = asyncio.get_running_loop()
            conn.rsd = conn.provider = object()
            conn.subscribed_at = loop.time()
            return _queue_later((first_after, INITIAL), (complete_after, SETTINGS))
        return subscribe

    async def test_settled_paces_from_the_subscription(self, fast_budget, paces, monkeypatch):
        monkeypatch.setattr(coredevice_ax, "FIRST_PUSH_S", 5.0)       # no spare here
        monkeypatch.setattr(coredevice_ax, "_subscribe", self._subscribe_slow(0.8, 1.6))
        monkeypatch.setattr(coredevice_ax, "_close", AsyncMock())
        from types import SimpleNamespace
        conn = SimpleNamespace(rsd=None, provider=None)
        tree, _, _ = await asyncio.wait_for(
            coredevice_ax._settled(PHONE, object(), None, conn, {}), 5)
        assert tree.dump_type == "AdditionalData"
        assert paces[0] >= 0.7, paces

    async def test_a_spare_that_wins_carries_its_own_time(self, fast_budget, monkeypatch):
        """The first subscription stays silent; the spare's InitialDump is
        slow, so its pace must be the spare's subscription, not none."""
        monkeypatch.setattr(coredevice_ax, "FIRST_PUSH_S", 0.05)
        calls = []

        async def subscribe(lib, address, conn, uuid, *, spare=False):
            calls.append(spare)
            conn.rsd = conn.provider = object()
            conn.subscribed_at = asyncio.get_running_loop().time()
            if not spare:
                return asyncio.Queue()                                # silent
            return _queue_later((0.8, INITIAL), (1.6, SETTINGS))

        monkeypatch.setattr(coredevice_ax, "_subscribe", subscribe)
        monkeypatch.setattr(coredevice_ax, "_close", AsyncMock())
        from types import SimpleNamespace
        conn = SimpleNamespace(rsd=None, provider=None)
        tree, _, _ = await asyncio.wait_for(
            coredevice_ax._settled(PHONE, object(), None, conn, {}), 5)
        assert calls == [False, True]
        assert tree.dump_type == "AdditionalData"

    async def test_a_stall_on_the_spare_is_timed_from_the_spare(
            self, fast_budget, paces, monkeypatch):
        """Timed from the first, silent subscription, a spare that sent one
        quick InitialDump and stalled was given 2.5 x the first's silence
        before the read subscribed again: a stall recovered late."""
        monkeypatch.setattr(coredevice_ax, "FIRST_PUSH_S", 1.0)
        loop = asyncio.get_running_loop()
        started = loop.time()
        subscribes: list[bool] = []

        async def subscribe(lib, address, conn, uuid, *, spare=False):
            subscribes.append(spare)
            conn.rsd = conn.provider = object()
            conn.subscribed_at = loop.time()
            if spare:
                return _queue_later((0.0, INITIAL))                   # then stalls
            if len(subscribes) > 1 or conn.subscribed_at - started > 0.1:
                return _queue_later((0.0, INITIAL), (0.05, SETTINGS))
            return asyncio.Queue()                                    # silent first

        monkeypatch.setattr(coredevice_ax, "_subscribe", subscribe)
        monkeypatch.setattr(coredevice_ax, "_close", AsyncMock())
        from types import SimpleNamespace
        await asyncio.wait_for(coredevice_ax._settled(
            PHONE, object(), None, SimpleNamespace(rsd=None, provider=None), {}), 10)
        # The spare's InitialDump came at once: its pace is that, not the
        # first subscription's 1 s of silence.
        assert paces[0] < 0.4, paces

    async def test_closing_the_loser_is_not_counted_as_the_devices_pace(
            self, fast_budget, paces, monkeypatch):
        """The loser is closed after the spare's first push arrived: counted
        as the device's pace, a slow close stretched the window over a stall
        on the spare (CodeRabbit on #772)."""
        monkeypatch.setattr(coredevice_ax, "FIRST_PUSH_S", 0.05)
        loop = asyncio.get_running_loop()
        subscribes: list[bool] = []

        async def subscribe(lib, address, conn, uuid, *, spare=False):
            subscribes.append(spare)
            conn.rsd = conn.provider = object()
            conn.subscribed_at = loop.time()
            if spare:
                return _queue_later((0.0, INITIAL))                   # at once, then stalls
            if len(subscribes) > 1:
                return _queue_later((0.0, INITIAL), (0.05, SETTINGS))
            return asyncio.Queue()                                    # silent first

        async def slow_close(provider, rsd):
            await asyncio.sleep(0.8)

        monkeypatch.setattr(coredevice_ax, "_subscribe", subscribe)
        monkeypatch.setattr(coredevice_ax, "_close", slow_close)
        from types import SimpleNamespace
        await asyncio.wait_for(coredevice_ax._settled(
            PHONE, object(), None, SimpleNamespace(rsd=None, provider=None), {}), 15)
        assert paces[0] < 0.4, f"the 0.8 s close was counted as pace: {paces}"

    async def test_a_slow_first_push_keeps_its_latency_past_a_slow_close(
            self, fast_budget, paces, monkeypatch):
        """The spare's first push took 0.4 s, then closing the loser took
        0.8 s. Shifting the start by the close cut the device's 0.4 s out of
        its pace, and its stall window with it (CodeRabbit on #772)."""
        monkeypatch.setattr(coredevice_ax, "FIRST_PUSH_S", 0.05)
        loop = asyncio.get_running_loop()
        subscribes: list[bool] = []

        async def subscribe(lib, address, conn, uuid, *, spare=False):
            subscribes.append(spare)
            conn.rsd = conn.provider = object()
            conn.subscribed_at = loop.time()
            if spare:
                return _queue_later((0.4, INITIAL))                   # late, then stalls
            if len(subscribes) > 1:
                return _queue_later((0.0, INITIAL), (0.05, SETTINGS))
            return asyncio.Queue()                                    # silent first

        async def slow_close(provider, rsd):
            await asyncio.sleep(0.8)

        monkeypatch.setattr(coredevice_ax, "_subscribe", subscribe)
        monkeypatch.setattr(coredevice_ax, "_close", slow_close)
        from types import SimpleNamespace
        await asyncio.wait_for(coredevice_ax._settled(
            PHONE, object(), None, SimpleNamespace(rsd=None, provider=None), {}), 15)
        assert 0.35 < paces[0] < 0.7, f"the device's 0.4 s was not its pace: {paces}"

    async def test_a_stalled_attempts_tree_is_not_left_for_the_next(
            self, fast_budget, monkeypatch):
        """Left in `held`, a big InitialDump from a stalled attempt made a push
        that then stayed silent read as "too busy" (review)."""
        monkeypatch.setattr(coredevice_ax, "FIRST_PUSH_S", 5.0)
        attempts = []

        async def subscribe(lib, address, conn, uuid, *, spare=False):
            attempts.append(1)
            conn.rsd = conn.provider = object()
            conn.subscribed_at = asyncio.get_running_loop().time()
            if len(attempts) == 1:
                return _queue_later((0.0, INITIAL))                   # then stalls
            return asyncio.Queue()                                    # silent

        monkeypatch.setattr(coredevice_ax, "_subscribe", subscribe)
        monkeypatch.setattr(coredevice_ax, "_close", AsyncMock())
        from types import SimpleNamespace
        held: dict = {}
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(coredevice_ax._settled(
                PHONE, object(), None, SimpleNamespace(rsd=None, provider=None), held), 1.0)
        assert len(attempts) >= 2
        assert "newest" not in held, "the stalled attempt's tree was kept"
        assert held.get("first") is not None                          # kept for #669


class TestTooBusyToRead:
    @staticmethod
    def _still_describing(elements: int):
        """`_settled` that has the screen's InitialDump in hand and never
        gets its complete tree."""
        big = Tree(dump_type="InitialDump", app_pid=711,
                   elements=[{"AXLabel": "Geocaching", "type": "Application"}]
                   + [{"AXLabel": f"pin {i}", "type": "Button"} for i in range(elements - 1)])

        async def settled(uuid, lib, address, conn, held):
            held.update(first=big, newest=big)
            await asyncio.sleep(60)

        return settled

    async def test_a_big_screen_still_building_at_the_deadline_says_so(
            self, monkeypatch, phone_address):
        monkeypatch.setattr(coredevice_ax, "_settled", self._still_describing(415))
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: object())
        with pytest.raises(ScreenTooBusy, match="415 elements") as caught:
            await read_tree(PHONE, timeout=0.3)
        assert "zoom a map in" in str(caught.value)
        assert caught.value.elements == 415

    @pytest.mark.parametrize(("elements", "busy"), [(99, False), (100, True)])
    async def test_the_threshold(self, monkeypatch, phone_address, elements, busy):
        monkeypatch.setattr(coredevice_ax, "_settled", self._still_describing(elements))
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: object())
        with pytest.raises(DeviceError) as caught:
            await read_tree(PHONE, timeout=0.3)
        assert isinstance(caught.value, ScreenTooBusy) is busy

    async def test_a_small_screen_that_times_out_is_not_called_busy(
            self, monkeypatch, phone_address):
        monkeypatch.setattr(coredevice_ax, "_settled", self._still_describing(20))
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: object())
        with pytest.raises(DeviceError, match="sent no complete") as caught:
            await read_tree(PHONE, timeout=0.3)
        assert not isinstance(caught.value, ScreenTooBusy)


@pytest.fixture
def phone_address(monkeypatch):
    monkeypatch.setattr("server.device.performance._device_address",
                        AsyncMock(return_value=("fd00::1", 51000)))


class TestNoWdaForIt:
    async def test_a_busy_screen_is_not_read_through_wda(self, monkeypatch):
        """WDA's /source timed out on the same map, and starting it began the
        auto-start loop (#769)."""
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl.coredevice_ax.describe_all = AsyncMock(side_effect=ScreenTooBusy(PHONE, 415, 20))

        with pytest.raises(ScreenTooBusy, match="415 elements"):
            await ctrl.get_ui_elements(PHONE, use_cache=False, whole_screen=True)

        ctrl.wda_client.describe_all.assert_not_called()


class TestTheScreenContextIsBounded:
    async def test_one_that_runs_long_says_so_and_returns(self, monkeypatch):
        monkeypatch.setattr(device_api, "SCREEN_CONTEXT_S", 0.2)

        async def slow(**kwargs):
            await asyncio.sleep(60)

        controller = AsyncMock()
        controller.get_screen_summary = slow
        context = await asyncio.wait_for(
            device_api._capture_screen_context(controller, PHONE), 5)
        assert "within 0.2s" in context["error"]
        assert "the action itself was done" in context["error"]

    async def test_one_that_fails_says_why(self):
        controller = AsyncMock()
        controller.get_screen_summary = AsyncMock(side_effect=ScreenTooBusy(PHONE, 415, 20))
        context = await device_api._capture_screen_context(controller, PHONE)
        assert "415 elements" in context["error"]

    async def test_one_that_works_has_no_error(self):
        controller = AsyncMock()
        controller.get_screen_summary = AsyncMock(
            return_value=({"summary": "a screen", "element_count": 3}, [], None))
        context = await device_api._capture_screen_context(controller, PHONE)
        assert "error" not in context and context["summary"] == "a screen"


def test_the_route_answers_503_with_the_reason():
    """A busy screen is not a server fault: 503, with how to make it smaller."""
    from server.api.device import _handle_device_error

    error = _handle_device_error(ScreenTooBusy(PHONE, 215, 20))
    assert error.status_code == 503
    assert "215 elements" in error.detail and "zoom a map in" in error.detail
